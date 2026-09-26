"""A2: undo the old engine's merges in the apply scope's area before this engine merges there.

TEMPORARY SCAFFOLDING, deleted in W5 (PLAN.md ledger A2) once the worker lane owns every grouping
that is not an operator ruling. Lane mode `apply` with `retire_legacy=1` runs it BEFORE the plan
it applies, in the same dispatch and under the same `dry_run`, so no duplicate reappears in
Browse between the undo and the engine's merges (decision 3). The mode refuses it with
`listing_ids` (the step reads blocks only) and, before anything is undone, for a generation with
no proposed group inside the scope (a typo'd or unstored generation would undo and merge nothing).

A legacy group is one `merge_group_id` of `property_merge_events` with `source = 'auto'` (the
removed engine; NOT `generation = 'legacy'`, which migration 475 stamped on every row of
2026-09-05). Its health is the W0 probe's `r1c_live_group_health`, in the probe's order:
`survivor_merged_on`, `listing_gone` (a row naming no advert included), `children_moved` (a
moved advert is off the survivor),
`built_on` (a later live merge onto the survivor by the operator or, once it merges, by this
engine); then `operator_split` (a row of it undone by anyone but this step: the operator took an
advert back by hand and ruled it apart from the rest), else `intact`. An intact group is undone
only when its adverts (the survivor's, the retired properties' and every advert the merge moved)
ALL sit inside the scope's blocks, the undo would neither separate two adverts an operator
ruled "same" that THIS RUN does not re-join (`skipped:operator_ruled_same`; E907: a ruled pair
the undo separates but that sits inside ONE engine group this run may merge is re-merged by the
apply that follows, so it does not hold the group back) nor NEWLY join two the operator ruled
apart (a
negative pair verdict, or a must-not-link whose source is 'operator': a machine guard is not a
ruling; `skipped:operator_ruled_different`), and every advert carries one of the scope's deal
types (`category_types`, decision 1: sales first; else `skipped:outside_scope_categories`). The
one exception to the deal types is a merge that CREATED a mix: what it took from one retired
property and the survivor's own adverts (those no live old-engine row moved there) are both
typed and disjoint (a rental merged onto a sale, in one step or two), wrong by construction and
never re-made by the engine, so undone whatever the scope names (`retired:mixed_deal_type`).
Last, a group the ENGINE holds whole - every advert of both sides in ONE engine group of the
generation that this run may not merge (not proposed, or not admitted by the scope: the trial
edge cuts it) - is left (`skipped:engine_agrees_out_of_scope`, with that group and why): undoing
it would split what the engine calls one property, and nothing would re-merge it until the
scope widens (W6). Where the engine splits the group itself (different engine groups, some or
all adverts ungrouped), it is undone. Every other group touching the area is reported and left
alone. The dry run reads the same
classification, so it predicts every skip.

Groups go newest first, each in its own transaction over its locked properties and a fresh read,
as a loop of `detach_listing` over the adverts it moved (ledger order, each back to where THAT
merge took it from) with `source='auto'`, so NO ruling is written: an old-engine merge is not the
operator's word in either direction; the in-transaction re-read refuses a group that no longer
qualifies (`refused:<reason>`). Its ledger rows are stamped
`undone_by='autodedup-legacy-retire:<run_id>'`. Undoing a newer merge can leave an older one
intact (a chain R1 -> S -> T), so a live run selects again after each pass until no new group
qualifies, reporting every group as its latest read saw it; after MAX_PASSES it stops and says
so, the rest `not_attempted`. Engine groups count only when this run may merge them (proposed,
admitted by the scope); the rest touching the retire set are counted apart.
`max_clusters_per_run` caps the ENGINE's merges only: live, the groups undone whose engine group
the cap deferred are counted, and a re-dispatch merges them; a dry run (whose plan still reads
the old merges) predicts the engine groups the live run would plan against the cap.

COST: the area (the scope's blocks as listing ids, AREA_SQL) is read ONCE per dispatch; every
later read - each pass and each group's re-check - takes those ids and is driven from them into
the ledger's indexes (see each statement's comment), never across the whole ledger: the first
shape scanned every live old-engine row with two correlated lookups each and hit the 2-minute
statement timeout (dry run 36148892044). Every read's wall time is in the summary (`timings`).

D7 carve-out, like the W0 probe's: this reads `property_merge_events` only to find what to undo.
Nothing read here becomes engine input.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from autodedup import apply_sql as S
from autodedup.census import write_json
from autodedup.ui_sql import NEGATIVE_VERDICTS
from toolkit.property_identity import MergeError, detach_listing

RETIRE_BY: str = "autodedup-legacy-retire"
LEGACY_SOURCE: str = "auto"
INTACT: str = "intact"
OPERATOR_SPLIT: str = "operator_split"
TO_RETIRE: str = "to_retire"
MIXED: str = "mixed_deal_type"
OUTSIDE_CATEGORIES: str = "outside_scope_categories"
RULED_SAME: str = "operator_ruled_same"
RULED_DIFFERENT: str = "operator_ruled_different"
ENGINE_AGREES: str = "engine_agrees_out_of_scope"
SAME_VERDICTS: tuple[str, ...] = ("same",)
MAX_PASSES: int = 10
ARTIFACT: tuple[str, str] = ("apply", "legacy_retire.json")
PARTIAL_ATTR: str = "autodedup_retire_result"
SUMMARY_ROWS: int = 50

# The area, read ONCE per dispatch: every later read takes these listing ids, so no pass and no
# per-group re-check scans `listing_location` again. The export lane's per-grain membership
# (`town:` = obec_kod, `quarter:` = cast_obce_kod). The town arm rides
# listing_location_obec_granularity (obec_kod, granularity) (migration 501); cast_obce_kod has no
# index, so the quarter arm is the dispatch's one scan of the table (timed in the summary).
AREA_SQL = """
select ll.listing_id
  from public.listing_location ll
 where ll.obec_kod = any(%(towns)s::bigint[])
