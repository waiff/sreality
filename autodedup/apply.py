"""W30 — one generation's groups into production merges, through THE chokepoint.

    python3 -m autodedup.lane --mode apply   --args "generation=g12" --out out/
    python3 -m autodedup.lane --mode apply   --args "generation=g12,dry_run=0" --out out/
    python3 -m autodedup.lane --mode unapply --args "generation=g12,dry_run=0" --out out/

`plan_apply` reads a generation's groups and, per group, names the survivor and the properties
that would retire into it — or the reasons the group is refused (E303). `apply_plan` records
that plan (dry run) or executes it through `toolkit.property_identity.merge_properties`, one
merge group per engine group (E301), so `unmerge_group` undoes a whole group and `unapply`
undoes a generation newest-first. Nothing here decides anything the engine did not: the
groups are read as stored, and every refusal only removes a group from the plan.

DARK THREE WAYS (E304). A dry run is the default and writes only `autodedup.applied_merges`.
A live run needs `app_settings.autodedup_apply_enabled = true` — re-read before EVERY group
(E39), so flipping it off on /settings stops a run between two groups — and a live scope
(`app_settings.autodedup_apply_scope`) that names its deal types and its area; a run's own
arguments can narrow that scope and never widen it. Absent rows mean OFF.

D7 holds: nothing here reads `property_merge_events`. The apply path's own ledger is the only
history it consults, and its one touch of the production ledger is a write-only stamp on rows
of a merge group it created in the same transaction (E302).
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from autodedup import apply_sql as S
from autodedup.census import write_json
from autodedup.ui_sql import NEGATIVE_VERDICTS
from toolkit.property_identity import MergeError, merge_properties, unmerge_group
from toolkit.room_taxonomy import category_main_compatible

ENABLED_SETTING: str = "autodedup_apply_enabled"
SCOPE_SETTING: str = "autodedup_apply_scope"
MERGE_SOURCE: str = "autodedup"
STAMP_PREFIX: str = "autodedup:"
UNAPPLY_BY_PREFIX: str = "autodedup-unapply:"
# `undone_by` on a ledger row whose merge someone else had already undone when `unapply` came
# to it: the engine records that the merge no longer stands, never that it undid it (E305).
EXTERNAL_UNDO: str = "external"
DEFAULT_MAX_CLUSTER_SIZE: int = 8
DEFAULT_MAX_CLUSTERS_PER_RUN: int = 200
SUMMARY_LIST_CAP: int = 200
ID_CHUNK: int = 5000

SCOPE_KEYS: tuple[str, ...] = (
    "category_types", "blocks", "listing_ids", "all_blocks", "max_cluster_size",
    "max_clusters_per_run",
)
APPLY_ARGS: frozenset[str] = frozenset({"generation", "dry_run", *SCOPE_KEYS})
UNAPPLY_ARGS: frozenset[str] = frozenset({"generation", "dry_run", "cluster_key"})

# The engine's block key is grain-prefixed (`o` obec, `c` cast obce); the rt lane's scope
# spells the same blocks `obec:` / `cast_obce:` and the export lane `town:` / `quarter:`.
GRAIN_ALIASES: dict[str, str] = {
    "o": "o", "obec": "o", "town": "o",
    "c": "c", "cast_obce": "c", "quarter": "c",
}

# E303 — why a group is refused. Order is the order they are checked and reported in.
SKIP_UNATTACHED = "unattached_member"
SKIP_INACTIVE_PROPERTY = "property_not_active"
SKIP_OVERSIZE = "oversize"
SKIP_CLUSTER_VERDICT = "operator_group_verdict"
SKIP_PAIR_VERDICT = "operator_pair_verdict"
SKIP_MUST_NOT_LINK = "must_not_link"
SKIP_CATEGORY_TYPE = "category_type_mix"
SKIP_CATEGORY_MAIN = "category_main_incompatible"
SKIP_CARRIES_OUT_OF_SCOPE = "carries_out_of_scope_listings"
SKIP_SPANS_GROUPS = "property_spans_groups"
SKIP_CARRIES_UNGROUPED = "carries_ungrouped_listings"
SKIP_REFUSED_BEFORE = "refused_at_chokepoint_before"
SKIP_RESTORED_ELSEWHERE = "restored_outside_engine"
SKIP_GENERATION_UNAPPLIED = "generation_unapplied"
# Re-checked inside the group's own transaction, just before the merge (E303).
SKIP_CHANGED_SINCE_PLAN = "changed_since_plan"
# Not a merge at all: a group ALREADY on one property that an operator negative now covers.
# Reported, never acted on — `unapply` is the operator's call (E305).
RULED_AFTER_MERGE = "ruled_different_after_merge"

# The real-time lane's generations (`rt…`, `left(generation, 2) = 'rt'` on the review pages)
# are rewritten every few minutes by a workflow in another concurrency group and spell their
# blocks differently, so no plan read across several statements can hold still. Refused.
REALTIME_PREFIX: str = "rt"


class ApplyRefused(RuntimeError):
    """A live apply was asked for while a switch or the scope forbids it."""


# ------------------------------------------------------------------ scope


@dataclass(frozen=True)
class Scope:
    """Which groups a run may touch. `None` = unrestricted on that axis; an EMPTY set admits
    nothing (a narrowing that intersected to nothing must not read as 'no filter')."""

    category_types: frozenset[str] | None = None
    blocks: frozenset[str] | None = None
    listing_ids: frozenset[int] | None = None
    all_blocks: bool = False
    max_cluster_size: int = DEFAULT_MAX_CLUSTER_SIZE
    max_clusters_per_run: int = DEFAULT_MAX_CLUSTERS_PER_RUN

    def to_json(self) -> dict[str, Any]:
        return {
            "category_types": sorted(self.category_types) if self.category_types is not None
            else None,
            "blocks": sorted(self.blocks) if self.blocks is not None else None,
            "listing_ids": len(self.listing_ids) if self.listing_ids is not None else None,
            "all_blocks": self.all_blocks,
            "max_cluster_size": self.max_cluster_size,
            "max_clusters_per_run": self.max_clusters_per_run,
        }

    def live_problems(self) -> list[str]:
        """Why this scope may not drive a LIVE run; empty when it may."""
        problems: list[str] = []
        if self.category_types is None:
            problems.append("the live scope names no category_types")
        if self.blocks is None and self.listing_ids is None and not self.all_blocks:
            problems.append(
                "the live scope names no area: set blocks, listing_ids, or all_blocks=true"
            )
        return problems

    def admits(self, cluster: Mapping[str, Any], members: Sequence["Member"]) -> bool:
        if self.category_types is not None and any(
            m.category_type not in self.category_types for m in members
        ):
            return False
        if self.blocks is not None:
            block = block_of(cluster)
            if block is None or block not in self.blocks:
                return False
        if self.listing_ids is not None and any(
            m.listing_id not in self.listing_ids for m in members
        ):
            return False
        return True


def normalize_block(raw: Any) -> str:
    """`obec:563510` / `town:563510` / `o563510` -> `o563510`; the same for a cast obce."""
    text = str(raw).strip().lower()
    if ":" in text:
        grain, _, code = text.partition(":")
    else:
        grain, code = text[:1], text[1:]
    letter = GRAIN_ALIASES.get(grain)
    if letter is None or not code.isdigit():
        raise ValueError(
            f"block {raw!r}: expected obec:<code> or cast_obce:<code> (or o<code> / c<code>)"
        )
    return f"{letter}{int(code)}"


def block_of(cluster: Mapping[str, Any]) -> str | None:
    grain, key = cluster.get("block_grain"), cluster.get("block_key")
    if grain is None or key is None:
        return None
    return f"{grain}{int(key)}"


def _words(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [w for w in value.replace("/", " ").split() if w]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(w).strip() for w in value if str(w).strip()]
    return [str(value).strip()]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


def scope_fields(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and normalise the scope keys present in `raw` (a setting or lane args). A key
    set to null is absent: the seeded setting spells every key so the operator sees them."""
    raw = raw or {}
    unknown = sorted(set(raw) - set(SCOPE_KEYS))
    if unknown:
        raise ValueError(f"unknown scope key(s): {', '.join(unknown)}")
    raw = {key: value for key, value in raw.items() if value is not None}
    out: dict[str, Any] = {}
    if "category_types" in raw:
        out["category_types"] = frozenset(w.lower() for w in _words(raw["category_types"]))
    if "blocks" in raw:
        out["blocks"] = frozenset(normalize_block(w) for w in _words(raw["blocks"]))
    if "listing_ids" in raw:
        out["listing_ids"] = frozenset(int(w) for w in _words(raw["listing_ids"]))
    if "all_blocks" in raw:
        out["all_blocks"] = _truthy(raw["all_blocks"])
    for key, floor in (("max_cluster_size", 2), ("max_clusters_per_run", 1)):
        if key in raw:
            value = int(raw[key])
            if value < floor:
                raise ValueError(f"{key} must be >= {floor}, got {value}")
            out[key] = value
    return out


