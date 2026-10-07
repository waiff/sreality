"""Two writers for the canonical `properties` parent (rule 15, decisions 8 and 17).

`merge_property_set` merges an active set into its oldest record under one lock and one gate,
carrying every property-anchored operator-state row through
`toolkit.property_carriers.PROPERTY_CARRIERS` and writing what it carried to the carry record
(migration 589); `detach_listings` moves a set of adverts back to their ledger origins, or to new
records through the one birth path (`_born`), under one lock taken up front, and routes the
curation they take (`property_carriers.curation_plan` / `route_curation`). Both finish with
`properties_changed` once per call (rollup, Browse row, broker queue). Callers: `api.property_merge`
and `toolkit.property_split` (the operator), `autodedup.apply` and `autodedup.reconcile` (merge,
inside `app_settings.autodedup_apply_scope`), `autodedup.apply.unapply` and
`autodedup.legacy_retire` (detach, one call per group). An operator merge is also a ruling (MS12:
"same" over the canonical pairs and every negative between its members); a detach never rules,
and a split writes its own "different" across letters (`toolkit.property_split`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from collections import Counter
from typing import Any, Collection, Iterable, Mapping, NamedTuple, Sequence

import psycopg
from psycopg.types.json import Jsonb

from autodedup import ui_sql as usql
from scraper.db import create_singleton_properties
from scripts.recompute_property_stats import properties_changed
from toolkit import property_carriers as carriers
from toolkit.property_carriers import MergeSource, Route  # re-exported: callers read them here
from toolkit.room_taxonomy import category_main_compatible

# The `detach_listings` outcomes that move the advert: back to its origin, or to a new record
# (a native advert, or a merged one the caller names in `new`).
MOVED = frozenset({"detached", "split_native", "split_new"})


class MergeError(ValueError):
    """A merge/detach precondition failed (e.g. a property is already merged)."""


class CategoryClash(MergeError):
    """Rule 15's refusal: two members differ on `field` (category_type or category_main); the
    merge's gate also names the two properties and an ad of each (`properties`, `ads`)."""

    def __init__(self, field: str, a: str | None, b: str | None, *,
                 properties: tuple[int, int] | None = None,
                 ads: tuple[int, int] | None = None) -> None:
        super().__init__(f"{field} mismatch ({a} vs {b}); refusing to merge")
        self.field, self.a, self.b = field, a, b
        self.properties, self.ads = properties, ads


class Hop(NamedTuple):
    """One live ledger row a detach undoes, oldest first."""

    event_id: int
    group: str
    survivor: int
    prev: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# A merged-away property (status <> 'active', merged_into set) still satisfies a
# `references properties(id)` FK, so a write keyed on a stale property_id lands on
# the retired row and never appears on the survivor the operator sees (the merge
# reconciler only re-points state that existed AT merge time — rule #18). Follow
# merged_into to the live survivor before any property-anchored write. Transitive
# (a survivor can itself later merge) with a depth guard against a malformed cycle.
_RESOLVE_SURVIVORS_SQL = """
WITH RECURSIVE chain(root, id, status, merged_into, depth) AS (
    SELECT p.id, p.id, p.status, p.merged_into, 0
    FROM properties p WHERE p.id = ANY(%(ids)s)
    UNION ALL
    SELECT c.root, p.id, p.status, p.merged_into, c.depth + 1
    FROM properties p JOIN chain c ON p.id = c.merged_into
    WHERE c.status <> 'active' AND c.depth < 20
)
SELECT root, id FROM chain WHERE status = 'active'
"""

_LOCK_SET_SQL = """
SELECT id, status, first_seen_at
FROM properties WHERE id = ANY(%(ids)s::bigint[])
ORDER BY id
FOR UPDATE
"""

# A contentless record (no price, area or layout and a blank text: an index sighting whose page
# was never read, stored under the default category), as `autodedup.category_splits.contentless`
# says; the TOASTed description is read only for an ad that lacks the other three.
_CONTENTLESS = """CASE WHEN l.price_czk IS NULL AND l.area_m2 IS NULL AND l.disposition IS NULL
     THEN coalesce(l.description, '') !~ '[^[:space:]]' ELSE false END"""

# Rule 15's gate reads ads: one row per (member, deal type, category) among its contentful ads,
# with the lowest such ad; a contentless record never counts.
_SET_ADS_SQL = f"""
SELECT l.property_id, l.category_type, l.category_main, min(l.id)
FROM listings l
WHERE l.property_id = ANY(%(ids)s::bigint[]) AND NOT {_CONTENTLESS}
GROUP BY 1, 2, 3
ORDER BY 1, 4
"""

# One ledger row per advert the retired property holds; `listing_id` is the LEGACY sreality_id,
# a detach reads `listing_ref_id` (mig 323). Before the re-point: it selects what that moves.
_LEDGER_SQL = """
INSERT INTO property_merge_events
    (merge_group_id, survivor_property_id, retired_property_id,
     listing_id, listing_ref_id, prev_property_id, reason,
     confidence, markers, source)
SELECT %(group)s, %(survivor)s, %(retired)s,
       l.sreality_id, l.id, %(retired)s, %(reason)s,
       %(confidence)s, %(markers)s, %(source)s
FROM listings l
WHERE l.property_id = %(retired)s
"""

