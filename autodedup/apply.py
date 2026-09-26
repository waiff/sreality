"""A1 — one generation's groups into production merges, through THE chokepoint.

    python3 -m autodedup.lane --mode apply   --args "generation=g12" --out out/
    python3 -m autodedup.lane --mode apply   --args "generation=g12,dry_run=0" --out out/
    python3 -m autodedup.lane --mode apply   --args "generation=g12,retire_legacy=1" --out out/
    python3 -m autodedup.lane --mode unapply --args "generation=g12,dry_run=0" --out out/
    python3 -m autodedup.lane --mode unapply --args "since=2026-09-25T08:00Z" --out out/

`plan_apply` reads a generation's groups and, per group, names the survivor (the toolkit's one
rule, `survivor_of`) and the properties that would retire into it — or the reasons the group is
refused (E903). `apply_plan` records that plan (dry run) or executes it through
`toolkit.property_identity.merge_property_set`, one merge group per engine group (E901), and
`unapply` undoes groups newest-first, picked by generation, run or time window, each as a loop
of `detach_listing` over the adverts its merge moved. Nothing here decides
anything the engine did not: the groups are read as stored, and every refusal only removes a
group from the plan.

THE SCOPE ROW IS THE ONE ROLLOUT CONTROL (E904). A dry run is the default and writes only
`autodedup.applied_merges`. A live run needs `app_settings.autodedup_apply_scope` to name its
deal types and its area; a row that is absent or names no area merges nothing. It is re-read
before EVERY group (E39), so emptying its area on /settings stops a run between two groups, and
a run's own arguments can narrow it and never widen it. A group is inside the area only when
EVERY advert its merge would move (the members and every other advert on the involved
properties) is LOCATED in one of its blocks by its live `listing_location` row (`town:` =
obec_kod, `quarter:` = cast_obce_kod) — never by the engine's own blocking key, which names a
quarter or nothing where the scope names the town.

D7 holds: nothing here reads `property_merge_events`, and only `toolkit.property_identity` writes
it (the chokepoint's merge rows, `source='autodedup'` saying who merged; a detach's undone stamps;
an operator's native split's one closed row). A detach and its read-only preview,
`detach_outcomes`, read it inside the toolkit. The apply path's own ledger is the only history
it consults.
The one carve-out is TEMPORARY: `retire_legacy=1` runs `autodedup.legacy_retire` (A2, kept until
W8) before the plan, which reads it to undo the old engine's merges in the scope's blocks and
deal types (plus any that mixes deal types).
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from autodedup import apply_sql as S
from autodedup import legacy_retire, rt_lease
from autodedup.census import write_json
from autodedup.incremental import GENERATION
from autodedup.ui_sql import NEGATIVE_VERDICTS
from toolkit.property_identity import (
    AssetLinkConflict,
    MergeError,
    detach_listing,
    detach_outcomes,
    merge_property_set,
    survivor_of,
)
from toolkit.room_taxonomy import category_main_compatible

SCOPE_SETTING: str = "autodedup_apply_scope"
# The worker lane's one switch (migrations 557/568): 0 = stopped. A live apply or unapply runs
# only while it is 0 (`_Writer`).
LANE_INTERVAL_SETTING: str = "realtime_autodedup_interval_seconds"
MERGE_SOURCE: str = "autodedup"
UNAPPLY_BY_PREFIX: str = "autodedup-unapply:"
# `undone_by` on a ledger row whose merge someone else had already undone when `unapply` came
# to it: the engine records that the merge no longer stands, never that it undid it (E905).
EXTERNAL_UNDO: str = "external"
# `properties.merged_at` (the chokepoint) and the ledger's `applied_at` are both now() of one
# group's transaction; a wider gap is another merge of the same pair, after an undo (E905).
SAME_MERGE_TOLERANCE: timedelta = timedelta(seconds=1)
DEFAULT_MAX_CLUSTERS_PER_RUN: int = 200
SUMMARY_LIST_CAP: int = 200
ID_CHUNK: int = 5000

SCOPE_KEYS: tuple[str, ...] = (
    "category_types", "blocks", "listing_ids", "all_blocks", "max_clusters_per_run",
)
# `retire_legacy`: A2's pre-step (autodedup/legacy_retire.py), temporary, kept until W8.
APPLY_ARGS: frozenset[str] = frozenset({"generation", "dry_run", "retire_legacy",
                                        rt_lease.RELEASE_ARG, *SCOPE_KEYS})
# What `unapply` selects by: a generation (and one of its groups), a run, a time window.
UNAPPLY_SELECTORS: tuple[str, ...] = ("generation", "cluster_key", "run", "since", "until")
UNAPPLY_ARGS: frozenset[str] = frozenset({"dry_run", rt_lease.RELEASE_ARG, *UNAPPLY_SELECTORS})

# A scope block, spelled as the export lane spells it: `town:<code>` (an advert's
# listing_location.obec_kod) / `quarter:<code>` (its cast_obce_kod).
GRAINS: tuple[str, ...] = ("town", "quarter")

# E904 — why a group is outside the scope, counted per reason in the run summary. Order is the
# order a group's reason is picked in when its members fall outside for several.
OUT_CATEGORY = "category_outside_scope"
OUT_BLOCKS = "member_outside_blocks"
OUT_NO_LOCATION = "member_without_location"
OUT_LISTING_IDS = "member_outside_listing_ids"
OUT_OF_SCOPE_REASONS: tuple[str, ...] = (OUT_CATEGORY, OUT_BLOCKS, OUT_NO_LOCATION,
                                         OUT_LISTING_IDS)

# E903 — why a group is refused. Order is the order they are checked and reported in.
SKIP_UNATTACHED = "unattached_member"
SKIP_INACTIVE_PROPERTY = "property_not_active"
SKIP_CLUSTER_VERDICT = "operator_group_verdict"
SKIP_PAIR_VERDICT = "operator_pair_verdict"
SKIP_MUST_NOT_LINK = "must_not_link"
SKIP_CATEGORY_TYPE = "category_type_mix"
SKIP_CATEGORY_MAIN = "category_main_incompatible"
SKIP_CARRIES_OUT_OF_SCOPE = "carries_out_of_scope_listings"
# The merge itself refused two different asset links (decision 17): one survivor keeps one.
SKIP_ASSET_LINKED = "asset_linked_units"
SKIP_SPANS_GROUPS = "property_spans_groups"
SKIP_CARRIES_UNGROUPED = "carries_ungrouped_listings"
SKIP_REFUSED_BEFORE = "refused_at_chokepoint_before"
SKIP_RESTORED_ELSEWHERE = "restored_outside_engine"
# Re-checked inside the group's own transaction, just before the merge (E903).
SKIP_CHANGED_SINCE_PLAN = "changed_since_plan"
# Not a merge at all: a group ALREADY on one property that an operator negative now covers.
# Reported, never acted on — `unapply` is the operator's call (E905).
RULED_AFTER_MERGE = "ruled_different_after_merge"



class ApplyRefused(RuntimeError):
    """A live apply was asked for while the scope forbids it."""


# An error that stops a live run mid-way carries the run's partial result under this
# attribute, so the lane still writes apply.json and the step summary before re-raising.
PARTIAL_RESULT_ATTR: str = "autodedup_apply_result"


# ------------------------------------------------------------------ scope


@dataclass(frozen=True)
class Scope:
    """Which groups a run may touch. `None` = unrestricted on that axis; an EMPTY set admits
    nothing (a narrowing that intersected to nothing must not read as 'no filter')."""

    category_types: frozenset[str] | None = None
    blocks: frozenset[str] | None = None
    listing_ids: frozenset[int] | None = None
    all_blocks: bool = False
    max_clusters_per_run: int = DEFAULT_MAX_CLUSTERS_PER_RUN

    def to_json(self) -> dict[str, Any]:
        return {
            "category_types": sorted(self.category_types) if self.category_types is not None
            else None,
            "blocks": sorted(self.blocks) if self.blocks is not None else None,
            "listing_ids": len(self.listing_ids) if self.listing_ids is not None else None,
            "all_blocks": self.all_blocks,
            "max_clusters_per_run": self.max_clusters_per_run,
        }

    def live_problems(self) -> list[str]:
        """Why this scope may not drive a LIVE run; empty when it may. An empty list names
        nothing, like an absent key: no deal types or no area merges nothing (E904)."""
        problems: list[str] = []
        if not self.category_types:
            problems.append("the live scope names no category_types")
        if not self.blocks and not self.listing_ids and not self.all_blocks:
            problems.append(
                "the live scope names no area: set blocks, listing_ids, or all_blocks=true"
            )
        return problems

    def outside(self, listing: "Member") -> str | None:
        """Why one advert is outside the scope, None when it is inside (E904). Inside the blocks
        means LOCATED in one: its live obec_kod names a `town:` of the scope or its cast_obce_kod
        a `quarter:` (legacy_retire.AREA_SQL's area); an advert with neither code is in none."""
        if self.category_types is not None and listing.category_type not in self.category_types:
            return OUT_CATEGORY
        if self.blocks is not None:
            if listing.obec_kod is None and listing.cast_obce_kod is None:
                return OUT_NO_LOCATION
            if (f"town:{listing.obec_kod}" not in self.blocks
                    and f"quarter:{listing.cast_obce_kod}" not in self.blocks):
                return OUT_BLOCKS
        if self.listing_ids is not None and listing.listing_id not in self.listing_ids:
            return OUT_LISTING_IDS
        return None

    def group_outside(self, members: Sequence["Member"]) -> str | None:
        """Why a group is outside the scope (the first of OUT_OF_SCOPE_REASONS any member
        gives), None when every member is inside; the advert its merge would carry along is
        `_set_reasons`' (SKIP_CARRIES_OUT_OF_SCOPE)."""
        found = {self.outside(m) for m in members}
        return next((why for why in OUT_OF_SCOPE_REASONS if why in found), None)


def normalize_block(raw: Any) -> str:
    """`town:563510` / `quarter:490245`, the export lane's spelling; anything else is refused."""
    grain, _, code = str(raw).strip().lower().partition(":")
    if grain not in GRAINS or not code.isdigit():
        raise ValueError(f"block {raw!r}: expected town:<code> or quarter:<code>")
    return f"{grain}:{int(code)}"


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
    if "max_clusters_per_run" in raw:
        value = int(raw["max_clusters_per_run"])
        if value < 1:
            raise ValueError(f"max_clusters_per_run must be >= 1, got {value}")
        out["max_clusters_per_run"] = value
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


def _int(value: Any) -> int | None:
    return int(value) if value is not None else None


@dataclass(frozen=True)
class Member:
    """One advert as the plan reads it: its property, deal type and category, and where it IS
    (its live listing_location codes, both None without a row)."""

    listing_id: int
    property_id: int | None
    category_type: str | None
    category_main: str | None
    obec_kod: int | None = None
    cast_obce_kod: int | None = None

    @classmethod
    def read(cls, row: Sequence[Any]) -> "Member":
        """A row of PROPERTY_LISTINGS_SQL / LOCK_PROPERTY_LISTINGS_SQL, or of MEMBERS_SQL
        after its cluster_key."""
        lid, pid, ctype, cmain, obec, cast = row
        return cls(int(lid), _int(pid), ctype, cmain, _int(obec), _int(cast))


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
    # Which involved property each of those listings sits on — re-recorded over the locked rows
    # as the group merges, so `unapply` knows which adverts its detach loop moves back (E905).
    listings_by_property: dict[int, list[int]] = field(default_factory=dict)

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
            "listings_by_property": {str(pid): lids for pid, lids
                                     in sorted(self.listings_by_property.items())},
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


def read_scope_setting(conn: Any) -> dict[str, Any] | None:
    value = read_setting(conn, SCOPE_SETTING)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{SCOPE_SETTING} must be a JSON object, got {type(value).__name__}")
    return value


def scope_closed(conn: Any) -> str | None:
    """Why the operator's scope row, read now, admits no live merge; None while it does. An
    absent or malformed row, or one naming no deal types or no area, is closed (E904)."""
    try:
        problems = Scope(**scope_fields(read_scope_setting(conn))).live_problems()
    except ValueError as exc:
        return str(exc)
    return "; ".join(problems) or None


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
        # Per (set, operator), only the newest ruling stands: a set ruled different and later
        # ruled same by the same operator no longer refuses. A setless row fails closed.
        newest: dict[tuple[frozenset[int], str], tuple[tuple[float, int], str]] = {}
        for key, verdict, _gen, member_ids, decided_by, decided_at, vid in _rows(
                conn, S.CLUSTER_VERDICTS_SQL, {
                    "listing_ids": ids, "cluster_keys": sorted(set(cluster_keys)),
                    "negatives": negatives}):
            if member_ids is None:
                if verdict in NEGATIVE_VERDICTS:
                    out.setless_keys.add(int(key))
                continue
            ruled = frozenset(int(x) for x in member_ids)
            if len(ruled) < 2:
                continue
            order = (decided_at.timestamp() if isinstance(decided_at, datetime) else 0.0,
                     int(vid or 0))
            slot = (ruled, str(decided_by))
            if slot not in newest or order > newest[slot][0]:
                newest[slot] = (order, str(verdict))
        for (ruled, _by), (_order, verdict) in sorted(
                newest.items(), key=lambda kv: (sorted(kv[0][0]), kv[0][1])):
            bucket = out.sets.setdefault(min(ruled), [])
            if verdict in NEGATIVE_VERDICTS and ruled not in bucket:
                bucket.append(ruled)
        return out

    def hits(self, listings: set[int], cluster_key: int) -> tuple[list[str], dict[str, Any]]:
        """Which negatives a property holding exactly `listings` would contradict. A pair
        ruling or must-not-link is contradicted when BOTH its sides are in the set; a group
        ruling ("these are not one property") when its WHOLE set is — this group's exact
        membership or any superset, under whichever key it was taken (E903)."""
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


def _unapply_args(generation: str, cluster_key: int) -> str:
    return f"generation={generation},cluster_key={int(cluster_key)}"


@dataclass(frozen=True)
class EngineMerge:
    """A merge this engine made (its own ledger, never property_merge_events — D7)."""

    generation: str
    cluster_key: int
    merge_group_id: str
    survivor_id: int | None
    member_ids: frozenset[int]
    live: bool = True
    # Undone by `unapply` itself — the one undo that states nothing about the listings.
    undone_by_engine: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"generation": self.generation, "cluster_key": self.cluster_key,
                "merge_group_id": self.merge_group_id,
                "unapply": _unapply_args(self.generation, self.cluster_key)}


