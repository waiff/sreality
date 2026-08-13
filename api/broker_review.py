"""Broker merge-review (Phase 5) — the operator queue + reversible merge/unmerge.

The auto-merge engine only unifies brokers sharing a PERSONAL contact. Corporate /
developer accounts behind role inboxes + a switchboard have no personal bridge and
are left apart (name-alone is never auto-merged). This module surfaces those
"same name + same firm" groups (proposed by the resolver into
broker_merge_candidates) and lets the operator merge or dismiss them. The sweep's
cross-source step feeds the same table under reason='contact_bridge_review' — pairs
that DO share a contact but failed the corroboration guard — so callers segment the
queue by `reason` rather than reading one mixed page.

Merges are reversible: every re-pointed identity is logged to broker_merge_events
(source='operator'), and unmerge replays it. Affected brokers' rollups are recomputed
inline (reusing the resolver's rollup SQL); the leaderboard matview catches up on
the next daily sweep, but a merged loser drops off the leaderboard immediately
(brokers_public is active-only). Writes live here in api/, not toolkit (rule #5).

Negative decisions are durable too (migration 399). Unmerging a group, and
dismissing a contact-bridge candidate, write broker_merge_suppressions rows — the
nightly sweep re-derives its whole candidate set from the contact table and would
otherwise re-apply the very merge the operator just undid. An explicit operator
merge LIFTS every suppression it covers: the rail gates the auto path only.
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

_IDENTITY_META_SQL = (
    "SELECT id, source, source_broker_id_native, display_name "
    "FROM broker_identities WHERE id = ANY(%(ids)s)"
)

_BROKER_IDENTITY_IDS_SQL = "SELECT id FROM broker_identities WHERE broker_id = %(broker)s"

_UNDONE_GROUP_EVENTS_SQL = (
    "SELECT identity_id, prev_broker_id FROM broker_merge_events "
    "WHERE merge_group_id = %(group)s AND undone_at IS NULL"
)

# Array-driven so one operator action is one statement whatever its fan-out.
# ON CONFLICT infers the partial unique index, so re-suppressing an already-active
# pair is a no-op rather than a 23505 the operator sees as a failed unmerge.
_SUPPRESSION_INSERT_SQL = """
INSERT INTO broker_merge_suppressions (
  identity_lo, identity_hi, source_lo, native_lo, source_hi, native_hi,
  display_lo, display_hi, origin, merge_group_id, candidate_id, created_by)
SELECT d.lo, d.hi, d.slo, d.nlo, d.shi, d.nhi, d.dlo, d.dhi,
       %(origin)s::text, %(group)s::uuid, %(candidate)s::bigint, %(by)s::text
FROM unnest(%(lo)s::bigint[], %(hi)s::bigint[], %(slo)s::text[], %(nlo)s::text[],
            %(shi)s::text[], %(nhi)s::text[], %(dlo)s::text[], %(dhi)s::text[])
     AS d(lo, hi, slo, nlo, shi, nhi, dlo, dhi)
ON CONFLICT (identity_lo, identity_hi) WHERE lifted_at IS NULL DO NOTHING
"""

# An explicit operator merge ALWAYS wins over an earlier NO — otherwise the
# verify_pipeline invariant (an active suppression whose identities share a broker)
# would red on a legitimate override. Never a DELETE: the lift columns are the audit
# trail (rule #3).
_SUPPRESSION_LIFT_SQL = """
UPDATE broker_merge_suppressions s
SET lifted_at = now(), lifted_by = %(by)s, lift_reason = 'operator_merge'
FROM broker_identities lo, broker_identities hi
WHERE s.lifted_at IS NULL
  AND lo.id = s.identity_lo AND hi.id = s.identity_hi
  AND lo.broker_id = ANY(%(ids)s) AND hi.broker_id = ANY(%(ids)s)
"""


def list_candidates(conn: Any, *, status: str = "proposed", limit: int = 100,
                    offset: int = 0, reason: str | None = None) -> dict[str, Any]:
    """Proposed merge groups, each enriched with its brokers' current public rows
    (name, firm, counts, primary contact) so the operator can judge the group.

    `reason` segments the queue. It has to: the two generators run at wildly
    different volumes (thousands of contact-bridge pairs per sweep against a few
    thousand name_firm groups) and the ordering is group size then recency, so an
    unfiltered page would let one generator's regeneration bury the other's backlog
    below a page the UI has no way to scroll past. `reason_counts` is over the whole
    status, not the page, so a caller can size the queue it is not showing."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, group_key, broker_ids, reason, evidence, status, created_at "
            "FROM broker_merge_candidates WHERE status = %(status)s "
            "AND (%(reason)s::text IS NULL OR reason = %(reason)s) "
            "ORDER BY array_length(broker_ids, 1) DESC, id DESC "
            "LIMIT %(limit)s OFFSET %(offset)s",
            {"status": status, "reason": reason, "limit": limit, "offset": offset})
        rows = cur.fetchall()
        cur.execute(
            "SELECT reason, count(*) AS n FROM broker_merge_candidates "
            "WHERE status = %s GROUP BY reason", (status,))
        reason_counts = {r["reason"]: int(r["n"]) for r in cur.fetchall()}
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
    return {"candidates": rows, "count": len(rows), "reason_counts": reason_counts}


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
    """Dismiss a proposal, and for a contact-bridge one also record it as a standing
    NO. Dismissal alone only stops the review row being re-PROPOSED; the auto-merge
    path never consulted it, so the same pair still auto-merged the moment its
    evidence crossed the corroboration bar (a second bridge value, converging display
    names, or its source being added to broker_auto_merge_sources).

    Only reason='contact_bridge_review' carries identity evidence. A name_firm
    candidate is a different mechanism (same name + firm, no bridge at all) with no
    identity ids to key on — it is dismissed and nothing more."""
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE broker_merge_candidates SET status='dismissed', resolved_at=now(), "
                "resolved_by=%s WHERE id=%s AND status='proposed' "
                "RETURNING id, status, reason, evidence",
                (resolved_by, candidate_id))
            row = cur.fetchone()
        if row is None:
            return None
        pair = (_evidence_pair(row.get("evidence"))
                if row.get("reason") == "contact_bridge_review" else None)
        if pair is not None:
            meta = _identity_meta(conn, list(pair))
            _write_suppressions(conn, [pair], meta, origin="dismiss",
                                candidate_id=candidate_id, created_by=resolved_by)
    return {"id": row["id"], "status": row["status"]}