union
select ll.listing_id
  from public.listing_location ll
 where ll.cast_obce_kod = any(%(quarters)s::bigint[])
"""

# Every row, live or undone, of every legacy group with a live row that touches the area through
# an advert it moved (the probe's `touches_trial`) or through an advert now on its survivor.
# Driven FROM the area (a few thousand adverts), never across the ledger: area adverts ->
# property_merge_events_listing_live_idx (listing_ref_id, id) WHERE undone_at IS NULL (migration
# 560); area adverts -> listings pkey -> their properties -> property_merge_events_survivor_idx
# (migration 100); the touching groups' rows -> property_merge_events_group_idx (merge_group_id)
# (migration 100); each row's advert -> listings pkey. A group touching ONLY through an advert
# on its RETIRED property is not read: `retired_property_id` has no index, and such a group
# (moved adverts and survivor all outside) could only ever be `straddling`, never undone.
# `merge_group_id` narrows the read to one group (the in-transaction re-check).
GROUPS_SQL = """
with area as materialized (
  select distinct unnest(%(area_ids)s::bigint[]) as listing_id
),
area_properties as materialized (
  select distinct l.property_id
    from area a
    join public.listings l on l.id = a.listing_id
   where l.property_id is not null
),
touching as (
  select e.merge_group_id
    from area a
    join public.property_merge_events e on e.listing_ref_id = a.listing_id
   where e.undone_at is null
     and e.source = 'auto'
     and (%(merge_group_id)s::uuid is null or e.merge_group_id = %(merge_group_id)s::uuid)
  union
  select e.merge_group_id
    from area_properties ap
    join public.property_merge_events e on e.survivor_property_id = ap.property_id
   where e.undone_at is null
     and e.source = 'auto'
     and (%(merge_group_id)s::uuid is null or e.merge_group_id = %(merge_group_id)s::uuid)
)
select e.id, e.merge_group_id::text, e.source, e.created_at, e.survivor_property_id,
       e.retired_property_id, e.listing_ref_id, l.id is not null, l.property_id,
       e.listing_ref_id in (select a.listing_id from area a),
       e.undone_at is not null, e.undone_by
  from touching t
  join public.property_merge_events e on e.merge_group_id = t.merge_group_id
  left join public.listings l on l.id = e.listing_ref_id
 order by e.merge_group_id, e.id
"""

# Every advert now on the groups' survivors and retired properties, inside the area or not:
# listings_property_id_idx (migration 091), the area as the ids AREA_SQL read.
ADVERTS_SQL = """
select l.property_id, l.id, l.category_type,
       l.id in (select unnest(%(area_ids)s::bigint[]))
  from public.listings l
 where l.property_id = any(%(property_ids)s::bigint[])
 order by l.property_id, l.id
"""

# Which of these adverts a live old-engine row moved: the rest of a survivor's adverts are its own
# (or an operator's merge), the side a merge that CREATED a deal-type mix is read against.
# property_merge_events_listing_live_idx (migration 560).
AUTO_MOVED_SQL = """
select distinct e.listing_ref_id
  from public.property_merge_events e
 where e.undone_at is null
   and e.source = 'auto'
   and e.listing_ref_id = any(%(listing_ids)s::bigint[])