def _undone_by_engine(undone: Any, undone_by: Any) -> bool:
    return bool(undone) and str(undone_by or "").startswith(UNAPPLY_BY_PREFIX)


def _engine_merges(conn: Any, listing_ids: Iterable[int]) -> list[EngineMerge]:
    ids = sorted(set(listing_ids))
    if not ids:
        return []
    return [
        EngineMerge(str(gen), int(key), str(group), int(surv) if surv is not None else None,
                    frozenset(int(x) for x in (members or ())), live=not undone,
                    undone_by_engine=_undone_by_engine(undone, undone_by))
        for gen, key, group, surv, members, undone, undone_by in _rows(
            conn, S.ENGINE_MERGES_SQL, {"listing_ids": ids})
    ]


def _separated_merges(
    listings: set[int], prop_of: Mapping[int, int], merges: Iterable[EngineMerge],
) -> list[dict[str, Any]]:
    """Engine merges someone other than `unapply` took apart, whose listings this set holds on
    two or more properties: a merge of the set would re-unite what was separated (E905). Keyed
    on listings, so it survives the restored property being merged on into another one."""
    out: list[dict[str, Any]] = []
    for merge in merges:
        if merge.undone_by_engine:
            continue
        inside = merge.member_ids & listings
        if len({prop_of.get(lid) for lid in inside} - {None}) > 1:
            out.append({"generation": merge.generation, "cluster_key": merge.cluster_key,
                        "merge_group_id": merge.merge_group_id,
                        "listings": sorted(inside)[:20]})
    return sorted(out, key=lambda m: (m["generation"], m["cluster_key"]))


