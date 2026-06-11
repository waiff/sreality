"""Merge / unmerge the canonical `properties` parent (multi-portal dedup PR2).

A merge re-points a retired property's child listings onto the survivor and
soft-retires the loser; the ~9 FK child tables key on listings.sreality_id, so
all history stays put. Every re-pointed child is logged to
`property_merge_events`, which makes unmerge a deterministic replay even after
the survivor later absorbs a third property. The survivor's stats are recomputed
inline (reusing the recompute job's exact SQL) so there is no stale window.

Both the operator review API (`api.property_dedup`) and the Tier-2 auto-merge
sweep (PR3) go through these two functions, so the transaction mechanics live in
one tested place. Auto-merge is only safe because every merge is reversible here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from scripts.recompute_property_stats import recompute_one

MergeSource = Literal["auto", "operator"]


class MergeError(ValueError):
    """A merge/unmerge precondition failed (e.g. a property is already merged)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    `property_merge_events` row per child, soft-retires the loser, marks any
    matching candidate pair merged, and recomputes the survivor inline. Returns
    the standard toolkit envelope with the new merge_group_id.
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
            # through (engine, cluster, operator one-click, Browse merge-mode).
            # A sale and a rental — or a flat and a house — are never the same
            # property; refuse even an operator-initiated merge. NULL = unknown,
            # not a conflict. The engine's classify_pair also gates earlier; this
            # backstops the manual merge surface (api.property_dedup) that calls
            # merge_properties directly without classify_pair.
            s_ct, s_cm = rows[survivor_id][2], rows[survivor_id][3]
            r_ct, r_cm = rows[retired_id][2], rows[retired_id][3]
            if s_ct is not None and r_ct is not None and s_ct != r_ct:
                raise MergeError(
                    f"category_type mismatch ({s_ct} vs {r_ct}); refusing to merge"
                )
            if s_cm is not None and r_cm is not None and s_cm != r_cm:
                raise MergeError(
                    f"category_main mismatch ({s_cm} vs {r_cm}); refusing to merge"
                )

            cur.execute(
                """
                INSERT INTO property_merge_events
                    (merge_group_id, survivor_property_id, retired_property_id,
                     listing_id, prev_property_id, reason, confidence, markers, source)
                SELECT %(group)s, %(survivor)s, %(retired)s,
                       l.sreality_id, %(retired)s, %(reason)s,
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
            cur.execute(
                """
                UPDATE properties
                SET status = 'merged_away', merged_into = %s,
                    merged_at = now(), is_active = false
                WHERE id = %s
                """,
                (survivor_id, retired_id),
            )

            lo, hi = sorted((survivor_id, retired_id))
            cur.execute(
                """
                UPDATE property_identity_candidates
                SET status = 'merged', reviewed_at = now(),
                    reviewed_action = %s, auto_merged = %s, merge_group_id = %s
                WHERE left_property_id = %s AND right_property_id = %s
                """,
                (source, source == "auto", group, lo, hi),
            )

        recompute_one(conn, survivor_id)

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
    properties are reactivated, the candidate re-opened for review, and both
    sides recomputed inline.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT survivor_property_id, retired_property_id, listing_id
                FROM property_merge_events
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
            for _surv, retired, listing_id in events:
                retired_ids.add(int(retired))
                cur.execute(
                    "UPDATE listings SET property_id = %s "
                    "WHERE sreality_id = %s AND property_id = %s",
                    (retired, listing_id, survivor_id),
                )
                if (cur.rowcount or 0) == 1:
                    moved_back += 1
                else:
                    conflicts.append(int(listing_id))

            cur.execute(
                """
                UPDATE properties
                SET status = 'active', merged_into = NULL, merged_at = NULL
                WHERE id = ANY(%s)
                """,
                (list(retired_ids),),
            )
            cur.execute(
                """
                UPDATE property_merge_events
                SET undone_at = now(), undone_by = %s
                WHERE merge_group_id = %s AND undone_at IS NULL
                """,
                (undone_by, merge_group_id),
            )
            cur.execute(
                """
                UPDATE property_identity_candidates
                SET status = 'proposed', reviewed_at = NULL, reviewed_action = NULL,
                    auto_merged = false, merge_group_id = NULL
                WHERE merge_group_id = %s
                """,
                (merge_group_id,),
            )

        recompute_one(conn, survivor_id)
        for rid in retired_ids:
            recompute_one(conn, rid)

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
