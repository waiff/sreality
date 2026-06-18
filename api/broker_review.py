"""Broker merge-review (Phase 5) — the operator queue + reversible merge/unmerge.

The auto-merge engine only unifies brokers sharing a PERSONAL contact. Corporate /
developer accounts behind role inboxes + a switchboard have no personal bridge and
are left apart (name-alone is never auto-merged). This module surfaces those
"same name + same firm" groups (proposed by the resolver into
broker_merge_candidates) and lets the operator merge or dismiss them.

Merges are reversible: every re-pointed identity is logged to broker_merge_events
(source='manual'), and unmerge replays it. Affected brokers' rollups are recomputed
inline (reusing the resolver's rollup SQL); the leaderboard matview catches up on
the next daily sweep, but a merged loser drops off the leaderboard immediately
(brokers_public is active-only). Writes live here in api/, not toolkit (rule #5).
"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg.rows import dict_row

from scripts.resolve_brokers import (
    _BROKER_ROLLUP,
    _IDENTITY_ROLLUP,
    _MEMBERSHIP_RECOMPUTE,
)


def list_candidates(conn: Any, *, status: str = "proposed", limit: int = 100,
                    offset: int = 0) -> dict[str, Any]:
    """Proposed merge groups, each enriched with its brokers' current public rows
    (name, firm, counts, primary contact) so the operator can judge the group."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, group_key, broker_ids, reason, evidence, status, created_at "
            "FROM broker_merge_candidates WHERE status = %s "
            "ORDER BY array_length(broker_ids, 1) DESC, id DESC LIMIT %s OFFSET %s",
            (status, limit, offset))
        rows = cur.fetchall()
        all_ids = sorted({b for r in rows for b in r["broker_ids"]})
        brokers: dict[int, dict[str, Any]] = {}
        if all_ids:
            cur.execute(
                "SELECT broker_id, display_name, firm_name, firm_domain, primary_email, "
                "  primary_phone, source_count, distinct_source_count, "
                "  active_property_count, property_count "
                "FROM brokers_public WHERE broker_id = ANY(%s)", (all_ids,))
            brokers = {r["broker_id"]: r for r in cur.fetchall()}
    for r in rows:
        r["created_at"] = _iso(r["created_at"])
        r["brokers"] = [brokers[b] for b in r["broker_ids"] if b in brokers]
    return {"candidates": rows, "count": len(rows)}


def merge_candidate(conn: Any, candidate_id: int, *, broker_ids: list[int] | None = None,
                    created_by: str | None = None) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM broker_merge_candidates WHERE id = %s", (candidate_id,))
        cand = cur.fetchone()
    if cand is None or cand["status"] != "proposed":
        return None
    ids = broker_ids if broker_ids else cand["broker_ids"]
    ids = [b for b in ids if b in cand["broker_ids"]]  # never merge ids outside the proposal
    result = merge_brokers(conn, ids, reason=cand["reason"], created_by=created_by)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE broker_merge_candidates SET status='merged', resolved_at=now(), "
            "resolved_by=%s WHERE id=%s", (created_by, candidate_id))
    return result


def dismiss_candidate(conn: Any, candidate_id: int, *, resolved_by: str | None = None
                      ) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "UPDATE broker_merge_candidates SET status='dismissed', resolved_at=now(), "
            "resolved_by=%s WHERE id=%s AND status='proposed' RETURNING id, status",
            (resolved_by, candidate_id))
        row = cur.fetchone()
    return row