def _evidence_pair(evidence: Any) -> tuple[int, int] | None:
    """The normalized identity pair in a contact-bridge candidate's evidence, or
    None if it is missing/malformed — a junk blob must not 500 a dismissal."""
    if not isinstance(evidence, dict):
        return None
    ids = evidence.get("identity_ids")
    if not isinstance(ids, list) or len(ids) != 2:
        return None
    try:
        a, b = int(ids[0]), int(ids[1])
    except (TypeError, ValueError):
        return None
    return None if a == b else ((a, b) if a < b else (b, a))


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
        cur.execute(_SUPPRESSION_LIFT_SQL, {"by": created_by, "ids": active})
        cur.execute(
            "INSERT INTO broker_merge_events (merge_group_id, survivor_broker_id, "
            "retired_broker_id, identity_id, prev_broker_id, reason, source) "
            # 'operator', not 'manual': migration 186 constrains source to
            # ('auto','operator') and every operator merge through this queue died
            # on that CHECK as an unmapped 500 (zero manual rows exist in prod).
            "SELECT %(g)s, %(s)s, bi.broker_id, bi.id, bi.broker_id, %(reason)s, 'operator' "
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
        # Derive the post-unmerge ownership BEFORE the re-point, while the survivor
        # still holds both the identities coming back and the ones staying: after
        # the UPDATE the two cohorts are indistinguishable. Without this the sweep
        # re-derives the same bridges tonight and re-applies the merge just undone.
        cur.execute(_UNDONE_GROUP_EVENTS_SQL, {"group": merge_group_id})
        restored = {int(i): int(prev) for i, prev in cur.fetchall()}
        cur.execute(_BROKER_IDENTITY_IDS_SQL, {"broker": survivor})
        owner: dict[int, int] = {int(r[0]): survivor for r in cur.fetchall()
                                 if int(r[0]) not in restored}
        owner.update(restored)
        meta = _identity_meta(conn, list(owner))
        written = _write_suppressions(
            conn, suppression_pairs(owner, {i: m[0] for i, m in meta.items()}), meta,
            origin="unmerge", merge_group_id=merge_group_id, created_by=undone_by)
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
            "restored_broker_ids": retired, "suppressions_written": written}


def suppression_pairs(owner: dict[int, int],
                      source_of: dict[int, str | None]) -> list[tuple[int, int]]:
    """Cross-owner, cross-SOURCE identity pairs an operator separation must block.

    Pure so the ownership algebra is testable on its own: `owner` maps every
    identity involved to the broker that holds it AFTER the separation (a restored
    identity -> its prev_broker_id, one still on the survivor -> the survivor).
    Only pairs that ended up under different brokers are a decision, and only
    cross-source pairs can ever auto-merge (decide_merges refuses a same-source
    bridge outright), so a same-source pair would be a suppression that can never
    fire. Keys come out normalized lo<hi, matching Bridge.pair()."""
    ids = sorted(owner)
    return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]
            if owner[a] != owner[b] and source_of.get(a) != source_of.get(b)]


def _identity_meta(conn: Any, identity_ids: list[int]) -> dict[int, tuple[str, str, str | None]]:
    """identity id -> (source, native id, display name) for the audit columns."""
    if not identity_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(_IDENTITY_META_SQL, {"ids": sorted(identity_ids)})
        return {int(r[0]): (r[1], r[2], r[3]) for r in cur.fetchall()}


def _write_suppressions(conn: Any, pairs: list[tuple[int, int]],
                        meta: dict[int, tuple[str, str, str | None]], *, origin: str,
                        merge_group_id: str | None = None, candidate_id: int | None = None,
                        created_by: str | None = None) -> int:
    """Record the operator's NO for each pair. Identities we cannot describe are
    skipped rather than written with a placeholder natural key."""
    rows = [(lo, hi) for lo, hi in pairs if lo in meta and hi in meta]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute(_SUPPRESSION_INSERT_SQL, {
            "origin": origin, "group": merge_group_id, "candidate": candidate_id,
            "by": created_by,
            "lo": [lo for lo, _ in rows], "hi": [hi for _, hi in rows],
            "slo": [meta[lo][0] for lo, _ in rows], "nlo": [meta[lo][1] for lo, _ in rows],
            "shi": [meta[hi][0] for _, hi in rows], "nhi": [meta[hi][1] for _, hi in rows],
            "dlo": [meta[lo][2] for lo, _ in rows], "dhi": [meta[hi][2] for _, hi in rows],
        })
    return len(rows)


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
