"""A2: undo the old engine's merges in the apply scope's area before this engine merges there.

TEMPORARY SCAFFOLDING, deleted in W5 (PLAN.md ledger A2) once the worker lane owns every grouping
that is not an operator ruling. Lane mode `apply` with `retire_legacy=1` runs it BEFORE
`plan_apply`, in the same dispatch and under the same `dry_run`, so no duplicate reappears in
Browse between the undo and the engine's merges (decision 3).

A legacy group is one `merge_group_id` of `property_merge_events` with `source = 'auto'` (the
removed engine; NOT `generation = 'legacy'`, which migration 475 stamped on every row of
2026-09-05) and its live rows. Its health is the W0 probe's `r1c_live_group_health`, checked in
the probe's order: `survivor_merged_on`, `listing_gone`, `children_moved` (a moved advert is off
the survivor), `built_on` (a later live merge onto the survivor by the operator or, once it
merges, by this engine), else `intact`. An intact group is undone only when its adverts (the
survivor's, the retired properties' and every advert the merge moved) ALL sit inside the scope's
blocks AND all carry one of the scope's deal types (`category_types`, decision 1: sales first),
so nothing the engine will not re-merge this wave comes apart. The one exception is a group that
MIXES deal types (a sale and a rental on one property): wrong by construction, never re-made by
the engine, so undone whatever the scope's deal types (`retired:mixed_deal_type`). Every other
group touching the area is reported and left alone (`skipped:<health>`, `skipped:straddling`,
`skipped:outside_scope_categories`, counted per deal type). Groups go newest
first, each in its own transaction, as a loop of `detach_listing` over the adverts it moved (in
ledger order) with `source='auto'`, so NO ruling is written: an old-engine merge is not the
operator's word in either direction. Its ledger rows are stamped
`undone_by='autodedup-legacy-retire:<run_id>'`.

D7 carve-out, like the W0 probe's: this reads `property_merge_events` only to find what to undo.
Nothing read here becomes engine input.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from autodedup import apply_sql as S
from autodedup.census import write_json
from toolkit.property_identity import MergeError, detach_listing

RETIRE_BY: str = "autodedup-legacy-retire"
LEGACY_SOURCE: str = "auto"
INTACT: str = "intact"
TO_RETIRE: str = "to_retire"
MIXED: str = "mixed_deal_type"
OUTSIDE_CATEGORIES: str = "outside_scope_categories"
ARTIFACT: tuple[str, str] = ("apply", "legacy_retire.json")
PARTIAL_ATTR: str = "autodedup_retire_result"
SUMMARY_ROWS: int = 50

# Every live row of every legacy group that touches the area through an advert it moved (the
# probe's `touches_trial`) or through an advert now on its survivor or retired property. The
# area is the export lane's per-grain membership (`town:` = obec_kod, `quarter:` =
# cast_obce_kod). `merge_group_id` narrows the read to one group for the in-transaction re-check.
GROUPS_SQL = """
with touching as (
  select distinct e.merge_group_id
    from public.property_merge_events e
   where e.undone_at is null
     and e.source = 'auto'
     and (%(merge_group_id)s::uuid is null or e.merge_group_id = %(merge_group_id)s::uuid)
     and (exists (select 1 from public.listing_location ll
                   where ll.listing_id = e.listing_ref_id
                     and (ll.obec_kod = any(%(towns)s::bigint[])
                          or ll.cast_obce_kod = any(%(quarters)s::bigint[])))
          or exists (select 1 from public.listings l
                       join public.listing_location ll on ll.listing_id = l.id
                      where l.property_id in (e.survivor_property_id, e.retired_property_id)
                        and (ll.obec_kod = any(%(towns)s::bigint[])
                             or ll.cast_obce_kod = any(%(quarters)s::bigint[]))))
)
select e.id, e.merge_group_id::text, e.source, e.created_at, e.survivor_property_id,
       e.retired_property_id, e.listing_ref_id, l.id is not null, l.property_id,
       exists (select 1 from public.listing_location ll
                where ll.listing_id = e.listing_ref_id
                  and (ll.obec_kod = any(%(towns)s::bigint[])
                       or ll.cast_obce_kod = any(%(quarters)s::bigint[])))
  from public.property_merge_events e
  join touching t on t.merge_group_id = e.merge_group_id
  left join public.listings l on l.id = e.listing_ref_id
 where e.undone_at is null
 order by e.merge_group_id, e.id