def effective_scope(
    setting: Mapping[str, Any] | None, override: Mapping[str, Any] | None, *, live: bool
) -> Scope:
    """A dry run may look anywhere: its arguments replace the setting key by key. A LIVE run
    needs the operator's setting to name deal types AND an area ON ITS OWN, and its arguments
    only NARROW it — sets intersect, caps take the minimum, `all_blocks` only from the setting.
    So /settings, never a dispatch, is the authority on where live merges may happen."""
    base = scope_fields(setting)
    extra = scope_fields(override)
    if not live:
        return Scope(**{**base, **extra})
    if setting is None:
        raise ValueError(f"a live run needs the app_settings.{SCOPE_SETTING} row")
    problems = Scope(**base).live_problems()
    if problems:
        raise ValueError(f"app_settings.{SCOPE_SETTING}: " + "; ".join(problems))
    merged = dict(base)
    defaults = Scope()
    for key, value in extra.items():
        if key == "all_blocks":
            continue
        if key in ("category_types", "blocks", "listing_ids"):
            merged[key] = value if merged.get(key) is None else merged[key] & value
        else:
            merged[key] = min(int(merged.get(key, getattr(defaults, key))), int(value))
    return Scope(**merged)


# ------------------------------------------------------------------ plan


@dataclass(frozen=True)
class Member:
    listing_id: int
    property_id: int | None
    category_type: str | None
    category_main: str | None


@dataclass
class GroupPlan:
    cluster_key: int
    size: int
    member_ids: list[int]
    property_ids: list[int]
    survivor_id: int | None
    retired_ids: list[int]
    confidence: float | None
    model_version: str | None
    feature_version: int | None
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    # Every listing the merge would put on the survivor: the members plus every other child
    # of the involved properties. Re-read inside the group's transaction before it merges.
    listing_ids: list[int] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        return bool(self.reasons)

    @property
    def ruled_after_merge(self) -> bool:
        return self.reasons == [RULED_AFTER_MERGE]

    def to_json(self) -> dict[str, Any]:
        return {
            "cluster_key": self.cluster_key,
            "size": self.size,
            "member_ids": self.member_ids,
            "listing_ids": self.listing_ids,
            "property_ids": self.property_ids,
            "survivor_id": self.survivor_id,
            "retired_ids": self.retired_ids,
            "confidence": self.confidence,
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "reasons": self.reasons,
            "detail": self.detail,
        }


@dataclass
class Plan:
    generation: str
    scope: Scope
    groups: list[GroupPlan]
    deferred: list[int]
    counts: dict[str, Any]

    @property
    def to_apply(self) -> list[GroupPlan]:
        return [g for g in self.groups if not g.reasons]

    @property
    def skipped(self) -> list[GroupPlan]:
        return [g for g in self.groups if g.reasons]


def _rows(conn: Any, sql: str, params: Mapping[str, Any] | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params or {}))
        return list(cur.fetchall())


def _exec(conn: Any, sql: str, params: Mapping[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params))


