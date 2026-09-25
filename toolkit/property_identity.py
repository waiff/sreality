"""One merge, one undo for the canonical `properties` parent (rule 15, decisions 8 and 17).

`merge_property_set` keeps the OLDEST record of a set and merges the rest into it through THE
chokepoint, `merge_properties`, recomputing the survivor once; `detach_listing` moves ONE
advert back to the property the ledger says it came from — a group undo is a loop of it. Callers:
the operator's routes (`api.property_merge`) and, inside `app_settings.autodedup_apply_scope`
only, the AUTODEDUP apply path. With `source='operator'` each is also a ruling (decision 8) in
the review pages' store; an engine merge or its bulk undo never is.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

import psycopg
from psycopg.types.json import Jsonb

from autodedup import ui_sql as usql
from scripts.recompute_property_stats import recompute_mf_one, recompute_one
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


class MergeError(ValueError):
    """A merge/detach precondition failed (e.g. a property is already merged)."""


class AssetLinkConflict(MergeError):
    """Two different asset links in one merge: the survivor could keep only one (decision 17)."""


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
SELECT id, status, first_seen_at, asset_id
FROM properties WHERE id = ANY(%(ids)s::bigint[])
ORDER BY id
FOR UPDATE
"""

# Decision 17: the one asset link moves onto the survivor, never left on a merged_away row; each
# membership change is logged as `toolkit.asset_identity` logs a link or an unlink.
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
       'merge', %(source)s::text
FROM moved m
"""

# The advert each property's Browse card shows, while still its child: the card the operator ticked.
_CANONICAL_ADVERTS_SQL = """
SELECT p.repr_listing_ref_id FROM properties p
JOIN listings l ON l.id = p.repr_listing_ref_id AND l.property_id = p.id
WHERE p.id = ANY(%(ids)s::bigint[])
"""

# Every live ledger row of these adverts, oldest first: the first row's `prev_property_id` is the
# advert's ORIGIN (where it sat before every merge that still stands), the last row's survivor
# where the newest merge put it.
_LIVE_MOVES_SQL = """
SELECT e.listing_ref_id, e.id, e.merge_group_id::text, e.survivor_property_id,
       e.prev_property_id
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


def record_rulings(
    conn: psycopg.Connection,
    pairs: set[tuple[int, int]],
    *,
    verdict: Literal["same", "different"],
    decided_by: str,
    note: str,
) -> int:
    """Rule each (lo, hi) listings.id pair as `POST /autodedup/verdict` does; returns the count."""
    ordered = sorted(pairs)
    if not ordered:
        return 0
    with conn.cursor() as cur:
        cur.executemany(usql.VERDICT_PAIR_UPSERT_SQL, [
            {"listing_lo": lo, "listing_hi": hi, "verdict": verdict, "note": note,
             "reasons": [], "decided_by": decided_by}
            for lo, hi in ordered
        ])
        if verdict in usql.NEGATIVE_VERDICTS:
            cur.executemany(usql.MUST_NOT_LINK_UPSERT_SQL, [
                {"listing_lo": lo, "listing_hi": hi, "reason": note} for lo, hi in ordered
            ])
        else:
            cur.executemany(usql.MUST_NOT_LINK_RETRACT_SQL, [
                {"listing_lo": lo, "listing_hi": hi} for lo, hi in ordered
            ])
    return len(ordered)


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
            # The category gate no caller can route around: sale != rent, flat != house, except
            # the one sanctioned dum <-> komercni. NULL = unknown, not a conflict.
            s_ct, s_cm = rows[survivor_id][2], rows[survivor_id][3]
            r_ct, r_cm = rows[retired_id][2], rows[retired_id][3]
            if s_ct is not None and r_ct is not None and s_ct != r_ct:
                raise MergeError(
                    f"category_type mismatch ({s_ct} vs {r_ct}); refusing to merge"
                )
            if not category_main_compatible(s_cm, r_cm):
                raise MergeError(
                    f"category_main mismatch ({s_cm} vs {r_cm}); refusing to merge"
                )
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
    different asset links refuse it. `source='operator'` rules the canonical adverts "same"."""
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
        assets = sorted({int(r[3]) for r in rows.values() if r[3] is not None})
        if len(assets) > 1:
            raise AssetLinkConflict(f"two different asset links {assets}; refusing to merge")
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
        recompute_mf_one(conn, survivor)
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


def listing_origins(conn: psycopg.Connection, listing_ids: list[int]) -> dict[int, int]:
    """Each advert's ORIGIN, where a detach returns it; one no standing merge moved is absent."""
    if not listing_ids:
        return {}
    out: dict[int, int] = {}
    with conn.cursor() as cur:
        cur.execute(_LIVE_MOVES_SQL, {"ids": sorted({int(i) for i in listing_ids})})
        for lid, _id, _group, _survivor, prev in cur.fetchall():
            out.setdefault(int(lid), int(prev))
    return out


def _detach_plan(
    current: int | None, moves: list[tuple], merge_group_id: str | None,
) -> tuple[str, list[tuple], int | None]:
    """(outcome, the ledger rows it undoes, where the advert goes). A move is one live ledger
    row, oldest first: (id, merge_group_id, survivor_property_id, prev_property_id)."""
    if not moves:
        return "not_merged", [], current
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


def detach_listing(
    conn: psycopg.Connection,
    listing_id: int,
    *,
    decided_by: str,
    reason: str | None = None,
    source: MergeSource = "operator",
    merge_group_id: str | None = None,
) -> dict[str, Any]:
    """Move ONE advert back to its ORIGIN, or with `merge_group_id` to where it sat before that
    merge (only while it is the newest to move it); idempotent, the `outcome` says why not.
    The origin comes back if merged away, with its pipeline card; other state stays (rule 18)."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT property_id FROM listings WHERE id = %s", (listing_id,))
            row = cur.fetchone()
            if row is None:
                raise MergeError(f"listing {listing_id} not found")
            current = int(row[0]) if row[0] is not None else None
            cur.execute(_LIVE_MOVES_SQL, {"ids": [listing_id]})
            moves = [tuple(r[1:]) for r in cur.fetchall()]
        outcome, undo, target = _detach_plan(current, moves, merge_group_id)
        reactivated, ruled = False, 0
        if outcome == "detached":
            # The merge's lock order, properties before the advert; it moves only if it still
            # sits where the ledger was read.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM properties WHERE id IN (%s, %s) ORDER BY id FOR UPDATE",
                    (current, target),
                )
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
                recompute_mf_one(conn, pid)
            sync_browse_list(conn, [current, target])

    return {
        "data": {
            "listing_id": int(listing_id),
            "detached": outcome == "detached",
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