_REPOINT_SQL = "UPDATE listings SET property_id = %s WHERE property_id = %s"

# One carry record row per curation row a step moved or folded (migration 589, MS14).
_CARRY_SQL = """
INSERT INTO property_merge_carries (merge_group_id, table_name, row_key, row_at, account_id,
    from_property_id, to_property_id, kind, snapshot)
VALUES (%(group)s, %(table)s, %(row_key)s, %(row_at)s, %(account_id)s, %(from_property)s,
        %(survivor)s, %(kind)s, %(snapshot)s)
"""

# One statement touching `status` and `is_active` together, so the status-event trigger
# (migration 559) sees a retirement, not a plain is_active flip; the last write of a pair.
_RETIRE_SQL = """
UPDATE properties
SET status = 'merged_away', merged_into = %s,
    merged_at = now(), is_active = false
WHERE id = %s
"""

# The compare-and-set every advert move is: only if it still sits where it was read.
_MOVE_ADVERT_SQL = "UPDATE listings SET property_id = %s WHERE id = %s AND property_id = %s"

_UNDO_SQL = (
    "UPDATE property_merge_events SET undone_at = now(), undone_by = %s "
    "WHERE id = ANY(%s) AND undone_at IS NULL"
)

# Every advert of these properties, the member each sits on.
_MEMBER_ADS_SQL = """
SELECT id, property_id FROM listings WHERE property_id = ANY(%(ids)s::bigint[]) ORDER BY id
"""

# Each set ruling's newest word per set (its members sorted, as `apply.Negatives` reads them)
# among these adverts: a negative one spanning two members is what a merge of them takes back.
_NEGATIVE_SETS_SQL = """
SELECT DISTINCT ON (m.ids) v.cluster_key, v.generation, m.ids, v.verdict
FROM autodedup.verdicts v
CROSS JOIN LATERAL (SELECT ARRAY(SELECT DISTINCT x FROM unnest(v.member_ids) x ORDER BY x)
                    AS ids) m
WHERE v.kind = 'cluster' AND v.member_ids <@ %(ids)s::bigint[]
ORDER BY m.ids, v.decided_at DESC, v.id DESC
"""

# What a split's preview reads of each advert: rule 15's categories, whether it is contentless
# (as `_SET_ADS_SQL` counts it), and when it was first seen, which dates the property a letter
# lands on once the recompute has run (the join keeps the oldest).
_AD_CATEGORIES_SQL = f"""
SELECT l.id, l.category_type, l.category_main, l.first_seen_at, {_CONTENTLESS}
FROM listings l WHERE l.id = ANY(%(ids)s::bigint[])
"""

# The advert each property's Browse card shows, while still its child: the card the operator ticked.
_CANONICAL_ADVERTS_SQL = """
SELECT p.repr_listing_ref_id FROM properties p
JOIN listings l ON l.id = p.repr_listing_ref_id AND l.property_id = p.id
WHERE p.id = ANY(%(ids)s::bigint[])
"""

_PLACES_SQL = "SELECT id, property_id FROM listings WHERE id = ANY(%(ids)s::bigint[])"

# Each property's adverts, and its own among them: no standing merge moved them there.
_SIZES_SQL = """
SELECT l.property_id, count(*), count(*) FILTER (WHERE NOT EXISTS (
         SELECT 1 FROM property_merge_events e
         WHERE e.listing_ref_id = l.id AND e.undone_at IS NULL))
FROM listings l WHERE l.property_id = ANY(%(ids)s::bigint[])
GROUP BY l.property_id
"""

# A birth's ONE ledger row: the grouping that put the advert where it leaves from, written as
# the merge it amounts to (the new record `born` merged into the property the advert leaves) and
# closed as undone, so the advert's origin is its new record and the row replays like any
# detached merge. `reason`: 'ingest_grouping' for a native advert (no merge ever recorded it),
# 'split_new' for a merged one whose own ledger rows the birth closed.
_INGEST_GROUPING_SQL = """
INSERT INTO property_merge_events
    (merge_group_id, survivor_property_id, retired_property_id, listing_id, listing_ref_id,
     prev_property_id, reason, source, undone_at, undone_by)
SELECT %(group)s, %(left)s, %(born)s, l.sreality_id, l.id, %(born)s, %(reason)s,
       'operator', now(), %(by)s
FROM listings l WHERE l.id = %(listing)s
RETURNING id
"""

_STATUS_SQL = """
SELECT id, status, merged_into FROM properties WHERE id = ANY(%(ids)s::bigint[]) ORDER BY id
"""

# Every live ledger row of these adverts, oldest first: the first row's `prev_property_id` is the
# advert's ORIGIN (where it sat before every merge that still stands), the last row's survivor
# where the newest merge put it.
_LIVE_MOVES_SQL = """
SELECT e.listing_ref_id, e.id, e.merge_group_id::text, e.survivor_property_id,
       e.prev_property_id, e.source, e.created_at
FROM property_merge_events e
WHERE e.listing_ref_id = ANY(%(ids)s::bigint[]) AND e.undone_at IS NULL
ORDER BY e.listing_ref_id, e.id
"""

