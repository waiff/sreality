"""One merge, one undo for the canonical `properties` parent (rule 15, decisions 8 and 17).

`merge_property_set` keeps the OLDEST record of a set and merges the rest into it through THE
chokepoint, `merge_properties`, recomputing the survivor once; `detach_listing` moves ONE
advert back to the property the ledger says it came from, or gives an advert no merge brought
(an ingest-time grouping) a new record through the one birth path — a group undo is a loop of
it. Callers:
the operator's routes (`api.property_merge`) and, inside `app_settings.autodedup_apply_scope`
only, the AUTODEDUP apply path. With `source='operator'` each is also a ruling (decision 8) in
the review pages' store; an engine merge or its bulk undo never is.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Sequence

import psycopg
from psycopg.types.json import Jsonb

from autodedup import ui_sql as usql
from scraper.db import create_singleton_properties
from scripts.recompute_property_stats import recompute_one
from toolkit.browse_read_model import sync_browse_list
from toolkit.dismissal_identity import reconcile_dismissals_on_merge
from toolkit.operator_state import carry_operator_state_on_merge
from toolkit.pipeline_identity import (
    reconcile_pipeline_on_detach,
    reconcile_pipeline_on_merge,
)
from toolkit.room_taxonomy import category_main_compatible

# "auto" = the removed legacy engine (historic rows only); "autodedup" = migration 558.
MergeSource = Literal["auto", "operator", "autodedup"]

# The `detach_listing` outcomes that move the advert: back to its origin, or to a new record.
MOVED = frozenset({"detached", "split_native"})


class MergeError(ValueError):
    """A merge/detach precondition failed (e.g. a property is already merged)."""


class AssetLinkConflict(MergeError):
    """Two different asset links in one merge (the survivor could keep only one, decision 17),
    or two linked units in an engine merge (the operator's "different units", E903)."""


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

# Decision 17: the one asset link moves onto the survivor, never left on a merged_away row; each
# membership change is logged as `toolkit.asset_identity` logs a link or an unlink, the reason
# naming the merge group so a detach can give the link back (`_RESTORE_ASSET_LINK_SQL`).
_CARRY_ASSET_LINK_SQL = """
WITH moved AS (
    UPDATE properties
    SET asset_id = CASE WHEN id = %(survivor)s::bigint THEN %(asset)s::bigint END
    WHERE id IN (%(survivor)s::bigint, %(retired)s::bigint)
      AND asset_id IS DISTINCT FROM
          CASE WHEN id = %(survivor)s::bigint THEN %(asset)s::bigint END
    RETURNING id, asset_id
)
INSERT INTO asset_membership_events (asset_id, property_id, action, reason, source)
SELECT %(asset)s::bigint, m.id,
       CASE WHEN m.asset_id IS NULL THEN 'unlinked' ELSE 'linked' END,
       %(reason)s::text, %(source)s::text
FROM moved m
"""

# The carry's inverse, when a detach reactivates the property a merge unlinked: the link it lost
# (unless its asset dissolved since) comes back while a survivor on the advert's path (`path`, the
# merges it undoes: `merges`) still holds it, and leaves each such survivor one of those merges
# linked it to — so detaches in any order give every link back to where it was.
_RESTORE_ASSET_LINK_SQL = """
WITH carried AS (
    SELECT e.asset_id FROM asset_membership_events e
    JOIN assets a ON a.id = e.asset_id AND a.status = 'active'
    WHERE e.property_id = %(restored)s::bigint AND e.action = 'unlinked'
      AND e.reason = %(merge)s::text
    ORDER BY e.id DESC LIMIT 1
), holders AS (
    SELECT p.id, EXISTS (
             SELECT 1 FROM asset_membership_events s
             WHERE s.property_id = p.id AND s.asset_id = p.asset_id AND s.action = 'linked'
               AND s.reason = ANY(%(merges)s::text[])) AS carried_here
    FROM properties p JOIN carried c ON p.asset_id = c.asset_id
    WHERE p.id = ANY(%(path)s::bigint[])
), moved AS (
    UPDATE properties p
    SET asset_id = CASE WHEN p.id = %(restored)s::bigint THEN c.asset_id END
    FROM carried c
    WHERE EXISTS (SELECT 1 FROM holders)
      AND ((p.id = %(restored)s::bigint AND p.asset_id IS NULL)
           OR p.id IN (SELECT h.id FROM holders h WHERE h.carried_here))
    RETURNING p.id, p.asset_id, c.asset_id AS carried
)
INSERT INTO asset_membership_events (asset_id, property_id, action, reason, source)
SELECT m.carried, m.id, CASE WHEN m.asset_id IS NULL THEN 'unlinked' ELSE 'linked' END,
       %(detach)s::text, %(source)s::text
FROM moved m
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


def survivor_of(first_seen: Mapping[int, datetime | None]) -> int:
    """Decision 17: the oldest record (`first_seen_at`, unknown last), then the lowest id."""
    return min(first_seen, key=lambda pid: (first_seen[pid] is None, first_seen[pid] or 0, pid))


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
    `veto_reason`, else the note; anything else retracts it). Returns the pair's newest row in
    `usql.VERDICT_COLUMNS` order."""
    with conn.cursor() as cur:
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
    verdict: Literal["same", "different"],
    decided_by: str,
    note: str,
) -> int:
    """Rule each (lo, hi) listings.id pair through `record_ruling`; returns the count."""
    ordered = sorted(pairs)
    for lo, hi in ordered:
        record_ruling(conn, lo, hi, verdict=verdict, decided_by=decided_by, note=note)
    return len(ordered)