"""

# Every advert now on the groups' survivors and retired properties, inside the area or not.
ADVERTS_SQL = """
select l.property_id, l.id, l.category_type,
       exists (select 1 from public.listing_location ll
                where ll.listing_id = l.id
                  and (ll.obec_kod = any(%(towns)s::bigint[])
                       or ll.cast_obce_kod = any(%(quarters)s::bigint[])))
  from public.listings l
 where l.property_id = any(%(property_ids)s::bigint[])
 order by l.property_id, l.id
"""

# The newest live merge onto each survivor by anyone but the old engine: one after the legacy
# group's own builds on it.
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
    moved_outside: list[int]
    gone: bool
    off_survivor: list[int]
    outside: list[int] = field(default_factory=list)
    category_types: list[str] = field(default_factory=list)
    uncategorised: bool = False
    health: str = INTACT
    selected: bool = False
    outcome: str = ""
    listings_moved_back: int = 0
    reactivated: int = 0

    @property
    def touches_by_moved(self) -> bool:
        return len(self.moved_outside) < len(self.moved)

    @property
    def mixed(self) -> bool:
        return len(self.category_types) > 1

    @property
    def category_label(self) -> str:
        if self.mixed:
            return "mixed"
        return "/".join(self.category_types + (["none"] if self.uncategorised else [])) or "none"

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "touches_by_moved": self.touches_by_moved,
                "category_label": self.category_label}


class _Refused(Exception):
    """The group changed since it was selected, or one detach would not move its advert."""


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


def _rows(conn: Any, sql: str, params: Mapping[str, Any]) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params))
        return list(cur.fetchall())


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
    return INTACT


def _outcome(g: LegacyGroup, categories: frozenset[str] | None) -> str:
    if g.health != INTACT:
        return f"skipped:{g.health}"
    if g.outside:
        return "skipped:straddling"
    if g.mixed:
        return f"{TO_RETIRE}:{MIXED}"
    if categories is not None and (g.uncategorised or not set(g.category_types) <= categories):
        return f"skipped:{OUTSIDE_CATEGORIES}"
    return TO_RETIRE


def read_groups(
    conn: Any, area: Mapping[str, list[int]], categories: frozenset[str] | None,
    merge_group_id: str | None = None,
) -> list[LegacyGroup]:
    """Every live legacy group touching the area, classified, newest first. `categories`
    None admits every deal type (a scope that names none), as `apply.Scope` reads it."""
    by_group: dict[str, list[tuple]] = {}
    for row in _rows(conn, GROUPS_SQL, {**area, "merge_group_id": merge_group_id}):
        by_group.setdefault(str(row[1]), []).append(row)
    groups: list[LegacyGroup] = []
    for gid, rows in by_group.items():
        if min(str(r[2]) for r in rows) != LEGACY_SOURCE:
            continue
        rows.sort(key=lambda r: int(r[0]))
        survivor = max(int(r[4]) for r in rows)
        groups.append(LegacyGroup(
            merge_group_id=gid, merged_at=min(r[3] for r in rows), survivor_id=survivor,
            retired_ids=sorted({int(r[5]) for r in rows}), moved=[int(r[6]) for r in rows],
            moved_outside=[int(r[6]) for r in rows if not r[9]],
            gone=any(not r[7] for r in rows),
            off_survivor=sorted(int(r[6]) for r in rows if r[7] and r[8] != survivor)))
    if not groups:
        return []
    props = sorted({pid for g in groups for pid in (g.survivor_id, *g.retired_ids)})
    status = {int(pid): st for pid, st, _into, _at in _rows(
        conn, S.PROPERTY_STATE_SQL, {"property_ids": props})}
    adverts: dict[int, list[tuple[int, str | None, bool]]] = {}
    for pid, lid, ctype, inside in _rows(conn, ADVERTS_SQL, {**area, "property_ids": props}):
        adverts.setdefault(int(pid), []).append((int(lid), ctype, bool(inside)))
    later: dict[int, list[Any]] = {}
    for pid, _source, last_at in _rows(conn, BUILT_ON_SQL, {
            "property_ids": sorted({g.survivor_id for g in groups})}):
        later.setdefault(int(pid), []).append(last_at)
    for g in groups:
        sides = [a for pid in (g.survivor_id, *g.retired_ids) for a in adverts.get(pid, ())]
        g.outside = sorted({lid for lid, _ct, inside in sides if not inside}
                           | set(g.moved_outside))
        g.category_types = sorted({ct for _lid, ct, _in in sides if ct})
        g.uncategorised = any(ct is None for _lid, ct, _in in sides)
        g.health = _health(g, status.get(g.survivor_id), later.get(g.survivor_id, ()))
        g.outcome = _outcome(g, categories)
        g.selected = g.outcome.startswith(TO_RETIRE)
    groups.sort(key=lambda g: (g.merged_at, g.merge_group_id), reverse=True)
    return groups


def _retire_one(
    conn: Any, group: LegacyGroup, area: Mapping[str, list[int]],
    categories: frozenset[str] | None, detach: Callable[..., dict[str, Any]], undone_by: str,
) -> None:
    """One group, one transaction: re-checked over its locked properties, then every advert it
    moved detached back to where that merge took it from; any refusal rolls the group back."""
    back = reactivated = 0
    try:
        with conn.transaction():
            _rows(conn, S.LOCK_PROPERTIES_SQL,
                  {"property_ids": sorted({group.survivor_id, *group.retired_ids})})
            fresh = read_groups(conn, area, categories, group.merge_group_id)
            if not fresh:
                raise _Refused("no_longer_live")
            if not fresh[0].selected:
                raise _Refused(fresh[0].outcome.removeprefix("skipped:"))
            if fresh[0].moved != group.moved:
                raise _Refused("changed_since_selection")
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


def _counts(groups: list[LegacyGroup]) -> dict[str, Any]:
    by_moved = [g for g in groups if g.touches_by_moved]
    inside = [g for g in groups if g.health == INTACT and not g.outside]
    chosen = [g for g in groups if g.selected]
    return {
        "groups_touching": len(groups),
        "touching_only_through_survivor_side": len(groups) - len(by_moved),
        # The W0 probe's own numbers (r1c_live_group_health, r1_merge_groups), re-read now.
        "probe_r1c_by_health": dict(sorted(Counter(g.health for g in by_moved).items())),
        "probe_r1_area": {"inside": sum(not g.moved_outside for g in by_moved),
                          "straddles": sum(bool(g.moved_outside) for g in by_moved)},
        # Blocks only, every deal type: the probe's grain, beside the deal-type-filtered set.
        "intact_both_sides_inside": len(inside),
        "intact_both_sides_inside_by_category_type": dict(sorted(Counter(
            g.category_label for g in inside).items())),
        "intact_straddling": sum(g.health == INTACT and bool(g.outside) for g in groups),
        "retire_set": len(chosen),
        "retire_set_mixed_deal_type": sum(g.mixed for g in chosen),
        "outside_scope_categories_by_category_type": dict(sorted(Counter(
            g.category_label for g in groups
            if g.outcome == f"skipped:{OUTSIDE_CATEGORIES}").items())),
        "outcomes": dict(sorted(Counter(g.outcome for g in groups).items())),
        "listings_moved_back": sum(g.listings_moved_back for g in groups),
        "properties_reactivated": sum(g.reactivated for g in groups),
    }


def retire_legacy(
    conn: Any, blocks: Iterable[str] | None, *, category_types: Iterable[str] | None,
    dry_run: bool, run_id: str, closed: Callable[[Any], str | None] | None = None,
    detach: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Select, then (live) undo, the intact legacy groups inside `blocks` and the scope's deal
    types. `closed` is the scope row's re-read before every group (E39); a dry run writes
    nothing."""
    area = area_params(blocks)
    categories = frozenset(category_types) if category_types is not None else None
    detach = detach or detach_listing
    undone_by = f"{RETIRE_BY}:{run_id}"
    groups = read_groups(conn, area, categories)
    result: dict[str, Any] = {
        "run_id": run_id, "dry_run": dry_run, "blocks": sorted(blocks or ()),
        "category_types": sorted(categories) if categories is not None else None,
        "undone_by": undone_by}
    todo = [g for g in groups if g.selected]
    if dry_run:
        for g in todo:
            g.outcome = "would_retire" + g.outcome.removeprefix(TO_RETIRE)
        return _finish(result, groups)
    current: LegacyGroup | None = None
    try:
        for index, group in enumerate(todo):
            reason = closed(conn) if closed else None
            if reason:
                result["stopped"] = f"the apply scope admits no live merge: {reason}"
                for g in todo[index:]:
                    g.outcome = "not_attempted"
                break
            current = group
            _retire_one(conn, group, area, categories, detach, undone_by)
            current = None
    except BaseException as exc:
        if current is not None:
            current.outcome = "failed"
        for g in todo:
            if g.outcome.startswith(TO_RETIRE):
                g.outcome = "not_attempted"
        result["aborted"] = f"{type(exc).__name__}: {exc}"
        try:
            setattr(exc, PARTIAL_ATTR, _finish(result, groups))
        except Exception:  # noqa: BLE001 - an error that takes no attribute still surfaces
            pass
        raise
    return _finish(result, groups)