# One statement, so the status-event trigger (migration 559) logs only where history disagrees.
_REACTIVATE_SQL = """
UPDATE properties p
SET status = 'active', merged_into = NULL, merged_at = NULL,
    is_active = EXISTS (
      SELECT 1 FROM listings l WHERE l.property_id = p.id AND l.is_active)
WHERE p.id = %(pid)s AND p.status = 'merged_away'
"""


def resolve_active_property_ids(
    conn: "psycopg.Connection", ids: list[int],
) -> dict[int, int]:
    """Map each property_id to the id of its active merge survivor.

    An already-active id maps to itself; a merged-away id follows merged_into to
    the surviving active property; an id with no active survivor (missing row or a
    broken chain) is absent from the result. Read-only; safe inside a caller txn.
    """
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(_RESOLVE_SURVIVORS_SQL, {"ids": list(ids)})
        return {int(root): int(sid) for root, sid in cur.fetchall()}


def resolve_active_property_id(
    conn: "psycopg.Connection", property_id: int,
) -> int | None:
    """Single-id `resolve_active_property_ids`; None if no active survivor."""
    return resolve_active_property_ids(conn, [property_id]).get(property_id)


def lock_properties(conn: psycopg.Connection, ids: list[int]) -> dict[int, tuple[str, int | None]]:
    """Row-lock these properties in id order, the order every writer locks in; (status,
    merged_into) per id."""
    with conn.cursor() as cur:
        cur.execute(_STATUS_SQL + " FOR UPDATE", {"ids": sorted({int(i) for i in ids})})
        return {int(r[0]): (r[1], r[2]) for r in cur.fetchall()}


def survivor_of(first_seen: Mapping[int, datetime | None]) -> int:
    """Decision 17: the oldest record (`first_seen_at`, unknown last), then the lowest id."""
    return min(first_seen, key=lambda pid: (first_seen[pid] is None, first_seen[pid] or 0, pid))


def listing_places(conn: psycopg.Connection, listing_ids: list[int]) -> dict[int, int | None]:
    """Where each advert sits now (None: on no property); an unknown id is absent."""
    with conn.cursor() as cur:
        cur.execute(_PLACES_SQL, {"ids": sorted({int(i) for i in listing_ids})})
        return {int(lid): int(pid) if pid is not None else None for lid, pid in cur.fetchall()}


def record_ruling(
    conn: psycopg.Connection,
    listing_lo: int,
    listing_hi: int,
    *,
    verdict: str,
    decided_by: str,
    note: str | None,
    reasons: Sequence[str] = (),
    veto_reason: str | None = None,
) -> tuple[Any, ...] | None:
    """THE pair-ruling writer: appends the ruling when it changes the pair's newest word
    (migration 574) and mirrors it into the operator's must-not-link (a negative upserts it with
    `veto_reason`, else the note; anything else retracts it). A bare operator veto is first
    written down as the `different` ruling it is, so the new word never erases it. Returns the
    pair's newest row in `usql.VERDICT_COLUMNS` order."""
    with conn.cursor() as cur:
        cur.execute(usql.VERDICT_PAIR_FROM_VETO_SQL,
                    {"listing_lo": listing_lo, "listing_hi": listing_hi})
        cur.execute(usql.VERDICT_PAIR_APPEND_SQL, {
            "listing_lo": listing_lo, "listing_hi": listing_hi, "verdict": verdict,
            "note": note, "reasons": list(reasons), "decided_by": decided_by})
        stored = cur.fetchone()
        if verdict in usql.NEGATIVE_VERDICTS:
            cur.execute(usql.MUST_NOT_LINK_UPSERT_SQL, {
                "listing_lo": listing_lo, "listing_hi": listing_hi,
                "reason": veto_reason or note or f"operator: {verdict}"})
        else:
            cur.execute(usql.MUST_NOT_LINK_RETRACT_SQL, {
                "listing_lo": listing_lo, "listing_hi": listing_hi})
    return stored


def record_rulings(
    conn: psycopg.Connection,
    pairs: set[tuple[int, int]],
    *,
    verdict: str,
    decided_by: str,
    note: str | None,
    reasons: Sequence[str] = (),
) -> int:
    """Rule each (lo, hi) listings.id pair through `record_ruling`; returns the count."""
    if verdict not in usql.VERDICT_VALUES:
        raise ValueError(f"not a pair verdict: {verdict!r}")
    ordered = sorted(pairs)
    for lo, hi in ordered:
        record_ruling(conn, lo, hi, verdict=verdict, decided_by=decided_by, note=note,
                      reasons=reasons)
    return len(ordered)


def category_clash(
    a: tuple[str | None, str | None], b: tuple[str | None, str | None],
) -> tuple[str, str | None, str | None] | None:
    """Rule 15's gate on two (category_type, category_main): the field that makes them two
    properties and its two values — sale != rent, flat != house, except the sanctioned pairs of
    `room_taxonomy.category_main_compatible` (dum–komercni, pozemek with either, byt–komercni)
    — or None. NULL = unknown, not a conflict. The pairs are not transitive, so a set is read
    pair by pair (`_gate_ads`). The chokepoint and the verdict route (E925) read this one
    definition."""
    if a[0] is not None and b[0] is not None and a[0] != b[0]:
        return ("category_type", a[0], b[0])
    if not category_main_compatible(a[1], b[1]):
        return ("category_main", a[1], b[1])
    return None