def _exec_many(conn: Any, sql: str, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(sql, [dict(row) for row in rows])


def _chunks(ids: Iterable[int], size: int = ID_CHUNK) -> Iterable[list[int]]:
    ordered = sorted(set(ids))
    for start in range(0, len(ordered), size):
        yield ordered[start:start + size]


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def read_setting(conn: Any, key: str) -> Any | None:
    rows = _rows(conn, S.SETTING_SQL, {"key": key})
    return _json_value(rows[0][0]) if rows else None


def apply_enabled(conn: Any) -> bool:
    """The operator's switch, `scraper.db._app_settings_flag`'s reading of it: an absent row,
    a NULL or anything but true is OFF."""
    value = read_setting(conn, ENABLED_SETTING)
    if isinstance(value, dict):
        value = value.get("enabled", value.get("value"))
    if value is True:
        return True
    return str(value).strip().lower() in ("true", "1", "on", "yes")


def read_scope_setting(conn: Any) -> dict[str, Any] | None:
    value = read_setting(conn, SCOPE_SETTING)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{SCOPE_SETTING} must be a JSON object, got {type(value).__name__}")
    return value


def _survivor(
    property_ids: Sequence[int],
    props: Mapping[int, Mapping[str, Any]],
    children: Mapping[int, set[int]],
) -> int:
    """Most listings wins; a tie goes to the oldest `first_seen_at`, then the lowest id."""

    def key(pid: int) -> tuple:
        first = props[pid].get("first_seen_at")
        stamp = first.timestamp() if isinstance(first, datetime) else 0.0
        return (-len(children.get(pid, ())), 0 if first is not None else 1, stamp, pid)

    return min(property_ids, key=key)


def is_realtime_generation(generation: str) -> bool:
    return generation.strip().lower().startswith(REALTIME_PREFIX)


@dataclass
class Negatives:
    """The operator's negatives over a set of listings, indexed by each ruling's lowest
    listing, so a group's check costs O(the listings its merge would unite)."""

    must_not_link: dict[int, list[tuple[int, str]]] = field(default_factory=dict)
    pairs: dict[int, list[int]] = field(default_factory=dict)
    sets: dict[int, list[frozenset[int]]] = field(default_factory=dict)
    setless_keys: set[int] = field(default_factory=set)

    @classmethod
    def read(cls, conn: Any, listing_ids: Iterable[int], cluster_keys: Iterable[int]
             ) -> "Negatives":
        out = cls()
        ids = sorted(set(listing_ids))
        if not ids:
            return out
        negatives = list(NEGATIVE_VERDICTS)
        for lo, hi, src in _rows(conn, S.MUST_NOT_LINK_SQL, {"listing_ids": ids}):
            out.must_not_link.setdefault(int(lo), []).append((int(hi), str(src)))
        for lo, hi, _verdict in _rows(conn, S.PAIR_VERDICTS_SQL, {
                "listing_ids": ids, "negatives": negatives}):
            out.pairs.setdefault(int(lo), []).append(int(hi))
        for key, _verdict, _gen, member_ids in _rows(conn, S.CLUSTER_VERDICTS_SQL, {
                "listing_ids": ids, "cluster_keys": sorted(set(cluster_keys)),
                "negatives": negatives}):
            if member_ids is None:
                out.setless_keys.add(int(key))
                continue
            ruled = frozenset(int(x) for x in member_ids)
            if len(ruled) >= 2:
                out.sets.setdefault(min(ruled), []).append(ruled)
        return out

    def hits(self, listings: set[int], cluster_key: int) -> tuple[list[str], dict[str, Any]]:
        """Which negatives a property holding exactly `listings` would contradict. A pair
        ruling or must-not-link is contradicted when BOTH its sides are in the set; a group
        ruling ("these are not one property") when its WHOLE set is — this group's exact
        membership or any superset, under whichever key it was taken (E303)."""
        reasons: list[str] = []
        detail: dict[str, Any] = {}
        ordered = sorted(listings)
        ruled = [sorted(r) for lo in ordered for r in self.sets.get(lo, ()) if r <= listings]
        if ruled or cluster_key in self.setless_keys:
            reasons.append(SKIP_CLUSTER_VERDICT)
            if ruled:
                detail["group_verdicts"] = ruled[:20]
        pairs = [[lo, hi] for lo in ordered for hi in self.pairs.get(lo, ()) if hi in listings]
        if pairs:
            reasons.append(SKIP_PAIR_VERDICT)
            detail["negative_pairs"] = pairs[:20]
        mnl = [[lo, hi, src] for lo in ordered
               for hi, src in self.must_not_link.get(lo, ()) if hi in listings]
        if mnl:
            reasons.append(SKIP_MUST_NOT_LINK)
            detail["must_not_link"] = mnl[:20]
        return reasons, detail


@dataclass(frozen=True)
class EngineMerge:
    """A live merge this engine made (its own ledger, never property_merge_events — D7)."""

    generation: str
    cluster_key: int
    merge_group_id: str
    survivor_id: int | None
    member_ids: frozenset[int]

    def to_json(self) -> dict[str, Any]:
        return {"generation": self.generation, "cluster_key": self.cluster_key,
                "merge_group_id": self.merge_group_id,
                "unapply": f"generation={self.generation},cluster_key={self.cluster_key}"}


def _live_engine_merges(conn: Any, listing_ids: Iterable[int]) -> list[EngineMerge]:
    ids = sorted(set(listing_ids))
    if not ids:
        return []
    return [
        EngineMerge(str(gen), int(key), str(group), int(surv) if surv is not None else None,
                    frozenset(int(x) for x in (members or ())))
        for gen, key, group, surv, members in _rows(
            conn, S.LIVE_ENGINE_MERGES_SQL, {"listing_ids": ids})
    ]


def _engine_vouched(
    members: set[int], extended: set[int], prop_of: Mapping[int, int],
    merges: Sequence[EngineMerge],
) -> set[int]:
    """The listings of `extended` this engine already merged onto the property they share
    with a grouped member — closed transitively over live engine merges. Nothing else may
    ride along: an older merge (the removed legacy engine's included) is not the engine's
    word, and D7 forbids reading who else made it."""
    vouched = set(members)
    changed = True
    while changed:
        changed = False
        for merge in merges:
            anchors = {prop_of.get(lid) for lid in merge.member_ids & vouched} - {None}
            if not anchors:
                continue
            for lid in (merge.member_ids & extended) - vouched:
                if prop_of.get(lid) in anchors:
                    vouched.add(lid)
                    changed = True
    return vouched


def plan_apply(conn: Any, generation: str, scope: Scope) -> Plan:
    """Read-only: which groups of `generation` would merge, into what, and which are refused."""
    if is_realtime_generation(generation):
        raise ValueError(
            f"generation {generation!r} belongs to the real-time lane: it is rewritten while a "
            "plan reads it and cannot be applied from this lane"
        )
    cluster_rows = [
        dict(zip(("cluster_key", "size", "status", "block_key", "block_grain",
                  "category_main", "category_type", "min_edge_score", "model_version",
                  "feature_version"), row))
        for row in _rows(conn, S.CLUSTERS_SQL, {"generation": generation})
    ]
    members_by: dict[int, list[Member]] = {}
    listing_group: dict[int, int] = {}
    for key, lid, pid, ctype, cmain in _rows(conn, S.MEMBERS_SQL, {"generation": generation}):
        member = Member(int(lid), int(pid) if pid is not None else None, ctype, cmain)
        members_by.setdefault(int(key), []).append(member)
        listing_group[int(lid)] = int(key)

    counts: Counter[str] = Counter(clusters=len(cluster_rows))
    candidates: list[tuple[dict[str, Any], list[Member]]] = []
    settled: list[tuple[dict[str, Any], list[Member]]] = []
    for cluster in cluster_rows:
        members = members_by.get(int(cluster["cluster_key"]), [])
        if cluster["status"] != "proposed":
            counts["not_proposed"] += 1
            continue
        if not members:
            counts["no_members"] += 1
            continue
        if not scope.admits(cluster, members):
            counts["out_of_scope"] += 1
            continue
        attached = {m.property_id for m in members if m.property_id is not None}
        if len(attached) < 2 and all(m.property_id is not None for m in members):
            settled.append((cluster, members))
            continue
        candidates.append((cluster, members))

    everyone = candidates + settled
    all_props = {m.property_id for _c, ms in everyone for m in ms if m.property_id is not None}
    props: dict[int, dict[str, Any]] = {}
    children: dict[int, set[int]] = {}
    prop_of: dict[int, int] = {}
    category_of: dict[int, tuple[str | None, str | None]] = {}
    for chunk in _chunks(all_props):
        for pid, status, ctype, cmain, first in _rows(
            conn, S.PROPERTIES_SQL, {"property_ids": chunk}
        ):
            props[int(pid)] = {"status": status, "category_type": ctype,
                               "category_main": cmain, "first_seen_at": first}
        for pid, lid, ctype, cmain in _rows(conn, S.PROPERTY_LISTINGS_SQL,
                                            {"property_ids": chunk}):
            children.setdefault(int(pid), set()).add(int(lid))
            prop_of[int(lid)] = int(pid)
            category_of[int(lid)] = (ctype, cmain)
    for _c, ms in everyone:
        for m in ms:
            category_of.setdefault(m.listing_id, (m.category_type, m.category_main))

    every_listing: set[int] = {m.listing_id for _c, ms in everyone for m in ms}
    for pid in all_props:
        every_listing |= children.get(pid, set())
    negatives = Negatives.read(conn, every_listing,
                               [int(c["cluster_key"]) for c, _m in everyone])
    engine_merges = _live_engine_merges(conn, every_listing)
    merges_by_listing: dict[int, list[EngineMerge]] = {}
    for merge in engine_merges:
        for lid in merge.member_ids:
            merges_by_listing.setdefault(lid, []).append(merge)
    refused_keys: set[int] = set()
    applied_by_retired: dict[int, list[dict[str, Any]]] = {}
    if candidates:
        for gen, key, _surv, retired, outcome, undone, undone_by in _rows(
                conn, S.LEDGER_HISTORY_SQL, {
                    "generation": generation, "property_ids": sorted(all_props)}):
            if outcome == "refused" and gen == generation:
                refused_keys.add(int(key))
            elif outcome == "applied" and retired is not None:
                by_engine = bool(undone) and str(undone_by or "").startswith(UNAPPLY_BY_PREFIX)
                applied_by_retired.setdefault(int(retired), []).append(
                    {"generation": gen, "undone_by_engine": by_engine})

    groups: list[GroupPlan] = []
    for cluster, members in settled:
        # Already one property: nothing to merge. But an operator negative over what that
        # property holds means production and the operator disagree — reported, with the
        # engine merge behind it when there is one, so the operator can `unapply` it.
        key = int(cluster["cluster_key"])
        member_ids = sorted(m.listing_id for m in members)
        (pid,) = {m.property_id for m in members}
        extended = set(member_ids) | children.get(pid, set())
        hit_reasons, detail = negatives.hits(extended, key)
        if not hit_reasons:
            counts["already_one_property"] += 1
            continue
        behind = sorted({mg for lid in member_ids for mg in merges_by_listing.get(lid, ())
                         if mg.survivor_id == pid},
                        key=lambda mg: (mg.generation, mg.cluster_key))
        detail.update(negatives=hit_reasons, engine_merges=[mg.to_json() for mg in behind])
        counts[RULED_AFTER_MERGE] += 1
        groups.append(_group_plan(cluster, members, [pid], pid, [], [RULED_AFTER_MERGE],
                                  detail, extended))

    for cluster, members in candidates:
        key = int(cluster["cluster_key"])
        member_set = {m.listing_id for m in members}
        property_ids = sorted({m.property_id for m in members if m.property_id is not None})
        reasons: list[str] = []
        detail = {}

        if any(m.property_id is None for m in members):
            reasons.append(SKIP_UNATTACHED)
        inactive = [pid for pid in property_ids
                    if pid not in props or props[pid]["status"] != "active"]
        if inactive:
            reasons.append(SKIP_INACTIVE_PROPERTY)
            detail["inactive_property_ids"] = inactive
        # A merge moves EVERY listing of a property, so every check below reads the whole
        # set it would put on the survivor, and the size cap bounds the property it builds.
        extended = set(member_set)
        for pid in property_ids:
            extended |= children.get(pid, set())
        carried = extended - member_set
        if max(int(cluster["size"] or 0), len(members), len(extended)) > scope.max_cluster_size:
            reasons.append(SKIP_OVERSIZE)
            detail["listings_after_merge"] = len(extended)

        hit_reasons, hit_detail = negatives.hits(extended, key)
        reasons += hit_reasons
        detail.update(hit_detail)

        live_props = [props[pid] for pid in property_ids if pid in props]
        types = {category_of.get(lid, (None, None))[0] for lid in extended} - {None}
        types |= {p["category_type"] for p in live_props if p["category_type"] is not None}
        if len(types) > 1:
            reasons.append(SKIP_CATEGORY_TYPE)
            detail["category_types"] = sorted(types)
        mains = {category_of.get(lid, (None, None))[1] for lid in extended}
        mains |= {p["category_main"] for p in live_props}
        if not all(category_main_compatible(a, b) for a, b in combinations(
                sorted(mains, key=lambda x: (x is None, x or "")), 2)):
            reasons.append(SKIP_CATEGORY_MAIN)
            detail["category_mains"] = sorted(m for m in mains if m is not None)
        # The scope admitted the members; what rides along with them must be inside it too.
        outside = sorted(
            lid for lid in carried
            if (scope.listing_ids is not None and lid not in scope.listing_ids)
            or (scope.category_types is not None
                and category_of.get(lid, (None, None))[0] not in scope.category_types))
        if outside:
            reasons.append(SKIP_CARRIES_OUT_OF_SCOPE)
            detail["out_of_scope_listings"] = outside[:20]

        # E37 at property grain: a property whose OTHER children the engine grouped elsewhere
        # would fuse two of its groups through a link the engine never made.
        foreign = sorted(lid for lid in carried if listing_group.get(lid) not in (None, key))
        if foreign:
            reasons.append(SKIP_SPANS_GROUPS)
            detail["listings_in_other_groups"] = foreign[:20]
        # The engine vouched for its members only. A child no group holds may ride along only
        # where THIS engine already merged it onto its property together with a grouped member.
        touching = {mg for lid in extended for mg in merges_by_listing.get(lid, ())}
        vouched = _engine_vouched(member_set, extended, prop_of,
                                  sorted(touching, key=lambda mg: mg.merge_group_id))
        ungrouped = sorted(lid for lid in carried - vouched if lid not in listing_group)
        if ungrouped:
            reasons.append(SKIP_CARRIES_UNGROUPED)
            detail["ungrouped_listings"] = ungrouped[:20]

        # The engine's own ledger (never property_merge_events, D7). A property this engine
        # retired that is active again was restored by someone: the engine never re-merges
        # it on its own. After `unapply`, the SAME generation never re-applies; a later one may.
        # A merge the operator undid first stays restored-elsewhere even once `unapply` has
        # noted it (E305): only the engine's own undo releases a property to a later generation.
        if key in refused_keys:
            reasons.append(SKIP_REFUSED_BEFORE)
        for pid in property_ids:
            if pid in inactive:
                continue
            for row in applied_by_retired.get(pid, ()):
                if not row["undone_by_engine"]:
                    reasons.append(SKIP_RESTORED_ELSEWHERE)
                elif row["generation"] == generation:
                    reasons.append(SKIP_GENERATION_UNAPPLIED)
        reasons = list(dict.fromkeys(reasons))

        survivor: int | None = None
        retired: list[int] = []
        if not inactive and len(property_ids) >= 2:
            survivor = _survivor(property_ids, props, children)
            retired = [pid for pid in property_ids if pid != survivor]
        groups.append(_group_plan(cluster, members, property_ids, survivor, retired, reasons,
                                  detail, extended))

    groups.sort(key=lambda g: g.cluster_key)
    eligible = [g for g in groups if not g.reasons]
    deferred = [g.cluster_key for g in eligible[scope.max_clusters_per_run:]]
    if deferred:
        dropped = set(deferred)
        groups = [g for g in groups if g.cluster_key not in dropped]
    counts["deferred_run_cap"] = len(deferred)
    counts["planned"] = sum(1 for g in groups if not g.reasons)
    counts["skipped"] = sum(1 for g in groups if g.reasons and not g.ruled_after_merge)
    by_reason = Counter(g.reasons[0] for g in groups if g.reasons and not g.ruled_after_merge)
    return Plan(
        generation=generation,
        scope=scope,
        groups=groups,
        deferred=deferred,
        counts={**dict(counts), "skipped_by_reason": dict(sorted(by_reason.items()))},
    )


def _group_plan(
    cluster: Mapping[str, Any], members: Sequence[Member], property_ids: list[int],
    survivor: int | None, retired: list[int], reasons: list[str], detail: dict[str, Any],
    extended: set[int],
) -> GroupPlan:
    score = cluster.get("min_edge_score")
    return GroupPlan(
        cluster_key=int(cluster["cluster_key"]),
        size=int(cluster["size"] or len(members)),
        member_ids=sorted(m.listing_id for m in members),
        property_ids=property_ids,
        survivor_id=survivor,
        retired_ids=retired,
        confidence=float(score) if score is not None else None,
        model_version=cluster.get("model_version"),
        feature_version=(int(cluster["feature_version"])
                         if cluster.get("feature_version") is not None else None),
        reasons=reasons,
        detail=detail,
        listing_ids=sorted(extended),
    )


# ------------------------------------------------------------------ apply


def new_run_id() -> str:
    gh = os.environ.get("GITHUB_RUN_ID")
    return f"gh-{gh}" if gh else f"local-{uuid.uuid4().hex[:12]}"


def _ledger_row(
    run_id: str, generation: str, group: GroupPlan, *, dry_run: bool, outcome: str,
    retired: int | None, merge_group_id: str | None = None, error: str | None = None,
    listings_moved: int | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "generation": generation,
        "cluster_key": group.cluster_key,
        "survivor_property_id": group.survivor_id,
        "retired_property_id": retired,
        "merge_group_id": merge_group_id,
        "dry_run": dry_run,
        "outcome": outcome,
        "error": error,
        "listings_moved": listings_moved,
        "member_ids": group.member_ids,
        "plan_json": json.dumps(group.to_json(), sort_keys=True),
    }


def _rows_for(
    run_id: str, generation: str, group: GroupPlan, *, dry_run: bool, outcome: str,
    merge_group_id: str | None = None, error: str | None = None,
    moved: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """One ledger row per retired property; a group with none (refused before a survivor
    could be named) still gets one row, so every refusal is on record."""
    if not group.retired_ids:
        return [_ledger_row(run_id, generation, group, dry_run=dry_run, outcome=outcome,
                            retired=None, merge_group_id=merge_group_id, error=error)]
    return [
        _ledger_row(run_id, generation, group, dry_run=dry_run, outcome=outcome,
                    retired=retired, merge_group_id=merge_group_id, error=error,
                    listings_moved=(moved[i] if moved is not None else None))
        for i, retired in enumerate(group.retired_ids)
    ]


def _terminal(exc: MergeError) -> bool:
    """E41: a category refusal at the chokepoint is final for this group in this generation;
    a property-state refusal (a concurrent operator merge) is re-planned from fresh state."""
    return "mismatch" in str(exc)


def _brief(group: GroupPlan, **extra: Any) -> dict[str, Any]:
    return {"cluster_key": group.cluster_key, "survivor_id": group.survivor_id,
            "retired_ids": group.retired_ids, **extra}


def _ruled_brief(group: GroupPlan) -> dict[str, Any]:
    merges = group.detail.get("engine_merges") or []
    note = ("merged by this engine - to undo: " + "; ".join(m["unapply"] for m in merges)
            if merges else "on one property by a merge this engine did not make")
    return _brief(group, negatives=group.detail.get("negatives") or [],
                  engine_merges=merges, note=note)


class _SkipAtApply(Exception):
    """Raised inside a group's transaction when the re-check refuses it: nothing merges."""

    def __init__(self, reasons: list[str], detail: dict[str, Any]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons
        self.detail = detail


def recheck_group(conn: Any, group: GroupPlan) -> tuple[list[str], dict[str, Any]]:
    """The plan's word, re-read inside the group's own transaction just before it merges: the
    listings the merge would unite must be the ones planned, and no operator negative may
    have landed on them since the plan read the negatives (E303)."""
    now = {int(row[1]) for row in _rows(conn, S.PROPERTY_LISTINGS_SQL,
                                        {"property_ids": group.property_ids})}
    planned = set(group.listing_ids)
    if now != planned:
        return [SKIP_CHANGED_SINCE_PLAN], {
            "arrived_since_plan": sorted(now - planned)[:20],
            "left_since_plan": sorted(planned - now)[:20]}
    return Negatives.read(conn, now, [group.cluster_key]).hits(now, group.cluster_key)


def apply_plan(
    conn: Any,
    plan: Plan,
    dry_run: bool,
    *,
    merge: Callable[..., dict[str, Any]] = merge_properties,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Record the plan (dry run) or execute it: one transaction and one merge group per engine
    group, so a refusal anywhere in a group rolls the whole group back."""
    run_id = run_id or new_run_id()
    if not dry_run:
        if not apply_enabled(conn):
            raise ApplyRefused(
                f"app_settings.{ENABLED_SETTING} is not true: a live apply is refused "
                "(a dry run is still allowed)"
            )
        problems = plan.scope.live_problems()
        if problems:
            raise ApplyRefused("; ".join(problems))

    gen = plan.generation
    result: dict[str, Any] = {
        "run_id": run_id,
        "generation": gen,
        "dry_run": dry_run,
        "scope": plan.scope.to_json(),
        "counts": dict(plan.counts),
        "planned": [_brief(g) for g in plan.to_apply],
        "skipped": [_brief(g, reasons=g.reasons) for g in plan.skipped
                    if not g.ruled_after_merge],
        RULED_AFTER_MERGE: [_ruled_brief(g) for g in plan.skipped
                            if g.ruled_after_merge],
        "deferred": list(plan.deferred),
        "applied": [],
        "skipped_at_apply": [],
        "refused": [],
        "failed": [],
    }
    skipped_rows = [row for g in plan.skipped
                    for row in _rows_for(run_id, gen, g, dry_run=dry_run, outcome="skipped",
                                         error=g.reasons[0])]
    if dry_run:
        planned_rows = [row for g in plan.to_apply
                        for row in _rows_for(run_id, gen, g, dry_run=True, outcome="planned")]
        with conn.transaction():
            _exec_many(conn, S.LEDGER_INSERT_SQL, skipped_rows + planned_rows)
        return result

    with conn.transaction():
        _exec_many(conn, S.LEDGER_INSERT_SQL, skipped_rows)

    counts = result["counts"]
    counts.update(applied=0, skipped_at_apply=0, refused=0, failed=0, listings_moved=0)
    todo = plan.to_apply
    for index, group in enumerate(todo):
        # E39: read fresh before every group, so the operator's switch stops a run mid-way.
        if not apply_enabled(conn):
            result["stopped"] = f"{ENABLED_SETTING} turned off during the run"
            counts["not_attempted"] = len(todo) - index
            break
        group_id = str(uuid.uuid4())
        markers = {
            "engine": "autodedup", "generation": gen, "cluster_key": group.cluster_key,
            "run_id": run_id, "model_version": group.model_version,
            "feature_version": group.feature_version, "members": group.member_ids,
            "scope": plan.scope.to_json(),
        }
        try:
            with conn.transaction():
                late_reasons, late_detail = recheck_group(conn, group)
                if late_reasons:
                    raise _SkipAtApply(late_reasons, late_detail)
                moved: list[int] = []
                for retired in group.retired_ids:
                    res = merge(
                        conn,
                        survivor_id=group.survivor_id,
                        retired_id=retired,
                        reason=f"autodedup {gen} {group.cluster_key}",
                        source=MERGE_SOURCE,
                        confidence=group.confidence,
                        markers=markers,
                        merge_group_id=group_id,
                    )
                    moved.append(int((res.get("data") or {}).get("listings_moved") or 0))
                _exec(conn, S.STAMP_GENERATION_SQL,
                      {"stamp": f"{STAMP_PREFIX}{gen}", "merge_group_id": group_id})
                _exec_many(conn, S.LEDGER_INSERT_SQL, _rows_for(
                    run_id, gen, group, dry_run=False, outcome="applied",
                    merge_group_id=group_id, moved=moved))
        except _SkipAtApply as skip:
            group.reasons = skip.reasons
            group.detail = {**group.detail, **skip.detail, "at_apply": True}
            counts["skipped_at_apply"] += 1
            result["skipped_at_apply"].append(_brief(group, reasons=skip.reasons))
            with conn.transaction():
                _exec_many(conn, S.LEDGER_INSERT_SQL, _rows_for(
                    run_id, gen, group, dry_run=False, outcome="skipped",
                    error=skip.reasons[0]))
            continue
        except MergeError as exc:
            outcome = "refused" if _terminal(exc) else "failed"
            counts[outcome] += 1
            result[outcome].append(_brief(group, error=str(exc)))
            with conn.transaction():
                _exec_many(conn, S.LEDGER_INSERT_SQL, _rows_for(
                    run_id, gen, group, dry_run=False, outcome=outcome, error=str(exc)))
            continue
        except Exception as exc:
            # Not a refusal the chokepoint names: record what can be recorded and stop, rather
            # than carry on merging over a database in a state nobody has looked at.
            try:
                with conn.transaction():
                    _exec_many(conn, S.LEDGER_INSERT_SQL, _rows_for(
                        run_id, gen, group, dry_run=False, outcome="failed",
                        error=f"{type(exc).__name__}: {exc}"))
            except Exception:  # noqa: BLE001 — the original error is the one to surface
                pass
            raise
        counts["applied"] += 1
        counts["listings_moved"] += sum(moved)
        result["applied"].append(_brief(group, merge_group_id=group_id,
                                        listings_moved=sum(moved)))
    return result


class _NothingMovedBack(Exception):
    """Raised inside an undo's transaction when it would reactivate properties and move no
    listing back: rolled back, so the ledger never calls a merge undone that still stands."""

    def __init__(self, conflicts: list[Any]) -> None:
        super().__init__(f"{len(conflicts)} conflicts, nothing moved back")
        self.conflicts = conflicts


def _survivor_block(conn: Any, target: Mapping[str, Any]) -> dict[str, Any] | None:
    """Why a group cannot be undone yet: its survivor has since been merged away (a LATER
    merge moved every listing on), so `unmerge_group` would only reactivate empty retired
    properties. Names the live engine merge to undo first, when the engine made it."""
    survivor = target.get("survivor_id")
    if survivor is None:
        return None
    rows = _rows(conn, S.PROPERTIES_SQL, {"property_ids": [survivor]})
    status = rows[0][1] if rows else None
    if status == "active":
        return None
    first = [{"generation": gen, "cluster_key": int(key),
              "unapply": f"generation={gen},cluster_key={int(key)}"}
             for gen, key, _retired in _rows(conn, S.LIVE_RETIRING_SQL,
                                             {"property_ids": [survivor]})]
    reason = (f"survivor {survivor} is {status or 'missing'}: "
              + ("undo the later engine merge first" if first
                 else "a merge this engine did not make moved it on"))
    return {"blocked": reason, "unapply_first": first}


def unapply(
    conn: Any,
    generation: str,
    *,
    dry_run: bool = False,
    cluster_key: int | None = None,
    unmerge: Callable[..., dict[str, Any]] = unmerge_group,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Undo a generation's live merges newest-first (or one group), each through
    `unmerge_group`; `dry_run=True` only lists them (the lane's default). NOT gated by
    `autodedup_apply_enabled`: undo is the way back. A group whose survivor a later merge has
    since retired is skipped with the reason and the merge to undo first."""
    run_id = run_id or new_run_id()
    targets = [
        {"merge_group_id": str(group), "cluster_key": int(key),
         "survivor_id": int(surv) if surv is not None else None,
         "retired_ids": [int(r) for r in (retired or []) if r is not None]}
        for group, key, surv, retired, _last in _rows(conn, S.UNAPPLY_TARGETS_SQL, {
            "generation": generation, "cluster_key": cluster_key})
    ]
    result: dict[str, Any] = {
        "run_id": run_id, "generation": generation, "dry_run": dry_run,
        "cluster_key": cluster_key, "groups": targets,
        "counts": {"groups": len(targets), "undone": 0, "already_undone": 0, "blocked": 0,
                   "listings_moved_back": 0, "conflicts": 0},
    }
    counts = result["counts"]
    if dry_run:
        for target in targets:
            block = _survivor_block(conn, target)
            if block:
                target.update(block)
                counts["blocked"] += 1
        return result
    undone_by = f"{UNAPPLY_BY_PREFIX}{run_id}"
    for target in targets:
        # Read per group, just before it is undone: an earlier undo in this run may have
        # restored the survivor this one needs.
        block = _survivor_block(conn, target)
        if block:
            target.update(block)
            counts["blocked"] += 1
            continue
        try:
            with conn.transaction():
                res = unmerge(conn, merge_group_id=target["merge_group_id"],
                              undone_by=undone_by)
                data = res.get("data") or {}
                if not int(data.get("listings_moved_back") or 0) and data.get("conflicts"):
                    raise _NothingMovedBack(list(data["conflicts"]))
                _exec(conn, S.LEDGER_UNDO_SQL, {
                    "merge_group_id": target["merge_group_id"], "undone_by": undone_by,
                    "undo_result": json.dumps(data, sort_keys=True, default=str)})
        except _NothingMovedBack as nothing:
            target["blocked"] = (f"nothing would move back ({len(nothing.conflicts)} listings "
                                 "moved on since the merge); left as it stands")
            target["conflicts"] = nothing.conflicts
            counts["blocked"] += 1
            continue
        except MergeError as exc:
            # Nothing left to undo: the group was reversed outside the engine (the merge
            # ledger's own unmerge). The engine's ledger records that it no longer stands —
            # as SOMEONE ELSE's undo, so the property stays restored-elsewhere (E305).
            with conn.transaction():
                _exec(conn, S.LEDGER_UNDO_SQL, {
                    "merge_group_id": target["merge_group_id"], "undone_by": EXTERNAL_UNDO,
                    "undo_result": json.dumps({"error": str(exc), "noted_by": undone_by})})
            counts["already_undone"] += 1
            target["error"] = str(exc)
            continue
        counts["undone"] += 1
        counts["listings_moved_back"] += int(data.get("listings_moved_back") or 0)
        counts["conflicts"] += len(data.get("conflicts") or [])
        target["conflicts"] = list(data.get("conflicts") or [])
    return result


# ------------------------------------------------------------------ lane modes


def _dry_run_arg(args: Mapping[str, str]) -> bool:
    """Only an explicit `dry_run=0` (or false/no/off) is live; absent or empty is a dry run."""
    raw = (args.get("dry_run") or "").strip()
    if not raw:
        return True
    try:
        return _truthy(raw)
    except ValueError as exc:
        raise SystemExit(f"dry_run: {exc}") from exc


def _generation_arg(args: Mapping[str, str]) -> str:
    generation = (args.get("generation") or "").strip()
    if not generation:
        raise SystemExit("generation= is required (e.g. generation=g12)")
    return generation


def _check_args(args: Mapping[str, str], allowed: frozenset[str]) -> None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise SystemExit(
            f"unknown arg(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}")


def _capped(result: dict[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    out = dict(result)
    for key in keys:
        items = out.get(key)
        if isinstance(items, list) and len(items) > SUMMARY_LIST_CAP:
            out[key] = items[:SUMMARY_LIST_CAP]
            out[f"{key}_truncated"] = len(items) - SUMMARY_LIST_CAP
    return out


def _close(conn: Any) -> None:
    close = getattr(conn, "close", None)
    if callable(close):
        close()


def summary_markdown(result: Mapping[str, Any], *, mode: str) -> str:
    """The run's page on GitHub: what was planned, applied or refused, and why."""
    counts = result.get("counts") or {}
    head = "DRY RUN — nothing merged" if result.get("dry_run") else "LIVE"
    lines = [f"## autodedup {mode} {result.get('generation')} — {head}", ""]
    if result.get("stopped"):
        lines += [f"**Stopped:** {result['stopped']}", ""]
    lines += ["| count | n |", "| --- | --- |"]
    lines += [f"| {key} | {value} |" for key, value in counts.items()
              if not isinstance(value, dict)]
    by_reason = counts.get("skipped_by_reason") or {}
    if by_reason:
        lines += ["", "| skipped because | groups |", "| --- | --- |"]
        lines += [f"| {reason} | {n} |" for reason, n in by_reason.items()]
    shown = 50
    if result.get(RULED_AFTER_MERGE):
        lines += ["", f"**{len(result[RULED_AFTER_MERGE])} group(s) already on one property "
                  "carry an operator negative** - production and the operator disagree; "
                  "nothing was changed. Undo an engine merge with `mode=unapply` and the "
                  "arguments named below."]
    for key in (RULED_AFTER_MERGE, "applied", "skipped_at_apply", "refused", "failed",
                "planned", "skipped", "groups"):
        rows = result.get(key) or []
        if not rows:
            continue
        lines += ["", f"### {key} ({len(rows)}{'+' if result.get(f'{key}_truncated') else ''})",
                  "", "| group | survivor | retired | note |", "| --- | --- | --- | --- |"]
        for row in rows[:shown]:
            note = row.get("note") or row.get("blocked") or row.get("error") \
                or ", ".join(row.get("reasons") or []) or row.get("merge_group_id") or ""
            lines.append(f"| {row.get('cluster_key')} | {row.get('survivor_id')} | "
                         f"{' '.join(str(r) for r in row.get('retired_ids') or [])} | {note} |")
        if len(rows) > shown:
            lines.append(f"| … {len(rows) - shown} more in the artifact | | | |")
    return "\n".join(lines) + "\n"


def _step_summary(result: Mapping[str, Any], *, mode: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(summary_markdown(result, mode=mode))
    except OSError:
        pass


def run_apply(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Path
) -> dict[str, Any]:
    _check_args(args, APPLY_ARGS)
    generation = _generation_arg(args)
    if is_realtime_generation(generation):
        raise SystemExit(
            f"generation={generation}: a real-time lane generation cannot be applied from "
            "this lane (it is rewritten while a plan reads it)"
        )
    dry_run = _dry_run_arg(args)
    override = {key: args[key] for key in SCOPE_KEYS if key in args}
    conn = conn_factory()
    try:
        # The switch first: a refused live run costs one query and reads no plan.
        if not dry_run and not apply_enabled(conn):
            raise SystemExit(
                f"a live apply needs app_settings.{ENABLED_SETTING} = true; it is not. "
                "Run with dry_run=1, or ask the operator to turn it on."
            )
        try:
            scope = effective_scope(read_scope_setting(conn), override, live=not dry_run)
        except ValueError as exc:
            raise SystemExit(f"scope: {exc}") from exc
        plan = plan_apply(conn, generation, scope)
        try:
            result = apply_plan(conn, plan, dry_run)
        except ApplyRefused as exc:
            raise SystemExit(str(exc)) from exc
    finally:
        _close(conn)
    out_dir = Path(out_dir)
    write_json(out_dir / "apply.json", {
        **result, "plan": [g.to_json() for g in plan.groups]})
    summary = _capped(result, ("planned", "skipped", RULED_AFTER_MERGE, "applied",
                               "skipped_at_apply", "refused", "failed", "deferred"))
    _step_summary(summary, mode="apply")
    return summary


def run_unapply(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Path
) -> dict[str, Any]:
    _check_args(args, UNAPPLY_ARGS)
    generation = _generation_arg(args)
    dry_run = _dry_run_arg(args)
    raw_key = (args.get("cluster_key") or "").strip()
    try:
        cluster_key = int(raw_key) if raw_key else None
    except ValueError as exc:
        raise SystemExit(f"cluster_key must be an integer, got {raw_key!r}") from exc
    conn = conn_factory()
    try:
        result = unapply(conn, generation, dry_run=dry_run, cluster_key=cluster_key)
    finally:
        _close(conn)
    write_json(Path(out_dir) / "unapply.json", result)
    summary = _capped(result, ("groups",))
    _step_summary(summary, mode="unapply")
    return summary
