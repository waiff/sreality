"""Operator-facing dedup review: list candidate pairs, merge, dismiss, unmerge.

Thin DB layer over `property_identity_candidates` + `property_merge_events`. The
actual merge/unmerge transaction mechanics live in `toolkit.property_identity`
(shared with the Tier-2 sweep); this module is the API's read + orchestration
surface. Mounted under `/dedup/*` (see `api.routes.dedup`).
"""

from __future__ import annotations

from typing import Any

import psycopg

from toolkit.property_identity import MergeError, merge_properties, unmerge_group

# A property side rendered on a review card. Built in SQL so geom -> lat/lng and
# the display fields come straight off the canonical row.
_PROP_SIDE_SQL = """
  jsonb_build_object(
    'property_id',         {p}.id,
    'status',              {p}.status,
    'sreality_id',         {p}.repr_listing_id,
    'price_czk',           {p}.current_price_czk,
    'area_m2',             {p}.area_m2,
    'disposition',         {p}.disposition,
    'district',            {p}.district,
    'category_main',       {p}.category_main,
    'category_type',       {p}.category_type,
    'source_count',        {p}.source_count,
    'distinct_site_count', {p}.distinct_site_count,
    'first_seen_at',       {p}.first_seen_at,
    'lat',                 ST_Y({p}.geom::geometry),
    'lng',                 ST_X({p}.geom::geometry)
  )
"""


# Sentinel reason for candidates from an older engine version that never wrote a
# `reason` into markers_matched — the operator filters these as one bucket.
LEGACY_REASON = "(legacy)"
# Sentinel verdict for "no verdict recorded" (most buckets), so a bucket keyed on
# (reason, NULL verdict) drills in exactly rather than mixing verdicts.
NULL_VERDICT = "(none)"


def _candidate_filters(
    status: str | None, tier: str | None, reason: str | None, verdict: str | None,
) -> tuple[str, dict[str, Any]]:
    """Shared WHERE for list_candidates + its COUNT (so the page total is real)."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if status is not None:
        clauses.append("c.status = %(status)s")
        params["status"] = status
    if tier is not None:
        clauses.append("c.tier = %(tier)s")
        params["tier"] = tier
    if reason == LEGACY_REASON:
        clauses.append("c.markers_matched->>'reason' IS NULL")
    elif reason is not None:
        clauses.append("c.markers_matched->>'reason' = %(reason)s")
        params["reason"] = reason
    if verdict == NULL_VERDICT:
        clauses.append("c.markers_matched->>'verdict' IS NULL")
    elif verdict is not None:
        clauses.append("c.markers_matched->>'verdict' = %(verdict)s")
        params["verdict"] = verdict
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


def list_candidates(
    conn: psycopg.Connection,
    *,
    status: str | None = "proposed",
    tier: str | None = None,
    reason: str | None = None,
    verdict: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where_sql, params = _candidate_filters(status, tier, reason, verdict)

    with conn.cursor() as cur:
        # Real total for THIS filter (the page is capped at `limit`), so the UI
        # can show the full backlog size + paginate — not just the page count.
        cur.execute(
            f"SELECT count(*) FROM property_identity_candidates c {where_sql}", params,
        )
        total = int(cur.fetchone()[0])

        cur.execute(
            f"""
            SELECT
              c.id, c.tier, c.status, c.confidence, c.markers_matched,
              c.auto_merged, c.merge_group_id::text, c.created_at, c.reviewed_at,
              {_PROP_SIDE_SQL.format(p="l")} AS left_property,
              {_PROP_SIDE_SQL.format(p="r")} AS right_property
            FROM property_identity_candidates c
            JOIN properties l ON l.id = c.left_property_id
            JOIN properties r ON r.id = c.right_property_id
            {where_sql}
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {**params, "limit": limit, "offset": offset},
        )
        rows = cur.fetchall()

    data = [
        {
            "id": r[0],
            "tier": r[1],
            "status": r[2],
            "confidence": float(r[3]) if r[3] is not None else None,
            "markers_matched": r[4],
            "auto_merged": r[5],
            "merge_group_id": r[6],
            "created_at": r[7],
            "reviewed_at": r[8],
            "left_property": r[9],
            "right_property": r[10],
        }
        for r in rows
    ]
    return {"data": data, "total": total, "returned": len(data)}