def _canonical_pairs(conn: psycopg.Connection, property_ids: list[int]) -> set[tuple[int, int]]:
    """Every (lo, hi) pair of the properties' canonical adverts."""
    with conn.cursor() as cur:
        cur.execute(_CANONICAL_ADVERTS_SQL, {"ids": sorted(set(property_ids))})
        ids = sorted({int(r[0]) for r in cur.fetchall()})
    return {(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]}


def adverts_on(conn: psycopg.Connection, property_ids: Iterable[int]) -> dict[int, list[int]]:
    """Each property's adverts, ascending (an empty list for one that holds none)."""
    ids = sorted({int(p) for p in property_ids})
    out: dict[int, list[int]] = {pid: [] for pid in ids}
    with conn.cursor() as cur:
        cur.execute(_MEMBER_ADS_SQL, {"ids": ids})
        for lid, pid in cur.fetchall():
            out.setdefault(int(pid), []).append(int(lid))
    return out


def negative_sets(
    conn: psycopg.Connection, listing_ids: Iterable[int],
) -> list[tuple[int, str | None, list[int]]]:
    """Each set ruling among these adverts whose newest word is negative, as (cluster_key,
    generation, member_ids)."""
    ids = sorted({int(i) for i in listing_ids})
    if len(ids) < 2:
        return []
    with conn.cursor() as cur:
        cur.execute(_NEGATIVE_SETS_SQL, {"ids": ids})
        rows = cur.fetchall()
    return [(int(key), gen, [int(i) for i in members]) for key, gen, members, verdict in rows
            if verdict in usql.NEGATIVE_VERDICTS]


def negatives_across(
    conn: psycopg.Connection, member: Mapping[int, Any],
) -> tuple[set[tuple[int, int]], list[tuple[int, str | None, list[int]]]]:
    """The negatives a merge of `member`'s members (advert -> its member) takes back (MS12): each
    pair of two members whose newest ruling is negative, and each negative set ruling spanning
    two or more members. A negative inside one member is not between what the merge joins."""
    # imported here: toolkit.property_split imports this module
    from toolkit.property_split import newest_pair_rulings

    if len(member) < 2:
        return set(), []
    stored = newest_pair_rulings(conn, member)
    pairs = {p for p, v in stored.items() if v["verdict"] in usql.NEGATIVE_VERDICTS
             and member[p[0]] != member[p[1]]}
    sets = [s for s in negative_sets(conn, member) if len({member[i] for i in s[2]}) >= 2]
    return pairs, sets


def ad_categories(
    conn: psycopg.Connection, listing_ids: Iterable[int],
) -> dict[int, tuple[str | None, str | None, datetime | None, bool]]:
    """Each advert's (category_type, category_main, first_seen_at, contentless), as rule 15's
    gate counts an advert (`_SET_ADS_SQL`)."""
    with conn.cursor() as cur:
        cur.execute(_AD_CATEGORIES_SQL, {"ids": sorted({int(i) for i in listing_ids})})
        return {int(r[0]): (r[1], r[2], r[3], bool(r[4])) for r in cur.fetchall()}


def merge_rulings(
    conn: psycopg.Connection, ids: list[int],
) -> tuple[set[tuple[int, int]], list[tuple[int, str | None, list[int]]], int]:
    """What an operator merge of `ids` rules "same" (MS12), read before it moves anything: the
    canonical pairs plus every negative between two members (`negatives_across`); with the
    count of "different" rulings that takes back."""
    member = {lid: pid for pid, lids in adverts_on(conn, ids).items() for lid in lids}
    pairs, sets = negatives_across(conn, member)
    return _canonical_pairs(conn, ids) | pairs, sets, len(pairs) + len(sets)


def merge_preview(conn: psycopg.Connection, ids: list[int]) -> dict[str, Any]:
    """The count an operator merge of `ids` would take back, before the click (MS12); reads only."""
    property_ids = sorted({int(p) for p in ids})
    return {"property_ids": property_ids,
            "rulings_taken_back": merge_rulings(conn, property_ids)[2]}


def _gate_set(rows: Mapping[int, tuple], ids: list[int]) -> None:
    """The set's refusal read under its lock: a member missing or not active."""
    missing = [pid for pid in ids if pid not in rows]
    inactive = [pid for pid in ids if pid in rows and rows[pid][1] != "active"]
    if missing or inactive:
        raise MergeError(f"properties not found {missing} or not active {inactive}")