def _category_refusal(
    a: tuple[str | None, str | None], b: tuple[str | None, str | None],
) -> str | None:
    """Rule 15's gate on two (category_type, category_main): sale != rent, flat != house, except
    the one sanctioned dum <-> komercni. NULL = unknown, not a conflict."""
    if a[0] is not None and b[0] is not None and a[0] != b[0]:
        return f"category_type mismatch ({a[0]} vs {b[0]}); refusing to merge"
    if not category_main_compatible(a[1], b[1]):
        return f"category_main mismatch ({a[1]} vs {b[1]}); refusing to merge"
    return None


def _canonical_pairs(conn: psycopg.Connection, property_ids: list[int]) -> set[tuple[int, int]]:
    """Every (lo, hi) pair of the properties' canonical adverts."""
    with conn.cursor() as cur:
        cur.execute(_CANONICAL_ADVERTS_SQL, {"ids": sorted(set(property_ids))})
        ids = sorted({int(r[0]) for r in cur.fetchall()})
    return {(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]}


def merge_properties(
    conn: psycopg.Connection,
    *,
    survivor_id: int,
    retired_id: int,
    reason: str,
    source: MergeSource,
    confidence: float | None = None,
    markers: dict[str, Any] | None = None,
    merge_group_id: str | None = None,
) -> int:
    """THE chokepoint: one retired property into the survivor, returning the adverts moved;
    `merge_property_set` recomputes."""
    if survivor_id == retired_id:
        raise MergeError("survivor and retired must differ")
    group = merge_group_id or str(uuid.uuid4())

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, category_type, category_main, asset_id "
                "FROM properties WHERE id IN (%s, %s) FOR UPDATE",
                (survivor_id, retired_id),
            )
            rows = {row[0]: row for row in cur.fetchall()}
            if survivor_id not in rows or retired_id not in rows:
                raise MergeError("survivor or retired property not found")
            if rows[survivor_id][1] != "active":
                raise MergeError(f"survivor {survivor_id} is not active")
            if rows[retired_id][1] != "active":
                raise MergeError(f"retired {retired_id} is not active")
            # The category gate no caller can route around.
            refusal = _category_refusal(rows[survivor_id][2:4], rows[retired_id][2:4])
            if refusal:
                raise MergeError(refusal)
            s_asset, r_asset = rows[survivor_id][4], rows[retired_id][4]
            if s_asset is not None and r_asset is not None and s_asset != r_asset:
                raise AssetLinkConflict(
                    f"two asset links ({s_asset} vs {r_asset}); refusing to merge"
                )

            # `listing_id` is the LEGACY sreality_id; a detach reads `listing_ref_id` (mig 323).
            cur.execute(
                """
                INSERT INTO property_merge_events
                    (merge_group_id, survivor_property_id, retired_property_id,
                     listing_id, listing_ref_id, prev_property_id, reason,
                     confidence, markers, source)
                SELECT %(group)s, %(survivor)s, %(retired)s,
                       l.sreality_id, l.id, %(retired)s, %(reason)s,
                       %(confidence)s, %(markers)s, %(source)s
                FROM listings l
                WHERE l.property_id = %(retired)s
                """,
                {
                    "group": group, "survivor": survivor_id, "retired": retired_id,
                    "reason": reason, "confidence": confidence,
                    "markers": Jsonb(markers) if markers is not None else None,
                    "source": source,
                },
            )
            moved = cur.rowcount or 0

            cur.execute(
                "UPDATE listings SET property_id = %s WHERE property_id = %s",
                (survivor_id, retired_id),
            )
            if r_asset is not None:
                cur.execute(_CARRY_ASSET_LINK_SQL, {
                    "survivor": survivor_id, "retired": retired_id, "asset": r_asset,
                    "reason": f"merge {group}",
                    "source": "operator" if source == "operator" else "auto",
                })
            carry_operator_state_on_merge(
                cur, retired_id=retired_id, survivor_id=survivor_id
            )
            reconcile_pipeline_on_merge(
                cur, retired_id=retired_id, survivor_id=survivor_id,
                merge_group_id=group,
            )
            # After the pipeline: a card that landed on the survivor lifts that
            # account's dismissal of it.
            reconcile_dismissals_on_merge(
                cur, retired_id=retired_id, survivor_id=survivor_id,
            )
            cur.execute(
                """
                UPDATE properties
                SET status = 'merged_away', merged_into = %s,
                    merged_at = now(), is_active = false
                WHERE id = %s
                """,
                (survivor_id, retired_id),
            )
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
    """Merge an active SET into its oldest record under ONE group, in one transaction; two
    different asset links, any rule-15 category clash within the set, or (not the operator's
    own merge) two linked units refuse it. `source='operator'` rules the canonical adverts
    "same"."""
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
        missing = [pid for pid in ids if pid not in rows]
        inactive = [pid for pid in ids if pid in rows and rows[pid][1] != "active"]
        if missing or inactive:
            raise MergeError(f"properties not found {missing} or not active {inactive}")
        carriers = [pid for pid in ids if rows[pid][3] is not None]
        assets = sorted({int(rows[pid][3]) for pid in carriers})
        if len(assets) > 1 or (len(carriers) > 1 and source != "operator"):
            raise AssetLinkConflict(
                f"asset links {assets} on {carriers}; refusing to merge")
        # Every pair, not only against the survivor: its stored category is not recomputed
        # until the whole set has merged, so a NULL there would let a sale and a rent through.
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                refusal = _category_refusal(rows[a][4:6], rows[b][4:6])
                if refusal:
                    raise MergeError(refusal)
        survivor = survivor_of({pid: rows[pid][2] for pid in ids})
        retired = [pid for pid in ids if pid != survivor]
        pairs = _canonical_pairs(conn, ids) if source == "operator" else set()
        moved = 0
        for rid in retired:
            moved += merge_properties(
                conn, survivor_id=survivor, retired_id=rid, reason=reason, source=source,
                confidence=confidence, markers=markers, merge_group_id=group,
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
    cur: psycopg.Cursor, listing_ids: list[int], merge_group_id: str | None,
) -> dict[int, tuple[int | None, str, list[tuple], int | None]]:
    """(current property, `_detach_plan`) per advert found, from unlocked reads."""
    cur.execute(_PLACES_SQL, {"ids": listing_ids})
    places = {int(lid): int(pid) if pid is not None else None for lid, pid in cur.fetchall()}
    cur.execute(_LIVE_MOVES_SQL, {"ids": listing_ids})
    moves: dict[int, list[tuple]] = {}
    for row in cur.fetchall():
        moves.setdefault(int(row[0]), []).append(tuple(row[1:5]))
    native = sorted({pid for lid, pid in places.items() if pid is not None and lid not in moves})
    sizes: dict[int, tuple[int, int]] = {}
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
    with conn.cursor() as cur:
        plans = _plan_detaches(cur, ids, merge_group_id)
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
        at, outcome, _undo, _target = _plan_detaches(cur, [listing_id], None)[listing_id]
        if at == current and outcome in ("not_merged", "last_native"):
            return outcome, [], current
        if at != current or outcome != "split_native":
            return "moved_since", [], current
        cur.execute(
            "UPDATE listings SET property_id = %s WHERE id = %s AND property_id = %s",
            (None, listing_id, current),
        )
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
    to where it sat before that merge, only while it is the newest to move it), its origin
    reactivated with its pipeline card and asset link if that merge retired it; a native one (no
    standing merge moved it), while another own advert stays, to a NEW record (`split_native`:
    the operator only, never group-scoped; any other source answers `propose_only`, decision 9).
    Other state, the pipeline card included, stays on the property left (rules 18, 22).
    Idempotent, the `outcome` saying why nothing moved. `source='operator'` rules it "different"
    from every advert that stays. Stable signature: the operator's routes, `unapply` and the
    engine's W5 reconcile call it."""
    with conn.transaction():
        with conn.cursor() as cur:
            plan = _plan_detaches(cur, [int(listing_id)], merge_group_id).get(int(listing_id))
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
                    cur.execute(
                        "UPDATE listings SET property_id = %s WHERE id = %s AND property_id = %s",
                        (target, listing_id, current),
                    )
                    if not cur.rowcount:
                        outcome, undo, target = "moved_since", [], current
        if outcome == "detached":
            assert current is not None and target is not None
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE property_merge_events SET undone_at = now(), undone_by = %s "
                    "WHERE id = ANY(%s) AND undone_at IS NULL",
                    (decided_by, [int(m[0]) for m in undo]),
                )
                cur.execute(_REACTIVATE_SQL, {"pid": target})
                reactivated = (cur.rowcount or 0) == 1
                if reactivated:
                    restore_group, restore_survivor = str(undo[0][1]), int(undo[0][2])
                    reconcile_pipeline_on_detach(
                        cur, merge_group_id=restore_group, restored_id=target,
                        survivor_id=current if current == restore_survivor else None,
                    )
                    cur.execute(_RESTORE_ASSET_LINK_SQL, {
                        "restored": target, "merge": f"merge {restore_group}",
                        "merges": [f"merge {m[1]}" for m in undo],
                        "path": [int(m[2]) for m in undo], "detach": f"detach {restore_group}",
                        "source": "operator" if source == "operator" else "auto",
                    })
        if outcome in MOVED:
            if source == "operator":
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM listings WHERE property_id = %s", (current,))
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