def _set_reasons(
    listings: set[int],
    listing_of: Mapping[int, Member],
    props: Sequence[Mapping[str, Any]],
    scope: Scope,
) -> tuple[list[str], dict[str, Any]]:
    """What the merge would build, checked as the chokepoint would plus the run's scope: at
    plan time and again, over locked rows, just before it merges."""
    reasons: list[str] = []
    detail: dict[str, Any] = {}
    facts = [listing_of.get(lid) or Member(lid, None, None, None) for lid in sorted(listings)]
    types = {f.category_type for f in facts} - {None}
    types |= {p["category_type"] for p in props if p["category_type"] is not None}
    if len(types) > 1:
        reasons.append(SKIP_CATEGORY_TYPE)
        detail["category_types"] = sorted(types)
    mains = {f.category_main for f in facts}
    mains |= {p["category_main"] for p in props}
    if not all(category_main_compatible(a, b) for a, b in combinations(
            sorted(mains, key=lambda x: (x is None, x or "")), 2)):
        reasons.append(SKIP_CATEGORY_MAIN)
        detail["category_mains"] = sorted(m for m in mains if m is not None)
    # The scope admitted the members; everything the merge puts on the survivor must be inside
    # it too (located in its blocks included) — and still be, when it is re-read just before
    # the merge.
    outside = [f.listing_id for f in facts if scope.outside(f)]
    if outside:
        reasons.append(SKIP_CARRIES_OUT_OF_SCOPE)
        detail["out_of_scope_listings"] = outside[:20]
    return reasons, detail


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


def _cluster_rows(conn: Any, generation: str) -> list[dict[str, Any]]:
    return [
        dict(zip(("cluster_key", "size", "status", "min_edge_score", "model_version",
                  "feature_version"), row))
        for row in _rows(conn, S.CLUSTERS_SQL, {"generation": generation})
    ]


def _members(conn: Any, generation: str) -> dict[int, list[Member]]:
    members: dict[int, list[Member]] = {}
    for row in _rows(conn, S.MEMBERS_SQL, {"generation": generation}):
        members.setdefault(int(row[0]), []).append(Member.read(row[1:]))
    return members


def plan_apply(conn: Any, generation: str, scope: Scope) -> Plan:
    """Read-only: which groups of `generation` would merge, into what, and which are refused.

    The live stream may be PLANNED (a dry run is how G3 predicts the lane's first reconcile)
    but never applied from here: the lane reconciles it itself, under its lease (A9)."""
    return plan_groups(conn, generation, _cluster_rows(conn, generation),
                       _members(conn, generation), scope)