def first_clash(
    ads: Sequence[tuple[Any, tuple[str | None, str | None], int]],
) -> tuple[tuple[str, str | None, str | None], tuple[Any, Any], tuple[int, int]] | None:
    """Rule 15 over (member, (category_type, category_main), advert) rows of contentful ads: the
    first pair of two members `category_clash` refuses that no member already holds together
    (that pair is the member's, not a merge's), as (clash, members, adverts); or None."""
    held = {frozenset((a[1], b[1])) for a in ads for b in ads if a[0] == b[0]}
    for i, (pa, ca, la) in enumerate(ads):
        for pb, cb, lb in ads[i + 1:]:
            if pa != pb and frozenset((ca, cb)) not in held and (clash := category_clash(ca, cb)):
                return clash, (pa, pb), (la, lb)
    return None


def _gate_ads(conn: psycopg.Connection, ids: list[int]) -> None:
    """Rule 15 over the set's ads (the operator, 2026-10-06): refused when the merge would newly
    put on one property two contentful ads `category_clash` refuses (`first_clash`). A
    contentless record never counts; stored categories do not gate."""
    with conn.cursor() as cur:
        cur.execute(_SET_ADS_SQL, {"ids": ids})
        ads = [(int(pid), (kind, main), int(lid)) for pid, kind, main, lid in cur.fetchall()]
    if found := first_clash(ads):
        clash, members, adverts = found
        raise CategoryClash(*clash, properties=members, ads=adverts)


def _merge_pair(cur: psycopg.Cursor, step: carriers.MergeStep, *, reason: str,
                confidence: float | None, markers: dict[str, Any] | None) -> int:
    """One retired property into the survivor under the set's lock and gate: the ledger, the
    re-point, every carrier in order, the carry record of what they moved or folded, the retire.
    Returns the ledger rows written."""
    cur.execute(_LEDGER_SQL, {
        "group": step.group, "survivor": step.survivor, "retired": step.retired,
        "reason": reason, "confidence": confidence,
        "markers": Jsonb(markers) if markers is not None else None,
        "source": step.source,
    })
    moved = cur.rowcount or 0
    cur.execute(_REPOINT_SQL, (step.survivor, step.retired))
    carried = [row for carrier in carriers.PROPERTY_CARRIERS
               for row in carrier.on_merge(cur, step)]
    if carried:
        cur.executemany(_CARRY_SQL, [
            {**row._asdict(), "group": step.group, "survivor": step.survivor,
             "snapshot": None if row.snapshot is None else Jsonb(row.snapshot)}
            for row in carried])
    cur.execute(_RETIRE_SQL, (step.survivor, step.retired))
    return moved


def merge_property_set(
    conn: psycopg.Connection,
    property_ids: list[int],
    *,
    source: MergeSource,
    reason: str,
    merge_group_id: str | None = None,
    confidence: float | None = None,
    markers: dict[str, Any] | None = None,
    decided_by: str | None = None,
) -> dict[str, Any]:
    """Merge an active SET into its oldest record under ONE group, in one transaction: one lock
    over every member in id order, one gate (`_gate_set`, then `_gate_ads`), then each retired
    property through `_merge_pair`. `source='operator'` is a "same" ruling (MS12,
    `merge_rulings`): the canonical pairs and every negative pair between two members, then each
    negative set spanning two members, counting the "different" rulings it took back
    (`rulings_taken_back`); then `properties_changed` once over the survivor and the retired."""
    ids = sorted({int(p) for p in property_ids})
    if len(ids) < 2:
        raise MergeError("need at least two distinct properties")
    if source == "operator" and not decided_by:
        raise MergeError("an operator merge is a ruling: decided_by is required")
    group = merge_group_id or str(uuid.uuid4())

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(_LOCK_SET_SQL, {"ids": ids})
            rows = {int(r[0]): r for r in cur.fetchall()}
        _gate_set(rows, ids)
        _gate_ads(conn, ids)
        survivor = survivor_of({pid: rows[pid][2] for pid in ids})
        retired = [pid for pid in ids if pid != survivor]
        pairs, sets, taken_back = (merge_rulings(conn, ids) if source == "operator"
                                    else (set(), [], 0))
        moved = 0
        with conn.cursor() as cur:
            for rid in retired:
                moved += _merge_pair(
                    cur, carriers.MergeStep(survivor, rid, group, source), reason=reason,
                    confidence=confidence, markers=markers,
                )
        ruled = record_rulings(
            conn, pairs, verdict="same", decided_by=str(decided_by),
            note=f"operator merge {group}",
        ) if pairs else 0
        if sets:
            with conn.cursor() as cur:
                cur.executemany(usql.VERDICT_CLUSTER_APPEND_SQL, [{
                    "cluster_key": key, "generation": gen, "member_ids": members,
                    "verdict": "same", "note": f"operator merge {group}", "reasons": [],
                    "decided_by": str(decided_by)} for key, gen, members in sets])
        properties_changed(conn, [survivor, *retired])

    return {
        "data": {
            "merge_group_id": group,
            "survivor_id": survivor,
            "retired_ids": retired,
            "listings_moved": moved,
            "pairs_ruled_same": ruled,
            "rulings_taken_back": taken_back,
        },
        "metadata": {
            "tool": "merge_property_set",
            "reason": reason,
            "source": source,
            "queried_at": _now_iso(),
        },
    }


