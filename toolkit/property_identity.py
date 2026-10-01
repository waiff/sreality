"""Two writers for the canonical `properties` parent (rule 15, decisions 8 and 17).

`merge_property_set` merges an active set into its oldest record under one lock and one gate;
`detach_listing` moves one advert back to its ledger origin, or (the operator only) a native
advert to a new record through the one birth path — a group undo is a loop of it. Both carry
every property-anchored row through `toolkit.property_carriers.PROPERTY_CARRIERS`, then
recompute and patch Browse. Callers: `api.property_merge` and `toolkit.property_split` (the
operator), `autodedup.apply` and `autodedup.reconcile` (merge, inside
`app_settings.autodedup_apply_scope`), `autodedup.apply.unapply` and `autodedup.legacy_retire`
(detach). `source='operator'` is also a ruling (decision 8); an engine merge or undo never is.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import psycopg
from psycopg.types.json import Jsonb

from autodedup import ui_sql as usql
from scraper.db import create_singleton_properties
from scripts.recompute_property_stats import recompute_one
from toolkit import property_carriers as carriers
from toolkit.browse_read_model import sync_browse_list
from toolkit.property_carriers import MergeSource  # re-exported: callers read it from here
from toolkit.room_taxonomy import category_main_compatible

# The `detach_listing` outcomes that move the advert: back to its origin, or to a new record.
MOVED = frozenset({"detached", "split_native"})


class MergeError(ValueError):
    """A merge/detach precondition failed (e.g. a property is already merged)."""


class AssetLinkConflict(MergeError):
    """Two different asset links in one merge (the survivor could keep only one, decision 17),
    or two linked units in an engine merge (the operator's "different units", E903)."""


class CategoryClash(MergeError):
    """Rule 15's refusal: two members differ on `field` (category_type or category_main)."""

    def __init__(self, field: str, a: str | None, b: str | None) -> None:
        super().__init__(f"{field} mismatch ({a} vs {b}); refusing to merge")
        self.field, self.a, self.b = field, a, b


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
SELECT id, status, first_seen_at, asset_id, category_type, category_main
FROM properties WHERE id = ANY(%(ids)s::bigint[])
ORDER BY id
FOR UPDATE
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

_STAYING_SQL = "SELECT id FROM listings WHERE property_id = %s"

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

# A native split's ONE ledger row: the ingest-time grouping no merge ever recorded, written as
# the merge it amounts to (the new record `born` merged into the property the advert leaves)
# and closed as undone by the operator's split, so the advert's origin is its new record and
# the row replays like any detached merge.
_INGEST_GROUPING_SQL = """
INSERT INTO property_merge_events
    (merge_group_id, survivor_property_id, retired_property_id, listing_id, listing_ref_id,
     prev_property_id, reason, source, undone_at, undone_by)
SELECT %(group)s, %(left)s, %(born)s, l.sreality_id, l.id, %(born)s, 'ingest_grouping',
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


def must_not_link_rows(
    conn: psycopg.Connection, listing_ids: list[int],
) -> dict[tuple[int, int], tuple[str, str | None]]:
    """(source, reason) of every must-not-link row among these adverts, whatever wrote it."""
    ids = sorted({int(i) for i in listing_ids})
    if len(ids) < 2:
        return {}
    with conn.cursor() as cur:
        cur.execute(usql.MUST_NOT_LINK_PAIRS_SQL, {"ids": ids})
        return {(int(lo), int(hi)): (str(src), reason) for lo, hi, src, reason in cur.fetchall()}


def restore_must_not_link(
    conn: psycopg.Connection, rows: Mapping[tuple[int, int], tuple[str, str | None]],
) -> int:
    """Put these must-not-link rows back as a pre-read found them, source and all, writing only
    those that differ now; returns the count. `record_rulings` makes every negative the
    operator's, so a split that crossed a machine veto (E919) hands it back through here."""
    if not rows:
        return 0
    now = must_not_link_rows(conn, [i for pair in rows for i in pair])
    put = sorted((p, row) for p, row in rows.items() if now.get(p) != (row[0], row[1]))
    with conn.cursor() as cur:
        cur.executemany(usql.MUST_NOT_LINK_RESTORE_SQL, [
            {"listing_lo": lo, "listing_hi": hi, "source": src, "reason": reason}
            for (lo, hi), (src, reason) in put
        ])
    return len(put)


def category_clash(
    a: tuple[str | None, str | None], b: tuple[str | None, str | None],
) -> tuple[str, str | None, str | None] | None:
    """Rule 15's gate on two (category_type, category_main): the field that makes them two
    properties and its two values — sale != rent, flat != house, except the one sanctioned
    dum <-> komercni — or None. NULL = unknown, not a conflict. The chokepoint and the verdict
    route (E925) read this one definition."""
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


def _gate_set(rows: Mapping[int, tuple], ids: list[int], source: MergeSource) -> None:
    """The set's refusals, read under its lock: a member missing or not active, two different
    asset links (the survivor keeps only one) or, not the operator's own merge, two linked units
    (E903), and any rule-15 category clash between ANY two members — the survivor's stored
    category is not recomputed until the whole set has merged, so a NULL there would otherwise
    let a sale and a rent through."""
    missing = [pid for pid in ids if pid not in rows]
    inactive = [pid for pid in ids if pid in rows and rows[pid][1] != "active"]
    if missing or inactive:
        raise MergeError(f"properties not found {missing} or not active {inactive}")
    linked = [pid for pid in ids if rows[pid][3] is not None]
    assets = sorted({int(rows[pid][3]) for pid in linked})
    if len(assets) > 1 or (len(linked) > 1 and source != "operator"):
        raise AssetLinkConflict(f"asset links {assets} on {linked}; refusing to merge")
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            clash = category_clash(rows[a][4:6], rows[b][4:6])
            if clash:
                raise CategoryClash(*clash)


def _merge_pair(cur: psycopg.Cursor, step: carriers.MergeStep, *, reason: str,
                confidence: float | None, markers: dict[str, Any] | None) -> int:
    """One retired property into the survivor under the set's lock and gate: the ledger, the
    re-point, every carrier in order, the retire. Returns the ledger rows written."""
    cur.execute(_LEDGER_SQL, {
        "group": step.group, "survivor": step.survivor, "retired": step.retired,
        "reason": reason, "confidence": confidence,
        "markers": Jsonb(markers) if markers is not None else None,
        "source": step.source,
    })
    moved = cur.rowcount or 0
    cur.execute(_REPOINT_SQL, (step.survivor, step.retired))
    for carrier in carriers.PROPERTY_CARRIERS:
        carrier.on_merge(cur, step)
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
    over every member in id order, one gate (`_gate_set`), then each retired property through
    `_merge_pair`. `source='operator'` rules the canonical adverts "same"."""
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
        _gate_set(rows, ids, source)
        survivor = survivor_of({pid: rows[pid][2] for pid in ids})
        retired = [pid for pid in ids if pid != survivor]
        pairs = _canonical_pairs(conn, ids) if source == "operator" else set()
        moved = 0
        with conn.cursor() as cur:
            for rid in retired:
                moved += _merge_pair(
                    cur, carriers.MergeStep(survivor, rid, group, source), reason=reason,
                    confidence=confidence, markers=markers,
                )
        recompute_one(conn, survivor)
        sync_browse_list(conn, [survivor, *retired])
        ruled = record_rulings(
            conn, pairs, verdict="same", decided_by=str(decided_by),
            note=f"operator merge {group}",
        ) if pairs else 0

    return {
        "data": {
            "merge_group_id": group,
            "survivor_id": survivor,
            "retired_ids": retired,
            "listings_moved": moved,
            "pairs_ruled_same": ruled,
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
    """The `outcome` `detach_listing` would answer for each advert now, read-only: what a bulk
    undo's dry run reports, and (in `MOVED`) whether a split would move it."""
    ids = sorted({int(i) for i in listing_ids})
    if not ids:
        return {}
    plans = _plan_detaches(conn, ids, merge_group_id)
    with conn.cursor() as cur:
        cur.execute(_STATUS_SQL, {"ids": sorted(
            {t for _at, out, _u, t in plans.values() if out == "detached"})})
        state = {int(r[0]): (r[1], r[2]) for r in cur.fetchall()}
    return {lid: "origin_moved_on" if out == "detached" and _origin_gone(state.get(t), undo)
            else out for lid, (_at, out, undo, t) in plans.items()}


def _split_native(
    conn: psycopg.Connection, listing_id: int, current: int, *, decided_by: str,
) -> tuple[str, list[tuple], int | None]:
    """A native advert leaves for a NEW record born through THE birth path
    (`scraper.db.create_singleton_properties`); ONE closed ledger row (`_INGEST_GROUPING_SQL`)
    records it. The merge's lock order: the property, then the advert, re-planned under it."""
    with conn.cursor() as cur:
        cur.execute(_STATUS_SQL + " FOR UPDATE", {"ids": [current]})
        at, outcome, _undo, _target = _plan_detaches(conn, [listing_id], None)[listing_id]
        if at == current and outcome in ("not_merged", "last_native"):
            return outcome, [], current
        if at != current or outcome != "split_native":
            return "moved_since", [], current
        cur.execute(_MOVE_ADVERT_SQL, (None, listing_id, current))
        if not cur.rowcount:
            return "moved_since", [], current
    (born,) = create_singleton_properties(conn, [listing_id])
    group = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(_INGEST_GROUPING_SQL, {
            "group": group, "left": current, "born": born, "by": decided_by,
            "listing": listing_id,
        })
        (row_id,) = cur.fetchone()
    return "split_native", [(int(row_id), group, current, born)], born


def detach_listing(
    conn: psycopg.Connection,
    listing_id: int,
    *,
    decided_by: str,
    reason: str | None = None,
    source: MergeSource = "operator",
    merge_group_id: str | None = None,
) -> dict[str, Any]:
    """Split ONE advert off its property: a merged one back to its ORIGIN (with `merge_group_id`,
    to where it sat before that merge, only while it is the newest to move it); if that merge
    retired the origin it is reactivated and every carrier's inverse runs, in reverse
    `PROPERTY_CARRIERS` order (its pipeline card and asset link come back; curation, dispatches
    and dismissals stay on the property left, rules 18, 22). A native one (no standing merge
    moved it), while another own advert stays, goes to a NEW record (`split_native`: the
    operator only, never group-scoped; any other source answers `propose_only`, decision 9); no
    carrier runs. Idempotent, the `outcome` saying why nothing moved. `source='operator'` rules
    it "different" from every advert that stays. Callers: `toolkit.property_split`,
    `autodedup.apply.unapply` and `autodedup.legacy_retire`."""
    with conn.transaction():
        plan = _plan_detaches(conn, [int(listing_id)], merge_group_id).get(int(listing_id))
        if plan is None:
            raise MergeError(f"listing {listing_id} not found")
        current, outcome, undo, target = plan
        reactivated, ruled = False, 0
        if outcome == "split_native":
            assert current is not None
            outcome, undo, target = _split_native(
                conn, int(listing_id), current, decided_by=decided_by,
            ) if source == "operator" else ("propose_only", [], current)
        if outcome == "detached":
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
        if outcome == "detached":
            assert current is not None and target is not None
            hops = tuple(carriers.Hop(int(m[0]), str(m[1]), int(m[2]), int(m[3])) for m in undo)
            with conn.cursor() as cur:
                cur.execute(_UNDO_SQL, (decided_by, [h.event_id for h in hops]))
                cur.execute(_REACTIVATE_SQL, {"pid": target})
                reactivated = (cur.rowcount or 0) == 1
                if reactivated:
                    step = carriers.DetachStep(int(target), int(current), hops, source)
                    for carrier in reversed(carriers.PROPERTY_CARRIERS):
                        carrier.on_detach(cur, step)
        if outcome in MOVED:
            if source == "operator":
                with conn.cursor() as cur:
                    cur.execute(_STAYING_SQL, (current,))
                    staying = {int(r[0]) for r in cur.fetchall()}
                ruled = record_rulings(
                    conn, {(min(listing_id, s), max(listing_id, s)) for s in staying},
                    verdict="different", decided_by=decided_by,
                    note=f"operator detach from {current}" + (f": {reason}" if reason else ""),
                )
            for pid in (current, target):
                recompute_one(conn, pid)
            sync_browse_list(conn, [current, target])

    return {
        "data": {
            "listing_id": int(listing_id),
            "detached": outcome in MOVED,
            "outcome": outcome,
            "survivor_property_id": current,
            "restored_property_id": target,
            "reactivated": reactivated,
            "merge_group_ids": sorted({str(m[1]) for m in undo}),
            "rulings_written": ruled,
        },
        "metadata": {
            "tool": "detach_listing",
            "source": source,
            "decided_by": decided_by,
            "queried_at": _now_iso(),
        },
    }