def plan_groups(
    conn: Any, generation: str, cluster_rows: Sequence[Mapping[str, Any]],
    members_by: Mapping[int, list[Member]], scope: Scope, *,
    group_of: Callable[[set[int]], Mapping[int, int]] | None = None, cap: bool = True,
) -> Plan:
    """The plan for the groups handed in — `plan_apply`'s for a whole batch generation, and the
    real-time lane's reconcile (A9) for the groups a pass re-clustered or swept. ONE set of
    refusals for both (E903): the reconcile is this function under the lane's lease.

    `group_of` answers which group of the generation holds a listing, for the carried adverts
    E37 reads at property grain; by default the groups handed in are the whole generation.
    `cap=False` leaves the run cap to the caller (the reconcile is bounded by its time)."""
    listing_group: dict[int, int] = {m.listing_id: key for key, ms in members_by.items()
                                     for m in ms}

    counts: Counter[str] = Counter(clusters=len(cluster_rows))
    out_of_scope: Counter[str] = Counter()
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
        why_out = scope.group_outside(members)
        if why_out:
            counts["out_of_scope"] += 1
            out_of_scope[why_out] += 1
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
    listing_of: dict[int, Member] = {}
    for chunk in _chunks(all_props):
        for pid, status, ctype, cmain, first in _rows(
            conn, S.PROPERTIES_SQL, {"property_ids": chunk}
        ):
            props[int(pid)] = {"status": status, "category_type": ctype,
                               "category_main": cmain, "first_seen_at": first}
        for row in _rows(conn, S.PROPERTY_LISTINGS_SQL, {"property_ids": chunk}):
            listing, pid = Member.read(row), int(row[1])
            children.setdefault(pid, set()).add(listing.listing_id)
            prop_of[listing.listing_id] = pid
            listing_of[listing.listing_id] = listing
    for _c, ms in everyone:
        for m in ms:
            listing_of.setdefault(m.listing_id, m)

    every_listing: set[int] = {m.listing_id for _c, ms in everyone for m in ms}
    for pid in all_props:
        every_listing |= children.get(pid, set())
    if group_of is not None:
        listing_group = {**dict(group_of(every_listing)), **listing_group}
    negatives = Negatives.read(conn, every_listing,
                               [int(c["cluster_key"]) for c, _m in everyone])
    all_merges = _engine_merges(conn, every_listing)
    merges_by_listing: dict[int, list[EngineMerge]] = {}
    standing_by_listing: dict[int, list[EngineMerge]] = {}
    for merge in all_merges:
        for lid in merge.member_ids:
            if merge.live:
                merges_by_listing.setdefault(lid, []).append(merge)
            if not merge.undone_by_engine:
                standing_by_listing.setdefault(lid, []).append(merge)
    # A chokepoint refusal is remembered by the MEMBER SET it refused, not by the key it was
    # filed under: a real-time generation's keys move as its groups do (A9), and a batch
    # generation's key names exactly one member set, so the two readings agree there.
    refused_sets: set[frozenset[int]] = set()
    # Properties this engine retired whose merge `unapply` did not undo: active again, someone
    # else restored them.
    restored: set[int] = set()
    if candidates:
        for gen, _key, retired, outcome, undone, undone_by, members in _rows(
                conn, S.LEDGER_HISTORY_SQL, {
                    "generation": generation, "property_ids": sorted(all_props),
                    "listing_ids": sorted(every_listing)}):
            if outcome == "refused" and gen == generation:
                refused_sets.add(frozenset(int(x) for x in (members or ())))
            elif (outcome == "applied" and retired is not None
                  and not _undone_by_engine(undone, undone_by)):
                restored.add(int(retired))

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
        # set it would put on the survivor.
        extended = set(member_set)
        for pid in property_ids:
            extended |= children.get(pid, set())
        carried = extended - member_set

        hit_reasons, hit_detail = negatives.hits(extended, key)
        reasons += hit_reasons
        detail.update(hit_detail)

        set_reasons, set_detail = _set_reasons(
            extended, listing_of, [props[pid] for pid in property_ids if pid in props], scope)
        reasons += set_reasons
        detail.update(set_detail)

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

        # The engine's own ledger (never property_merge_events, D7). An engine merge someone
        # else took apart is the operator's word on its LISTINGS: a merge that would re-unite
        # them is refused whatever property they sit on now. A property this engine retired
        # that is active again was restored by someone: never re-merged on the engine's own
        # authority. Only the engine's own undo releases a property, to any later apply; a
        # merge the operator undid first stays restored-elsewhere even once `unapply` has
        # noted it (E905).
        if frozenset(member_set) in refused_sets:
            reasons.append(SKIP_REFUSED_BEFORE)
        separated = _separated_merges(
            extended, prop_of,
            {mg for lid in extended for mg in standing_by_listing.get(lid, ())})
        if separated:
            reasons.append(SKIP_RESTORED_ELSEWHERE)
            detail["separated_engine_merges"] = separated[:20]
        if any(pid in restored for pid in property_ids if pid not in inactive):
            reasons.append(SKIP_RESTORED_ELSEWHERE)
        reasons = list(dict.fromkeys(reasons))

        survivor: int | None = None
        retired: list[int] = []
        if not inactive and len(property_ids) >= 2:
            survivor = survivor_of({pid: props[pid]["first_seen_at"] for pid in property_ids})
            retired = [pid for pid in property_ids if pid != survivor]
        group = _group_plan(cluster, members, property_ids, survivor, retired, reasons, detail,
                            extended)
        group.listings_by_property = {pid: sorted(children.get(pid, ())) for pid in property_ids}
        groups.append(group)

    groups.sort(key=lambda g: g.cluster_key)
    eligible = [g for g in groups if not g.reasons]
    deferred = [g.cluster_key for g in eligible[scope.max_clusters_per_run:]] if cap else []
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
        counts={**dict(counts), "skipped_by_reason": dict(sorted(by_reason.items())),
                "out_of_scope_by_reason": dict(sorted(out_of_scope.items()))},
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


def recheck_group(
    conn: Any, group: GroupPlan, scope: Scope
) -> tuple[list[str], dict[str, Any]]:
    """The plan's word, re-read inside the group's own transaction just before it merges, over
    rows it LOCKS (the properties FOR UPDATE, their listings FOR SHARE) so nothing re-points or
    re-categorises them before the merge commits: the listings must be the ones planned, every
    property still active, the deal types, categories and scope still clean, and no operator
    negative may have landed on them since the plan read the negatives (E903). The locked
    placement is recorded on the group, for its ledger row's plan and for `unapply` (E905)."""
    locked = {
        int(pid): {"status": status, "category_type": ctype, "category_main": cmain}
        for pid, status, ctype, cmain in _rows(
            conn, S.LOCK_PROPERTIES_SQL, {"property_ids": group.property_ids})
    }
    rows = [Member.read(row) for row in _rows(
        conn, S.LOCK_PROPERTY_LISTINGS_SQL, {"property_ids": group.property_ids})]
    now = {m.listing_id for m in rows}
    planned = set(group.listing_ids)
    if now != planned:
        return [SKIP_CHANGED_SINCE_PLAN], {
            "arrived_since_plan": sorted(now - planned)[:20],
            "left_since_plan": sorted(planned - now)[:20]}
    placed: dict[int, list[int]] = {pid: [] for pid in group.property_ids}
    prop_of: dict[int, int] = {}
    for m in rows:
        placed.setdefault(int(m.property_id), []).append(m.listing_id)
        prop_of[m.listing_id] = int(m.property_id)
    group.listings_by_property = {pid: sorted(lids) for pid, lids in placed.items()}
    reasons: list[str] = []
    detail: dict[str, Any] = {}
    inactive = [pid for pid in group.property_ids
                if pid not in locked or locked[pid]["status"] != "active"]
    if inactive:
        reasons.append(SKIP_INACTIVE_PROPERTY)
        detail["inactive_property_ids"] = inactive
    hit_reasons, hit_detail = Negatives.read(conn, now, [group.cluster_key]).hits(
        now, group.cluster_key)
    reasons += hit_reasons
    detail.update(hit_detail)
    set_reasons, set_detail = _set_reasons(
        now, {m.listing_id: m for m in rows}, list(locked.values()), scope)
    reasons += set_reasons
    detail.update(set_detail)
    separated = _separated_merges(now, prop_of, _engine_merges(conn, now))
    if separated:
        reasons.append(SKIP_RESTORED_ELSEWHERE)
        detail["separated_engine_merges"] = separated[:20]
    return list(dict.fromkeys(reasons)), detail