def listing_origins(
    conn: psycopg.Connection, listing_ids: list[int],
) -> dict[int, tuple[int, str, datetime]]:
    """Each advert's ORIGIN, where a detach returns it, with the source and time of the merge
    that took it from there; one no standing merge moved is absent."""
    if not listing_ids:
        return {}
    out: dict[int, tuple[int, str, datetime]] = {}
    with conn.cursor() as cur:
        cur.execute(_LIVE_MOVES_SQL, {"ids": sorted({int(i) for i in listing_ids})})
        for lid, _id, _group, _survivor, prev, source, at in cur.fetchall():
            out.setdefault(int(lid), (int(prev), source, at))
    return out


def _detach_plan(
    current: int | None, moves: list[tuple], merge_group_id: str | None,
    size: tuple[int, int],
) -> tuple[str, list[tuple], int | None]:
    """(outcome, the ledger rows it undoes, where the advert goes; None = a record not yet born).
    A move is one live ledger row, oldest first: (id, merge_group_id, survivor_property_id,
    prev_property_id); `size` = (adverts, own adverts) on the advert's property. A native advert
    leaves only while another own advert stays: the merged ones go home, never leaving a
    property with no advert of its own (`last_native`)."""
    if not moves:
        if merge_group_id is not None or size[0] < 2:
            return "not_merged", [], current
        if size[1] < 2:
            return "last_native", [], current
        return "split_native", [], None
    if merge_group_id is not None:
        last = moves[-1]
        if last[1] != merge_group_id:
            live = any(m[1] == merge_group_id for m in moves)
            return ("moved_on" if live else "not_merged"), [], current
        undo, target = [last], int(last[3])
    else:
        undo, target = list(moves), int(moves[0][3])
    if current != int(moves[-1][2]):
        return "moved_since", [], current
    if target == current:
        return "on_origin", [], current
    return "detached", undo, target


def _origin_gone(state: tuple[str, int | None] | None, undo: list[tuple]) -> bool:
    """The origin was retired since into another property by a merge that still stands: bringing
    it back would half-undo that merge, so the advert stays (`origin_moved_on`)."""
    return state is None or (state[0] != "active" and state[1] != int(undo[0][2]))


def _plan_detaches(
    conn: psycopg.Connection, listing_ids: list[int], merge_group_id: str | None,
) -> dict[int, tuple[int | None, str, list[tuple], int | None]]:
    """(current property, `_detach_plan`) per advert found, from unlocked reads."""
    places = listing_places(conn, listing_ids)
    moves: dict[int, list[tuple]] = {}
    sizes: dict[int, tuple[int, int]] = {}
    with conn.cursor() as cur:
        cur.execute(_LIVE_MOVES_SQL, {"ids": listing_ids})
        for row in cur.fetchall():
            moves.setdefault(int(row[0]), []).append(tuple(row[1:5]))
        native = sorted({pid for lid, pid in places.items()
                         if pid is not None and lid not in moves})
        if merge_group_id is None and native:
            cur.execute(_SIZES_SQL, {"ids": native})
            sizes = {int(pid): (int(n), int(own)) for pid, n, own in cur.fetchall()}
    return {lid: (at, *_detach_plan(at, moves.get(lid, []), merge_group_id,
                                    sizes.get(at, (0, 0))))
            for lid, at in places.items()}


def detach_outcomes(
    conn: psycopg.Connection, listing_ids: list[int], *, merge_group_id: str | None = None,
) -> dict[int, str]:
    """The `outcome` `detach_listings` would answer for each advert now, read-only: what a bulk
    undo's dry run reports. A native advert answers `split_native` while another own advert
    stays (it would be born a new record), else `last_native`."""
    ids = sorted({int(i) for i in listing_ids})
    if not ids:
        return {}
    plans = _plan_detaches(conn, ids, merge_group_id)
    with conn.cursor() as cur:
        cur.execute(_STATUS_SQL, {"ids": sorted(
            {t for _at, out, _u, t in plans.values() if out == "detached"})})
        state = {int(r[0]): (r[1], r[2]) for r in cur.fetchall()}
    return {lid: ("origin_moved_on" if outcome == "detached"
                  and _origin_gone(state.get(target), undo) else outcome)
            for lid, (_at, outcome, undo, target) in plans.items()}


class Landings(NamedTuple):
    """Where a split's letters land (MS18): the letter that keeps the property (its number and
    page), each leaving advert that goes back to its origin, and each born a new property."""

    staying: str
    origin: dict[int, int]
    new: frozenset[int]