def merge_brokers(conn: Any, broker_ids: list[int], *, reason: str = "manual",
                  created_by: str | None = None) -> dict[str, Any]:
    """Unify active brokers onto the lowest id; reversible via broker_merge_events."""
    ids = sorted({int(b) for b in broker_ids})
    if len(ids) < 2:
        raise MergeError("need at least two brokers to merge")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM brokers WHERE id = ANY(%s) AND status='active'", (ids,))
        active = sorted(int(r[0]) for r in cur.fetchall())
    if len(active) < 2:
        raise MergeError("fewer than two of the given brokers are active")
    survivor = active[0]
    losers = active[1:]
    group = str(uuid.uuid4())
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO broker_merge_events (merge_group_id, survivor_broker_id, "
            "retired_broker_id, identity_id, prev_broker_id, reason, source) "
            "SELECT %(g)s, %(s)s, bi.broker_id, bi.id, bi.broker_id, %(reason)s, 'manual' "
            "FROM broker_identities bi WHERE bi.broker_id = ANY(%(losers)s)",
            {"g": group, "s": survivor, "reason": reason, "losers": losers})
        cur.execute(
            "UPDATE broker_identities SET broker_id=%(s)s WHERE broker_id = ANY(%(losers)s)",
            {"s": survivor, "losers": losers})
        cur.execute(
            "UPDATE brokers SET status='merged_away', merged_into=%(s)s, merged_at=now() "
            "WHERE id = ANY(%(losers)s)", {"s": survivor, "losers": losers})
    _recompute_brokers(conn, [survivor])
    return {"merge_group_id": group, "survivor_broker_id": survivor, "retired_broker_ids": losers}


def list_recent_merges(conn: Any, *, limit: int = 50) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT e.merge_group_id, e.survivor_broker_id, "
            "  array_agg(DISTINCT e.retired_broker_id) AS retired_broker_ids, "
            "  max(e.reason) AS reason, max(e.source) AS source, max(e.created_at) AS merged_at, "
            "  b.display_name AS survivor_name "
            "FROM broker_merge_events e "
            "LEFT JOIN brokers b ON b.id = e.survivor_broker_id "
            "WHERE e.undone_at IS NULL "
            "GROUP BY e.merge_group_id, e.survivor_broker_id, b.display_name "
            "ORDER BY max(e.created_at) DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    for r in rows:
        r["merged_at"] = _iso(r["merged_at"])
    return {"merges": rows}


def unmerge_group(conn: Any, merge_group_id: str, *, undone_by: str | None = None
                  ) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT survivor_broker_id, array_agg(DISTINCT retired_broker_id) "
            "FROM broker_merge_events WHERE merge_group_id=%s AND undone_at IS NULL "
            "GROUP BY survivor_broker_id", (merge_group_id,))
        row = cur.fetchone()
    if row is None:
        return None
    survivor, retired = int(row[0]), [int(x) for x in row[1]]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE broker_identities bi SET broker_id = ev.prev_broker_id "
            "FROM broker_merge_events ev "
            "WHERE ev.merge_group_id=%s AND ev.undone_at IS NULL AND ev.identity_id = bi.id",
            (merge_group_id,))
        cur.execute(
            "UPDATE brokers SET status='active', merged_into=NULL, merged_at=NULL "
            "WHERE id = ANY(%s)", (retired,))
        cur.execute(
            "UPDATE broker_merge_events SET undone_at=now(), undone_by=%s "
            "WHERE merge_group_id=%s AND undone_at IS NULL", (undone_by, merge_group_id))
    _recompute_brokers(conn, [survivor, *retired])
    return {"merge_group_id": merge_group_id, "survivor_broker_id": survivor,
            "restored_broker_ids": retired}


def _recompute_brokers(conn: Any, broker_ids: list[int]) -> None:
    bids = sorted({int(b) for b in broker_ids})
    if not bids:
        return
    with conn.cursor() as cur:
        cur.execute(_IDENTITY_ROLLUP.format(
            extra="AND broker_identity_id IN (SELECT id FROM broker_identities "
                  "WHERE broker_id = ANY(%(bids)s))"), {"bids": bids})
        cur.execute(_BROKER_ROLLUP.format(bscope="AND broker_id = ANY(%(bids)s)"), {"bids": bids})
        cur.execute(_MEMBERSHIP_RECOMPUTE.format(
            bscope="AND bi.broker_id = ANY(%(bids)s)",
            mscope="m.broker_id = ANY(%(bids)s) AND"), {"bids": bids})


def _iso(v: Any) -> Any:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


class MergeError(Exception):
    """Raised when a broker merge can't proceed (too few active brokers, etc.)."""