"""

# The newest live merge onto each survivor by anyone but the old engine: one after the legacy
# group's own builds on it. property_merge_events_survivor_idx (migration 100).
BUILT_ON_SQL = """
select e.survivor_property_id, e.source, max(e.created_at)
  from public.property_merge_events e
 where e.undone_at is null
   and e.source <> 'auto'
   and e.survivor_property_id = any(%(property_ids)s::bigint[])
 group by e.survivor_property_id, e.source
"""


@dataclass
class LegacyGroup:
    merge_group_id: str
    merged_at: Any
    survivor_id: int
    retired_ids: list[int]
    moved: list[int]
    origin: dict[int, int]
    moved_outside: list[int]
    gone: bool
    off_survivor: list[int]
    undone_by_others: list[str]
    adverts: list[int] = field(default_factory=list)
    outside: list[int] = field(default_factory=list)
    category_types: list[str] = field(default_factory=list)
    uncategorised: bool = False
    mixed: bool = False
    separates_same: list[list[int]] = field(default_factory=list)
    # E907: the ruled-same pairs of `separates_same` one mergeable engine group re-joins.
    same_held_by_engine: list[list[int]] = field(default_factory=list)
    joins_different: list[list[int]] = field(default_factory=list)
    health: str = INTACT
    selected: bool = False
    outcome: str = ""
    retired_in_pass: int | None = None
    engine_clusters: list[int] = field(default_factory=list)
    engine_clusters_not_merging: list[int] = field(default_factory=list)
    engine_agrees_cluster: int | None = None
    engine_agrees_why: str | None = None
    listings_moved_back: int = 0
    reactivated: int = 0

    @property
    def touches_by_moved(self) -> bool:
        return len(self.moved_outside) < len(self.moved)

    @property
    def category_label(self) -> str:
        if self.mixed:
            return "mixed"
        return "/".join(self.category_types + (["none"] if self.uncategorised else [])) or "none"

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "touches_by_moved": self.touches_by_moved,
                "category_label": self.category_label}


class _Refused(Exception):
    """The group changed since it was selected, or one detach may not move its advert."""


def area_params(blocks: Iterable[str] | None) -> dict[str, list[int]]:
    """`town:<code>` / `quarter:<code>` (the scope's spelling) into the two grains' codes."""
    out: dict[str, list[int]] = {"towns": [], "quarters": []}
    for block in sorted(blocks or ()):
        grain, _, code = block.partition(":")
        if grain not in ("town", "quarter") or not code.isdigit():
            raise ValueError(f"block {block!r}: expected town:<code> or quarter:<code>")
        out["towns" if grain == "town" else "quarters"].append(int(code))
    if not out["towns"] and not out["quarters"]:
        raise ValueError("the scope names no blocks; the legacy retire reads blocks only")
    return out


# Wall time of every read, by statement, since the step started: a slow read shows in the
# summary before it reaches the database's statement timeout. One step per lane process.
_CLOCK: dict[str, list[float]] = {}


def _statement(sql: str) -> str:
    names = {AREA_SQL: "area", GROUPS_SQL: "groups", ADVERTS_SQL: "adverts",
             AUTO_MOVED_SQL: "auto_moved", BUILT_ON_SQL: "built_on",
             S.PROPERTY_STATE_SQL: "property_state", S.PAIR_VERDICTS_SQL: "pair_verdicts",
             S.MUST_NOT_LINK_SQL: "must_not_link", S.LOCK_PROPERTIES_SQL: "lock_properties"}
    return names.get(sql, "other")


def _rows(conn: Any, sql: str, params: Mapping[str, Any]) -> list[tuple]:
    started = time.monotonic()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, dict(params))
            return list(cur.fetchall())
    finally:
        _CLOCK.setdefault(_statement(sql), []).append(time.monotonic() - started)


def read_area(conn: Any, blocks: Iterable[str] | None) -> dict[str, list[int]]:
    """The scope's blocks as the listing ids every later read takes (`area_ids`)."""
    ids = {int(r[0]) for r in _rows(conn, AREA_SQL, area_params(blocks))}
    return {"area_ids": sorted(ids)}


def _timings() -> dict[str, dict[str, float]]:
    return {name: {"n": len(took), "total_s": round(sum(took), 3),
                   "max_s": round(max(took), 3)}
            for name, took in sorted(_CLOCK.items())}


def _health(group: LegacyGroup, status: str | None, later: Iterable[Any]) -> str:
    if status != "active":
        return "survivor_merged_on"
    if group.gone:
        return "listing_gone"
    if group.off_survivor:
        return "children_moved"
    if any(at is not None and group.merged_at is not None and at > group.merged_at
           for at in later):
        return "built_on"
    if group.undone_by_others:
        return OPERATOR_SPLIT
    return INTACT


def _outcome(g: LegacyGroup, categories: frozenset[str] | None) -> str:
    if g.health != INTACT:
        return f"skipped:{g.health}"
    if g.outside:
        return "skipped:straddling"
    # E907: only a ruled-same pair nothing re-joins holds the group back.
    if len(g.same_held_by_engine) < len(g.separates_same):
        return f"skipped:{RULED_SAME}"
    if g.joins_different:
        return f"skipped:{RULED_DIFFERENT}"
    if not g.mixed and categories is not None and (
            g.uncategorised or not set(g.category_types) <= categories):
        return f"skipped:{OUTSIDE_CATEGORIES}"
    if g.engine_agrees_cluster is not None:
        return f"skipped:{ENGINE_AGREES}"
    return f"{TO_RETIRE}:{MIXED}" if g.mixed else TO_RETIRE


@dataclass(frozen=True)
class EngineMaps:
    """The generation being applied, per listing: the engine group this run may merge it in
    (proposed, admitted by the scope), or one it never will, with why it is not admitted."""

    merging: Mapping[int, int] = field(default_factory=dict)
    other: Mapping[int, int] = field(default_factory=dict)
    why: Mapping[int, str] = field(default_factory=dict)


def _held_by_engine(pair: list[int], engine: EngineMaps) -> bool:
    """E907: do both adverts of an operator "same" pair sit in ONE engine group this run may
    merge? Then the undo separates them only until the apply re-merges that group; a pair
    across two groups, with a side ungrouped, or in a group this run will not merge (not
    proposed, not admitted by the scope) stays apart, and the ruling holds the group back."""
    lo, hi = pair
    return (lo in engine.merging and hi in engine.merging
            and engine.merging[lo] == engine.merging[hi])


def _engine(g: LegacyGroup, engine: EngineMaps) -> None:
    ids = {*g.adverts, *g.moved}
    g.same_held_by_engine = [pair for pair in g.separates_same if _held_by_engine(pair, engine)]
    g.engine_clusters = sorted({engine.merging[lid] for lid in ids if lid in engine.merging})
    g.engine_clusters_not_merging = sorted({engine.other[lid] for lid in ids
                                            if lid in engine.other})
    # The engine holds every advert of the group, both sides, in ONE group this run may not
    # merge: undoing now would split what the engine calls one property (W6 re-merges it).
    one = g.engine_clusters_not_merging
    if ids and not g.engine_clusters and len(one) == 1 and all(lid in engine.other
                                                                for lid in ids):
        g.engine_agrees_cluster = one[0]
        g.engine_agrees_why = engine.why.get(one[0])


def read_groups(
    conn: Any, area: Mapping[str, list[int]], categories: frozenset[str] | None,
    merge_group_id: str | None = None, engine: EngineMaps | None = None,
) -> list[LegacyGroup]:
    """Every live legacy group touching the area, classified, newest first. `categories`
    None admits every deal type (a scope that names none), as `apply.Scope` reads it."""
    engine = engine or EngineMaps()
    by_group: dict[str, list[tuple]] = {}
    for row in _rows(conn, GROUPS_SQL, {**area, "merge_group_id": merge_group_id}):
        by_group.setdefault(str(row[1]), []).append(row)
    groups: list[LegacyGroup] = []
    for gid, rows in by_group.items():
        live = sorted((r for r in rows if not r[10]), key=lambda r: int(r[0]))
        if not live or min(str(r[2]) for r in rows) != LEGACY_SOURCE:
            continue
        survivor = max(int(r[4]) for r in live)
        # A row with no listing_ref_id names no advert: read as a gone one.
        real = [r for r in live if r[6] is not None]
        groups.append(LegacyGroup(
            merge_group_id=gid, merged_at=min(r[3] for r in rows), survivor_id=survivor,
            retired_ids=sorted({int(r[5]) for r in live}), moved=[int(r[6]) for r in real],
            origin={int(r[6]): int(r[5]) for r in real},
            moved_outside=[int(r[6]) for r in real if not r[9]],
            gone=any(not r[7] for r in live),
            off_survivor=sorted(int(r[6]) for r in real if r[7] and r[8] != survivor),
            undone_by_others=sorted({str(r[11] or "unknown") for r in rows if r[10]
                                     and not str(r[11] or "").startswith(f"{RETIRE_BY}:")})))
    if not groups:
        return []
    props = sorted({pid for g in groups for pid in (g.survivor_id, *g.retired_ids)})
    status = {int(pid): st for pid, st, _into, _at in _rows(
        conn, S.PROPERTY_STATE_SQL, {"property_ids": props})}
    adverts: dict[int, list[tuple[int, int, str | None, bool]]] = {}
    for pid, lid, ctype, inside in _rows(conn, ADVERTS_SQL, {**area, "property_ids": props}):
        adverts.setdefault(int(pid), []).append((int(lid), int(pid), ctype, bool(inside)))
    later: dict[int, list[Any]] = {}
    for pid, _source, last_at in _rows(conn, BUILT_ON_SQL, {
            "property_ids": sorted({g.survivor_id for g in groups})}):
        later.setdefault(int(pid), []).append(last_at)
    every = sorted({a[0] for rows in adverts.values() for a in rows})
    same, different, auto_moved = _rulings(conn, every)
    for g in groups:
        sides = [a for pid in (g.survivor_id, *g.retired_ids) for a in adverts.get(pid, ())]
        g.adverts = sorted(a[0] for a in sides)
        g.outside = sorted({lid for lid, _p, _ct, inside in sides if not inside}
                           | set(g.moved_outside))
        g.category_types = sorted({ct for _l, _p, ct, _i in sides if ct})
        g.uncategorised = any(ct is None for _l, _p, ct, _i in sides)
        # Created a mix: what the merge took from one retired property and the survivor's own
        # adverts (no live old-engine row moved them) are both typed and disjoint.
        own = {ct for lid, pid, ct, _i in sides
               if pid == g.survivor_id and lid not in auto_moved and ct}
        taken: dict[int, set[str]] = {}
        for lid, pid, ct, _i in sides:
            if lid in g.origin and ct:
                taken.setdefault(g.origin[lid], set()).add(ct)
        g.mixed = bool(own) and any(types.isdisjoint(own) for types in taken.values())
        now = {lid: pid for lid, pid, _ct, _i in sides}
        after = {lid: g.origin.get(lid, pid) for lid, pid in now.items()}
        g.separates_same = [[lo, hi] for lo, hi in sorted(same) if lo in now and hi in now
                            and now[lo] == now[hi] and after[lo] != after[hi]]
        g.joins_different = [[lo, hi] for lo, hi in sorted(different) if lo in now
                             and hi in now and now[lo] != now[hi] and after[lo] == after[hi]]
        _engine(g, engine)
        g.health = _health(g, status.get(g.survivor_id), later.get(g.survivor_id, ()))
        g.outcome = _outcome(g, categories)
        g.selected = g.outcome.startswith(TO_RETIRE)
    groups.sort(key=lambda g: (g.merged_at, g.merge_group_id), reverse=True)
    return groups


def _rulings(
    conn: Any, listing_ids: list[int],
) -> tuple[set[tuple[int, int]], set[tuple[int, int]], set[int]]:
    """The operator's "same" pairs and negatives (pair verdicts, and must-not-links whose
    source is 'operator': a machine guard is not a ruling) among these adverts, and which of
    them a live old-engine row moved."""
    if not listing_ids:
        return set(), set(), set()
    same = {(int(lo), int(hi)) for lo, hi, _v in _rows(conn, S.PAIR_VERDICTS_SQL, {
        "listing_ids": listing_ids, "negatives": list(SAME_VERDICTS)})}
    different = {(int(lo), int(hi)) for lo, hi, _v in _rows(conn, S.PAIR_VERDICTS_SQL, {
        "listing_ids": listing_ids, "negatives": list(NEGATIVE_VERDICTS)})}
    different |= {(int(lo), int(hi)) for lo, hi, src in _rows(
        conn, S.MUST_NOT_LINK_SQL, {"listing_ids": listing_ids}) if src == "operator"}
    moved = {int(r[0]) for r in _rows(conn, AUTO_MOVED_SQL, {"listing_ids": listing_ids})}
    return same, different, moved


def _retire_one(
    conn: Any, group: LegacyGroup, area: Mapping[str, list[int]],
    categories: frozenset[str] | None, engine: EngineMaps,
    detach: Callable[..., dict[str, Any]], undone_by: str,
) -> None:
    """One group, one transaction: re-checked over its locked properties, then every advert it
    moved detached back to where that merge took it from; any refusal rolls the group back."""
    back = reactivated = 0
    try:
        with conn.transaction():
            _rows(conn, S.LOCK_PROPERTIES_SQL,
                  {"property_ids": sorted({group.survivor_id, *group.retired_ids})})
            fresh = read_groups(conn, area, categories, group.merge_group_id, engine)
            if not fresh:
                raise _Refused("no_longer_live")
            if not fresh[0].selected:
                raise _Refused(fresh[0].outcome.removeprefix("skipped:"))
            for lid in group.moved:
                data = detach(conn, lid, decided_by=undone_by, source=LEGACY_SOURCE,
                              merge_group_id=group.merge_group_id)["data"]
                if not data["detached"]:
                    raise _Refused(str(data["outcome"]))
                back += 1
                reactivated += int(bool(data.get("reactivated")))
    except (_Refused, MergeError) as exc:
        group.outcome = f"refused:{exc}"
        return
    group.outcome = "retired" + fresh[0].outcome.removeprefix(TO_RETIRE)
    group.listings_moved_back, group.reactivated = back, reactivated


def _counts(first: list[LegacyGroup], final: list[LegacyGroup]) -> dict[str, Any]:
    """`first`: the selection as read before anything moved (the probe's grain); `final`: every
    group as it ended, later passes included."""
    by_moved = [g for g in first if g.touches_by_moved]
    inside = [g for g in first if g.health == INTACT and not g.outside]
    chosen = [g for g in final if g.selected]
    freed = {rid for g in first if g.selected for rid in g.retired_ids}
    return {
        "groups_touching": len(first),
        "touching_only_through_survivor_side": len(first) - len(by_moved),
        # The W0 probe's own numbers (r1c_live_group_health, r1_merge_groups), re-read now;
        # its `intact` is this `intact` plus `operator_split`.
        "probe_r1c_by_health": dict(sorted(Counter(g.health for g in by_moved).items())),
        "probe_r1_area": {"inside": sum(not g.moved_outside for g in by_moved),
                          "straddles": sum(bool(g.moved_outside) for g in by_moved)},
        # Blocks only, every deal type: the probe's grain, beside the filtered retire set.
        "intact_both_sides_inside": len(inside),
        "intact_both_sides_inside_by_category_type": dict(sorted(Counter(
            g.category_label for g in inside).items())),
        "intact_straddling": sum(g.health == INTACT and bool(g.outside) for g in first),
        "retire_set": len(chosen),
        "retire_set_mixed_deal_type": sum(g.mixed for g in chosen),
        "retire_set_after_first_pass": sum((g.retired_in_pass or 1) > 1 for g in chosen),
        # A survivor the first pass hands back: its older merge may qualify on a later pass.
        "chained_behind_retire_set": sum(g.health == "survivor_merged_on"
                                         and g.survivor_id in freed for g in first),
        "outside_scope_categories_by_category_type": dict(sorted(Counter(
            g.category_label for g in final
            if g.outcome == f"skipped:{OUTSIDE_CATEGORIES}").items())),
        # Engine groups this run may merge (proposed, inside the scope) vs ones it never will.
        "engine_clusters_touching_retire_set": len({k for g in chosen
                                                    for k in g.engine_clusters}),
        "retire_set_without_engine_cluster": sum(not g.engine_clusters for g in chosen),
        "engine_clusters_not_merging_touching_retire_set": len({
            k for g in chosen for k in g.engine_clusters_not_merging}),
        # Left for W6: the engine holds the whole group in one group this run may not merge.
        ENGINE_AGREES: sum(g.outcome == f"skipped:{ENGINE_AGREES}" for g in final),
        f"{ENGINE_AGREES}_by_reason": dict(sorted(Counter(
            g.engine_agrees_why or "unknown" for g in final
            if g.outcome == f"skipped:{ENGINE_AGREES}").items())),
        # E907: retired although the undo separates an operator "same" pair, because one
        # engine group this run merges holds the pair and the apply re-joins it.
        "retire_set_with_ruled_same_held_by_engine": sum(bool(g.same_held_by_engine)
                                                         for g in chosen),
        "outcomes": dict(sorted(Counter(g.outcome for g in final).items())),
        "listings_moved_back": sum(g.listings_moved_back for g in final),
        "properties_reactivated": sum(g.reactivated for g in final),
    }


def retire_legacy(
    conn: Any, blocks: Iterable[str] | None, *, category_types: Iterable[str] | None,
    dry_run: bool, run_id: str, closed: Callable[[Any], str | None] | None = None,
    detach: Callable[..., dict[str, Any]] | None = None,
    engine: EngineMaps | None = None,
) -> dict[str, Any]:
    """Select, then (live) undo pass after pass, the qualifying legacy groups. `closed` is the
    scope row's re-read before every group (E39); `engine` is the generation being applied
    (`EngineMaps`); a dry run writes nothing."""
    _CLOCK.clear()
    area = read_area(conn, blocks)
    categories = frozenset(category_types) if category_types is not None else None
    detach = detach or detach_listing
    undone_by = f"{RETIRE_BY}:{run_id}"
    engine = engine or EngineMaps()
    first = read_groups(conn, area, categories, engine=engine)
    final: dict[str, LegacyGroup] = {g.merge_group_id: g for g in first}
    result: dict[str, Any] = {
        "run_id": run_id, "dry_run": dry_run, "blocks": sorted(blocks or ()),
        "area_listings": len(area["area_ids"]),
        "category_types": sorted(categories) if categories is not None else None,
        "undone_by": undone_by, "passes": 0}
    todo = [g for g in first if g.selected]
    if dry_run:
        for g in todo:
            g.outcome = "would_retire" + g.outcome.removeprefix(TO_RETIRE)
        return _finish(result, first, final)
    current: LegacyGroup | None = None
    try:
        while todo and result["passes"] < MAX_PASSES and "stopped" not in result:
            result["passes"] += 1
            for index, group in enumerate(todo):
                reason = closed(conn) if closed else None
                if reason:
                    result["stopped"] = f"the apply scope admits no live merge: {reason}"
                    for g in todo[index:]:
                        g.outcome = "not_attempted"
                        final[g.merge_group_id] = g
                    break
                current, group.retired_in_pass = group, result["passes"]
                final[group.merge_group_id] = group
                _retire_one(conn, group, area, categories, engine, detach, undone_by)
                current = None
            if "stopped" in result:
                break
            done = {gid for gid, g in final.items() if g.retired_in_pass}
            todo = []
            for g in read_groups(conn, area, categories, engine=engine):
                if g.merge_group_id not in done:
                    final[g.merge_group_id] = g
                    if g.selected:
                        todo.append(g)
            if todo and result["passes"] >= MAX_PASSES:
                result["stopped"] = (f"{MAX_PASSES} passes reached with {len(todo)} group(s) "
                                     "still qualifying: dispatch again")
                for g in todo:
                    g.outcome = "not_attempted"
    except BaseException as exc:
        if current is not None:
            current.outcome = "failed"
        for g in final.values():
            if g.outcome.startswith(TO_RETIRE):
                g.outcome = "not_attempted"
        result["aborted"] = f"{type(exc).__name__}: {exc}"
        try:
            setattr(exc, PARTIAL_ATTR, _finish(result, first, final))
        except Exception:  # noqa: BLE001 - an error that takes no attribute still surfaces
            pass
        raise
    return _finish(result, first, final)


def _finish(
    result: dict[str, Any], first: list[LegacyGroup], final: dict[str, LegacyGroup],
) -> dict[str, Any]:
    groups = sorted(final.values(), key=lambda g: (g.merged_at, g.merge_group_id), reverse=True)
    return {**result, "counts": _counts(first, groups), "timings": _timings(),
            "engine_clusters": sorted({k for g in groups if g.selected
                                       for k in g.engine_clusters}),
            "engine_clusters_not_merging": sorted({k for g in groups if g.selected
                                                   for k in g.engine_clusters_not_merging}),
            "groups": [g.to_json() for g in groups]}


def note_deferred(
    out_dir: Path, result: dict[str, Any], *, deferred: Iterable[int], planned: Iterable[int],
    cap: int,
) -> dict[str, Any]:
    """After the engine's plan, so nobody reads a capped merge as lost. Live: the groups undone
    whose engine group `max_clusters_per_run` deferred to the next dispatch. Dry run: its plan
    still reads the old merges (their engine groups are `already_one_property`), so it
    predicts: the engine groups the live run would plan after the undo, against the cap."""
    counts = result["counts"]
    if result["dry_run"]:
        predicted = len(set(result.get("engine_clusters") or ()) | set(planned))
        counts["predicted_engine_groups_after_undo"] = predicted
        counts["predicted_deferred_by_cap"] = max(0, predicted - cap)
        note = (f"about {predicted} engine groups to plan after the undo, above the run cap "
                f"of {cap}: a live run defers {predicted - cap} to a re-dispatch"
                if predicted > cap else "")
    else:
        late = set(deferred)
        held = [g["merge_group_id"] for g in result.get("groups") or []
                if g["outcome"].startswith("retired") and late & set(g["engine_clusters"])]
        counts["undone_but_engine_group_deferred_by_cap"] = len(held)
        result["deferred_by_cap"] = held
        # E907: of those, the groups that also parted an operator "same" pair the deferred
        # engine group re-joins - apart until the re-dispatch merges it.
        counts["ruled_same_apart_until_redispatch"] = sum(
            1 for g in result.get("groups") or []
            if g["merge_group_id"] in held and g.get("same_held_by_engine"))
        note = (f"{len(held)} legacy group(s) undone whose engine group the run cap deferred: "
                "dispatch apply again to merge them" if held else "")
    write_json(Path(out_dir).joinpath(*ARTIFACT), result)
    if note:
        _append_summary(f"\n**Run cap:** {note}.\n")
    return _brief(result)


def summary_markdown(result: Mapping[str, Any]) -> str:
    counts = result.get("counts") or {}
    head = "DRY RUN - nothing undone" if result.get("dry_run") else "LIVE"
    lines = [f"## autodedup apply - legacy retire (A2, temporary) - {head}", "",
             f"Blocks: {' '.join(result.get('blocks') or [])}; deal types: "
             f"{' '.join(result.get('category_types') or ['all'])} (a merge that created a "
             f"sale/rental mix is retired whatever they are); stamp `{result.get('undone_by')}`; "
             f"passes: {result.get('passes')}; adverts in the area: "
             f"{result.get('area_listings')}.", ""]
    if result.get("dry_run"):
        lines += ["The engine's dry-run plan below reads the old engine's merges as they "
                  "stand; the live run plans after they are undone. "
                  "`chained_behind_retire_set` counts older merges a live run's later passes "
                  "may also undo.", ""]
    for key in ("stopped", "aborted"):
        if result.get(key):
            lines += [f"**{key.capitalize()}:** {result[key]}", ""]
    if result.get("timings"):
        lines += ["| read | n | total s | max s |", "| --- | --- | --- | --- |"]
        lines += [f"| {name} | {t['n']} | {t['total_s']} | {t['max_s']} |"
                  for name, t in result["timings"].items()]
        lines += [""]
    lines += ["| count | n |", "| --- | --- |"]
    for key, value in counts.items():
        if isinstance(value, dict):
            value = ", ".join(f"{k}={v}" for k, v in value.items()) or "-"
        lines.append(f"| {key} | {value} |")
    if result.get("engine_clusters"):
        lines += ["", "Engine groups touching the retire set: "
                  + " ".join(str(k) for k in result["engine_clusters"][:200])]
    if result.get("engine_clusters_not_merging"):
        lines += ["", "Engine groups touching it that this run will not merge (not proposed, "
                  "or outside the scope): "
                  + " ".join(str(k) for k in result["engine_clusters_not_merging"][:200])]
    rows = [g for g in result.get("groups") or [] if not g["outcome"].startswith("skipped:")]
    rows += [g for g in result.get("groups") or [] if g["outcome"].startswith("skipped:")]
    lines += ["", "| group | merged | survivor | retired | adverts | engine groups | outcome |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for g in rows[:SUMMARY_ROWS]:
        why = (f" ({g['engine_agrees_cluster']}: {g['engine_agrees_why']})"
               if g.get("engine_agrees_cluster") is not None else "")
        lines.append(f"| {g['merge_group_id']} | {g['merged_at']} | {g['survivor_id']} | "
                     f"{' '.join(str(r) for r in g['retired_ids'])} | {len(g['moved'])} | "
                     f"{' '.join(str(k) for k in g['engine_clusters'])} | {g['outcome']}{why} |")
    if len(rows) > SUMMARY_ROWS:
        lines.append(f"| ... {len(rows) - SUMMARY_ROWS} more in {'/'.join(ARTIFACT)} "
                     "| | | | | | |")
    return "\n".join(lines) + "\n"


def _append_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass


def _publish(out_dir: Path, result: dict[str, Any]) -> None:
    write_json(Path(out_dir).joinpath(*ARTIFACT), result)
    _append_summary(summary_markdown(result))


def _brief(result: Mapping[str, Any]) -> dict[str, Any]:
    return {key: result[key] for key in (
        "counts", "stopped", "aborted", "undone_by", "category_types", "passes", "timings",
        "area_listings")
        if key in result}


def run(
    conn: Any, blocks: Iterable[str] | None, *, category_types: Iterable[str] | None,
    dry_run: bool, run_id: str, out_dir: Path, closed: Callable[[Any], str | None] | None = None,
    engine: EngineMaps | None = None,
) -> dict[str, Any]:
    """The apply mode's pre-step: retire, publish the artifact and the step summary (a crash
    included), and return the full result (`note_deferred` adds the cap's count after the plan)."""
    try:
        area_params(blocks)
    except ValueError as exc:
        raise SystemExit(f"retire_legacy: {exc}") from exc
    try:
        result = retire_legacy(conn, blocks or (), category_types=category_types,
                               dry_run=dry_run, run_id=run_id, closed=closed, engine=engine)
    except BaseException as exc:
        partial = getattr(exc, PARTIAL_ATTR, None)
        if isinstance(partial, dict):
            _publish(out_dir, partial)
        raise
    _publish(out_dir, result)
    return result