def letter_landings(conn: psycopg.Connection, record: int, letters: Mapping[int, str]) -> Landings:
    """The split's rule, read: the letter holding most of the property's own adverts (no
    standing ledger row moved them) keeps it, the earliest on a tie, the canonical advert's
    letter when none holds one. A leaving advert goes back to its origin while that origin is
    merged away, its chain resolves to `record` and a detach would take it there; when two
    leaving letters came from one origin, the letter holding more of its adverts gets it, the
    earliest on a tie. Every other leaving advert is born a new property."""
    ads = sorted(int(a) for a in letters)
    moves: dict[int, list[tuple]] = {}
    with conn.cursor() as cur:
        cur.execute(_LIVE_MOVES_SQL, {"ids": ads})
        for row in cur.fetchall():
            moves.setdefault(int(row[0]), []).append(tuple(row[1:5]))
    own = Counter(letters[a] for a in ads if a not in moves)
    if own:
        staying = min(own, key=lambda letter: (-own[letter], letter))
    else:
        with conn.cursor() as cur:
            cur.execute(_CANONICAL_ADVERTS_SQL, {"ids": [record]})
            canonical = {int(r[0]) for r in cur.fetchall()}
        staying = next((letters[a] for a in ads if a in canonical), min(letters.values()))
    leaving = [a for a in ads if letters[a] != staying]
    homes = {a: int(moves[a][0][3]) for a in leaving if a in moves}
    state: dict[int, tuple[str, int | None]] = {}
    if homes:
        with conn.cursor() as cur:
            cur.execute(_STATUS_SQL, {"ids": sorted(set(homes.values()))})
            state = {int(r[0]): (r[1], r[2]) for r in cur.fetchall()}
    chain = resolve_active_property_ids(conn, sorted(set(homes.values())))
    fit = {a: o for a, o in homes.items()
           if int(moves[a][-1][2]) == record and o != record
           and (state.get(o) or ("",))[0] == "merged_away"
           and not _origin_gone(state.get(o), moves[a]) and chain.get(o) == record}
    held: dict[int, Counter] = {}
    for a, o in fit.items():
        held.setdefault(o, Counter())[letters[a]] += 1
    taker = {o: min(c, key=lambda letter: (-c[letter], letter)) for o, c in held.items()}
    origin = {a: o for a, o in fit.items() if taker[o] == letters[a]}
    return Landings(staying, origin, frozenset(a for a in leaving if a not in origin))


def _born(
    conn: psycopg.Connection, listing_id: int, current: int, *, decided_by: str, named: bool,
) -> tuple[str, list[tuple], int | None]:
    """An advert leaves for a NEW record born through THE birth path
    (`scraper.db.create_singleton_properties`): a native one while another own advert stays
    (`split_native`, re-planned under the lock), or one the caller names in `new`, whose live
    ledger rows close (`split_new` when it had any). ONE closed ledger row records the birth.
    The merge's lock order: the property, then the advert."""
    with conn.cursor() as cur:
        cur.execute(_STATUS_SQL + " FOR UPDATE", {"ids": [current]})
        at, outcome, _undo, _target = _plan_detaches(conn, [listing_id], None)[listing_id]
        if at != current:
            return "moved_since", [], current
        if not named and outcome != "split_native":
            return (outcome if outcome in ("not_merged", "last_native") else "moved_since",
                    [], current)
        cur.execute(_LIVE_MOVES_SQL, {"ids": [listing_id]})
        live = [tuple(row[1:5]) for row in cur.fetchall()]
        cur.execute(_MOVE_ADVERT_SQL, (None, listing_id, current))
        if not cur.rowcount:
            return "moved_since", [], current
        if live:
            cur.execute(_UNDO_SQL, (decided_by, [m[0] for m in live]))
    (born,) = create_singleton_properties(conn, [listing_id])
    group = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(_INGEST_GROUPING_SQL, {
            "group": group, "left": current, "born": born, "by": decided_by,
            "listing": listing_id, "reason": "split_new" if live else "ingest_grouping",
        })
        (row_id,) = cur.fetchone()
    return ("split_new" if live else "split_native",
            [*live, (int(row_id), group, current, born)], born)


class _Detached(NamedTuple):
    """One advert's answer inside a `detach_listings` call."""

    listing_id: int
    outcome: str
    left: int | None  # the property it sat on
    target: int | None  # where it went: its origin or a new record (`left` when it stayed)
    undo: tuple[Hop, ...]
    reactivated: bool


def _lock_set(
    conn: psycopg.Connection,
    plans: Mapping[int, tuple[int | None, str, list[tuple], int | None]],
    new: Collection[int],
) -> dict[int, tuple[str, int | None]]:
    """Every property the call's per-advert steps lock, taken up front in id order (the order
    every writer locks in): the advert's property and its origin for a detach, the property for
    a birth; their (status, merged_into). The steps' own locks then re-enter these."""
    ids: set[int] = set()
    for lid, (current, outcome, _undo, target) in plans.items():
        if current is None:
            continue
        if lid in new or outcome == "split_native":
            ids.add(int(current))
        elif outcome == "detached":
            ids |= {int(current), int(target)}
    return lock_properties(conn, sorted(ids)) if ids else {}