def _finish(result: dict[str, Any], groups: list[LegacyGroup]) -> dict[str, Any]:
    return {**result, "counts": _counts(groups), "groups": [g.to_json() for g in groups]}


def summary_markdown(result: Mapping[str, Any]) -> str:
    counts = result.get("counts") or {}
    head = "DRY RUN - nothing undone" if result.get("dry_run") else "LIVE"
    lines = [f"## autodedup apply - legacy retire (A2, temporary) - {head}", "",
             f"Blocks: {' '.join(result.get('blocks') or [])}; deal types: "
             f"{' '.join(result.get('category_types') or ['all'])} (a group mixing deal types "
             f"is retired whatever they are); stamp `{result.get('undone_by')}`.", ""]
    if result.get("dry_run"):
        lines += ["The engine's dry-run plan below reads the old engine's merges as they "
                  "stand; the live run plans after they are undone.", ""]
    for key in ("stopped", "aborted"):
        if result.get(key):
            lines += [f"**{key.capitalize()}:** {result[key]}", ""]
    lines += ["| count | n |", "| --- | --- |"]
    for key, value in counts.items():
        if isinstance(value, dict):
            value = ", ".join(f"{k}={v}" for k, v in value.items()) or "-"
        lines.append(f"| {key} | {value} |")
    rows = [g for g in result.get("groups") or [] if not g["outcome"].startswith("skipped:")]
    rows += [g for g in result.get("groups") or [] if g["outcome"].startswith("skipped:")]
    lines += ["", "| group | merged | survivor | retired | adverts | outcome |",
              "| --- | --- | --- | --- | --- | --- |"]
    for g in rows[:SUMMARY_ROWS]:
        lines.append(f"| {g['merge_group_id']} | {g['merged_at']} | {g['survivor_id']} | "
                     f"{' '.join(str(r) for r in g['retired_ids'])} | {len(g['moved'])} | "
                     f"{g['outcome']} |")
    if len(rows) > SUMMARY_ROWS:
        lines.append(f"| ... {len(rows) - SUMMARY_ROWS} more in {'/'.join(ARTIFACT)} "
                     "| | | | | |")
    return "\n".join(lines) + "\n"


def _publish(out_dir: Path, result: dict[str, Any]) -> None:
    write_json(Path(out_dir).joinpath(*ARTIFACT), result)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(summary_markdown(result))
        except OSError:
            pass


def run(
    conn: Any, blocks: Iterable[str] | None, *, category_types: Iterable[str] | None,
    dry_run: bool, run_id: str, out_dir: Path, closed: Callable[[Any], str | None] | None = None,
) -> dict[str, Any]:
    """The apply mode's pre-step: retire, publish the artifact and the step summary (a crash
    included), and return the counts the apply result carries."""
    try:
        area_params(blocks)
    except ValueError as exc:
        raise SystemExit(f"retire_legacy: {exc}") from exc
    try:
        result = retire_legacy(conn, blocks or (), category_types=category_types,
                               dry_run=dry_run, run_id=run_id, closed=closed)
    except BaseException as exc:
        partial = getattr(exc, PARTIAL_ATTR, None)
        if isinstance(partial, dict):
            _publish(out_dir, partial)
        raise
    _publish(out_dir, result)
    return {key: result[key] for key in (
        "counts", "stopped", "aborted", "undone_by", "category_types") if key in result}