def apply_plan(
    conn: Any,
    plan: Plan,
    dry_run: bool,
    *,
    merge: Callable[..., dict[str, Any]] = merge_property_set,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Record the plan (dry run) or execute it: one transaction and one merge group per engine
    group, so a refusal anywhere in a group rolls the whole group back."""
    run_id = run_id or new_run_id()
    if not dry_run:
        if plan.generation.strip() == GENERATION:
            raise ApplyRefused(
                f"generation {plan.generation!r} belongs to the real-time lane, which reconciles "
                "it itself under its own lease (A9) — a dry run may plan it, nothing here "
                "applies it")
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
    at = 0
    try:
        for index, group in enumerate(todo):
            at = index
            # E39: the scope row read fresh before every group, so emptying its area stops a
            # run mid-way.
            closed = scope_closed(conn)
            if closed:
                result["stopped"] = f"app_settings.{SCOPE_SETTING} admits no live merge: {closed}"
                counts["not_attempted"] = len(todo) - index
                break
            try:
                outcome, brief = apply_group(conn, group, plan.scope, run_id=run_id,
                                             generation=gen, merge=merge)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                counts["failed"] += 1
                counts["not_attempted"] = len(todo) - index - 1
                result["failed"].append(_brief(group, error=error))
                result["aborted"] = f"stopped at group {group.cluster_key}: {error}"
                raise
            counts[outcome] += 1
            result[outcome].append(brief)
            if outcome == "applied":
                counts["listings_moved"] += int(brief["listings_moved"])
    except BaseException as exc:
        # Any stop — an error, or a cancelled job (SIGINT raises KeyboardInterrupt) — after
        # groups merged for real: the result rides on the error, so the lane still publishes
        # what DID merge before re-raising.
        if "aborted" not in result:
            result["aborted"] = (f"stopped at group {todo[at].cluster_key}: "
                                 f"{type(exc).__name__}: {exc}")
            counts["not_attempted"] = len(todo) - at
        _attach_partial(exc, result)
        raise
    return result


def apply_group(
    conn: Any, group: GroupPlan, scope: Scope, *, run_id: str, generation: str,
    merge: Callable[..., dict[str, Any]] = merge_property_set,
) -> tuple[str, dict[str, Any]]:
    """ONE planned group through THE chokepoint, in its own transaction, with its ledger rows:
    `applied`, `skipped_at_apply` (the in-transaction re-check or two asset links refused it),
    `refused` / `failed` (the chokepoint named why). The batch run and the real-time lane's
    reconcile (A9) both merge through here. An error the chokepoint does not name is recorded
    and RAISED: nothing carries on merging over a database nobody has looked at."""
    group_id = str(uuid.uuid4())
    markers = {
        "engine": "autodedup", "generation": generation, "cluster_key": group.cluster_key,
        "run_id": run_id, "model_version": group.model_version,
        "feature_version": group.feature_version, "members": group.member_ids,
        "scope": scope.to_json(),
    }
    try:
        with conn.transaction():
            late_reasons, late_detail = recheck_group(conn, group, scope)
            if late_reasons:
                raise _SkipAtApply(late_reasons, late_detail)
            try:
                res = merge(
                    conn, group.property_ids, source=MERGE_SOURCE,
                    reason=f"autodedup {generation} {group.cluster_key}",
                    merge_group_id=group_id, confidence=group.confidence,
                    markers=markers,
                )
            except AssetLinkConflict as exc:
                raise _SkipAtApply([SKIP_ASSET_LINKED], {"asset_links": str(exc)}) from exc
            data = res.get("data") or {}
            group.survivor_id = int(data["survivor_id"])
            group.retired_ids = [int(r) for r in data["retired_ids"]]
            moved = [len(group.listings_by_property.get(r, ())) for r in group.retired_ids]
            _exec_many(conn, S.LEDGER_INSERT_SQL, _rows_for(
                run_id, generation, group, dry_run=False, outcome="applied",
                merge_group_id=group_id, moved=moved))
    except _SkipAtApply as skip:
        group.reasons = skip.reasons
        group.detail = {**group.detail, **skip.detail, "at_apply": True}
        with conn.transaction():
            _exec_many(conn, S.LEDGER_INSERT_SQL, _rows_for(
                run_id, generation, group, dry_run=False, outcome="skipped",
                error=skip.reasons[0]))
        return "skipped_at_apply", _brief(group, reasons=skip.reasons)
    except MergeError as exc:
        outcome = "refused" if _terminal(exc) else "failed"
        with conn.transaction():
            _exec_many(conn, S.LEDGER_INSERT_SQL, _rows_for(
                run_id, generation, group, dry_run=False, outcome=outcome, error=str(exc)))
        return outcome, _brief(group, error=str(exc))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            with conn.transaction():
                _exec_many(conn, S.LEDGER_INSERT_SQL, _rows_for(
                    run_id, generation, group, dry_run=False, outcome="failed", error=error))
        except Exception:  # noqa: BLE001 — the original error is the one to surface
            pass
        raise
    return "applied", _brief(group, merge_group_id=group_id, listings_moved=sum(moved))


def _attach_partial(exc: BaseException, result: dict[str, Any]) -> None:
    try:
        setattr(exc, PARTIAL_RESULT_ATTR, result)
    except Exception:  # noqa: BLE001 — an error that takes no attribute still surfaces
        pass


def _nothing_back(conflicts: Sequence[Any]) -> dict[str, Any]:
    return {"blocked": (f"nothing would move back ({len(conflicts)} listings moved on since the "
                        "merge); left as it stands"),
            "conflicts": list(conflicts), "unapply_first": []}


def _recorded_moves(plan_json: Any, retired_ids: Sequence[int]) -> list[int] | None:
    """The listings a merge moved off its retired properties, from the placement its ledger row
    recorded as it merged; None for a row that recorded none."""
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    placed = plan_json.get("listings_by_property") if isinstance(plan_json, dict) else None
    if not placed:
        return None
    retired = set(retired_ids)
    return sorted({int(lid) for pid, lids in placed.items() if int(pid) in retired
                   for lid in lids})


def _merge_stands(
    state: tuple[Any, Any, Any] | None, into: int | None, applied_at: datetime | str | None
) -> bool:
    """Whether a property is still merged into `into` by the merge a ledger row recorded at
    `applied_at`: the chokepoint's `merged_at` is now() of that same transaction, so the same
    pair merged again at another time (by hand, after an undo) is a different merge (E905)."""
    if state is None or into is None or state[1] != into:
        return False
    merged_at = state[2]
    if isinstance(applied_at, str):
        applied_at = datetime.fromisoformat(applied_at)
    if merged_at is None or applied_at is None:
        return True
    return abs(merged_at - applied_at) <= SAME_MERGE_TOLERANCE


def _undo_state(conn: Any, target: Mapping[str, Any]) -> dict[str, Any]:
    """Where a group stands now, read before any later merge is named (E905): its survivor's
    status and what it is merged into; its members off the survivor; and, per listing the merge
    moved off its retired properties, what its detach loop would answer (`detach_outcomes`, the
    loop's own rule): the ones it would move back, the ones it would report as conflicts, and
    `undone` when it would find nothing live at all (someone undid every advert of it)."""
    survivor = target["survivor_id"]
    props = {int(pid): (status, merged_into, merged_at)
             for pid, status, merged_into, merged_at in _rows(
                 conn, S.PROPERTY_STATE_SQL,
                 {"property_ids": [survivor] if survivor is not None else []})}
    moved = target.get("moved_listings") or []
    members = target.get("member_ids") or []
    placed = {int(lid): pid for lid, pid in _rows(conn, S.MEMBER_PROPERTIES_SQL, {
        "listing_ids": sorted(set(members))})}
    outcomes = detach_outcomes(conn, moved, merge_group_id=target["merge_group_id"])
    back = [lid for lid in moved if outcomes.get(lid) == "detached"]
    conflicts = [lid for lid in moved
                 if outcomes.get(lid, "not_merged") not in ("detached", "not_merged")]
    return {
        "survivor": props.get(survivor),
        "status": (props.get(survivor) or (None,))[0],
        "undone": not back and not conflicts,
        "taken": [lid for lid in members if placed.get(lid) != survivor],
        "back": back,
        "conflicts": conflicts,
    }


def _moves_nothing_back(state: Mapping[str, Any]) -> bool:
    """A merge that still stands, none of whose moved listings would move back: its detach loop
    would move nothing, so the live undo is refused."""
    return bool(state["conflicts"]) and not state["back"]


def _later_merges(
    conn: Any, target: Mapping[str, Any], undone_in_run: set[str], state: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The LATER live engine merges whose undo would free a group: those that merged more
    listings onto its survivor and whose own undo is not refused for moving nothing back
    (some of what they moved is still on the survivor, or someone already undid them and
    `unapply` notes it), and the one whose retirement of the survivor still stands (the
    survivor merged into that merge's survivor at that merge's `applied_at`)."""
    survivor = target["survivor_id"]
    later: dict[str, dict[str, Any]] = {}
    for gen, key, group, surv, retired, applied_at, plan_json in _rows(
            conn, S.LATER_LIVE_MERGES_SQL,
            {"after_id": target["last_id"], "property_id": survivor}):
        if str(group) in undone_in_run:
            continue
        merge = later.setdefault(str(group), {
            "group": str(group), "generation": gen, "cluster_key": int(key), "survivor_id": surv,
            "retired_ids": [], "applied_at": applied_at, "plan_json": plan_json})
        if retired is not None:
            merge["retired_ids"].append(int(retired))
    onto: list[dict[str, Any]] = []
    retiring: list[dict[str, Any]] = []
    for merge in later.values():
        named = {"generation": merge["generation"], "cluster_key": merge["cluster_key"],
                 "unapply": _unapply_args(merge["generation"], merge["cluster_key"])}
        if merge["survivor_id"] == survivor:
            own = _undo_state(conn, {
                "merge_group_id": merge["group"], "survivor_id": survivor,
                "moved_listings": _recorded_moves(merge["plan_json"], merge["retired_ids"])})
            if not _moves_nothing_back(own):
                onto.append(named)
        elif survivor in merge["retired_ids"] and _merge_stands(
                state["survivor"], merge["survivor_id"], merge["applied_at"]):
            retiring.append(named)
    return onto, retiring


def _undo_block(
    conn: Any, target: Mapping[str, Any], undone_in_run: set[str], state: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Why a group cannot be undone yet (E905), decided from where it stands (`_undo_state`)
    BEFORE any later engine merge is looked for, so `unapply_first` only ever names an undo
    that would free it. A group someone already undid is never blocked, wherever its survivor
    has gone since: its detach loop finds nothing live and `unapply` notes the undo as theirs.
    A survivor still active: a group whose moved listings have all left it would move nothing
    back whatever came later; otherwise a LATER live engine merge that put more listings on it
    (and whose own undo would not be refused for moving nothing back) is undone first, or
    undoing this one would leave the survivor holding a set no generation grouped. A survivor
    merged away: by a later engine merge that still stands (undo that and it is back), or else
    by a merge this engine did not make — its listings are off it, the group was taken apart
    outside the engine, and there is nothing to name.
    `undone_in_run`: later groups a dry run expects this same run to undo first."""
    survivor = target.get("survivor_id")
    if survivor is None or state["undone"]:
        return None
    status = state["status"]
    if status == "active" and _moves_nothing_back(state):
        return _nothing_back(state["conflicts"])
    onto, retiring = _later_merges(conn, target, undone_in_run, state)
    if status == "active":
        if not onto:
            return None
        return {"blocked": f"a later engine merge put more listings on survivor {survivor}: "
                           "undo the later engine merge first",
                "unapply_first": onto}
    if retiring:
        return {"blocked": f"a later engine merge retired survivor {survivor}: undo the later "
                           "engine merge first",
                "unapply_first": retiring}
    return {"blocked": f"survivor {survivor} is {status or 'missing'}: a merge this engine did "
                       "not make moved it on and the group's listings are off it - taken apart "
                       "outside the engine; left as it stands",
            "taken_apart": list(state["taken"]), "unapply_first": []}


def unapply(
    conn: Any,
    generation: str | None,
    *,
    dry_run: bool,
    cluster_key: int | None = None,
    run: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    detach: Callable[..., dict[str, Any]] = detach_listing,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Undo the live merge groups a generation (or one of its groups), an apply run (`run`) or
    a time window (`since` <= applied_at < `until`) made — every selector given must hold —
    newest-first, each as a loop of `detach_listing` over the adverts its merge moved (no
    ruling: an engine undo is not the operator's word); `dry_run=True` only lists them. `dry_run` has
    no default: this writes to production. NOT gated by the scope: undo is the way back, and
    an undone group is free to merge again on a later apply. A group someone else already
    undid is noted undone, as THEIR undo, wherever its survivor has gone since. A
    group a later engine merge still builds on (more listings merged onto its survivor, by a
    merge whose own undo would not be refused), or whose survivor a later engine merge that
    still stands retired, is skipped with the reason and the merge to undo first; one taken
    apart outside the engine, or with nothing left to move back, is skipped naming nothing —
    and the dry run says so from the same reads. A merge someone else had already partly taken
    apart is undone but recorded as THEIR undo (`undone_by='external'`), so the listings they
    separated stay apart in every later generation (E905)."""
    if generation is None and cluster_key is not None:
        raise ValueError("cluster_key selects a group of one generation: name the generation")
    if generation is None and run is None and since is None and until is None:
        raise ValueError("unapply needs a generation, a run or a time window")
    run_id = run_id or new_run_id()
    targets = []
    for group, gen, key, surv, retired, last, members, plan_json, applied_at in _rows(
            conn, S.UNAPPLY_TARGETS_SQL, {"generation": generation, "cluster_key": cluster_key,
                                          "run": run, "since": since, "until": until}):
        retired_ids = [int(r) for r in (retired or []) if r is not None]
        targets.append({
            "merge_group_id": str(group), "generation": str(gen), "cluster_key": int(key),
            "survivor_id": int(surv) if surv is not None else None,
            "retired_ids": retired_ids,
            "member_ids": sorted(int(x) for x in (members or ())), "last_id": int(last),
            "moved_listings": _recorded_moves(plan_json, retired_ids),
            "applied_at": (applied_at.isoformat() if isinstance(applied_at, datetime)
                           else applied_at)})
    result: dict[str, Any] = {
        "run_id": run_id, "generation": generation, "dry_run": dry_run,
        "cluster_key": cluster_key, "run": run,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None, "groups": targets,
        "counts": {"groups": len(targets), "undone": 0, "already_undone": 0, "blocked": 0,
                   "taken_apart_before": 0, "listings_moved_back": 0, "conflicts": 0},
    }
    counts = result["counts"]
    if dry_run:
        # What the live run below would do with each group, from the same reads.
        expected: set[str] = set()
        for target in targets:
            state = _undo_state(conn, target)
            block = _undo_block(conn, target, expected, state)
            if block:
                target.update(block)
                counts["blocked"] += 1
                continue
            expected.add(target["merge_group_id"])
            if state["undone"]:
                target["note"] = ("already undone outside the engine: would be recorded as "
                                  "someone else's undo")
                counts["already_undone"] += 1
                continue
            counts["listings_moved_back"] += len(state["back"])
            counts["conflicts"] += len(state["conflicts"])
            if state["taken"] or state["conflicts"]:
                target.update(taken_apart=state["taken"], conflicts=state["conflicts"])
                counts["taken_apart_before"] += 1
        return result
    try:
        _unapply_live(conn, targets, result, detach, run_id)
    except BaseException as exc:
        # A crash or a cancelled job after groups were undone for real: the result rides on
        # the error, so the lane still publishes what WAS undone before re-raising.
        stuck = [t["cluster_key"] for t in targets if t.get("outcome") == "aborted"]
        where = f"stopped at group {stuck[0]}" if stuck else "stopped"
        result.setdefault("aborted", f"{where}: {type(exc).__name__}: {exc}")
        _attach_partial(exc, result)
        raise
    return result


def _taken_apart(conn: Any, target: Mapping[str, Any]) -> list[int]:
    """The group's members no longer on its survivor: someone else took the merge apart."""
    placed = {int(lid): pid for lid, pid in _rows(
        conn, S.MEMBER_PROPERTIES_SQL, {"listing_ids": target["member_ids"]})}
    return [lid for lid in target["member_ids"] if placed.get(lid) != target["survivor_id"]]


def _detach_group(
    conn: Any, target: Mapping[str, Any], detach: Callable[..., dict[str, Any]], undone_by: str,
) -> dict[str, Any]:
    """A group undone as a loop of detaches over the adverts its merge moved (its recorded
    placement); one that moved on since is a conflict, left where it is."""
    back, conflicts = 0, []
    for lid in target.get("moved_listings") or []:
        out = detach(conn, lid, decided_by=undone_by, source=MERGE_SOURCE,
                     merge_group_id=target["merge_group_id"])["data"]
        back += int(out["detached"])
        if not out["detached"] and out["outcome"] != "not_merged":
            conflicts.append(lid)
    return {"merge_group_id": target["merge_group_id"], "listings_moved_back": back,
            "conflicts": conflicts}


def _unapply_live(
    conn: Any, targets: list[dict[str, Any]], result: dict[str, Any],
    detach: Callable[..., dict[str, Any]], run_id: str,
) -> None:
    counts = result["counts"]
    undone_by = f"{UNAPPLY_BY_PREFIX}{run_id}"
    for target in targets:
        target["outcome"] = "aborted"
        # Read per group, just before it is undone: an earlier undo in this run may have
        # released what this one needs.
        block = _undo_block(conn, target, set(), _undo_state(conn, target))
        if block:
            target.update(block, outcome="blocked")
            counts["blocked"] += 1
            continue
        with conn.transaction():
            taken = _taken_apart(conn, target)
            data = _detach_group(conn, target, detach, undone_by)
            # Someone else had already taken the merge apart (an operator split), or wholly
            # undone it: the separation is THEIR word on those listings, recorded as theirs so a
            # later generation never re-unites them (E905).
            external = bool(taken or data["conflicts"]) or not data["listings_moved_back"]
            record = {**data, "noted_by": undone_by, "taken_apart": taken} if external \
                else data
            if data["listings_moved_back"] or not data["conflicts"]:
                _exec(conn, S.LEDGER_UNDO_SQL, {
                    "merge_group_id": target["merge_group_id"],
                    "undone_by": EXTERNAL_UNDO if external else undone_by,
                    "undo_result": json.dumps(record, sort_keys=True, default=str)})
        if not data["listings_moved_back"]:
            if data["conflicts"]:
                target.update(_nothing_back(data["conflicts"]), outcome="blocked")
                counts["blocked"] += 1
            else:
                counts["already_undone"] += 1
                target.update(error="nothing live left to undo", outcome="already_undone")
            continue
        counts["undone"] += 1
        counts["listings_moved_back"] += data["listings_moved_back"]
        counts["conflicts"] += len(data["conflicts"])
        target.update(conflicts=list(data["conflicts"]), outcome="undone")
        if external:
            counts["taken_apart_before"] += 1
            target.update(taken_apart=taken, outcome="undone_after_outside_split")


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


def _retire_arg(args: Mapping[str, str]) -> bool:
    raw = (args.get("retire_legacy") or "").strip()
    try:
        return _truthy(raw) if raw else False
    except ValueError as exc:
        raise SystemExit(f"retire_legacy: {exc}") from exc


def _generation_arg(args: Mapping[str, str]) -> str:
    generation = (args.get("generation") or "").strip()
    if not generation:
        raise SystemExit("generation= is required (e.g. generation=g12)")
    return generation


def _time_arg(args: Mapping[str, str], key: str) -> datetime | None:
    raw = (args.get(key) or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"{key} must be an ISO time such as 2026-09-25T08:00Z, got {raw!r}") \
            from exc
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


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
    picked = result.get("generation") or " ".join(
        f"{key}={result[key]}" for key in ("run", "since", "until") if result.get(key))
    lines = [f"## autodedup {mode} {picked} — {head}", ""]
    if result.get("stopped"):
        lines += [f"**Stopped:** {result['stopped']}", ""]
    if result.get("aborted"):
        done = ("the groups marked `undone` below WERE undone" if mode == "unapply"
                else "the groups listed under `applied` below DID merge")
        lines += [f"**Aborted:** {result['aborted']} - {done}.", ""]
    lines += ["| count | n |", "| --- | --- |"]
    lines += [f"| {key} | {value} |" for key, value in counts.items()
              if not isinstance(value, dict)]
    for key, title in (("out_of_scope_by_reason", "out of scope because"),
                       ("skipped_by_reason", "skipped because")):
        by_reason = counts.get(key) or {}
        if by_reason:
            lines += ["", f"| {title} | groups |", "| --- | --- |"]
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
                or ", ".join(row.get("reasons") or []) or row.get("outcome") \
                or row.get("merge_group_id") or ""
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


def lane_interval(conn: Any) -> int:
    """`app_settings.realtime_autodedup_interval_seconds` as the worker reads it: an absent or
    unreadable row is 0 (the lane idle), anything else its integer."""
    value = read_setting(conn, LANE_INTERVAL_SETTING)
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


class _Writer:
    """THE lane's lease (`autodedup.rt_lease`), held by a live dispatch: one writer of
    production merges at a time (A9). The worker's lane reconciles under it, so a live apply or
    unapply takes it for its run and refuses while a pass holds it — and refuses outright while
    the lane is RUNNING (its interval above 0): an unapply would be re-merged by the lane's next
    sweep minutes later, and an apply would race it. The brake is interval 0, THEN unapply. A
    dry run of the live stream takes the lease too, so the plan it predicts is not moved under
    it by a pass (review B8); it needs no stopped lane."""

    def __init__(self, conn: Any, live: bool, *, lease: bool | None = None,
                 release: str | None = None) -> None:
        self.conn = conn
        self.live = live
        self.lease = live if lease is None else lease
        self.release = (release or "").strip() or None
        self.holder = f"dispatch:{new_run_id()}"
        self.held = False
        self.released: dict[str, Any] | None = None

    def __enter__(self) -> "_Writer":
        if self.live:
            interval = lane_interval(self.conn)
            if interval > 0:
                raise SystemExit(
                    f"refused: app_settings.{LANE_INTERVAL_SETTING} is {interval} — the worker's "
                    "autodedup lane is running and would re-merge (or race) what this run "
                    f"changes. Set {LANE_INTERVAL_SETTING} to 0 on /settings first, then run "
                    "again. Nothing was written.")
        if not self.lease:
            return self
        if self.release:
            self.released = rt_lease.release_stale(self.conn, self.release)
        if not rt_lease.take(self.conn, self.holder, rt_lease.DISPATCH_TTL_S):
            raise SystemExit(
                f"refused: autodedup.rt_lease is {rt_lease.describe(self.conn)}. Set "
                f"app_settings.{LANE_INTERVAL_SETTING} to 0, wait for the lease to clear, and "
                "run again. Nothing was written.")
        self.held = True
        return self

    def __exit__(self, _type: Any, exc: BaseException | None, _tb: Any) -> None:
        if self.held:
            rt_lease.release_after(self.conn, self.holder, exc)


def run_apply(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Path
) -> dict[str, Any]:
    _check_args(args, APPLY_ARGS)
    generation = _generation_arg(args)
    dry_run = _dry_run_arg(args)
    if generation.strip() == GENERATION and (not dry_run or _retire_arg(args)):
        raise SystemExit(
            f"generation={generation}: the real-time lane's generation is reconciled by the "
            "lane itself, under its own lease (A9) — a dry run may plan it (the G3 "
            "prediction), nothing here applies it"
        )
    retire = _retire_arg(args)
    override = {key: args[key] for key in SCOPE_KEYS if key in args}
    out_dir = Path(out_dir)
    conn = conn_factory()
    try:
        with _Writer(conn, live=not dry_run, lease=not dry_run or generation == GENERATION,
                     release=args.get(rt_lease.RELEASE_ARG)):
            return _apply_run(conn, generation, dry_run, retire, override, out_dir)
    finally:
        _close(conn)


def _apply_run(conn: Any, generation: str, dry_run: bool, retire: bool,
               override: Mapping[str, Any], out_dir: Path) -> dict[str, Any]:
    retired: dict[str, Any] | None = None
    try:
        scope = effective_scope(read_scope_setting(conn), override, live=not dry_run)
    except ValueError as exc:
        raise SystemExit(f"scope: {exc}") from exc
    plan = plan_apply(conn, generation, scope)
    if retire:
        retired = _retire_legacy(conn, generation, scope, plan, dry_run=dry_run,
                                 out_dir=out_dir)
        if not dry_run:
            plan = plan_apply(conn, generation, scope)
        retired = legacy_retire.note_deferred(
            out_dir, retired, deferred=plan.deferred,
            planned=[g.cluster_key for g in plan.to_apply],
            cap=plan.scope.max_clusters_per_run)
    try:
        result = apply_plan(conn, plan, dry_run)
    except ApplyRefused as exc:
        raise SystemExit(str(exc)) from exc
    except BaseException as exc:
        # A live run that stops mid-way (a crash, or a cancelled job's KeyboardInterrupt)
        # has merged for real: publish what it did, then fail.
        partial = getattr(exc, PARTIAL_RESULT_ATTR, None)
        if isinstance(partial, dict):
            _publish_apply(out_dir, _with_retire(partial, retired), plan)
        raise
    return _publish_apply(out_dir, _with_retire(result, retired), plan)


def _retire_legacy(
    conn: Any, generation: str, scope: Scope, plan: Plan, *, dry_run: bool, out_dir: Path,
) -> dict[str, Any]:
    """A2 (temporary, kept until W8): the old engine's merges in the scope undone first, in this
    same dispatch, so the plan that follows reads them apart. Refused before anything moves when
    the run narrows by listing (the step reads blocks only) or when `plan`, read first, holds no
    proposed group inside the scope: a typo'd or unstored generation would re-merge nothing."""
    if scope.listing_ids is not None:
        raise SystemExit("retire_legacy=1 cannot run with listing_ids: the retire step reads the "
                         "scope's blocks only; nothing was undone")
    counts = plan.counts
    proposed = counts.get("clusters", 0) - sum(
        counts.get(key, 0) for key in ("not_proposed", "no_members", "out_of_scope"))
    if proposed <= 0:
        raise SystemExit(f"retire_legacy=1: generation {plan.generation!r} holds no proposed "
                         "group inside the scope, so nothing would merge again; nothing was undone")
    # Only a group this run may merge (proposed, inside the scope by the plan's own rule)
    # re-merges what the step undoes; the rest touching it are reported apart, with why.
    members = _members(conn, generation)
    merging: dict[int, int] = {}
    other: dict[int, int] = {}
    why: dict[int, str] = {}
    for cluster in _cluster_rows(conn, generation):
        key = int(cluster["cluster_key"])
        found = members.get(key, [])
        why_not = (f"status {cluster['status']}" if cluster["status"] != "proposed"
                   else scope.group_outside(found))
        into = other if why_not else merging
        if why_not:
            why[key] = why_not
        for member in found:
            into.setdefault(member.listing_id, key)
    return legacy_retire.run(conn, scope.blocks, category_types=scope.category_types,
                             dry_run=dry_run, run_id=new_run_id(), out_dir=out_dir,
                             closed=scope_closed,
                             engine=legacy_retire.EngineMaps(merging, other, why))


def _with_retire(result: dict[str, Any], retired: dict[str, Any] | None) -> dict[str, Any]:
    return {**result, "legacy_retire": retired} if retired is not None else result


def _publish_apply(out_dir: Path, result: dict[str, Any], plan: Plan) -> dict[str, Any]:
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
    dry_run = _dry_run_arg(args)
    for key in UNAPPLY_SELECTORS:
        if key in args and not (args[key] or "").strip():
            # Absent narrows nothing; an empty value must never widen a selection.
            raise SystemExit(f"{key}= is empty; omit it instead")
    generation = (args.get("generation") or "").strip() or None
    run = (args.get("run") or "").strip() or None
    raw_key = (args.get("cluster_key") or "").strip()
    try:
        cluster_key = int(raw_key) if raw_key else None
    except ValueError as exc:
        raise SystemExit(f"cluster_key must be an integer, got {raw_key!r}") from exc
    since, until = _time_arg(args, "since"), _time_arg(args, "until")
    if cluster_key is not None and generation is None:
        raise SystemExit("cluster_key= needs generation= (a key names a group of one generation)")
    if generation is None and run is None and since is None and until is None:
        raise SystemExit("unapply needs generation=, run=, since= or until= (e.g. generation=g12)")
    conn = conn_factory()
    try:
        with _Writer(conn, live=not dry_run, release=args.get(rt_lease.RELEASE_ARG)):
            result = unapply(conn, generation, dry_run=dry_run, cluster_key=cluster_key,
                             run=run, since=since, until=until)
    except BaseException as exc:
        # A live undo that stops mid-way has undone groups for real: publish them, then fail.
        partial = getattr(exc, PARTIAL_RESULT_ATTR, None)
        if isinstance(partial, dict):
            _publish_unapply(out_dir, partial)
        raise
    finally:
        _close(conn)
    return _publish_unapply(out_dir, result)


def _publish_unapply(out_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    write_json(Path(out_dir) / "unapply.json", result)
    summary = _capped(result, ("groups",))
    _step_summary(summary, mode="unapply")
    return summary