def _detach_one(
    conn: psycopg.Connection, listing_id: int, *, at: int | None, decided_by: str,
    merge_group_id: str | None, new: bool,
) -> _Detached:
    """ONE advert off its property, re-planned here so it sees the call's earlier adverts (a
    sibling born or gone home, an origin already reactivated); `at` is where the call's plan
    found it, so an advert moved before the call's lock stays (`moved_since`). No after-step:
    the set writer runs it once."""
    plan = _plan_detaches(conn, [listing_id], merge_group_id).get(listing_id)
    if plan is None:
        raise MergeError(f"listing {listing_id} not found")
    current, outcome, undo, target = plan
    if current != at:
        current, outcome, undo, target = at, "moved_since", [], at
    reactivated = False
    if current is not None and (new or outcome == "split_native"):
        outcome, undo, target = _born(conn, listing_id, int(current), decided_by=decided_by,
                                      named=new)
    elif outcome == "detached":
        # The merge's lock order, properties before the advert; the origin's state is read
        # under the lock, and the advert moves only if it still sits where the ledger was read.
        with conn.cursor() as cur:
            cur.execute(_STATUS_SQL + " FOR UPDATE", {"ids": sorted({current, target})})
            locked = {int(r[0]): (r[1], r[2]) for r in cur.fetchall()}
            if _origin_gone(locked.get(target), undo):
                outcome, undo, target = "origin_moved_on", [], current
            else:
                cur.execute(_MOVE_ADVERT_SQL, (target, listing_id, current))
                if not cur.rowcount:
                    outcome, undo, target = "moved_since", [], current
    hops = tuple(Hop(int(m[0]), str(m[1]), int(m[2]), int(m[3])) for m in undo)
    if outcome == "detached":
        with conn.cursor() as cur:
            cur.execute(_UNDO_SQL, (decided_by, [h.event_id for h in hops]))
            cur.execute(_REACTIVATE_SQL, {"pid": target})
            reactivated = (cur.rowcount or 0) == 1
    return _Detached(listing_id, outcome, current, target, hops, reactivated)


def detach_listings(
    conn: psycopg.Connection,
    listing_ids: Sequence[int],
    *,
    decided_by: str,
    reason: str | None = None,
    source: MergeSource = "operator",
    merge_group_id: str | None = None,
    new: Collection[int] = (),
    curation: Sequence[Route] | None = None,
) -> dict[str, Any]:
    """Split a SET of adverts off their properties, in the caller's order, in ONE transaction: a
    merged one back to its ORIGIN (with `merge_group_id`, to where it sat before that merge, only
    while it is the newest to move it); if that merge retired the origin it is reactivated. A
    native one (no standing merge moved it) while another own advert stays, and every advert
    named in `new`, go to a NEW record (`_born`). Every property the steps lock is locked first,
    in id order. The curation the adverts take is planned before anything moves (`curation`,
    else `property_carriers.curation_plan` per property left, along `merge_group_id`'s carry
    rows when given) and routed after (`route_curation`), counted in `curation`. Idempotent,
    each `outcome` saying why nothing moved. A detach writes no ruling (`rulings_written` stays
    0; `reason` is unused since a split writes its own); then `properties_changed` ONCE over
    every property left and reached. An unknown advert refuses the set before anything moves;
    an empty set is a no-op. Callers: `toolkit.property_split`; `autodedup.apply.unapply` and
    `autodedup.legacy_retire`, one call per group."""
    ids = list(dict.fromkeys(int(i) for i in listing_ids))
    born = {int(i) for i in new}
    done: list[_Detached] = []
    counts = {"carry_rows": 0, "note_moves": 0}
    if ids:
        with conn.transaction():
            plans = _plan_detaches(conn, ids, merge_group_id)
            if missing := [lid for lid in ids if lid not in plans]:
                raise MergeError(f"listing {missing[0]} not found")
            state = _lock_set(conn, plans, born)
            lefts: dict[int, dict[int, int | None]] = {}
            for lid in ids:
                at, outcome, undo, target = plans[lid]
                if at is not None and (lid in born or outcome == "split_native"):
                    lefts.setdefault(int(at), {})[lid] = None
                elif outcome == "detached" and not _origin_gone(state.get(target), undo):
                    lefts.setdefault(int(at), {})[lid] = int(target)
            if curation is not None and len(lefts) > 1:
                raise MergeError("planned curation routes the adverts of one property")
            with conn.cursor() as cur:
                routes = {left: (list(curation) if curation is not None else
                                 carriers.curation_plan(cur, left=left, movers=movers,
                                                        group=merge_group_id))
                          for left, movers in sorted(lefts.items())}
            done = [_detach_one(conn, lid, at=plans[lid][0], decided_by=decided_by,
                                merge_group_id=merge_group_id, new=lid in born) for lid in ids]
            moved = [d for d in done if d.outcome in MOVED]
            landed = {d.listing_id: int(d.target) for d in moved}
            with conn.cursor() as cur:
                for left, plan in routes.items():
                    routed = carriers.route_curation(cur, plan, left=left, landed=landed)
                    for key, n in routed.items():
                        counts[key] = counts.get(key, 0) + n
            properties_changed(conn, [p for d in moved for p in (d.left, d.target)])

    return {
        "data": {
            "adverts": [{
                "listing_id": d.listing_id,
                "detached": d.outcome in MOVED,
                "outcome": d.outcome,
                "left_property_id": d.left,
                "restored_property_id": d.target,
                "reactivated": d.reactivated,
                "merge_group_ids": sorted({h.group for h in d.undo}),
            } for d in done],
            "rulings_written": 0,
            "curation": counts,
        },
        "metadata": {
            "tool": "detach_listings",
            "source": source,
            "decided_by": decided_by,
            "queried_at": _now_iso(),
        },
    }
