"""Merge / unmerge the canonical `properties` parent (multi-portal dedup PR2).

A merge re-points a retired property's child listings onto the survivor and
soft-retires the loser; the 19 FK child columns (15 tables) key on listings.sreality_id, so
all history stays put. Every re-pointed child is logged to
`property_merge_events`, which makes unmerge a deterministic replay even after
the survivor later absorbs a third property. The survivor's stats are recomputed
inline (reusing the recompute job's exact SQL) so there is no stale window.

This module is the single merge chokepoint. Since the 2026-08 "NEW DEDUP" cutoff
there is no automatic decision path at all — every merge is operator-ordered via
`api.property_merge` (`POST /properties/merge`) — but the mechanics stay in one
tested place, and every merge is reversible (`unmerge_group`). The operator's
"same" / "different" rulings are written by that caller, not here, so an engine
merge or its bulk undo never stands in for a human ruling.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from scripts.recompute_property_stats import recompute_mf_one, recompute_one
from toolkit.browse_read_model import sync_browse_list
from toolkit.dismissal_identity import reconcile_dismissals_on_merge
from toolkit.operator_state import carry_operator_state_on_merge
from toolkit.pipeline_identity import (
    reconcile_pipeline_on_merge,
    reconcile_pipeline_on_unmerge,
)
from toolkit.room_taxonomy import category_main_compatible

MergeSource = Literal["auto", "operator"]


class MergeError(ValueError):
    """A merge/unmerge precondition failed (e.g. a property is already merged)."""


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
) -> dict[str, Any]:
    """Merge `retired_id` into `survivor_id`. One transaction, reversible.

    Re-points every child of the retired property onto the survivor, logs one
    `property_merge_events` row per child, carries the retired property's
    property-anchored operator state (collections/tags/notes/watchdog dispatches,
    see `toolkit.operator_state`) onto the survivor, soft-retires the loser, and
    recomputes the survivor inline.
    Returns the standard toolkit envelope with the new merge_group_id.
    """
    if survivor_id == retired_id:
        raise MergeError("survivor and retired must differ")
    group = merge_group_id or str(uuid.uuid4())

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, category_type, category_main "
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
            # Final category guard at THE chokepoint every merge path funnels
            # through (operator one-click, Browse merge-mode).
            # A sale and a rental are never the same property, and a flat and a
            # house aren't either — but dum <-> komercni IS allowed (the same
            # building listed as a house on one portal, commercial on another).
            # `category_main_compatible` encodes that one sanctioned cross-type;
            # refuse everything else even on an operator-initiated merge. NULL =
            # unknown, not a conflict. With no automatic decision layer left,
            # this is the ONLY category gate — nothing upstream pre-screens.
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

            # `listing_id` here is a LEGACY sreality_id; `listing_ref_id` (migration
            # 323) carries the surrogate listings.id alongside it during dual-write.
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
            # Property-anchored operator state (collections/tags/notes/watchdog
            # dispatches) follows the property onto the survivor in this same
            # transaction, so it never orphans onto the merged_away loser.
            carry_operator_state_on_merge(
                cur, retired_id=retired_id, survivor_id=survivor_id
            )
            # The single-valued deal-pipeline stage can't be a generic re-point
            # (PK on property_id); keep the most-advanced stage on the survivor.
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

        recompute_one(conn, survivor_id)
        recompute_mf_one(conn, survivor_id)
        # Patch the Browse read model in the same txn so the merge is visible
        # before the next 5-min rebuild (the survivor updates, the retired row
        # drops out — it no longer matches browse_projection's active filter).
        sync_browse_list(conn, [survivor_id, retired_id])

    return {
        "data": {
            "merge_group_id": group,
            "survivor_id": survivor_id,
            "retired_id": retired_id,
            "listings_moved": moved,
        },
        "metadata": {
            "tool": "merge_properties",
            "reason": reason,
            "source": source,
            "queried_at": _now_iso(),
        },
    }


def unmerge_group(
    conn: psycopg.Connection,
    *,
    merge_group_id: str,
    undone_by: str,
) -> dict[str, Any]:
    """Reverse one merge group: a deterministic replay of its ledger.

    Each not-yet-undone event moves its child back to the retired property — but
    only if the child still points at the survivor (a child re-merged elsewhere
    since is left alone and reported as a conflict, never yanked). Retired
    properties are reactivated and both sides recomputed inline.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT survivor_property_id, retired_property_id,
                       -- Prefer the surrogate (mig 323); fall back to resolving the
                       -- legacy handle for ledger rows written before dual-write.
                       -- NOTE: property_merge_events.listing_id is SREALITY-valued
                       -- despite its name — listing_ref_id is the surrogate twin.
                       coalesce(
                         e.listing_ref_id,
                         (SELECT l.id FROM listings l WHERE l.sreality_id = e.listing_id)
                       ) AS listing_ref_id
                FROM property_merge_events e
                WHERE merge_group_id = %s AND undone_at IS NULL
                ORDER BY id
                """,
                (merge_group_id,),
            )
            events = cur.fetchall()
            if not events:
                raise MergeError(
                    f"no active merge events for group {merge_group_id}"
                )

            survivor_id = int(events[0][0])
            retired_ids: set[int] = set()
            moved_back = 0
            conflicts: list[int] = []
            for _surv, retired, listing_ref_id in events:
                retired_ids.add(int(retired))
                if listing_ref_id is None:
                    # Neither handle resolves (the listing is gone entirely) —
                    # nothing to move back; record it as a conflict, don't crash.
                    conflicts.append(0)
                    continue
                cur.execute(
                    "UPDATE listings SET property_id = %s "
                    "WHERE id = %s AND property_id = %s",
                    (retired, listing_ref_id, survivor_id),
                )
                if (cur.rowcount or 0) == 1:
                    moved_back += 1
                else:
                    conflicts.append(int(listing_ref_id))

            # is_active is restored in the SAME statement that clears merged_away, so
            # the status-event trigger (migration 559) sees neither the retirement nor
            # this reactivation as an activity transition: no false gap either side.
            cur.execute(
                """
                UPDATE properties p
                SET status = 'active', merged_into = NULL, merged_at = NULL,
                    is_active = EXISTS (
                      SELECT 1 FROM listings l
                      WHERE l.property_id = p.id AND l.is_active)
                WHERE p.id = ANY(%s)
                """,
                (list(retired_ids),),
            )
            # Reactivated retired properties get their pre-merge pipeline card
            # back from the snapshot (lossless); runs after the reactivation so
            # the restore targets active properties.
            reconcile_pipeline_on_unmerge(
                cur, merge_group_id=merge_group_id, survivor_id=survivor_id
            )
            cur.execute(
                """
                UPDATE property_merge_events
                SET undone_at = now(), undone_by = %s
                WHERE merge_group_id = %s AND undone_at IS NULL
                """,
                (undone_by, merge_group_id),
            )

        recompute_one(conn, survivor_id)
        recompute_mf_one(conn, survivor_id)
        for rid in retired_ids:
            recompute_one(conn, rid)
            recompute_mf_one(conn, rid)
        # The survivor plus every reactivated retired property are all active
        # again — patch them into the read model in the same txn so the unmerge
        # is visible before the next rebuild.
        sync_browse_list(conn, [survivor_id, *retired_ids])

    return {
        "data": {
            "merge_group_id": merge_group_id,
            "survivor_id": survivor_id,
            "retired_ids": sorted(retired_ids),
            "listings_moved_back": moved_back,
            "conflicts": conflicts,
        },
        "metadata": {
            "tool": "unmerge_group",
            "undone_by": undone_by,
            "queried_at": _now_iso(),
        },
    }
