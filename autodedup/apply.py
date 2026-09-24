"""W29 APPLY — one generation's groups into production merges, through THE chokepoint.

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
SKIP_SPANS_GROUPS = "property_spans_groups"
SKIP_REFUSED_BEFORE = "refused_at_chokepoint_before"
SKIP_RESTORED_ELSEWHERE = "restored_outside_engine"
SKIP_GENERATION_UNAPPLIED = "generation_unapplied"


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

    @property
    def skipped(self) -> bool:
        return bool(self.reasons)

    def to_json(self) -> dict[str, Any]:
        return {
            "cluster_key": self.cluster_key,
            "size": self.size,
            "member_ids": self.member_ids,
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


def _verdict_applies(row: Mapping[str, Any], generation: str, member_ids: list[int]) -> bool:
    """The review page's own rule (E58): a group ruling is about a SET of listings."""
    recorded = row.get("member_ids")
    if recorded is not None:
        return sorted(int(x) for x in recorded) == member_ids
    return row.get("generation") is None or row.get("generation") == generation


def plan_apply(conn: Any, generation: str, scope: Scope) -> Plan:
    """Read-only: which groups of `generation` would merge, into what, and which are refused."""
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
            counts["already_one_property"] += 1
            continue
        candidates.append((cluster, members))

    all_props = {m.property_id for _c, ms in candidates for m in ms if m.property_id is not None}
    props: dict[int, dict[str, Any]] = {}
    children: dict[int, set[int]] = {}
    for chunk in _chunks(all_props):
        for pid, status, ctype, cmain, first in _rows(
            conn, S.PROPERTIES_SQL, {"property_ids": chunk}
        ):
            props[int(pid)] = {"status": status, "category_type": ctype,
                               "category_main": cmain, "first_seen_at": first}
        for pid, lid in _rows(conn, S.PROPERTY_LISTINGS_SQL, {"property_ids": chunk}):
            children.setdefault(int(pid), set()).add(int(lid))

    extended_all: set[int] = {m.listing_id for _c, ms in candidates for m in ms}
    for pid in all_props:
        extended_all |= children.get(pid, set())
    listing_arr = sorted(extended_all)
    negatives = list(NEGATIVE_VERDICTS)
    # Indexed by the pair's low side, so a group's check is O(its own listings).
    mnl_by_lo: dict[int, list[tuple[int, str]]] = {}
    verdict_by_lo: dict[int, list[int]] = {}
    group_verdicts: dict[int, list[dict[str, Any]]] = {}
    refused_keys: set[int] = set()
    applied_by_retired: dict[int, list[dict[str, Any]]] = {}
    if candidates:
        for lo, hi, src in _rows(conn, S.MUST_NOT_LINK_SQL, {"listing_ids": listing_arr}):
            mnl_by_lo.setdefault(int(lo), []).append((int(hi), str(src)))
        for lo, hi, _verdict in _rows(conn, S.PAIR_VERDICTS_SQL, {
                "listing_ids": listing_arr, "negatives": negatives}):
            verdict_by_lo.setdefault(int(lo), []).append(int(hi))
        for key, verdict, gen, member_ids in _rows(conn, S.CLUSTER_VERDICTS_SQL, {
            "cluster_keys": sorted(int(c["cluster_key"]) for c, _m in candidates),
            "negatives": negatives,
        }):
            group_verdicts.setdefault(int(key), []).append(
                {"verdict": verdict, "generation": gen, "member_ids": member_ids})
        for gen, key, _surv, retired, outcome, undone in _rows(conn, S.LEDGER_HISTORY_SQL, {
                "generation": generation, "property_ids": sorted(all_props)}):
            if outcome == "refused" and gen == generation:
                refused_keys.add(int(key))
            elif outcome == "applied" and retired is not None:
                applied_by_retired.setdefault(int(retired), []).append(
                    {"generation": gen, "undone": bool(undone)})

    groups: list[GroupPlan] = []
    for cluster, members in candidates:
        key = int(cluster["cluster_key"])
        member_ids = sorted(m.listing_id for m in members)
        member_set = set(member_ids)
        property_ids = sorted({m.property_id for m in members if m.property_id is not None})
        reasons: list[str] = []
        detail: dict[str, Any] = {}

        if any(m.property_id is None for m in members):
            reasons.append(SKIP_UNATTACHED)
        inactive = [pid for pid in property_ids
                    if pid not in props or props[pid]["status"] != "active"]
        if inactive:
            reasons.append(SKIP_INACTIVE_PROPERTY)
            detail["inactive_property_ids"] = inactive
        if max(int(cluster["size"] or 0), len(members)) > scope.max_cluster_size:
            reasons.append(SKIP_OVERSIZE)

        extended = set(member_set)
        for pid in property_ids:
            extended |= children.get(pid, set())
        if any(_verdict_applies(v, generation, member_ids)
               for v in group_verdicts.get(key, [])):
            reasons.append(SKIP_CLUSTER_VERDICT)
        pair_hits = [[lo, hi] for lo in sorted(extended)
                     for hi in verdict_by_lo.get(lo, ()) if hi in extended]
        if pair_hits:
            reasons.append(SKIP_PAIR_VERDICT)
            detail["negative_pairs"] = pair_hits[:20]
        mnl_hits = [[lo, hi, src] for lo in sorted(extended)
                    for hi, src in mnl_by_lo.get(lo, ()) if hi in extended]
        if mnl_hits:
            reasons.append(SKIP_MUST_NOT_LINK)
            detail["must_not_link"] = mnl_hits[:20]

        live_props = [props[pid] for pid in property_ids if pid in props]
        types = {m.category_type for m in members if m.category_type is not None}
        types |= {p["category_type"] for p in live_props if p["category_type"] is not None}
        if len(types) > 1:
            reasons.append(SKIP_CATEGORY_TYPE)
            detail["category_types"] = sorted(types)
        mains = {m.category_main for m in members} | {p["category_main"] for p in live_props}
        if not all(category_main_compatible(a, b) for a, b in combinations(
                sorted(mains, key=lambda x: (x is None, x or "")), 2)):
            reasons.append(SKIP_CATEGORY_MAIN)
            detail["category_mains"] = sorted(m for m in mains if m is not None)

        # E37 at property grain: a property whose OTHER children the engine grouped elsewhere
        # would fuse two of its groups through a link the engine never made.
        foreign = sorted(lid for lid in extended - member_set
                         if listing_group.get(lid) not in (None, key))
        if foreign:
            reasons.append(SKIP_SPANS_GROUPS)
            detail["listings_in_other_groups"] = foreign[:20]

        # The engine's own ledger (never property_merge_events, D7). A property this engine
        # retired that is active again was restored by someone: the engine never re-merges
        # it on its own. After `unapply`, the SAME generation never re-applies; a later one may.
        if key in refused_keys:
            reasons.append(SKIP_REFUSED_BEFORE)
        for pid in property_ids:
            if pid in inactive:
                continue
            for row in applied_by_retired.get(pid, ()):
                if not row["undone"]:
                    reasons.append(SKIP_RESTORED_ELSEWHERE)
                elif row["generation"] == generation:
                    reasons.append(SKIP_GENERATION_UNAPPLIED)
        reasons = list(dict.fromkeys(reasons))

        survivor: int | None = None
        retired: list[int] = []
        if not inactive and len(property_ids) >= 2:
            survivor = _survivor(property_ids, props, children)
            retired = [pid for pid in property_ids if pid != survivor]
        score = cluster.get("min_edge_score")
        groups.append(GroupPlan(
            cluster_key=key,
            size=int(cluster["size"] or len(members)),
            member_ids=member_ids,
            property_ids=property_ids,
            survivor_id=survivor,
            retired_ids=retired,
            confidence=float(score) if score is not None else None,
            model_version=cluster.get("model_version"),
            feature_version=(int(cluster["feature_version"])
                             if cluster.get("feature_version") is not None else None),
            reasons=reasons,
            detail=detail,
        ))

    eligible = [g for g in groups if not g.reasons]
    deferred = [g.cluster_key for g in eligible[scope.max_clusters_per_run:]]
    if deferred:
        dropped = set(deferred)
        groups = [g for g in groups if g.cluster_key not in dropped]
    counts["deferred_run_cap"] = len(deferred)
    counts["planned"] = sum(1 for g in groups if not g.reasons)
    counts["skipped"] = sum(1 for g in groups if g.reasons)
    by_reason = Counter(g.reasons[0] for g in groups if g.reasons)
    return Plan(
        generation=generation,
        scope=scope,
        groups=groups,
        deferred=deferred,
        counts={**dict(counts), "skipped_by_reason": dict(sorted(by_reason.items()))},
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
        "skipped": [_brief(g, reasons=g.reasons) for g in plan.skipped],
        "deferred": list(plan.deferred),
        "applied": [],
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
    counts.update(applied=0, refused=0, failed=0, listings_moved=0)
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
    `autodedup_apply_enabled`: undo is the way back."""
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
        "counts": {"groups": len(targets), "undone": 0, "already_undone": 0,
                   "listings_moved_back": 0, "conflicts": 0},
    }
    if dry_run:
        return result
    undone_by = f"autodedup-unapply:{run_id}"
    counts = result["counts"]
    for target in targets:
        try:
            with conn.transaction():
                res = unmerge(conn, merge_group_id=target["merge_group_id"],
                              undone_by=undone_by)
                data = res.get("data") or {}
                _exec(conn, S.LEDGER_UNDO_SQL, {
                    "merge_group_id": target["merge_group_id"], "undone_by": undone_by,
                    "undo_result": json.dumps(data, sort_keys=True, default=str)})
        except MergeError as exc:
            # Nothing left to undo: the group was reversed outside the engine (the merge
            # ledger's own unmerge). The engine's ledger records that it no longer stands.
            with conn.transaction():
                _exec(conn, S.LEDGER_UNDO_SQL, {
                    "merge_group_id": target["merge_group_id"], "undone_by": undone_by,
                    "undo_result": json.dumps({"error": str(exc)})})
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
    for key in ("applied", "refused", "failed", "planned", "skipped", "groups"):
        rows = result.get(key) or []
        if not rows:
            continue
        lines += ["", f"### {key} ({len(rows)}{'+' if result.get(f'{key}_truncated') else ''})",
                  "", "| group | survivor | retired | note |", "| --- | --- | --- | --- |"]
        for row in rows[:shown]:
            note = row.get("error") or ", ".join(row.get("reasons") or []) \
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


def run_apply(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Path
) -> dict[str, Any]:
    _check_args(args, APPLY_ARGS)
    generation = _generation_arg(args)
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
    summary = _capped(result, ("planned", "skipped", "applied", "refused", "failed", "deferred"))
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