def summary(conn: psycopg.Connection, *, status: str = "proposed") -> dict[str, Any]:
    """Cumulative review backlog + its composition by reason (why each pair queued).

    The /dedup dashboard reads this so the operator sees the WHOLE pending queue
    and what it's made of — not just the page of cards on screen. `reason` /
    `verdict` are the filter keys list_candidates accepts to drill into a bucket
    (legacy rows — no recorded reason — bucket under LEGACY_REASON).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              coalesce(markers_matched->>'reason', %(legacy)s) AS reason,
              markers_matched->>'verdict' AS verdict,
              count(*) AS n
            FROM property_identity_candidates
            WHERE status = %(status)s
            GROUP BY 1, 2
            ORDER BY n DESC
            """,
            {"status": status, "legacy": LEGACY_REASON},
        )
        buckets = [
            {"reason": r[0], "verdict": r[1], "count": int(r[2])}
            for r in cur.fetchall()
        ]
        # Per-tier facet for the review rail: each family's pending count + the
        # bulk-approvable STRONG subset (geo matches with concordant area+price/house#).
        cur.execute(
            """
            SELECT c.tier, count(*) AS n,
                   count(*) FILTER (
                     WHERE c.markers_matched->>'reason' IN ('geo_exact', 'geo_strong')
                   ) AS strong
            FROM property_identity_candidates c
            WHERE c.status = %(status)s
            GROUP BY c.tier
            ORDER BY n DESC
            """,
            {"status": status},
        )
        tiers = [
            {"tier": r[0], "count": int(r[1]), "strong": int(r[2])}
            for r in cur.fetchall()
        ]
    return {
        "data": {
            "status": status,
            "total": sum(b["count"] for b in buckets),
            "buckets": buckets,
            "tiers": tiers,
        }
    }


def list_pair_audit(
    conn: psycopg.Connection,
    *,
    outcome: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Recent per-pair decision audit rows (the /dedup history surface reads this)."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if outcome is not None:
        clauses.append("outcome = %(outcome)s")
        params["outcome"] = outcome
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM dedup_pair_audit {where}", params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""
            SELECT run_at, left_sreality_id, right_sreality_id,
                   left_property_id, right_property_id, category_main,
                   stage, outcome, detail
            FROM dedup_pair_audit {where}
            ORDER BY run_at DESC, id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {**params, "limit": limit, "offset": offset},
        )
        rows = cur.fetchall()
    return {
        "data": [
            {
                "run_at": r[0], "left_sreality_id": r[1], "right_sreality_id": r[2],
                "left_property_id": r[3], "right_property_id": r[4],
                "category_main": r[5], "stage": r[6], "outcome": r[7], "detail": r[8],
            }
            for r in rows
        ],
        "total": total,
        "returned": len(rows),
    }


def merge_candidate(
    conn: psycopg.Connection, candidate_id: int,
) -> dict[str, Any] | None:
    """Merge a proposed candidate (survivor = the older property). None = 404."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT left_property_id, right_property_id, status, tier, "
            "confidence, markers_matched "
            "FROM property_identity_candidates WHERE id = %s",
            (candidate_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    left, right, status, _tier, confidence, markers = row
    if status != "proposed":
        raise MergeError(
            f"candidate {candidate_id} is not proposed (status={status})"
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM properties WHERE id IN (%s, %s) "
            "ORDER BY first_seen_at ASC, id ASC LIMIT 1",
            (left, right),
        )
        survivor = int(cur.fetchone()[0])
    retired = int(right) if survivor == int(left) else int(left)

    return merge_properties(
        conn,
        survivor_id=survivor,
        retired_id=retired,
        reason="manual",
        source="operator",
        confidence=float(confidence) if confidence is not None else None,
        markers=markers,
    )


def bulk_merge_candidates(
    conn: psycopg.Connection, candidate_ids: list[int],
) -> dict[str, Any]:
    """Approve many proposed candidates as INDEPENDENT pairs — each its own reversible
    merge group, so any one can be undone alone. Per-pair tolerant: a candidate whose
    property an earlier pair in this batch already merged (a shared endpoint) is skipped,
    not fatal, and stays proposed for the next batch (the engine self-heals it). This is
    the operator's scoped bulk-approve; the caller decides WHICH ids (e.g. the loaded
    STRONG geo pairs of one category) — this function never selects, only executes."""
    merged = 0
    skipped = 0
    group_ids: list[str] = []
    for cid in candidate_ids:
        try:
            result = merge_candidate(conn, cid)
        except MergeError:
            skipped += 1
            continue
        if result is None:
            skipped += 1
            continue
        merged += 1
        group_ids.append(result["data"]["merge_group_id"])
    return {"data": {"merged": merged, "skipped": skipped, "merge_group_ids": group_ids}}


def merge_cluster(
    conn: psycopg.Connection, candidate_ids: list[int],
) -> dict[str, Any] | None:
    """Merge a CLUSTER of proposed candidates into one property in one group.

    A cluster is several pairwise candidates that connect the same real-world
    property (A-B, B-C, A-C ...). We collect every distinct property across the
    given candidate ids, pick the oldest as the single survivor, and merge every
    other into it under ONE merge_group_id — so the whole cluster reverses with
    one Undo. Merging always targets the single oldest survivor (never a chain),
    so no intermediate property is retired before it's used. None = 404 (no rows).
    """
    if not candidate_ids:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, left_property_id, right_property_id, status "
            "FROM property_identity_candidates WHERE id = ANY(%s)",
            (candidate_ids,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    for cid, _l, _r, status in rows:
        if status != "proposed":
            raise MergeError(f"candidate {cid} is not proposed (status={status})")

    prop_ids: set[int] = set()
    for _cid, left, right, _status in rows:
        prop_ids.add(int(left))
        prop_ids.add(int(right))
    if len(prop_ids) < 2:
        raise MergeError("cluster has fewer than two distinct properties")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM properties WHERE id = ANY(%s) AND status = 'active' "
            "ORDER BY first_seen_at ASC, id ASC LIMIT 1",
            (list(prop_ids),),
        )
        srow = cur.fetchone()
    if srow is None:
        raise MergeError("no active property in the cluster")
    survivor = int(srow[0])
    retired_ids = sorted(p for p in prop_ids if p != survivor)

    # One outer transaction so the whole cluster merge is ATOMIC: each
    # merge_properties opens its own `with conn.transaction()` which nests as a
    # savepoint here, so if a later pair is refused (e.g. the category guard)
    # the entire cluster rolls back rather than leaving a partial merge.
    group: str | None = None
    moved = 0
    with conn.transaction():
        for retired in retired_ids:
            result = merge_properties(
                conn,
                survivor_id=survivor,
                retired_id=retired,
                reason="manual_cluster",
                source="operator",
                merge_group_id=group,
            )
            group = result["data"]["merge_group_id"]
            moved += int(result["data"]["listings_moved"])

        # merge_properties only marks the (survivor, retired) candidate row per
        # call; cluster-internal pairs (retired_i, retired_j) are now stale —
        # mark them all.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE property_identity_candidates "
                "SET status = 'merged', reviewed_at = now(), "
                "    reviewed_action = 'operator', merge_group_id = %s "
                "WHERE id = ANY(%s) AND status = 'proposed'",
                (group, candidate_ids),
            )

    return {
        "merge_group_id": group,
        "survivor_id": survivor,
        "retired_ids": retired_ids,
        "listings_moved": moved,
        "candidates_resolved": len(rows),
    }


def merge_property_set(
    conn: psycopg.Connection, property_ids: list[int],
) -> dict[str, Any] | None:
    """Merge an explicit SET of properties into one (the operator-checked subset).

    Unlike merge_cluster (which works off candidate edges), this takes the
    property ids the operator ticked — so "merge exactly these, regardless of
    which pairwise edges exist between them" works directly. The oldest is the
    survivor; every other merges into it under one reversible group.

    Candidate hygiene afterward: edges fully inside the set are marked 'merged';
    edges with ONE endpoint in the set (a still-proposed match to an UNCHECKED
    property) are re-pointed onto the survivor so the remaining proposal stays
    valid and points at an active property — never a merged-away ghost. None =
    nothing to do (fewer than two active properties).
    """
    ids = sorted({int(p) for p in property_ids})
    if len(ids) < 2:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM properties WHERE id = ANY(%s) AND status = 'active' "
            "ORDER BY first_seen_at ASC, id ASC",
            (ids,),
        )
        active = [int(r[0]) for r in cur.fetchall()]
    if len(active) < 2:
        raise MergeError("fewer than two active properties in the selection")
    survivor, retired_ids = active[0], active[1:]

    # One outer transaction so the subset merge is ATOMIC: each merge_properties
    # nests as a savepoint, so a later refusal (e.g. the category guard) rolls
    # the whole set back instead of committing a partial merge.
    group: str | None = None
    moved = 0
    with conn.transaction():
        for retired in retired_ids:
            result = merge_properties(
                conn, survivor_id=survivor, retired_id=retired,
                reason="manual_subset", source="operator", merge_group_id=group,
            )
            group = result["data"]["merge_group_id"]
            moved += int(result["data"]["listings_moved"])

        _repoint_proposed_candidates(conn, survivor, retired_ids)

    return {
        "merge_group_id": group,
        "survivor_id": survivor,
        "retired_ids": retired_ids,
        "listings_moved": moved,
    }


def _repoint_proposed_candidates(
    conn: psycopg.Connection, survivor: int, retired_ids: list[int],
) -> None:
    """Keep the still-proposed candidate queue consistent after a merge.

    Any 'proposed' candidate that referenced a now-retired property is re-pointed
    onto the survivor (preserving the ordered-pair invariant left<right, deduping
    via ON CONFLICT). A pair that collapses to (survivor, survivor) — both ends
    were merged — is just dropped. Done in Python for clarity over a clever SQL.
    """
    retired = set(retired_ids)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT id, left_property_id, right_property_id "
            "FROM property_identity_candidates "
            "WHERE status = 'proposed' "
            "AND (left_property_id = ANY(%s) OR right_property_id = ANY(%s))",
            (retired_ids, retired_ids),
        )
        rows = cur.fetchall()
        for cid, left, right in rows:
            a = survivor if int(left) in retired else int(left)
            b = survivor if int(right) in retired else int(right)
            if a == b:
                # both endpoints merged into the survivor — fully resolved.
                cur.execute(
                    "UPDATE property_identity_candidates "
                    "SET status = 'merged', reviewed_at = now(), reviewed_action = 'operator' "
                    "WHERE id = %s",
                    (cid,),
                )
                continue
            lo, hi = sorted((a, b))
            # Re-point this edge onto the survivor; if that pair already exists
            # drop this now-redundant row, else move it.
            cur.execute(
                "DELETE FROM property_identity_candidates WHERE id = %s "
                "AND EXISTS (SELECT 1 FROM property_identity_candidates x "
                "  WHERE x.left_property_id = %s AND x.right_property_id = %s AND x.id <> %s)",
                (cid, lo, hi, cid),
            )
            cur.execute(
                "UPDATE property_identity_candidates "
                "SET left_property_id = %s, right_property_id = %s "
                "WHERE id = %s",
                (lo, hi, cid),
            )


def dismiss_cluster(
    conn: psycopg.Connection, candidate_ids: list[int],
) -> dict[str, Any] | None:
    """Dismiss every proposed candidate in a cluster. None = nothing dismissed."""
    if not candidate_ids:
        return None
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE property_identity_candidates "
            "SET status = 'dismissed', reviewed_at = now(), "
            "    reviewed_action = 'operator' "
            "WHERE id = ANY(%s) AND status = 'proposed' RETURNING id",
            (candidate_ids,),
        )
        dismissed = [int(r[0]) for r in cur.fetchall()]
    if not dismissed:
        return None
    return {"dismissed": dismissed, "status": "dismissed"}


def dismiss_candidate(
    conn: psycopg.Connection, candidate_id: int,
) -> dict[str, Any] | None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE property_identity_candidates "
            "SET status = 'dismissed', reviewed_at = now(), "
            "    reviewed_action = 'operator' "
            "WHERE id = %s AND status = 'proposed' RETURNING id",
            (candidate_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": candidate_id, "status": "dismissed"}


def list_merges(
    conn: psycopg.Connection, *, limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    sql = """
        SELECT
          merge_group_id::text,
          min(created_at)                       AS merged_at,
          max(survivor_property_id)             AS survivor_property_id,
          count(distinct retired_property_id)   AS retired_count,
          count(*)                              AS listings_moved,
          max(source)                           AS source,
          max(reason)                           AS reason,
          bool_and(undone_at IS NOT NULL)       AS fully_undone
        FROM property_merge_events
        GROUP BY merge_group_id
        ORDER BY min(created_at) DESC
        LIMIT %(limit)s OFFSET %(offset)s
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"limit": limit, "offset": offset})
        rows = cur.fetchall()
    data = [
        {
            "merge_group_id": r[0],
            "merged_at": r[1],
            "survivor_property_id": r[2],
            "retired_count": r[3],
            "listings_moved": r[4],
            "source": r[5],
            "reason": r[6],
            "fully_undone": r[7],
        }
        for r in rows
    ]
    return {"data": data, "total": len(data)}


def unmerge(
    conn: psycopg.Connection, merge_group_id: str, *, undone_by: str,
) -> dict[str, Any]:
    """Reverse a merge group. Raises MergeError if it has no active events."""
    return unmerge_group(conn, merge_group_id=merge_group_id, undone_by=undone_by)
