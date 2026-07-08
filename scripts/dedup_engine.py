"""Street + disposition dedup engine — the I/O orchestrator.

Drives the pure rules in `toolkit.dedup_engine` against the live DB and the
cached vision tools, autonomously linking listings into shared `properties`.
Replaces the old geo `scripts.dedup_sweep`.

Pipeline (rules A-E; see toolkit.dedup_engine for the rule text):

  0. Load ELIGIBLE listings (street + disposition both present; active AND
     inactive — price history must survive a delisting/relisting) grouped by
     street_key — rows with both a canonical street_id and a street name are
     dual-keyed into their 'id:' and 'name:' groups. Eligibility is computed
     inline (rule A; a partial index backs the scan — see migration 127).
  1. Within each (street_key) group, classify every cross-property pair
     (classify_pair). Rule C contradictions are rejected; the rest are
     candidates. (Rule B's exact-address AUTO-MERGE was retired — 6.7% of those
     merges were later unmerged — so an exact address is now a strong candidate
     that still needs the photo evidence below.)
  2. For each candidate pair:
       a. pHash fast-path (FREE, no LLM, runs FIRST, all sources) — >=2
          near-identical image pairs (any image) -> auto-merge. Catches
          identical-photo re-posts before paying for any classify/compare.
       b. layered visual confirmation (rule D), ALL sources: classify ->
          room-aware forensic comparison in priority order, stop at first High.
          (The old cross-source gate was removed in Wave 3: it cut ~36% of pairs
          off the paid stage but cost ~1.4% recall — same-portal relists with
          changed photos never got compared.)
     A High verdict (or the pHash fast-path) merges; everything else queues
     (rule E).

Bounded + cached: per-run caps on candidate pairs visually examined, room
attempts per pair, and total vision calls; classification + comparison are
cached so re-runs are nearly free. Writes one `dedup_engine_runs` row.

Runnable as `python -m scripts.dedup_engine`. Required env: SUPABASE_DB_URL
(+ ANTHROPIC_API_KEY / R2_* for the visual layer; absent these the engine still
does rule-A/B/C work + the free pHash fast-path).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from toolkit.dedup_engine import (
    PHASH_IDENTICAL_MAX,
    PHASH_MIN_IDENTICAL_PAIRS,
    CosineBands,
    ListingKey,
    PairDecision,
    classify_geo_pair,
    classify_pair,
    DISTINCTIVE_ROOMS,
    distinctive_rooms_for,
    decide_phash_fastpath,
    decide_visual_dismiss,
    disposition_class,
    phash_excluded_tags_for,
    phash_render_exclude_for,
    prioritized_group_pairs,
    profile_for,
    render_exclusion_clause,
    RENDER_SCORE_EXCLUDE_MIN,
    rooms_in_priority,
    route_by_cosine,
    street_group_keys,
    verdict_is_merge,
)
from toolkit.image_classification import SITE_PLAN_ROOM_TYPE
from toolkit.property_identity import MergeError, merge_properties
from toolkit.publication import GEO_ELIGIBLE_PREDICATE
from toolkit.room_taxonomy import FLOOR_PLAN_ROOM_TYPE

LOG = logging.getLogger("dedup_engine")

# A street group bigger than this is almost certainly a whole street/development
# rather than one building's units; the O(n^2) pairing would explode and the
# matches would be low-value. Skip + log (no silent truncation).
MAX_GROUP_SIZE = 40

# A geo cell (~11 m) holding more than this many distinct single-dwelling properties
# is a development/geocode-pileup, not one building's re-posts — pairing it is
# O(n^2) and low-value. Skip + log (mirrors MAX_GROUP_SIZE for the street path).
MAX_GEO_GROUP_SIZE = 25

# An OVERSIZED group (still > MAX_GROUP_SIZE after disposition-class sharding — a
# same-disposition development or a geo pileup) is processed BOUNDED instead of
# skipped: its best MAX_GROUP_PAIRS pairs in value order (prioritized_group_pairs).
# The historical whole-group skip was a silent recall hole — 342 street groups
# holding 18.7% of the eligible market (2026-07 audit), invisible in run rows, and
# a dirty run CLEARED those properties' queue rows as if handled. The cap equals a
# full 40-group's pair count, so an oversized group costs at most what the biggest
# normal group already costs.
MAX_GROUP_PAIRS = MAX_GROUP_SIZE * (MAX_GROUP_SIZE - 1) // 2

# Above this many members the group-level pHash batch probe is skipped (its IN-list
# and result set scale with members^2); resolve_pair falls back to per-pair probes,
# bounded by MAX_GROUP_PAIRS.
PHASH_BATCH_MAX_MEMBERS = 150

# After this many vision/LLM ERRORS in one run the paid fns stop calling out and
# serve warm-cache reads only. A dead key / exhausted credit otherwise burns the
# whole wall-clock budget on doomed calls: the 2026-07 credit outage produced 38k+
# failed calls and auto_visual=0 for days with nothing recording why.
VISION_ERROR_BREAKER = 10


# Eligible listings that can still be matched, with everything the rules need.
# Rule A eligibility (street + disposition both present) is computed inline — see
# migration 127 for why it isn't a stored column. Ordered so grouping by street_key
# is a simple consecutive walk.
#
# INACTIVE listings participate too (no is_active filter). A property's price/lifecycle
# history is only complete if a listing taken down on one portal — or delisted and later
# relisted under a new id — can still merge into the surviving group; gating on is_active
# would orphan that history. The properties JOIN already excludes merged-away groups, and
# an inactive listing keeps its own active singleton property, so it stays matchable.
_ELIGIBLE_COLS = """
      l.sreality_id, l.property_id, l.source,
      l.street, l.street_id, l.disposition, l.house_number, l.floor, l.area_m2,
      left(l.description, 600) AS description,
      l.category_type, l.category_main, l.obec_id, l.price_czk"""
_ELIGIBILITY = "l.street IS NOT NULL AND l.street <> '' AND l.disposition IS NOT NULL"
_ELIGIBLE_ORDER = (
    "ORDER BY l.obec_id NULLS LAST, l.street_id NULLS LAST, lower(l.street), l.disposition"
)

_ELIGIBLE_SQL = f"""
    SELECT {_ELIGIBLE_COLS}
    FROM listings l
    JOIN properties p ON p.id = l.property_id AND p.status = 'active'
    WHERE {_ELIGIBILITY}
      {{filter}}
    {_ELIGIBLE_ORDER}
"""

# --dirty scoped load: gather the listing ids in the claimed street groups via TARGETED
# index seeks (NOT a full eligible scan), then fetch their rows + active-property join by
# PK. Each arm is an unnest-JOIN so the planner index-seeks PER claimed key — the street_id
# arm via migration 127's listings_dedup_eligible_idx, the obec-scoped name key via
# listings_dedup_name_key_idx (migration 256). UNION dedups a listing matching both arms.
# This is O(dirty), validated by EXPLAIN — an OR of (street_id ANY) + a row-comparison IN
# collapsed to a single full-eligible bitmap scan + filter, defeating the scope. An empty
# claimed set yields empty unnests -> no rows (loads nothing, never a full scan).
_ELIGIBLE_SCOPED_SQL = f"""
    WITH claimed AS (
        SELECT l.sreality_id
        FROM unnest(%(sids)s::bigint[]) AS s(id)
        JOIN listings l ON l.street_id = s.id
        WHERE {_ELIGIBILITY}
      UNION
        SELECT l.sreality_id
        FROM unnest(%(obecs)s::bigint[], %(keys)s::text[]) AS g(o, k)
        JOIN listings l ON coalesce(l.obec_id, -1) = g.o AND l.street_name_key = g.k
        WHERE {_ELIGIBILITY}
    )
    SELECT {_ELIGIBLE_COLS}
    FROM claimed c
    JOIN listings l ON l.sreality_id = c.sreality_id
    JOIN properties p ON p.id = l.property_id AND p.status = 'active'
    {_ELIGIBLE_ORDER}
"""


def _load_eligible(
    conn: Any,
    restrict_property_ids: set[int] | None = None,
    restrict_street_groups: tuple[set[int], set[tuple[int, str]]] | None = None,
) -> list[ListingKey]:
    """One ListingKey per (listing, grouping key): a row with both a canonical
    street_id and a street name is dual-keyed into its 'id:' and 'name:' groups
    so cross-portal rows keyed differently can still meet (run_engine dedups
    the listing pairs that surface in both groups).

    Two MUTUALLY-EXCLUSIVE scoping modes keep the load O(work), not O(market):
    - `restrict_property_ids` scopes to those properties' OWN listings — the
      candidate-priority drain: the two properties of a queued candidate share a
      street+disposition, so they still land in one street group and get re-decided
      by the SAME resolve_pair, without scanning the world.
    - `restrict_street_groups` = (street_ids, name_keys) loads every eligible
      listing in those GROUPS (peers included) — the real-time --dirty drain: the
      claimed dirty properties' groups, computed from the STORED street_name_key
      (_claimed_street_groups), via the targeted-seek _ELIGIBLE_SCOPED_SQL so a
      dirty property re-decides against its whole group while staying O(dirty).
      obec-bounded ⇒ complete; the load folds a NULL obec to -1 to mirror
      _claimed_street_groups. (None on both = full scan.)"""
    params: dict[str, Any] = {}
    if restrict_street_groups is not None:
        # Targeted index-seek CTE (NOT the {filter} OR form, which scanned all eligible).
        # An EMPTY (set, set) -> empty unnests -> no rows (loads nothing, not a full scan).
        street_ids, name_keys = restrict_street_groups
        params["sids"] = list(street_ids)
        params["obecs"] = [o for o, _ in name_keys]
        params["keys"] = [k for _, k in name_keys]
        sql = _ELIGIBLE_SCOPED_SQL
    else:
        flt = ""
        if restrict_property_ids is not None:  # an EMPTY set restricts to nothing (not all)
            flt = "AND l.property_id = ANY(%(pids)s)"
            params["pids"] = list(restrict_property_ids)
        sql = _ELIGIBLE_SQL.format(filter=flt)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    keys: list[ListingKey] = []
    for r in rows:
        raw_street_id = int(r[4]) if r[4] is not None else None
        street_id = raw_street_id if raw_street_id is not None and raw_street_id > 0 else None
        obec_id = int(r[12]) if r[12] is not None else None
        # Groups are SHARDED by the disposition's loose-equivalence class: every
        # classify_pair-compatible pair shares a class (loss-free), and a busy street
        # splits into per-disposition shards that mostly fit MAX_GROUP_SIZE — the 40+
        # groups the engine used to skip whole were 18.7% of the eligible market.
        shard = disposition_class(r[5])
        for street_key in street_group_keys(r[3], raw_street_id, obec_id):
            keys.append(ListingKey(
                sreality_id=int(r[0]),
                property_id=int(r[1]) if r[1] is not None else None,
                source=r[2],
                street_key=f"{street_key}|d:{shard}",
                disposition=r[5],
                house_number=r[6],
                floor=int(r[7]) if r[7] is not None else None,
                area_m2=float(r[8]) if r[8] is not None else None,
                description=r[9],
                category_type=r[10],
                category_main=r[11],
                street_id=street_id,
                price_czk=int(r[13]) if r[13] is not None else None,
            ))
    return keys


def _group_by_street(keys: list[ListingKey]) -> dict[str, list[ListingKey]]:
    groups: dict[str, list[ListingKey]] = {}
    for k in keys:
        groups.setdefault(k.street_key, []).append(k)
    return groups


def _eligibility_counts(conn: Any) -> dict[str, int]:
    """Rule A breakdown over all listings (eligibility computed inline).

    Counts active AND inactive, matching the eligible set _load_eligible processes
    — an inactive listing still merges so its price history survives a delisting.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              count(*) FILTER (
                WHERE street IS NOT NULL AND street <> '' AND disposition IS NOT NULL
              ) AS eligible,
              count(*) FILTER (WHERE street IS NULL OR street = '') AS flagged_location,
              count(*) FILTER (
                WHERE street IS NOT NULL AND street <> '' AND disposition IS NULL
              ) AS flagged_disposition
            FROM listings
            """
        )
        row = cur.fetchone()
    return {
        "eligible": int(row[0]),
        "flagged_location": int(row[1]),
        "flagged_disposition": int(row[2]),
    }


def _merge_pair(conn: Any, a: ListingKey, b: ListingKey, reason: str, markers: dict[str, Any]) -> str | None:
    """Merge the two listings' properties (older survives). Returns the
    merge_group_id (the undo handle, recorded in the decision audit) on success,
    or None on skip.

    The in-memory ListingKeys hold the property_id as loaded at run start; a
    merge earlier this run may have retired one of them. merge_properties raises
    MergeError on a non-active survivor/retired, which we catch and skip — the
    daily re-run sees the settled state and completes the chain. The job is
    idempotent and converges over runs, so a deferred chain merge is harmless.
    """
    if a.property_id is None or b.property_id is None or a.property_id == b.property_id:
        return None
    # Survivor = the older property (smaller id is a stable proxy for first_seen).
    survivor, retired = sorted((a.property_id, b.property_id))
    try:
        res = merge_properties(
            conn, survivor_id=survivor, retired_id=retired,
            reason=reason, source="auto", confidence=markers.get("confidence"),
            markers=markers,
        )
        return res["data"]["merge_group_id"]
    except MergeError as exc:
        LOG.warning("merge %s<-%s skipped: %s", survivor, retired, exc)
        return None


def _canon_pair(a: ListingKey, b: ListingKey) -> tuple[int, int] | None:
    """Canonical (lo, hi) property pair, or None if either side is unlinked / same."""
    if a.property_id is None or b.property_id is None or a.property_id == b.property_id:
        return None
    return (min(a.property_id, b.property_id), max(a.property_id, b.property_id))


def _reconcile_stale_candidates(conn: Any, *, dry_run: bool) -> int:
    """Dismiss proposed candidates that point to a non-active property (a side has
    since merged away / been retired) — pure queue hygiene, recall-neutral. Returns
    the count affected (computed even on dry-run)."""
    count_sql = (
        "SELECT count(*) FROM property_identity_candidates c "
        "JOIN properties pl ON pl.id = c.left_property_id "
        "JOIN properties pr ON pr.id = c.right_property_id "
        "WHERE c.status = 'proposed' AND (pl.status <> 'active' OR pr.status <> 'active')"
    )
    with conn.cursor() as cur:
        cur.execute(count_sql)
        n = int(cur.fetchone()[0])
        if not dry_run and n:
            cur.execute(
                "UPDATE property_identity_candidates c "
                "SET status = 'dismissed', reviewed_at = now() "
                "FROM properties pl, properties pr "
                "WHERE pl.id = c.left_property_id AND pr.id = c.right_property_id "
                "AND c.status = 'proposed' AND (pl.status <> 'active' OR pr.status <> 'active')"
            )
    return n


def _proposed_candidate_property_ids(
    conn: Any, redecide_hours: float | None = None,
) -> set[int]:
    """Every property that appears in a DUE still-`proposed` /dedup candidate — the
    work-list for the candidate-priority drain. The two properties of a candidate share
    a street + disposition, so scoping `_load_eligible` to this set re-forms exactly
    those pairs in their street group and re-decides them via resolve_pair, without a
    full market scan.

    DUE (migration 272 — the treadmill fix): never engine-evaluated, evaluated longer
    than `redecide_hours` ago (backoff), or carrying FRESH photo evidence (an image
    CLIP-tagged after the stamp — the same signal the prior-dismissal consult trusts).
    Everything else was already looked at with the same inputs; re-deciding it every
    2h re-chewed ~296 cached-inconclusive pairs per run for nothing. `redecide_hours`
    None = no due-filter (every proposed candidate, the historical behavior)."""
    if redecide_hours is None:
        sql = ("SELECT left_property_id, right_property_id "
               "FROM property_identity_candidates WHERE status = 'proposed'")
        params: dict[str, Any] = {}
    else:
        sql = """
            SELECT c.left_property_id, c.right_property_id
            FROM property_identity_candidates c
            WHERE c.status = 'proposed'
              AND (
                c.last_engine_decision_at IS NULL
                OR c.last_engine_decision_at
                     < now() - make_interval(hours => %(backoff_h)s)
                OR EXISTS (
                    SELECT 1
                    FROM listings l
                    JOIN images i ON i.sreality_id = l.sreality_id
                    WHERE l.property_id IN (c.left_property_id, c.right_property_id)
                      AND i.clip_tagged_at > c.last_engine_decision_at)
              )
        """
        params = {"backoff_h": float(redecide_hours)}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out: set[int] = set()
    for left, right in rows:
        if left is not None:
            out.add(int(left))
        if right is not None:
            out.add(int(right))
    return out


def _stamp_engine_looked(conn: Any, looked: dict[tuple[int, int], str]) -> None:
    """Record 'the engine evaluated this proposed pair and left it proposed' — the
    stamp the candidate drain's due-filter keys on. Set-based, proposed rows only;
    terminal outcomes (merged / dismissed) change status elsewhere and never need it."""
    if not looked:
        return
    items = list(looked.items())
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE property_identity_candidates c
            SET last_engine_decision_at = now(), engine_decision = v.reason
            FROM (SELECT unnest(%(los)s::bigint[]) AS lo,
                         unnest(%(his)s::bigint[]) AS hi,
                         unnest(%(reasons)s::text[]) AS reason) v
            WHERE c.left_property_id = v.lo AND c.right_property_id = v.hi
              AND c.status = 'proposed'
            """,
            {
                "los": [lo for (lo, _hi), _r in items],
                "his": [hi for (_lo, hi), _r in items],
                "reasons": [r for _pair, r in items],
            },
        )


# Publication gate (migration 273): a property is visible in Browse / map / Stats / to
# agents / in watchdog only once the dedup engine has evaluated it. finalize() stamps every
# property whose street/geo group this run scanned; the ineligible sweep + merge/split paths
# publish the rest. Only NULL -> now(), so a re-scan never overwrites a stamped reason.
_PUBLICATION_STAMP_CHUNK = 5000


def _stamp_publication_checked(conn: Any, property_ids: set[int]) -> int:
    """Publish the properties the engine dedup-evaluated this run (reason 'dedup_checked').
    Set-based, chunked, unpublished-only — idempotent across runs."""
    if not property_ids:
        return 0
    ids = sorted(property_ids)
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(ids), _PUBLICATION_STAMP_CHUNK):
            cur.execute(
                "UPDATE properties SET published_at = now(), "
                "publish_reason = 'dedup_checked' "
                "WHERE id = ANY(%(ids)s) AND published_at IS NULL",
                {"ids": ids[i:i + _PUBLICATION_STAMP_CHUNK]},
            )
            total += cur.rowcount or 0
    return total


# --- real-time dedup queue drain (dedup_dirty_properties, migration 242; the writer-side
#     enqueue is scraper.db.mark_properties_dedup_dirty_for_images, mirroring dirty_properties)
# The real-time lane is a LATENCY optimization backstopped by the 6h full scan, so its claim is
# NEWEST-FIRST + TTL-evicted (not FIFO): a freshly-tagged cross-portal listing must merge in
# minutes even when a transient backlog (a portal launch) is draining, and a row older than the
# TTL is dropped rather than pinning the head. Bound + TTL keep the queue O(steady-state inflow).
# The backstop is only as good as full-scan COVERAGE (the 2026-07 audit caught the pre-cursor
# scans chronically deadline-cut at ~9% of the market, so the then-unconditional TTL silently
# discarded uncovered work). Two rails now hold the handoff honest: eviction is CYCLE-GATED
# (_prune_stale_dedup_dirty only drops rows a COMPLETED full-scan cycle provably covered —
# migration 261's cursor), and every run row records run_kind + truncated (migration 262) so a
# stalling cursor is a queryable fact rather than silent loss.
_DEDUP_DIRTY_TTL_HOURS = 24  # the eviction horizon; evicted rows fall to the full scan's frontier


def _claim_dedup_dirty(conn: Any, cutoff: Any, limit: int | None = None) -> list[int]:
    """Property ids dirtied at/before `cutoff` (claim slice), NEWEST-FIRST — an ORDERED
    list, and the order is load-bearing: run_engine processes the claimed groups in this
    rank (priority_property_order), so the queue head actually advances every run. (The
    claim was historically returned as a set, which discarded the ORDER BY — groups then
    processed in load order, i.e. obec-ASC, and the same head-of-list groups cleared every
    run while the claimed-newest starved: claimed ~600 / cleared ~40, hourly, for days.)
    A row re-dirtied AFTER cutoff (marked_at > cutoff via a writer's ON CONFLICT) is
    neither claimed nor cleared — it survives to the next pass (race-free + terminating,
    mirrors recompute's dirty drain).

    `limit` bounds the slice to the N FRESHEST dirty properties. The drain MUST be bounded like
    every sibling drain (a backlog can spike on a portal launch), and it claims NEWEST-first so
    the real-time SLO ("a new cross-portal listing merges in minutes") holds even under backlog —
    the stale tail ages out via `_prune_stale_dedup_dirty` and is swept by the 6h full scan, so it
    never pins the head the way FIFO did (a June-backfill head that never cleared)."""
    sql = "SELECT property_id FROM dedup_dirty_properties WHERE marked_at <= %s ORDER BY marked_at DESC"
    params: list[Any] = [cutoff]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [int(r[0]) for r in cur.fetchall()]


def _dirty_queue_age_p95_seconds(conn: Any, cutoff: Any) -> int | None:
    """p95 age of the WHOLE dirty queue at claim time — the starvation gauge (a rising
    p95 with dirty_pruned=0 means the drain+full-scan handoff is falling behind)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT percentile_cont(0.95) WITHIN GROUP "
            "(ORDER BY extract(epoch FROM (now() - marked_at))) "
            "FROM dedup_dirty_properties WHERE marked_at <= %s",
            (cutoff,),
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _prune_stale_dedup_dirty(conn: Any, ttl_hours: int = _DEDUP_DIRTY_TTL_HOURS) -> int:
    """Evict dirty rows older than the TTL AND provably covered by a COMPLETED full-scan cycle
    (`marked_at < dedup_scan_state.last_cycle_started_at`: every street group was scanned at some
    point during that cycle, i.e. AFTER the row was enqueued — eviction hands work to a backstop
    that actually covered it, never silent loss). Before the cursor (migration 261) the full scan
    head-restarted every run and covered ~9% of the market, so the old unconditional TTL was
    discarding uncovered work. No completed cycle yet -> the NULL comparison evicts NOTHING (safe
    default); the queue then only grows if BOTH the dirty drain and full cycles stall — which the
    depth metric + stall banner make loud, the honest failure mode. Still bounds the queue in
    steady state (cycles complete every ~2-3 days) and, with the newest-first claim + per-group
    clear, the head is never pinned. Returns rows evicted."""
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM dedup_dirty_properties "
            f"WHERE marked_at < now() - interval '{ttl_hours} hours' "
            f"  AND marked_at < (SELECT last_cycle_started_at FROM dedup_scan_state "
            f"                   WHERE lane = 'street')"
        )
        return cur.rowcount or 0


def _load_scan_state(conn: Any, lane: str = "street") -> dict[str, Any]:
    """The lane's scan frontier (migration 261). Missing row == a fresh lane: cursor at the
    top, no cycle ever completed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cursor_key, cycle_started_at FROM dedup_scan_state WHERE lane = %s",
            (lane,),
        )
        row = cur.fetchone()
    if row is None:
        return {"cursor_key": None, "cycle_started_at": None}
    return {"cursor_key": row[0], "cycle_started_at": row[1]}


def _save_scan_state(
    conn: Any, lane: str, *, cursor_key: str | None,
    cycle_started_at: Any, completed: bool,
) -> None:
    """Persist the frontier after a full-scan run. `completed` == the run reached the end of
    the ordered group list: stamp the finished cycle (what gates the dirty-queue TTL eviction)
    and reset the cursor so the next run starts a new cycle from the top."""
    with conn.cursor() as cur:
        if completed:
            cur.execute(
                """
                INSERT INTO dedup_scan_state
                  (lane, cursor_key, cycle_started_at, last_cycle_started_at,
                   last_cycle_completed_at, updated_at)
                VALUES (%s, NULL, NULL, %s, now(), now())
                ON CONFLICT (lane) DO UPDATE SET
                  cursor_key = NULL, cycle_started_at = NULL,
                  last_cycle_started_at = EXCLUDED.last_cycle_started_at,
                  last_cycle_completed_at = now(), updated_at = now()
                """,
                (lane, cycle_started_at),
            )
        else:
            cur.execute(
                """
                INSERT INTO dedup_scan_state (lane, cursor_key, cycle_started_at, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (lane) DO UPDATE SET
                  cursor_key = EXCLUDED.cursor_key,
                  cycle_started_at = EXCLUDED.cycle_started_at, updated_at = now()
                """,
                (lane, cursor_key, cycle_started_at),
            )


def _should_run_geo(*, geo: bool, geo_only: bool, geo_enabled: bool, dirty: bool) -> bool:
    """Whether MAIN should run the FULL geo (single-dwelling) pass. Geo runs ONLY on an explicit
    flag — it is NEVER auto-bolted onto the street full-scan / candidate-drain (where it was
    deadline-starved / inherited the apartment restrict and produced nothing). `--geo-only` (the
    dedicated scheduled cron) is gated by the `dedup_geo_enabled` master switch — when it's passed
    but the setting is off this returns False, and the caller logs "GEO-only run but
    dedup_geo_enabled is off" and exits (no work). `--geo` forces it ad-hoc (ignores the setting,
    for debugging). Always False on the real-time dirty drain: run_dirty_pass runs its OWN geo
    sub-pass scoped to the claimed properties' cells, so main appending the FULL geo pass there
    would double-run geo, not enable it."""
    return (geo or (geo_only and geo_enabled)) and not dirty


def _clear_dedup_dirty(conn: Any, property_ids: set[int], cutoff: Any) -> int:
    """Delete the claimed ids that have NOT been re-dirtied since the cutoff."""
    if not property_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM dedup_dirty_properties WHERE property_id = ANY(%s) "
                    "AND marked_at <= %s", (list(property_ids), cutoff))
        return cur.rowcount or 0


def _claimed_street_groups(
    conn: Any, property_ids: set[int]
) -> tuple[set[int], set[tuple[int, str]]]:
    """The street GROUPS the claimed dirty properties' ELIGIBLE listings belong to —
    the work-list the --dirty scoped load (_load_eligible restrict_street_groups)
    expands into full groups, peers included, instead of scanning the whole market.

    Returns (street_ids, name_keys): `street_ids` are the positive portal street ids
    (the 'id:' groups); `name_keys` are (coalesce(obec_id,-1), street_name_key) pairs
    — the SAME obec-scoped 'name:' grouping the engine keys on (street_group_keys),
    with a NULL obec folded to -1 so the ~0.4% of eligible rows lacking a CZ obec
    (which group as `name:None:<key>`) stay matchable. Reading the STORED
    street_name_key is what lets this be a cheap SQL filter rather than recomputing
    the key for the market in Python. Loading every listing matching either set is
    COMPLETE: a group containing a claimed property is keyed by one of these, and
    street groups are obec-bounded. Empty sets => the claimed properties carry no
    eligible (street+disposition) listing — the scoped load then returns nothing."""
    if not property_ids:
        return set(), set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT l.street_id, coalesce(l.obec_id, -1) AS obec,
                            l.street_name_key
            FROM listings l
            WHERE l.property_id = ANY(%s)
              AND l.street IS NOT NULL AND l.street <> '' AND l.disposition IS NOT NULL
            """,
            (list(property_ids),),
        )
        rows = cur.fetchall()
    street_ids: set[int] = set()
    name_keys: set[tuple[int, str]] = set()
    for sid, obec, key in rows:
        if sid is not None and int(sid) > 0:
            street_ids.add(int(sid))
        if key is not None:
            name_keys.add((int(obec), key))
    return street_ids, name_keys


# The geo-family analogue of _claimed_street_groups: the STORED geo_cell_key is what
# lets the dirty geo sub-pass be a cheap SQL filter (an index seek per cell via
# listings_geo_cell_key_idx) rather than a Python recompute over the market. The
# eligibility mirrors _GEO_ELIGIBLE_SQL's WHERE minus the properties join (the scoped
# load re-applies it): active single-dwelling rows with an area that are NOT
# street-eligible (those are the street sub-pass's work). A NULL stored cell can't be
# grouped (_load_geo_eligible skips it), so it is excluded here AND treated as
# not-geo-eligible by _claimed_family_eligibility — the two stay coherent.
_CLAIMED_GEO_CELLS_SQL = f"""
    SELECT DISTINCT l.geo_cell_key
    FROM listings l
    WHERE l.property_id = ANY(%s)
      AND l.geo_cell_key IS NOT NULL
      AND {GEO_ELIGIBLE_PREDICATE}
      AND NOT ({_ELIGIBILITY})
"""


def _claimed_geo_cells(conn: Any, property_ids: set[int]) -> set[str]:
    """The geo blocking CELLS the claimed dirty properties' geo-eligible listings
    occupy — the work-list the --dirty geo sub-pass expands into full cells (peers
    included) via _load_geo_eligible(restrict_cells=...). Empty claim / no
    geo-family listings => empty set (the sub-pass is skipped, never a full scan)."""
    if not property_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(_CLAIMED_GEO_CELLS_SQL, (list(property_ids),))
        return {r[0] for r in cur.fetchall() if r[0] is not None}


# Per-family eligibility of each claimed property — what the dirty pass's PER-FAMILY
# clear keys on. The geo arm mirrors _CLAIMED_GEO_CELLS_SQL (stored cell required), so
# "geo-eligible" here == "the geo sub-pass can actually load it"; a geo-family row
# whose cell isn't stamped yet resolves as not-geo-eligible and falls to the scheduled
# geo scan instead of pinning its queue row forever.
_CLAIMED_FAMILY_ELIGIBILITY_SQL = f"""
    SELECT l.property_id,
           bool_or({_ELIGIBILITY}) AS street_eligible,
           bool_or({GEO_ELIGIBLE_PREDICATE}
                   AND NOT ({_ELIGIBILITY})
                   AND l.geo_cell_key IS NOT NULL) AS geo_eligible
    FROM listings l
    WHERE l.property_id = ANY(%s)
    GROUP BY 1
"""


def _claimed_family_eligibility(
    conn: Any, property_ids: set[int]
) -> dict[int, tuple[bool, bool]]:
    """property_id -> (street_eligible, geo_eligible) over ANY of its listings. A pid
    clears from dedup_dirty_properties only when every family it is eligible for was
    resolved this pass; a pid eligible for NEITHER clears immediately (queue hygiene —
    the ineligible publish sweep owns its publication). Missing pids (no listings)
    read as (False, False)."""
    if not property_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(_CLAIMED_FAMILY_ELIGIBILITY_SQL, (list(property_ids),))
        return {int(r[0]): (bool(r[1]), bool(r[2])) for r in cur.fetchall()}


def _resolve_candidates(conn: Any, pairs: set[tuple[int, int]], new_status: str) -> int:
    """Set-based: mark every still-'proposed' candidate for these (lo, hi) pairs as
    `new_status`. A no-op for pairs that have no proposed candidate. Returns rowcount."""
    if not pairs:
        return 0
    los = [p[0] for p in pairs]
    his = [p[1] for p in pairs]
    with conn.cursor() as cur:
        cur.execute(
            # Two-arg unnest zips the arrays element-wise into (lo, hi) rows — the
            # canonical, unambiguous parallel-unnest form.
            "UPDATE property_identity_candidates c "
            "SET status = %s, reviewed_at = now() "
            "FROM unnest(%s::bigint[], %s::bigint[]) AS p(lo, hi) "
            "WHERE c.left_property_id = p.lo AND c.right_property_id = p.hi "
            "AND c.status = 'proposed'",
            (new_status, los, his),
        )
        return cur.rowcount or 0


def _record_auto_dismissed(conn: Any, pairs: set[tuple[int, int]], tier: str) -> int:
    """Insert-if-absent a status='dismissed' candidate row for each pair the engine
    auto-dismissed on a VERDICT this run (floor-plan different_layout / confident visual
    "different") — the durable negative decision the prior-dismissal consult reads. Only
    verdict-backed dismissals land here (rule-C rejects are deterministic re-computations,
    not worth a row); an already-existing row was flipped by _resolve_candidates and the
    ON CONFLICT leaves it alone. The full decision detail lives in dedup_pair_audit; this
    row is the cheap indexed "already decided" marker. Returns rows inserted."""
    if not pairs:
        return 0
    los = [p[0] for p in pairs]
    his = [p[1] for p in pairs]
    with conn.cursor() as cur:
        cur.execute(
            # Re-dismissal refreshes reviewed_at: a pair re-opened on fresh evidence and
            # re-DISMISSED must move its settled timestamp forward, or it reads as
            # perpetually "fresh" and re-runs every scoped pass (the treadmill again).
            # Guarded to dismissed rows only — proposed/merged rows are never touched.
            "INSERT INTO property_identity_candidates "
            "  (left_property_id, right_property_id, tier, status, reviewed_at) "
            "SELECT p.lo, p.hi, %s, 'dismissed', now() "
            "FROM unnest(%s::bigint[], %s::bigint[]) AS p(lo, hi) "
            "ON CONFLICT (left_property_id, right_property_id) DO UPDATE "
            "  SET reviewed_at = now() "
            "  WHERE property_identity_candidates.status = 'dismissed'",
            (tier, los, his),
        )
        return cur.rowcount or 0


def _load_prior_dismissed(
    conn: Any, property_ids: set[int],
) -> dict[tuple[int, int], Any]:
    """{(lo, hi): reviewed_at} for every DISMISSED candidate pair among these properties —
    the prior-decision map the scoped drains consult so a settled pair isn't re-probed
    every hour (the audit's 5.8x dismissal treadmill). Loaded ONCE per scoped run
    (O(scope) ids — the dirty/candidate work-lists are small by construction; full scans
    pass nothing and never consult)."""
    if not property_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT left_property_id, right_property_id, reviewed_at "
            "FROM property_identity_candidates "
            "WHERE status = 'dismissed' AND left_property_id = ANY(%(pids)s) "
            "AND right_property_id = ANY(%(pids)s)",
            {"pids": list(property_ids)},
        )
        return {(int(r[0]), int(r[1])): r[2] for r in cur.fetchall()}


def _enqueue_candidate(
    conn: Any, a: ListingKey, b: ListingKey, markers: dict[str, Any],
    *, tier: str = "street_disposition", reopen: bool = False,
) -> None:
    # `markers["tier"]` is the source of truth for the COLUMN (resolve_pair sets it to
    # ctx.tier — 'street_disposition' or 'geo'); the `tier` kwarg is only the fallback for
    # the rare markers dict that omits it (the street-only rule-B auto_merge_off path).
    #
    # `reopen` (set ONLY when the prior-dismissal consult re-decided this pair on FRESH
    # photo evidence): a queue outcome must then RE-PROPOSE an engine-dismissed row —
    # otherwise the recall valve is one-way (the re-decision lands on ON CONFLICT DO
    # NOTHING against the recorded dismissal and the operator never sees the pair; the
    # review caught this). Operator dismissals (reviewed_action='operator') stay
    # respected; the default DO NOTHING path is unchanged for every other caller, so a
    # mass re-decide (a full scan, an auto-merge-off dispatch) can never bulk-reopen
    # settled dismissals.
    from psycopg.types.json import Jsonb
    if a.property_id is None or b.property_id is None or a.property_id == b.property_id:
        return
    lo, hi = sorted((a.property_id, b.property_id))
    conflict = (
        """ON CONFLICT (left_property_id, right_property_id) DO UPDATE
                SET status = 'proposed', reviewed_at = NULL,
                    confidence = EXCLUDED.confidence,
                    markers_matched = EXCLUDED.markers_matched
                WHERE property_identity_candidates.status = 'dismissed'
                  AND property_identity_candidates.reviewed_action
                      IS DISTINCT FROM 'operator'"""
        if reopen else
        "ON CONFLICT (left_property_id, right_property_id) DO NOTHING"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO property_identity_candidates
                (left_property_id, right_property_id, tier, confidence, markers_matched)
            VALUES (%s, %s, %s, %s, %s)
            {conflict}
            """,
            (lo, hi, markers.get("tier", tier), markers.get("confidence"), Jsonb(markers)),
        )


# --- geo path (single-dwelling families) ------------------------------------
# Disposition-less houses/land/commercial that the street pass can't reach. Blocked
# by geo cell (one obec + a rounded coordinate). ACTIVE only for P1 (the operator's
# pain is active duplicate cards); inactive-for-history is a later concern. The
# NOT(street AND disposition) clause hands the rare disposition-bearing non-apartment
# to the street pass instead, so the two passes never double-handle a pair.
_GEO_ELIGIBLE_SQL = f"""
    SELECT l.sreality_id, l.property_id, l.source, l.house_number,
           coalesce(l.area_m2, l.estate_area, l.usable_area) AS area,
           left(l.description, 600) AS description,
           l.category_type, l.category_main, l.price_czk,
           ST_Y(l.geom::geometry) AS lat, ST_X(l.geom::geometry) AS lng,
           l.geo_cell_key
    FROM listings l
    JOIN properties p ON p.id = l.property_id AND p.status = 'active'
    WHERE {GEO_ELIGIBLE_PREDICATE}
      AND NOT ({_ELIGIBILITY})
      {{filter}}
    ORDER BY l.obec_id, l.category_main, l.category_type
"""


def _load_geo_eligible(conn: Any,
                       restrict_property_ids: set[int] | None = None,
                       restrict_cells: set[str] | None = None) -> list[ListingKey]:
    """One ListingKey per geo-eligible single-dwelling listing, keyed by the STORED
    listings.geo_cell_key (migration 276 trigger — the single SQL definition of the
    blocking cell; no Python recompute) so the existing _group_by_street groups them.
    Carries lat/lng/price for the geo classifier; disposition/floor/street_id are
    unused on this path. Rows whose stored key is still NULL (pre-backfill / trigger
    not yet fired) are skipped — they merely wait for the next run, never mis-group.

    Two MUTUALLY-EXCLUSIVE scoping modes (mirrors _load_eligible); an EMPTY set on
    either restricts to nothing (not all):
    - `restrict_property_ids` scopes to those properties' OWN listings (the candidate
      drain).
    - `restrict_cells` loads every geo-eligible listing in those CELLS — peers
      included, an index seek on listings_geo_cell_key_idx — the --dirty geo
      sub-pass: a dirty property re-decides against its whole cell, O(dirty)."""
    if restrict_property_ids is not None and restrict_cells is not None:
        raise ValueError("restrict_property_ids and restrict_cells are mutually exclusive")
    params: dict[str, Any] = {}
    flt = ""
    if restrict_cells is not None:
        flt = "AND l.geo_cell_key = ANY(%(cells)s)"
        params["cells"] = list(restrict_cells)
    elif restrict_property_ids is not None:
        flt = "AND l.property_id = ANY(%(pids)s)"
        params["pids"] = list(restrict_property_ids)
    with conn.cursor() as cur:
        cur.execute(_GEO_ELIGIBLE_SQL.format(filter=flt), params)
        rows = cur.fetchall()
    keys: list[ListingKey] = []
    for r in rows:
        cell = r[11]
        if cell is None:
            continue
        keys.append(ListingKey(
            sreality_id=int(r[0]),
            property_id=int(r[1]) if r[1] is not None else None,
            source=r[2], street_key=cell, disposition="",
            house_number=r[3], floor=None,
            area_m2=float(r[4]) if r[4] is not None else None,
            description=r[5], category_type=r[6], category_main=r[7],
            street_id=None,
            lat=float(r[9]) if r[9] is not None else None,
            lng=float(r[10]) if r[10] is not None else None,
            price_czk=int(r[8]) if r[8] is not None else None,
        ))
    return keys


# The pHash exclusion predicate is shared with the /dedup evidence reader via
# toolkit.dedup_engine.render_exclusion_clause (one source, so they never drift).
_render_exclusion_predicate = render_exclusion_clause


def _phash_identical_pairs(
    conn: Any, a_id: int, b_id: int, excluded_tags: tuple[str, ...] = (),
    render_exclude_min: float | None = None,
) -> int:
    """Count near-identical image pairs (Hamming <= PHASH_IDENTICAL_MAX) across the two
    listings' stored images — no classify needed, so this runs BEFORE the LLM stage. A
    development sharing one stock facade/plan yields 1 such pair; an actual re-post of the
    same listing shares many; the PHASH_MIN_IDENTICAL_PAIRS count threshold separates them.

    For byt, `excluded_tags` (NON_INTERIOR_TAGS) drops any pair touching a KNOWN-exterior /
    shared image, and `render_exclude_min` drops any pair touching a high render_score image
    (a shared development RENDER) — both sourced from CLIP `image_clip_tags`. Untagged /
    not-yet-scored images still count, so recall holds as CLIP coverage fills in. Empty /
    None (non-byt) -> count any image, unchanged.
    """
    sql = (
        "SELECT count(*) FROM images ia JOIN images ib ON true "
        "WHERE ia.sreality_id = %(a)s AND ib.sreality_id = %(b)s "
        "AND ia.phash IS NOT NULL AND ib.phash IS NOT NULL "
        "AND bit_count((ia.phash # ib.phash)::bit(64)) <= %(max)s"
    )
    params: dict[str, Any] = {"a": a_id, "b": b_id, "max": PHASH_IDENTICAL_MAX}
    sql += _render_exclusion_predicate(params, "ia", excluded_tags, render_exclude_min)
    sql += _render_exclusion_predicate(params, "ib", excluded_tags, render_exclude_min)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()[0])


def _phash_distinctive_match(
    conn: Any, a_id: int, b_id: int, rooms: tuple[str, ...] | frozenset[str] = DISTINCTIVE_ROOMS,
    render_exclude_min: float | None = None,
) -> bool:
    """True if the two listings share >=1 near-identical image pair where BOTH images are
    a DISTINCTIVE room (kitchen/bathroom, CLIP-tagged). A single such match auto-merges
    (operator policy): wet rooms are unit-specific, not shared development marketing, so
    one identical match there is conclusive — unlike the >=2 needed for generic images.
    `render_exclude_min` drops a shared kitchen/bathroom RENDER (it is tagged kitchen but
    high render_score), so a reused render can't trigger the single-match override."""
    rfilter = ""
    params: dict[str, Any] = {"a": a_id, "b": b_id, "rooms": list(rooms), "max": PHASH_IDENTICAL_MAX}
    if render_exclude_min is not None:
        rfilter = " AND coalesce({t}.render_score, 0) < %(rmin)s"
        params["rmin"] = render_exclude_min
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM images ia"
            "  JOIN image_clip_tags ta ON ta.image_id = ia.id AND ta.logical_tag = ANY(%(rooms)s)"
            + rfilter.format(t="ta") +
            "  JOIN images ib ON ib.sreality_id = %(b)s AND ib.phash IS NOT NULL"
            "  JOIN image_clip_tags tb ON tb.image_id = ib.id AND tb.logical_tag = ANY(%(rooms)s)"
            + rfilter.format(t="tb") +
            "  WHERE ia.sreality_id = %(a)s AND ia.phash IS NOT NULL"
            "    AND bit_count((ia.phash # ib.phash)::bit(64)) <= %(max)s)",
            params,
        )
        return bool(cur.fetchone()[0])


def _phash_group_counts(
    conn: Any, sreality_ids: list[int], excluded_tags: tuple[str, ...] = (),
    render_exclude_min: float | None = None,
) -> dict[tuple[int, int], int]:
    """_phash_identical_pairs for EVERY cross-listing pair of a street group in ONE round
    trip: {(lo_sid, hi_sid): count}, pairs with no near-identical match absent (= 0). The
    2026-07 audit measured per-pair sequential round-trips as the dirty lane's cost floor
    (~0.5-0.75 s/pair from a GitHub runner to the EU pooler ≈ the whole 1200 s budget), so
    the group batch turns O(pairs) pHash trips into O(groups). Exclusion predicates are the
    SAME `render_exclusion_clause` fragments as the per-pair query — one source, no drift."""
    sql = (
        "SELECT least(ia.sreality_id, ib.sreality_id), "
        "       greatest(ia.sreality_id, ib.sreality_id), count(*) "
        "FROM images ia JOIN images ib ON ia.sreality_id < ib.sreality_id "
        "WHERE ia.sreality_id = ANY(%(ids)s) AND ib.sreality_id = ANY(%(ids)s) "
        "AND ia.phash IS NOT NULL AND ib.phash IS NOT NULL "
        "AND bit_count((ia.phash # ib.phash)::bit(64)) <= %(max)s"
    )
    params: dict[str, Any] = {"ids": list(sreality_ids), "max": PHASH_IDENTICAL_MAX}
    sql += _render_exclusion_predicate(params, "ia", excluded_tags, render_exclude_min)
    sql += _render_exclusion_predicate(params, "ib", excluded_tags, render_exclude_min)
    sql += " GROUP BY 1, 2"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {(int(r[0]), int(r[1])): int(r[2]) for r in cur.fetchall()}


def _phash_group_distinctive(
    conn: Any, sreality_ids: list[int],
    rooms: tuple[str, ...] | frozenset[str] = DISTINCTIVE_ROOMS,
    render_exclude_min: float | None = None,
) -> set[tuple[int, int]]:
    """_phash_distinctive_match for every cross-listing pair of a group in one round trip:
    the (lo_sid, hi_sid) pairs sharing >=1 near-identical DISTINCTIVE-room (kitchen/
    bathroom) image pair. Same predicates as the per-pair query, batched."""
    rfilter = ""
    params: dict[str, Any] = {
        "ids": list(sreality_ids), "rooms": list(rooms), "max": PHASH_IDENTICAL_MAX}
    if render_exclude_min is not None:
        rfilter = " AND coalesce({t}.render_score, 0) < %(rmin)s"
        params["rmin"] = render_exclude_min
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT least(ia.sreality_id, ib.sreality_id),"
            "       greatest(ia.sreality_id, ib.sreality_id)"
            " FROM images ia"
            "  JOIN image_clip_tags ta ON ta.image_id = ia.id AND ta.logical_tag = ANY(%(rooms)s)"
            + rfilter.format(t="ta") +
            "  JOIN images ib ON ib.sreality_id = ANY(%(ids)s) AND ib.phash IS NOT NULL"
            "   AND ia.sreality_id < ib.sreality_id"
            "  JOIN image_clip_tags tb ON tb.image_id = ib.id AND tb.logical_tag = ANY(%(rooms)s)"
            + rfilter.format(t="tb") +
            " WHERE ia.sreality_id = ANY(%(ids)s) AND ia.phash IS NOT NULL"
            "   AND bit_count((ia.phash # ib.phash)::bit(64)) <= %(max)s",
            params,
        )
        return {(int(r[0]), int(r[1])) for r in cur.fetchall()}


def _high_render_image_ids(conn: Any, a_id: int, b_id: int, threshold: float) -> set[int]:
    """image_ids of the two listings whose CLIP render_score >= threshold — shared
    development RENDERS excluded from the forensic compare (migration 239)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.image_id FROM image_clip_tags t JOIN images i ON i.id = t.image_id "
            "WHERE i.sreality_id IN (%(a)s, %(b)s) AND t.render_score >= %(rmin)s",
            {"a": a_id, "b": b_id, "rmin": threshold},
        )
        return {int(r[0]) for r in cur.fetchall()}


# A CLIP floor_plan tag below this confidence is dropped from the gate's plan set. Validated on
# the live distribution: 95% of CLIP floor_plan tags score >= 0.52 (p05=0.518), 87.5% >= 0.70, and
# the false positives (a non-plan photo mis-tagged floor_plan — e.g. an idnes location map at 0.36)
# concentrate BELOW 0.50 (4.4% of tags). Dropping the low-confidence tail stops a phantom plan from
# creating a false 'one-sided' read that queues an otherwise-mergeable pair. The floor is CLIP-only:
# only `image_clip_tags.confidence` is a numeric 0..1 score. The LLM `image_room_classifications`
# carries a coarse 'high'/'medium'/'low' TEXT enum (and only ~530 floor_plan rows), so that source
# is left unfiltered — a numeric floor there would be a type error, and the validated false-positive
# problem is CLIP-specific.
FLOOR_PLAN_MIN_CONFIDENCE = 0.50


def _floor_plan_image_ids(
    conn: Any, sreality_id: int, min_confidence: float = FLOOR_PLAN_MIN_CONFIDENCE,
) -> list[int]:
    """Stored floor-plan image ids for a listing — a CLIP floor_plan tag at or above
    `min_confidence` OR an LLM room classification (enum confidence, unfiltered); storage_path
    present so they can be sent to vision. Empty -> no floor plan."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.id FROM images i "
            "WHERE i.sreality_id = %(sid)s AND i.storage_path IS NOT NULL AND ("
            "  EXISTS (SELECT 1 FROM image_clip_tags t "
            "          WHERE t.image_id = i.id AND t.logical_tag = %(fp)s "
            "            AND t.confidence >= %(minconf)s)"
            "  OR EXISTS (SELECT 1 FROM image_room_classifications c "
            "             WHERE c.image_id = i.id AND c.room_type = %(fp)s)) "
            "ORDER BY i.sequence ASC NULLS LAST, i.id ASC",
            {"sid": sreality_id, "fp": FLOOR_PLAN_ROOM_TYPE, "minconf": min_confidence},
        )
        return [r[0] for r in cur.fetchall()]


def _clip_incomplete(conn: Any, sreality_ids: list[int], model: str) -> list[int]:
    """Which of these listings are NOT fully CLIP-tagged yet — any STORED image still pending
    the tagger (`clip_tagged_at IS NULL`). The dedup readiness gate: deciding a pair before a
    listing's images finish tagging mis-reads the floor-plan gate (a pending plan looks absent,
    the false 'one-sided') and under-pairs the visual flow, so such a pair is DEFERRED until
    tagging completes. Gates on `clip_tagged_at` (processed?), NOT on tag presence — a
    processed-but-untaggable image is terminal, so it never blocks readiness forever. One query
    for the pair (a single round-trip). `model` is unused here (the tagger stamps `clip_tagged_at`
    model-agnostically) but kept for call-site symmetry."""
    del model
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.sid FROM unnest(%s::bigint[]) AS s(sid) "
            "WHERE EXISTS (SELECT 1 FROM images i WHERE i.sreality_id = s.sid "
            "              AND i.storage_path IS NOT NULL AND i.clip_tagged_at IS NULL)",
            (sreality_ids,),
        )
        return [int(r[0]) for r in cur.fetchall()]


def _trigger_clip_tagging(conn: Any, sreality_ids: list[int], model: str) -> None:
    """Re-queue a CLIP-untagged listing's images for the tagger (clip_tag.yml drains
    `clip_tagged_at IS NULL`): reset the marker on every stored image of these listings that
    has no CLIP tag for `model`. A never-tagged image is already pending (no-op); a stuck one
    (marked done but tagless — a failed/old run) gets re-queued, so a gap can self-heal
    instead of deferring forever."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE images SET clip_tagged_at = NULL "
            "WHERE sreality_id = ANY(%s) AND storage_path IS NOT NULL "
            "  AND clip_tagged_at IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM image_clip_tags t "
            "                  WHERE t.image_id = images.id AND t.model = %s)",
            (sreality_ids, model),
        )


def _floor_plan_gate(
    conn: Any, a_id: int, b_id: int, *, floor_plan_fn: Any, vision_budget: list[int],
    inconclusive_to_review: bool = True, cache: "_ProbeCache | None" = None,
) -> str:
    """The floor-plan validation on a pair the engine WOULD merge (pHash or visual).
    Returns 'merge' | 'dismiss' | 'queue' | 'defer'. It is a CONTRADICTION VETO layered on a
    strong primary signal (pHash >=2 / visual High): the ONLY thing it may do beyond letting the
    merge proceed is DISMISS on a proven `different_layout`, or QUEUE the one genuinely-human case
    (both sides have real 2D plans yet the compare is inconclusive). Whenever it CANNOT do a
    2D-plan-to-2D-plan comparison it is a no-op → the primary signal MERGES (it never queues a
    would-merge pair just because a plan can't be read — that produced ~600 false-queues of obvious
    cross-portal re-posts whose "plans" were 3D renders).
      * both sides carry a plan-tagged image + a Sonnet verdict is available -> the verdict decides:
        'different_layout' -> 'dismiss'; 'no_2d_plan' (>=1 side has only 3D renders / illegible, so
        no reliable 2D compare) -> 'merge'; 'inconclusive' (BOTH have usable 2D plans but the model
        still can't decide) -> 'queue' when `inconclusive_to_review`
        (app_settings.dedup_floor_plan_inconclusive_to_review, default on), else 'merge';
        'same_layout' -> 'merge'. N×N over multiple plans (migration 243): 'same_layout' if ANY
        A-plan matches ANY B-plan, 'different_layout' only if NONE do;
      * both sides carry a plan but the verdict isn't available yet (no fn / no budget /
        cache-miss) -> 'defer': skip this run and re-try next, once the batch lane warms
        the verdict — NOT the operator queue (this pair is automatable, not a human call);
      * exactly ONE side / neither side has a plan-tagged image -> 'merge' (no plan-to-plan
        compare is possible, so the gate learned nothing — the primary signal stands).
    """
    ids_a = _floor_plan_ids_cached(conn, a_id, cache)
    ids_b = _floor_plan_ids_cached(conn, b_id, cache)
    if ids_a and ids_b:
        if floor_plan_fn is None or vision_budget[0] <= 0:
            return "defer"
        res = floor_plan_fn(a_id, b_id, ids_a, ids_b)
        if res is None:
            return "defer"
        if not res.get("cache_hit"):
            vision_budget[0] -= 1
        verdict = res.get("verdict")
        if verdict == "different_layout":
            return "dismiss"
        if verdict == "no_2d_plan":
            return "merge"
        if verdict == "inconclusive" and inconclusive_to_review:
            return "queue"
        return "merge"
    return "merge"


def _both_have_site_plan(
    conn: Any, a_id: int, b_id: int, cache: "_ProbeCache | None" = None,
) -> bool:
    """True if BOTH listings carry a site/situation plan (CLIP tag OR LLM
    classification), so the pHash fast-path defers to the site-plan development
    guard instead of auto-merging two units of one development. `cache` memoizes
    per canonical pair for the run (the probe re-ran on every re-formed pair).

    Prefers the full-inventory CLIP image_clip_tags, falling back to the LLM
    image_room_classifications cache — mirroring _floor_plan_image_ids. The LLM
    classifier never tagged a single dum/pozemek/komercni site plan, so reading
    CLIP is what lets this guard fire on house/land developments (where shared
    masterplans drive most false merges), not just the ~1% of classified flats.
    """
    key = (min(a_id, b_id), max(a_id, b_id))
    if cache is not None and key in cache.site_plan_pair:
        return cache.site_plan_pair[key]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE i.sreality_id = %(a)s) > 0 "
            "   AND count(*) FILTER (WHERE i.sreality_id = %(b)s) > 0 "
            "FROM images i "
            "WHERE i.sreality_id IN (%(a)s, %(b)s) AND ("
            "  EXISTS (SELECT 1 FROM image_clip_tags t "
            "          WHERE t.image_id = i.id AND t.logical_tag = %(sp)s) "
            "  OR EXISTS (SELECT 1 FROM image_room_classifications c "
            "             WHERE c.image_id = i.id AND c.room_type = %(sp)s))",
            {"a": a_id, "b": b_id, "sp": SITE_PLAN_ROOM_TYPE},
        )
        result = bool(cur.fetchone()[0])
    if cache is not None:
        cache.site_plan_pair[key] = result
    return result


def _classify_or_none(classify_fn: Any, sreality_id: int) -> list[dict[str, Any]] | None:
    if classify_fn is None:
        return None
    try:
        res = classify_fn(sreality_id)
        if res is None:  # cache-only: not fully warmed yet -> wait for the batch lane
            return None
        return res["data"]["images"]
    except Exception as exc:  # noqa: BLE001 - one bad listing must not kill the run
        LOG.warning("classify %s failed: %s", sreality_id, exc)
        return None


def _resolve_visual(
    conn: Any,
    a: ListingKey,
    b: ListingKey,
    *,
    classify_fn: Any,
    compare_fn: Any,
    site_plan_fn: Any,
    floor_plan_fn: Any = None,
    vision_budget: list[int],
    floor_plan_budget: list[int] | None = None,
    max_room_attempts: int,
    autodismiss: bool = True,
    cosine_fn: Any = None,
    bands: Any = None,
    model_for: dict[str, str] | None = None,
    render_min: float = RENDER_SCORE_EXCLUDE_MIN,
    inconclusive_to_review: bool = True,
    tag_overrides: dict[str, list[str]] | None = None,
    stats: dict[str, int] | None = None,
    probe_cache: "_ProbeCache | None" = None,
) -> dict[str, Any]:
    """Rule D for one candidate pair. Returns a dict describing the outcome.

    {action: 'auto_merge'|'dismiss'|'queue', reason, room_type?, verdict?, rationale?}.
    Mutates vision_budget[0] as forensic calls are spent. When `autodismiss` and the
    room verdicts confidently say "different property" (decide_visual_dismiss), the
    pair is auto-dismissed instead of queued for the operator.
    """
    imgs_a = _classify_or_none(classify_fn, a.sreality_id)
    imgs_b = _classify_or_none(classify_fn, b.sreality_id)
    if imgs_a is None or imgs_b is None:
        return {"action": "queue", "reason": "no_images"}

    # Development guard (runs FIRST): if both listings carry a site/situation
    # plan, check whether they highlight the same unit. A 'different_unit'
    # verdict is the strongest "same project, distinct property" signal — QUEUE
    # for the operator, never auto-merge. (Never auto-rejects: same_unit /
    # inconclusive fall through to the normal confirmation below.)
    site_a = [i["image_id"] for i in imgs_a if i["room_type"] == SITE_PLAN_ROOM_TYPE]
    site_b = [i["image_id"] for i in imgs_b if i["room_type"] == SITE_PLAN_ROOM_TYPE]
    if site_a and site_b and site_plan_fn is not None and vision_budget[0] > 0:
        sp = site_plan_fn(a.sreality_id, b.sreality_id, site_a, site_b)
        if sp is not None and not sp.get("cache_hit"):
            vision_budget[0] -= 1  # only a COLD (paid) call consumes the budget
        if sp is not None and sp.get("verdict") == "different_unit":
            return {
                "action": "queue", "reason": "site_plan_different_unit",
                "verdict": sp["verdict"], "rationale": sp.get("rationale"),
            }

    # The free pHash fast-path now runs in run_engine BEFORE classify (so a strong
    # photo match never pays for the LLM at all). A pair only reaches here if pHash
    # did NOT resolve it, so the visual layer is purely the forensic room compare.
    if compare_fn is None:
        return {"action": "queue", "reason": "vision_unavailable"}

    # Drop shared development RENDERS from the forensic compare for byt (migration 239):
    # a reused render is not a real photo of THE unit, so two units sharing one must not
    # score High on it. Untagged / not-yet-scored images are kept (recall holds).
    rmin = phash_render_exclude_for(a.category_main, render_min)
    if rmin is not None:
        render_ids = _high_render_image_ids(conn, a.sreality_id, b.sreality_id, rmin)
        if render_ids:
            imgs_a = [i for i in imgs_a if i["image_id"] not in render_ids]
            imgs_b = [i for i in imgs_b if i["image_id"] not in render_ids]

    # Room-aware forensic comparison, priority order, stop at first High.
    rooms_a = {i["room_type"] for i in imgs_a}
    rooms_b = {i["room_type"] for i in imgs_b}
    common = rooms_a & rooms_b
    by_room_a = _group_ids_by_room(imgs_a)
    by_room_b = _group_ids_by_room(imgs_b)

    priority = rooms_in_priority(common, a.category_main, tag_overrides)
    tried = 0
    last_verdict = None
    last_rationale = None
    last_cos: float | None = None
    room_verdicts: dict[str, str] = {}
    room_rationales: dict[str, str | None] = {}
    room_cos: dict[str, float | None] = {}
    for room in priority:
        if tried >= max_room_attempts or vision_budget[0] <= 0:
            break  # rooms remain untried — captured by the all-rooms-verdicted guard
        cos: float | None = None
        # Stage 4b: the CLIP cosine recall tier picks WHICH model judges this room
        # (high cosine -> cheap Haiku, uncertain -> Sonnet), or skips the LLM for a
        # too-dissimilar room ('manual'). NEVER merges/dismisses on cosine alone, so
        # a skipped room just leaves the pair to queue (protects reshoots). bands is
        # None -> tier off -> today's behaviour (default model, every common room).
        model: str | None = None
        if bands is not None:
            cos = cosine_fn(by_room_a[room], by_room_b[room]) if cosine_fn else None
            if stats is not None and cos is not None:
                stats["clip_cosine_calls"] = stats.get("clip_cosine_calls", 0) + 1
            decision = route_by_cosine(cos, bands)
            if decision == "manual":
                continue  # too dissimilar to spend the LLM here (not a dismiss)
            model = (model_for or {}).get(decision)
            if stats is not None:
                stats[f"routed_{decision}"] = stats.get(f"routed_{decision}", 0) + 1
        tried += 1
        verdict_obj = compare_fn(
            a.sreality_id, b.sreality_id, room, by_room_a[room], by_room_b[room], model)
        if verdict_obj is None:
            continue
        # Only a COLD (cache-miss) call consumes the budget — a warm cache hit is
        # free, so a run can apply unlimited already-paid-for verdicts while still
        # capping NEW paid comparisons. (Cache-only consume passes a huge budget.)
        if not verdict_obj.get("cache_hit"):
            vision_budget[0] -= 1
        last_verdict, last_rationale, last_cos = (
            verdict_obj["verdict"], verdict_obj.get("rationale"), cos)
        room_verdicts[room] = last_verdict
        room_rationales[room] = last_rationale
        room_cos[room] = cos
        if verdict_is_merge(last_verdict):
            # Floor-plan validation gate (migration 234): a different 2D floor plan overrides
            # even a High forensic verdict (dismiss); a both-2D INCONCLUSIVE verdict -> manual
            # queue; an unwarmed both-plan verdict -> defer (re-try next run once the batch warms
            # it, NOT queue). same_layout / no_2d_plan (renders) / one-sided / none -> the
            # auto-merge stands (the gate can't contradict, so it doesn't block).
            fp = _floor_plan_gate(
                conn, a.sreality_id, b.sreality_id,
                floor_plan_fn=floor_plan_fn,
                vision_budget=(floor_plan_budget if floor_plan_budget is not None
                               else vision_budget),
                inconclusive_to_review=inconclusive_to_review, cache=probe_cache)
            base = {
                "room_type": room, "verdict": last_verdict,
                "rationale": last_rationale, "cosine": cos,
            }
            if fp == "dismiss":
                return {**base, "action": "dismiss", "reason": "floor_plan_different_layout"}
            if fp == "queue":
                return {**base, "action": "queue", "reason": "floor_plan_review"}
            if fp == "defer":
                return {**base, "action": "defer", "reason": "floor_plan_pending"}
            return {**base, "action": "auto_merge", "reason": "visual_match"}

    # No High on any COMPARED room. Only auto-dismiss when EVERY common room produced
    # a verdict (`len(room_verdicts) == len(priority)`): if the room cap / vision
    # budget stopped us early, OR a room was un-warmed (cache-only) / its compare
    # failed, an unseen room might still match — the OR-gate "rescue" only covers
    # rooms actually verdicted, so leave that pair for human review. Then dismiss only
    # on a confident distinctive-room Low (decide_visual_dismiss); calibrated: 0/273
    # operator merges were Low. Otherwise queue.
    all_rooms_verdicted = len(room_verdicts) == len(priority)
    if autodismiss and all_rooms_verdicted and decide_visual_dismiss(room_verdicts):
        room = next(
            (r for r in rooms_in_priority(set(room_verdicts), a.category_main, tag_overrides)
             if room_verdicts[r] == "Low"),
            None,
        )
        return {
            "action": "dismiss", "reason": "visual_different",
            "room_type": room, "verdict": "Low",
            "rationale": room_rationales.get(room) if room else last_rationale,
            "cosine": room_cos.get(room) if room else last_cos,
        }

    return {
        "action": "queue", "reason": "visual_inconclusive",
        "verdict": last_verdict, "rationale": last_rationale, "cosine": last_cos,
    }


def _group_ids_by_room(images: list[dict[str, Any]]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for i in images:
        out.setdefault(i["room_type"], []).append(i["image_id"])
    return out


def _auto_merge_enabled(conn: Any) -> bool:
    """Read the operator's /dedup auto-merge toggle (app_settings). Default on."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = 'dedup_auto_merge_enabled'"
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return True
    v = row[0]
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


def _factors(
    stage: str,
    *,
    reason: str | None = None,
    street_key: str | None = None,
    house_number: str | None = None,
    floor: Any = None,
    phash_pairs: int | None = None,
    phash_distinctive: bool = False,
    cosine: float | None = None,
    verdict: str | None = None,
    room_type: str | None = None,
    rationale: str | None = None,
) -> dict[str, Any]:
    """The canonical decision-factor dict — ONE shape feeding BOTH a terminal audit
    row's `detail` and a queued candidate's `markers_matched`, so Decision history and
    Needs-review render identical factor detail (the operator's ask). Image-similarity
    factors carry the threshold the operator tunes in settings, so the UI can show the
    value against its bar. None entries drop out; the UI hydrates the actual photos by
    (sreality_id, room_type)."""
    f: dict[str, Any] = {
        "stage": stage,
        "reason": reason,
        "street_key": street_key,
        "house_number": house_number,
        "floor": floor,
        "verdict": verdict,
        "room_type": room_type,
        "rationale": rationale,
    }
    if phash_pairs is not None:
        f["phash_pairs"] = phash_pairs
        f["phash_threshold"] = PHASH_IDENTICAL_MAX
        f["phash_min_pairs"] = PHASH_MIN_IDENTICAL_PAIRS
    if phash_distinctive:
        f["phash_distinctive"] = True  # merged on a single kitchen/bathroom match
    if cosine is not None:
        f["cosine"] = round(float(cosine), 4)
    return {k: v for k, v in f.items() if v is not None}


def _audit(
    audit: list[dict[str, Any]] | None, a: ListingKey, b: ListingKey,
    stage: str, outcome: str, detail: dict[str, Any] | None = None,
    *, source: str = "engine", merge_group_id: str | None = None,
) -> None:
    """Append one TERMINAL (merged | dismissed) per-pair decision record (opt-in: no-op
    when audit is None). Queued pairs are NOT audited — a queued pair IS a candidate
    (the review queue), so its factor detail lives in `markers_matched` instead.
    `merge_group_id` is the undo handle for a merged row; `source` distinguishes an
    autonomous engine decision from an operator /dedup action."""
    if audit is None:
        return
    audit.append({
        "left_sreality_id": a.sreality_id, "right_sreality_id": b.sreality_id,
        "left_property_id": a.property_id, "right_property_id": b.property_id,
        "category_main": a.category_main or b.category_main,
        "stage": stage, "outcome": outcome, "source": source,
        "merge_group_id": merge_group_id,
        "detail": {k: v for k, v in (detail or {}).items() if v is not None},
    })


# A DISMISSAL identical to one already logged within this window is run cadence, not a
# new decision — don't re-append it (measured: 3,621 'dismissed' audit rows for 620
# distinct pairs in 7 days, ~5.8x inflation of the /dedup decision stats). ONLY
# dismissals dedupe: a merged record must ALWAYS land — after an operator unmerge the
# engine can legitimately re-merge the same pair within the window, and that fresh
# audit row carries the NEW merge_group_id (the operator's only undo handle; the
# review caught the unrestricted dedupe silently swallowing it).
_AUDIT_DEDUPE_DAYS = 7


def _write_pair_audit(
    conn: Any, run_at: Any, records: list[dict[str, Any]],
) -> None:
    """Append the run's terminal per-pair decisions, SKIPPING dismissal records identical
    to one already logged in the last _AUDIT_DEDUPE_DAYS (same pair + stage + source) —
    the audit is a log of DECISIONS, not of run cadence. Merged records are exempt (each
    carries a unique merge_group_id undo handle). One set-based existence probe + one
    executemany for the novel rows (never a per-record round trip)."""
    if not records:
        return
    import json
    dismissals = [r for r in records if r["outcome"] == "dismissed"]
    seen: set[tuple[Any, ...]] = set()
    if dismissals:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT d.left_sreality_id, d.right_sreality_id, d.stage, "
                "       d.source "
                "FROM dedup_pair_audit d "
                "JOIN unnest(%(ls)s::bigint[], %(rs)s::bigint[], %(st)s::text[], "
                "            %(so)s::text[]) AS u(l, r, st, so) "
                "  ON d.left_sreality_id = u.l AND d.right_sreality_id = u.r "
                " AND d.stage = u.st AND d.source = u.so "
                "WHERE d.outcome = 'dismissed' "
                "  AND d.run_at > now() - make_interval(days => %(days)s)",
                {
                    "ls": [r["left_sreality_id"] for r in dismissals],
                    "rs": [r["right_sreality_id"] for r in dismissals],
                    "st": [r["stage"] for r in dismissals],
                    "so": [r.get("source", "engine") for r in dismissals],
                    "days": _AUDIT_DEDUPE_DAYS,
                },
            )
            seen = {(int(r[0]), int(r[1]), r[2], r[3]) for r in cur.fetchall()}
    novel = [
        r for r in records
        if r["outcome"] != "dismissed"
        or (r["left_sreality_id"], r["right_sreality_id"], r["stage"],
            r.get("source", "engine")) not in seen
    ]
    if not novel:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO dedup_pair_audit (run_at, left_sreality_id, "
            "right_sreality_id, left_property_id, right_property_id, "
            "category_main, stage, outcome, source, merge_group_id, detail) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            [
                (run_at, r["left_sreality_id"], r["right_sreality_id"],
                 r["left_property_id"], r["right_property_id"], r["category_main"],
                 r["stage"], r["outcome"], r.get("source", "engine"),
                 r.get("merge_group_id"), json.dumps(r["detail"]))
                for r in novel
            ],
        )


@dataclass
class _ProbeCache:
    """Per-run memo of the pair-stage DB probes. resolve_pair used to pay 2-3 SEQUENTIAL
    round-trips PER CANDIDATE PAIR re-querying facts that are per-LISTING (CLIP
    completeness, floor-plan ids, site-plan presence) or batchable per-GROUP (pHash) —
    the 2026-07 audit measured that as the dirty lane's cost floor (~0.5-0.75 s/pair from
    a GitHub runner to the EU pooler ≈ the whole 1200 s budget, hence the chronic
    truncation). Memoized, a group of n listings costs O(n) listing probes + O(1) pHash
    batches instead of O(n²) trips. Facts are stable within a run (minutes) — a
    mid-run tag/image change is picked up next run, the same staleness window every
    lane already accepts."""
    clip_incomplete: dict[int, bool] = field(default_factory=dict)
    floor_plan_ids: dict[int, list[int]] = field(default_factory=dict)
    site_plan_pair: dict[tuple[int, int], bool] = field(default_factory=dict)
    # pHash batches are keyed by (lo_sid, hi_sid, profile) — the exclusion profile
    # (excluded_tags, render_min) differs by category family (byt excludes exteriors +
    # renders), so a mixed-family group batches once per profile actually used.
    phash_counts: dict[tuple[int, int, tuple], int] = field(default_factory=dict)
    phash_batched: set[tuple[tuple[int, ...], tuple]] = field(default_factory=set)
    phash_distinctive: dict[tuple[int, int, tuple], bool] = field(default_factory=dict)
    distinctive_batched: set[tuple[tuple[int, ...], tuple]] = field(default_factory=set)
    # max(images.clip_tagged_at) per listing — the "new evidence since?" timestamp the
    # prior-dismissal consult compares against reviewed_at.
    evidence_at: dict[int, Any] = field(default_factory=dict)


def _clip_incomplete_any(
    conn: Any, sreality_ids: list[int], model: str, cache: _ProbeCache | None = None,
) -> bool:
    """Memoized any-incomplete check over `_clip_incomplete` (a per-LISTING fact queried
    per PAIR before — a group of n re-checked each listing n-1 times). Only the not-yet-
    cached ids hit the DB; no cache -> the plain one-shot query (tests, standalone use)."""
    if cache is None:
        return bool(_clip_incomplete(conn, sreality_ids, model))
    unknown = [s for s in sreality_ids if s not in cache.clip_incomplete]
    if unknown:
        incomplete = set(_clip_incomplete(conn, unknown, model))
        for s in unknown:
            cache.clip_incomplete[s] = s in incomplete
    return any(cache.clip_incomplete[s] for s in sreality_ids)


def _floor_plan_ids_cached(
    conn: Any, sreality_id: int, cache: _ProbeCache | None = None,
) -> list[int]:
    """Memoized `_floor_plan_image_ids` (per-listing; the floor-plan gate queries both
    sides of every would-merge pair). Resolved via the module attribute so tests that
    monkeypatch `_floor_plan_image_ids` keep working."""
    if cache is None:
        return _floor_plan_image_ids(conn, sreality_id)
    if sreality_id not in cache.floor_plan_ids:
        cache.floor_plan_ids[sreality_id] = _floor_plan_image_ids(conn, sreality_id)
    return cache.floor_plan_ids[sreality_id]


def _phash_pairs_cached(
    conn: Any, a_id: int, b_id: int, excluded_tags: tuple[str, ...],
    render_exclude_min: float | None, cache: _ProbeCache | None = None,
    group_sids: tuple[int, ...] | None = None,
) -> int:
    """`_phash_identical_pairs` served from the per-group batch: on the first lookup for a
    (group, exclusion-profile) the whole group's pair counts land in one round trip; later
    pairs of the group are cache hits. No cache/group -> the per-pair query, unchanged."""
    if cache is None or group_sids is None or len(group_sids) < 3:
        # A 2-member group is a single pair — the batch saves nothing over the per-pair
        # query, so keep the simpler (and independently testable) path for it.
        return _phash_identical_pairs(
            conn, a_id, b_id, excluded_tags, render_exclude_min=render_exclude_min)
    profile = (tuple(excluded_tags), render_exclude_min)
    gkey = (group_sids, profile)
    if gkey not in cache.phash_batched:
        counts = _phash_group_counts(
            conn, list(group_sids), excluded_tags, render_exclude_min=render_exclude_min)
        for (lo, hi), n in counts.items():
            cache.phash_counts[(lo, hi, profile)] = n
        cache.phash_batched.add(gkey)
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    return cache.phash_counts.get((lo, hi, profile), 0)


def _phash_distinctive_cached(
    conn: Any, a_id: int, b_id: int, rooms: tuple[str, ...] | frozenset[str],
    render_exclude_min: float | None, cache: _ProbeCache | None = None,
    group_sids: tuple[int, ...] | None = None,
) -> bool:
    """`_phash_distinctive_match` served from the per-group batch (lazy: only byt pairs
    whose generic count fell short ever need it, so the batch runs on first demand)."""
    if cache is None or group_sids is None or len(group_sids) < 3:
        return _phash_distinctive_match(
            conn, a_id, b_id, rooms=rooms, render_exclude_min=render_exclude_min)
    profile = (tuple(sorted(rooms)), render_exclude_min)
    gkey = (group_sids, profile)
    if gkey not in cache.distinctive_batched:
        matches = _phash_group_distinctive(
            conn, list(group_sids), rooms=rooms, render_exclude_min=render_exclude_min)
        for lo, hi in matches:
            cache.phash_distinctive[(lo, hi, profile)] = True
        cache.distinctive_batched.add(gkey)
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    return cache.phash_distinctive.get((lo, hi, profile), False)


def _last_evidence_at(conn: Any, sreality_id: int, cache: _ProbeCache) -> Any:
    """max(images.clip_tagged_at) for a listing — "when did its photo evidence last
    change" for the prior-dismissal consult. Memoized; None = no tagged images."""
    if sreality_id not in cache.evidence_at:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max(clip_tagged_at) FROM images WHERE sreality_id = %s",
                (sreality_id,),
            )
            row = cur.fetchone()
        cache.evidence_at[sreality_id] = row[0] if row else None
    return cache.evidence_at[sreality_id]


@dataclass
class _RunContext:
    """All per-run state + tunables a pair decision needs. ONE object so the per-pair
    logic (`resolve_pair`) is reusable by the full-scan loop, the candidate-priority
    drain, AND the future real-time per-listing path — without re-threading 20 params or
    duplicating the decision tree. The fns/flags are read-only inputs; the sets, stats,
    vision_budget and pairs_left are mutated as pairs resolve."""
    # vision fns + cosine routing (read-only)
    classify_fn: Any = None
    compare_fn: Any = None
    site_plan_fn: Any = None
    floor_plan_fn: Any = None
    cosine_fn: Any = None
    bands: Any = None
    model_for: dict[str, str] | None = None
    # candidate FILTER + queue tier — the ONLY things that differ between the street path
    # (classify_pair, street+disposition keyed) and the geo path (classify_geo_pair wrapper,
    # geo-proximity keyed). Everything downstream (pHash → cosine → forensic → plan gate) is
    # shared, so houses/land/commercial run the exact same free-first flow as apartments.
    classify: Any = classify_pair
    tier: str = "street_disposition"
    # Operator-reordered per-family comparison-tag priorities (app_settings.dedup_tag_priorities);
    # None / partial → the coded defaults (rooms_in_priority normalizes per family).
    tag_overrides: dict[str, list[str]] | None = None
    # clip_model is the taxonomy model the CLIP tags are keyed on; set => the always-on
    # tagging-readiness gate (_clip_incomplete) is active.
    clip_model: str | None = None
    # tunables (read-only)
    auto_merge_enabled: bool = True
    autodismiss: bool = True
    enqueue_unresolved: bool = True
    dry_run: bool = False
    render_min: float = RENDER_SCORE_EXCLUDE_MIN
    inconclusive_to_review: bool = True
    max_room_attempts: int = 4
    # Non-byt attribute fast-path: when set, a house/land/commercial candidate with
    # area-within-2% + identical price auto-merges through the floor-plan gate WITHOUT the
    # paid room compare (dedup_nonbyt_attr_merge_enabled). Off = every non-byt pair pays vision.
    nonbyt_attr_merge: bool = False
    # Prior engine dismissals among the loaded properties ({(lo,hi): reviewed_at}) —
    # loaded once per SCOPED run (dirty/candidates). resolve_pair skips a pair already
    # dismissed with NO new photo evidence since, instead of re-running the whole probe
    # + verdict chain every hour (the audit's 5.8x dismissal treadmill). None = consult
    # off (full scans: cursor-rotated, each pair re-decided once per cycle by design).
    dismissed_prior: dict[tuple[int, int], Any] | None = None
    # mutable run state
    stats: dict[str, int] = field(default_factory=dict)
    vision_budget: list[int] = field(default_factory=lambda: [0])
    # The floor-plan validation gate's OWN budget, kept SEPARATE from vision_budget in
    # --free bounded-forensics mode (W2): compares/site-plan draw vision_budget, the plan
    # gate draws floor_plan_budget, so a capped compare budget can't starve the plan gate
    # (or vice-versa) on lanes where dedup_floor_plan_budget is large. run_engine ALIASES it
    # to the SAME list as vision_budget in every non-free mode (floor_plan_calls=None) → the
    # historical single shared pool, byte-identical. Default matches vision_budget's [0].
    floor_plan_budget: list[int] = field(default_factory=lambda: [0])
    audit: list[dict[str, Any]] | None = None
    probes: _ProbeCache = field(default_factory=_ProbeCache)
    seen_listing_pairs: set[tuple[int, int]] = field(default_factory=set)
    seen_property_pairs: set[tuple[int, int]] = field(default_factory=set)
    merged_pairs: set[tuple[int, int]] = field(default_factory=set)
    dismissed_pairs: set[tuple[int, int]] = field(default_factory=set)
    # Pairs the engine EVALUATED this run but left proposed (queue outcome / free-mode
    # skip / tagging defer) → stamped set-based in finalize (migration 272); the
    # candidate drain's due-filter then skips them until backoff or fresh evidence.
    engine_looked: dict[tuple[int, int], str] = field(default_factory=dict)
    # The subset of dismissed_pairs that were AUTO-DISMISSED by a verdict this run
    # (floor-plan different_layout / confident visual "different") — NOT the rule-C
    # rejects. finalize() upserts these as status='dismissed' candidate rows so future
    # scoped runs can consult them (66% of treadmill pairs had NO candidate row at all).
    auto_dismissed_pairs: set[tuple[int, int]] = field(default_factory=set)
    pairs_left: int = 10 ** 9


# Non-apartment attribute fast-path tolerance: areas within this fraction (coalesced
# area_m2/estate_area/usable_area, loaded on the geo path) count as "same area". Paired
# with an EXACT price match it is a same-property signal at 99.6% agreement with the
# forensic verdict on 574 decided house/land/commercial pairs (validated 2026-07). The
# retained floor-plan gate + site-plan fall-through keep the two conservative vetoes.
NONBYT_ATTR_AREA_MAX_PCT = 0.02


def _attr_exact_nonbyt(a: ListingKey, b: ListingKey,
                       area_max_pct: float = NONBYT_ATTR_AREA_MAX_PCT) -> bool:
    """The non-byt attribute fast-path predicate: coalesced areas within `area_max_pct`
    AND identical asking price (both non-null). Pure. Mirrors the validated SQL exactly
    (max-denominator area diff, exact price)."""
    if a.area_m2 is None or b.area_m2 is None or a.price_czk is None or b.price_czk is None:
        return False
    hi = max(a.area_m2, b.area_m2)
    if hi <= 0:
        return False
    return abs(a.area_m2 - b.area_m2) / hi <= area_max_pct and a.price_czk == b.price_czk


def resolve_pair(conn: Any, a: ListingKey, b: ListingKey, *, street_key: str,
                 ctx: _RunContext, group_sids: tuple[int, ...] | None = None) -> None:
    """Decide ONE eligible same-street pair and apply the outcome (merge / dismiss /
    queue / defer / skip), mutating `ctx`. The single source of truth for the dedup
    decision tree — rule A/B/C, the pHash fast-path + floor-plan gate, and rule-D
    forensic visual — shared by every driver. Returns nothing; its effects are the DB
    writes (merge/enqueue) plus ctx mutations the caller persists. `group_sids` (the
    street group's listing ids, passed by run_engine's loop) lets the pHash probes batch
    per GROUP instead of per pair; absent -> per-pair queries, identical results."""
    stats = ctx.stats
    # Dual-keyed listings appear in their 'id:' AND 'name:' groups; classify each
    # listing pair once (first group wins).
    lpair = (min(a.sreality_id, b.sreality_id), max(a.sreality_id, b.sreality_id))
    if lpair in ctx.seen_listing_pairs:
        return
    ctx.seen_listing_pairs.add(lpair)
    decision = ctx.classify(a, b)
    cp = _canon_pair(a, b)
    if decision.action == "reject":
        stats["rejected"] += 1
        # A pair the current rules reject is a deterministic non-match: dismiss any
        # stale proposed candidate for it (recall-neutral).
        if cp is not None:
            ctx.dismissed_pairs.add(cp)
        return

    # Re-pointing happens at the property grain; skip a property pair we already acted
    # on this run (merges mutate property_id live).
    if cp is None:
        return
    if cp in ctx.seen_property_pairs:
        return
    ctx.seen_property_pairs.add(cp)

    # Prior-dismissal consult (scoped runs): a pair the engine already CONFIDENTLY
    # dismissed (floor-plan different_layout / visual "different") re-forms every time
    # its group re-drains, and without this check it re-ran the whole probe + verdict
    # chain hourly (measured: 3,621 audit rows for 620 distinct pairs in 7 days). Skip it
    # UNLESS either side gained photo evidence since the dismissal (new images tagged
    # after reviewed_at) — new photos can legitimately flip a verdict, so recall
    # survives. Full scans don't consult (cursor-rotated: one re-decision per cycle is
    # the designed refresh).
    if ctx.dismissed_prior is not None and cp in ctx.dismissed_prior:
        reviewed_at = ctx.dismissed_prior[cp]
        if reviewed_at is None:
            # Unknown dismissal time -> can't prove staleness -> re-decide (recall-safe;
            # prod has 0 such rows today, this is a guard against future writers).
            pass
        else:
            ev_a = _last_evidence_at(conn, a.sreality_id, ctx.probes)
            ev_b = _last_evidence_at(conn, b.sreality_id, ctx.probes)
            fresh = any(ev is not None and ev > reviewed_at for ev in (ev_a, ev_b))
            if not fresh:
                stats["skipped_prior_dismissed"] = (
                    stats.get("skipped_prior_dismissed", 0) + 1)
                return

    # True only when the consult above re-opened this pair on fresh evidence — a queue
    # outcome below must then re-propose the recorded dismissal (see _enqueue_candidate).
    reopened = ctx.dismissed_prior is not None and cp in ctx.dismissed_prior

    # Tagging-readiness gate: a listing must be FULLY CLIP-tagged before the engine decides on
    # it. An incompletely-tagged listing's floor-plan / room images may still be in the tag
    # queue, so the floor-plan gate would mis-read 'one-sided' (a pending plan looks absent — the
    # false floor_plan_review queue) and the visual flow would under-pair rooms. DEFER (no
    # re-queue needed: a pending image already has clip_tagged_at IS NULL, so `clip_tag.yml` will
    # tag it — re-queuing here would only cycle a terminally-undecodable image). Once the LAST
    # image is tagged, the clip_tag job enqueues the property into dedup_dirty_properties IF it
    # passes the writer-side gate (eligible + first_seen within the recency window, #666) — a
    # RECENT listing then re-decides within minutes via the hourly --dirty drain; an old/regated
    # one falls to the next scan that loads its group (candidate drain or full scan). Always on
    # whenever CLIP is the tagger (clip_model set) — there is no opt-out (it replaced the retired
    # dedup_clip_only setting).
    if ctx.clip_model and _clip_incomplete_any(
            conn, [a.sreality_id, b.sreality_id], ctx.clip_model, ctx.probes):
        stats["clip_deferred"] += 1
        ctx.engine_looked[cp] = "clip_deferred"  # fresh clip_tagged_at re-opens the pair
        return

    # Rule B (exact address) is RETIRED (2026-06): it was the only auto-merge path with false
    # merges (6.7% later unmerged — two units at one address — vs 0% for pHash/visual). Exact
    # address is not unit-conclusive, so classify_pair now returns it as a CANDIDATE: the pair
    # flows through the pHash fast-path + forensic visual + floor-plan gate below, like any
    # street+disposition pair.

    # pHash fast-path (FREE, BEFORE classify, ALL sources). A strong raw photo match
    # (>= PHASH_MIN_IDENTICAL_PAIRS near-identical pairs over the listings' images) is a
    # same-property signal that needs no LLM. The pair already passed rule C, so a match
    # here auto-merges. Runs before classify, so identical-photo re-posts (same-source
    # relists included) merge for free and never pay for classify OR compare. For byt, known-exterior/
    # shared images are excluded from the count so a development's reused renders can't
    # reach the >=2 threshold; other categories count any image. A single near-identical
    # KITCHEN/BATHROOM match also qualifies (distinctive override) — but ONLY for byt: a
    # house's facade/garden is shared across a development's units, so distinctive_rooms_for
    # returns an empty set for non-byt families and the override is skipped (require >=2).
    _rmin = phash_render_exclude_for(a.category_main, ctx.render_min)
    phash_pairs = _phash_pairs_cached(
        conn, a.sreality_id, b.sreality_id,
        phash_excluded_tags_for(a.category_main), _rmin,
        cache=ctx.probes, group_sids=group_sids)
    _distinctive_rooms = distinctive_rooms_for(a.category_main)
    distinctive = (
        bool(_distinctive_rooms)
        and phash_pairs < PHASH_MIN_IDENTICAL_PAIRS
        and _phash_distinctive_cached(
            conn, a.sreality_id, b.sreality_id, _distinctive_rooms, _rmin,
            cache=ctx.probes, group_sids=group_sids))
    if decide_phash_fastpath(phash_pairs, distinctive) and not _both_have_site_plan(
        conn, a.sreality_id, b.sreality_id, ctx.probes
    ):
        factors = _factors("phash", reason="image_phash",
                           street_key=street_key, phash_pairs=phash_pairs,
                           phash_distinctive=distinctive)
        if not ctx.auto_merge_enabled:
            if not ctx.dry_run:
                _enqueue_candidate(conn, a, b, {
                    **factors, "tier": ctx.tier,
                    "reason": "auto_merge_off:image_phash", "confidence": 0.97},
                    reopen=reopened)
            stats["queued"] += 1
            ctx.engine_looked[cp] = "auto_merge_off:image_phash"
            return
        # Floor-plan validation gate (migration 234): a different 2D floor plan DISMISSES, a
        # both-2D INCONCLUSIVE verdict goes to MANUAL queue, an unwarmed both-plan verdict DEFERS
        # (skip, re-try next run once the batch warms it — never the manual queue); a no_2d_plan
        # (renders) / one-sided / same_layout verdict lets the pHash merge proceed.
        fp = _floor_plan_gate(
            conn, a.sreality_id, b.sreality_id,
            floor_plan_fn=ctx.floor_plan_fn, vision_budget=ctx.floor_plan_budget,
            inconclusive_to_review=ctx.inconclusive_to_review, cache=ctx.probes)
        if fp == "dismiss":
            stats["auto_dismissed"] += 1
            ctx.dismissed_pairs.add(cp)
            ctx.auto_dismissed_pairs.add(cp)
            _audit(ctx.audit, a, b, "phash", "dismissed",
                   {**factors, "reason": "floor_plan_different_layout"},
                   source="engine")
            return
        if fp == "queue":
            if not ctx.dry_run:
                _enqueue_candidate(conn, a, b, {
                    **factors, "tier": ctx.tier,
                    "reason": "floor_plan_review", "confidence": 0.6},
                    reopen=reopened)
            stats["queued"] += 1
            ctx.engine_looked[cp] = "floor_plan_review"
            return
        if fp == "defer":
            stats["floor_plan_deferred"] += 1
            return
        mg = None if ctx.dry_run else _merge_pair(
            conn, a, b, "image_phash",
            {**factors, "tier": ctx.tier, "confidence": 0.97})
        if ctx.dry_run or mg:
            stats["auto_phash"] += 1
            ctx.merged_pairs.add(cp)
            _audit(ctx.audit, a, b, "phash", "merged", factors,
                   source="engine", merge_group_id=mg)
        return

    # Non-byt attribute fast-path (FREE, houses / land / commercial). A co-located candidate
    # whose coalesced areas match within 2% AND whose asking prices are IDENTICAL is the same
    # property at 99.6% agreement with the forensic verdict — merge without the paid room
    # compare. Mirrors the pHash fast-path exactly: it still runs the floor-plan gate (a proven
    # different_layout DISMISSES; both-plan-unwarmed DEFERS; inconclusive QUEUES), and — like
    # pHash — it steps aside whenever BOTH sides carry a site plan, so a development's near-
    # identical units still pay the site-plan same-unit guard on the forensic path. Off by
    # default (dedup_nonbyt_attr_merge_enabled); byt is excluded (its area+price collide across
    # a development's identical units — the retired rule-B trap). phash_pairs (0/1 here — the
    # fast-path didn't fire) rides along as the "photos differ, decided on attributes" signal.
    if (ctx.nonbyt_attr_merge and a.category_main and a.category_main != "byt"
            and _attr_exact_nonbyt(a, b)
            and not _both_have_site_plan(conn, a.sreality_id, b.sreality_id, ctx.probes)):
        factors = _factors("attr", reason="attr_exact", street_key=street_key,
                           phash_pairs=phash_pairs)
        if not ctx.auto_merge_enabled:
            if not ctx.dry_run:
                _enqueue_candidate(conn, a, b, {
                    **factors, "tier": ctx.tier,
                    "reason": "auto_merge_off:attr_exact", "confidence": 0.97},
                    reopen=reopened)
            stats["queued"] += 1
            ctx.engine_looked[cp] = "auto_merge_off:attr_exact"
            return
        fp = _floor_plan_gate(
            conn, a.sreality_id, b.sreality_id,
            floor_plan_fn=ctx.floor_plan_fn, vision_budget=ctx.floor_plan_budget,
            inconclusive_to_review=ctx.inconclusive_to_review, cache=ctx.probes)
        if fp == "dismiss":
            stats["auto_dismissed"] += 1
            ctx.dismissed_pairs.add(cp)
            ctx.auto_dismissed_pairs.add(cp)
            _audit(ctx.audit, a, b, "attr", "dismissed",
                   {**factors, "reason": "floor_plan_different_layout"},
                   source="engine")
            return
        if fp == "queue":
            if not ctx.dry_run:
                _enqueue_candidate(conn, a, b, {
                    **factors, "tier": ctx.tier,
                    "reason": "floor_plan_review", "confidence": 0.6},
                    reopen=reopened)
            stats["queued"] += 1
            ctx.engine_looked[cp] = "floor_plan_review"
            return
        if fp == "defer":
            stats["floor_plan_deferred"] += 1
            return
        mg = None if ctx.dry_run else _merge_pair(
            conn, a, b, "attr_exact",
            {**factors, "tier": ctx.tier, "confidence": 0.97})
        if ctx.dry_run or mg:
            stats["auto_attr"] += 1
            ctx.merged_pairs.add(cp)
            _audit(ctx.audit, a, b, "attr", "merged", factors,
                   source="engine", merge_group_id=mg)
        return

    # Wave 3 removed the cross-source gate: the engine no longer skips same-source non-exact
    # pairs. The gate cut ~36% of pairs off the (paid) visual stage but cost ~1.4% recall —
    # a same-portal relist with changed photos, or two cross-posts on one portal, were dropped.
    # Now ALL rule-C candidates reach the visual stage; the forensic High verdict + the
    # floor/site-plan gates remain the precision guards, so recall rises without false merges.
    # (pHash already auto-merged identical-photo same-source relists for free, above.)

    # rule C candidate -> rule D visual
    ctx.pairs_left -= 1
    stats["pairs_considered"] += 1
    if not ctx.auto_merge_enabled:
        # Auto-merge off: queue for manual review without spending vision. No forensic
        # verdict here, but pHash already ran — carry it so the Needs-review card still
        # shows the one similarity signal we have.
        if not ctx.dry_run:
            _enqueue_candidate(conn, a, b, {
                **_factors("candidate", reason="auto_merge_off",
                           street_key=street_key, phash_pairs=phash_pairs),
                "tier": ctx.tier, "confidence": 0.6,
            }, reopen=reopened)
        stats["queued"] += 1
        ctx.engine_looked[cp] = "auto_merge_off"
        return
    # (Tagging readiness is enforced once, up front — see the _clip_incomplete gate at the top
    # of resolve_pair — so every pair reaching the visual stage is fully CLIP-tagged.)
    outcome = _resolve_visual(
        conn, a, b, classify_fn=ctx.classify_fn, compare_fn=ctx.compare_fn,
        site_plan_fn=ctx.site_plan_fn, floor_plan_fn=ctx.floor_plan_fn,
        vision_budget=ctx.vision_budget, floor_plan_budget=ctx.floor_plan_budget,
        max_room_attempts=ctx.max_room_attempts,
        autodismiss=ctx.autodismiss,
        cosine_fn=ctx.cosine_fn, bands=ctx.bands, model_for=ctx.model_for,
        render_min=ctx.render_min, inconclusive_to_review=ctx.inconclusive_to_review,
        tag_overrides=ctx.tag_overrides,
        stats=stats, probe_cache=ctx.probes,
    )
    # ONE factor set per pair — fed to BOTH the terminal audit `detail` (merged/dismissed)
    # AND, when queued, the candidate `markers_matched`, so Decision history and
    # Needs-review show identical detail. phash_pairs is carried even on the visual path
    # (it's 0/1 here — the fast-path didn't fire — which is itself the "photos differ,
    # escalated to vision" signal).
    factors = _factors(
        "visual", reason=outcome.get("reason"), street_key=street_key,
        verdict=outcome.get("verdict"), room_type=outcome.get("room_type"),
        rationale=outcome.get("rationale"), cosine=outcome.get("cosine"),
        phash_pairs=phash_pairs)
    markers = {**factors, "tier": ctx.tier,
               "confidence": 0.97 if outcome["action"] == "auto_merge" else 0.6}
    # pHash already ran (pre-classify); the visual stage only auto-merges via a High
    # forensic verdict, and auto-dismisses on a confident "different".
    if outcome["action"] == "auto_merge":
        mg = None if ctx.dry_run else _merge_pair(conn, a, b, outcome["reason"], markers)
        if ctx.dry_run or mg:
            stats["auto_visual"] += 1
            ctx.merged_pairs.add(cp)
            _audit(ctx.audit, a, b, "visual", "merged", factors,
                   source="engine", merge_group_id=mg)
    elif outcome["action"] == "dismiss":
        stats["auto_dismissed"] += 1
        ctx.dismissed_pairs.add(cp)
        ctx.auto_dismissed_pairs.add(cp)
        _audit(ctx.audit, a, b, "visual", "dismissed", factors, source="engine")
    elif outcome["action"] == "defer":
        # Floor-plan verdict not warmed yet -> skip, re-try next run (the batch lane
        # warms it). NOT the manual queue.
        stats["floor_plan_deferred"] += 1
    elif ctx.enqueue_unresolved:
        if not ctx.dry_run:
            _enqueue_candidate(conn, a, b, markers, reopen=reopened)
        stats["queued"] += 1
        ctx.engine_looked[cp] = str(outcome.get("reason") or "queued")
        # NOT audited — a queued pair IS the candidate; its factor detail lives in
        # markers_matched (Needs-review reads it). Auditing queued re-logged the same
        # pair every run (the duplicate-row bug).
    else:
        # Free mode: don't pile un-vision'd pairs into the review queue (they'd just be
        # 'no photos compared' placeholders). pHash / rule-B / reconcile already ran; this
        # pair is left for a future run (free pHash as coverage grows, or vision if
        # re-enabled).
        stats["skipped_unresolved"] += 1
        ctx.engine_looked[cp] = str(outcome.get("reason") or "skipped_unresolved")


def _make_geo_classify(area_max_pct: float | None) -> Any:
    """The geo path's candidate filter for resolve_pair: classify_geo_pair (per the FIRST
    listing's profile, operator policy) with the operator's geo area tolerance, mapping its
    deterministic auto_merge → candidate so the geo signal NEVER merges on its own — the
    shared free-first visual flow is the sole merge gate."""
    def _fn(a: ListingKey, b: ListingKey) -> PairDecision:
        d = classify_geo_pair(
            a, b, profile_for(a.category_main), max_area_pct=area_max_pct)
        if d.action == "auto_merge":
            return PairDecision("candidate", d.reason, d.detail)
        return d
    return _fn


def run_engine(
    conn: Any,
    *,
    classify_fn: Any = None,
    compare_fn: Any = None,
    site_plan_fn: Any = None,
    floor_plan_fn: Any = None,
    cosine_fn: Any = None,
    bands: Any = None,
    model_for: dict[str, str] | None = None,
    render_min: float = RENDER_SCORE_EXCLUDE_MIN,
    inconclusive_to_review: bool = True,
    audit: list[dict[str, Any]] | None = None,
    max_pairs: int = 2000,
    max_vision_calls: int = 200,
    floor_plan_calls: int | None = None,
    max_room_attempts: int = 4,
    auto_merge_enabled: bool = True,
    autodismiss: bool = True,
    enqueue_unresolved: bool = True,
    dry_run: bool = False,
    deadline: float | None = None,
    restrict_property_ids: set[int] | None = None,
    restrict_street_groups: tuple[set[int], set[tuple[int, str]]] | None = None,
    restrict_geo_cells: set[str] | None = None,
    only_groups_with_property_ids: set[int] | None = None,
    resolved_property_ids: set[int] | None = None,
    priority_property_order: list[int] | None = None,
    scan_cursor: str | None = None,
    cursor_out: dict[str, Any] | None = None,
    geo: bool = False,
    geo_area_max_pct: float | None = None,
    nonbyt_attr_merge: bool = False,
    clip_model: str | None = None,
) -> dict[str, int]:
    """Run the full pipeline once. classify_fn/compare_fn are injectable for tests.

    geo=True runs the SAME flow over single-dwelling families (house/land/commercial)
    keyed by geo-proximity instead of street+disposition: the only differences are the
    candidate loader (_load_geo_eligible — scoped by `restrict_geo_cells` on the dirty
    geo sub-pass, the cell analogue of restrict_street_groups), the candidate filter
    (classify_geo_pair, mapped auto_merge→candidate so the free-first visual flow is
    the sole merge gate), and the queue tier ('geo'). Everything else — pHash → cosine
    → forensic compare (facade / site-plan priority via room_priority_for) →
    floor/site-plan gate — is shared.

    classify_fn(sreality_id) -> classify_listing_images envelope.
    compare_fn(a, b, room_type, ids_a, ids_b) -> {verdict, rationale} | None.
    site_plan_fn(a, b, ids_a, ids_b) -> {verdict, rationale} | None (development
    guard: verdict ∈ same_unit|different_unit|inconclusive).

    Self-healing: each run also RESOLVES stale proposed candidates rather than
    letting them pile up in the operator queue — it dismisses a pair the current
    rules reject (deterministic non-match), or a
    confident visual "different" (autodismiss); merges the now-mergeable; and
    reconciles candidates pointing to a merged-away property.

    When auto_merge_enabled is False (the operator's /dedup toggle), the engine
    still finds candidates but queues every one for manual review instead of
    auto-merging — and skips the forensic vision step (no LLM spend).

    dry_run computes every action + counter but writes nothing (no merges, no
    candidate status changes) — a shadow preview of what a live run would do.
    """
    # Geo runs in the same main() invocation AFTER the street pass, which already
    # reconciled + counted street eligibility; a geo run reports its own eligible count and
    # skips the (street-keyed) eligibility scan + the (already-done) reconcile.
    # A SCOPED run's work-list is explicit (dirty groups / candidate properties);
    # only the unscoped full scan measures the market.
    scoped = (restrict_property_ids is not None or restrict_street_groups is not None
              or restrict_geo_cells is not None
              or only_groups_with_property_ids is not None)
    if geo:
        # restrict_geo_cells is the --dirty geo sub-pass's scope (the claimed
        # properties' stored cells, peers included) — the geo analogue of the street
        # path's restrict_street_groups.
        keys = _load_geo_eligible(conn, restrict_property_ids=restrict_property_ids,
                                  restrict_cells=restrict_geo_cells)
        stats = {
            "eligible": len({k.sreality_id for k in keys}),
            "flagged_location": None, "flagged_disposition": None,
        }
    else:
        keys = _load_eligible(
            conn, restrict_property_ids=restrict_property_ids,
            restrict_street_groups=restrict_street_groups)
        # The market gauges cost a ~9s full-table aggregate — pointless on the hourly
        # dirty / 2-hourly candidate drains whose own work takes milliseconds. NULL =
        # "not measured" (migration 265); dashboards read gauges from full-scan rows.
        stats = (_eligibility_counts(conn) if not scoped else
                 {"eligible": None, "flagged_location": None, "flagged_disposition": None})
    stats.update({
        "pairs_considered": 0, "rejected": 0,
        "auto_address": 0, "auto_phash": 0, "auto_visual": 0, "auto_attr": 0,
        "queued": 0, "vision_calls": 0,
        "auto_dismissed": 0, "reconciled": 0, "skipped_unresolved": 0,
        "floor_plan_deferred": 0, "clip_deferred": 0, "truncated": 0,
        "clip_classified": 0, "clip_cosine_calls": 0,
        "routed_haiku": 0, "routed_sonnet": 0,
        # Migration 271 observability: why a run stopped, what oversized groups cost,
        # and whether the paid fns were erroring (vision_errors is stamped by main()
        # from the shared breaker counter — the credit-outage tripwire).
        "oversized_groups": 0, "skipped_oversized": 0,
        "truncated_cause": None, "vision_errors": 0,
        "scan_groups_total": None, "scan_groups_scanned": None,
        # Observability: the --dirty path stamps these post-claim (NULL on other run modes).
        # dirty_cleared / dirty_truncated expose whether the drain actually advanced the head —
        # cleared==0 while queue_depth stays high is the silent-livelock the FIFO stall lacked.
        "dirty_queue_depth": None, "dirty_claimed": None,
        "dirty_cleared": None, "dirty_truncated": None,
        "dirty_age_p95_seconds": None, "dirty_pruned": None,
    })

    if not geo:
        stats["reconciled"] = _reconcile_stale_candidates(conn, dry_run=dry_run)

    groups = _group_by_street(keys)
    max_group_size = MAX_GEO_GROUP_SIZE if geo else MAX_GROUP_SIZE

    # Per-group progress for the --dirty drain's INCREMENTAL clear (`resolved_property_ids`
    # out-collector): a property is RESOLVED once EVERY group containing it has been scanned —
    # a listing dual-keys into its 'id:' and 'name:' groups, so one completed group is not
    # enough. Zero-group claimed properties (no eligible listing anymore) resolve immediately.
    # Skipped groups count as scanned: an oversized group is unresolvable everywhere (the full
    # scan's job if it shrinks), and a dirty-filter-skipped group contains no claimed property
    # by construction. This is what lets a deadline-truncated run CLEAR the slice it finished
    # instead of keeping its whole claim — monotonic progress, no re-processing livelock.
    # Publication gate (migration 273): collect every property the engine scans this run
    # (ANY street/geo group of it processed == the engine evaluated it) so finalize() can
    # stamp it visible. Populated on ALL non-dry run modes — full scan, candidate drain,
    # dirty drain, geo — INDEPENDENT of the dirty out-param's stricter all-groups-scanned
    # accounting below (which must stay unchanged to keep the dirty clear correct).
    publication_checked: set[int] = set()
    remaining_groups: dict[int, int] | None = None
    if resolved_property_ids is not None:
        remaining_groups = {}
        for members in groups.values():
            for m in members:
                remaining_groups[m.property_id] = remaining_groups.get(m.property_id, 0) + 1
        if only_groups_with_property_ids is not None:
            resolved_property_ids.update(
                pid for pid in only_groups_with_property_ids if pid not in remaining_groups)

    def _group_scanned(members: list[Any]) -> None:
        for m in members:
            if m.property_id is not None:
                publication_checked.add(m.property_id)
        if remaining_groups is None:
            return
        for m in members:
            left = remaining_groups.get(m.property_id)
            if left is None:
                continue
            if left <= 1:
                del remaining_groups[m.property_id]
                resolved_property_ids.add(m.property_id)  # type: ignore[union-attr]
            else:
                remaining_groups[m.property_id] = left - 1
    # The candidate FILTER + queue tier are the geo path's only divergence; the geo classify
    # maps auto_merge → candidate so a deterministic geo signal never merges on its own — the
    # shared free-first visual flow is the sole merge gate.
    classify = _make_geo_classify(geo_area_max_pct) if geo else classify_pair
    from toolkit.dedup_priorities import load_tag_priority_overrides
    tag_overrides = load_tag_priority_overrides(conn)
    # Prior-dismissal consult, SCOPED runs only (dirty / candidate drains re-form the
    # same pairs every pass; the full scan is cursor-rotated — one re-decision per cycle
    # is its designed refresh, and its property set is the whole market, too big to load).
    dismissed_prior = (
        _load_prior_dismissed(conn, {k.property_id for k in keys if k.property_id is not None})
        if scoped and not geo else None)
    _vision_budget = [max_vision_calls]
    ctx = _RunContext(
        dismissed_prior=dismissed_prior,
        classify=classify, tier=("geo" if geo else "street_disposition"),
        tag_overrides=tag_overrides, clip_model=clip_model,
        classify_fn=classify_fn, compare_fn=compare_fn, site_plan_fn=site_plan_fn,
        floor_plan_fn=floor_plan_fn, cosine_fn=cosine_fn, bands=bands, model_for=model_for,
        auto_merge_enabled=auto_merge_enabled, autodismiss=autodismiss,
        enqueue_unresolved=enqueue_unresolved, dry_run=dry_run, render_min=render_min,
        inconclusive_to_review=inconclusive_to_review, max_room_attempts=max_room_attempts,
        nonbyt_attr_merge=nonbyt_attr_merge,
        stats=stats, vision_budget=_vision_budget,
        # floor_plan_calls None (every non-free mode) ALIASES the plan gate to the SAME
        # pool as compares → historical shared-budget behaviour; a value (free mode) gives
        # the gate its own separate counter.
        floor_plan_budget=(_vision_budget if floor_plan_calls is None else [floor_plan_calls]),
        audit=audit, pairs_left=max_pairs,
    )

    # Full-scan CURSOR (migration 261): iterate groups in a stable sorted order and resume
    # AFTER `scan_cursor`, so successive deadline-bounded runs advance a frontier over the
    # whole market instead of head-restarting (the old behavior covered ~9% of pair slots
    # per run and structurally never re-scanned the tail). `cursor_out` reports the last
    # fully-scanned/skipped key + whether the run reached the end of the list (= the cycle
    # completed — what re-arms the dirty-queue TTL eviction). Enabled only when the caller
    # passes cursor_out (the plain scheduled full scan); scoped runs keep insertion order.
    scan_frontier: dict[str, Any] = {"last_key": None}
    if cursor_out is not None:
        ordered_keys = sorted(groups)
        if scan_cursor:
            after = [k for k in ordered_keys if k > scan_cursor]
            skipped_behind = len(ordered_keys) - len(after)
            if skipped_behind:
                LOG.info("CURSOR resuming after %r (skipping %d already-scanned groups)",
                         scan_cursor, skipped_behind)
            ordered_keys = after
    else:
        ordered_keys = list(groups)
        if priority_property_order is not None:
            # Dirty drain: process groups in CLAIM order (newest dirty property first),
            # so a deadline-cut run spends its budget on the queue head — the real-time
            # SLO pairs — instead of whatever fell first in load (obec-ASC) order.
            rank = {pid: i for i, pid in enumerate(priority_property_order)}
            worst = len(rank)
            ordered_keys.sort(key=lambda k: min(
                (rank.get(m.property_id, worst) for m in groups[k]), default=worst))

    def finalize() -> dict[str, int]:
        # Resolve every candidate the engine acted on this run (no-op for pairs
        # without a proposed row); a no-op set-based UPDATE when nothing collected.
        if not dry_run:
            _resolve_candidates(conn, ctx.merged_pairs, "merged")
            _resolve_candidates(conn, ctx.dismissed_pairs, "dismissed")
            # Record this run's VERDICT-BACKED dismissals as status='dismissed' candidate
            # rows (insert-if-absent — 66% of the measured treadmill pairs had NO row for
            # the consult to find; an existing row was just updated above).
            _record_auto_dismissed(conn, ctx.auto_dismissed_pairs, ctx.tier)
            # Stamp every pair the engine evaluated but left proposed (migration 272) —
            # the candidate drain's due-filter keys on this to stop the re-chew treadmill.
            _stamp_engine_looked(conn, ctx.engine_looked)
            # Publication gate (migration 273): publish every property this run
            # dedup-evaluated. Unpublished-only, so a re-scan is a no-op — this is the
            # writer side of the properties_public / properties_map_mv gate.
            _stamp_publication_checked(conn, publication_checked)
        if cursor_out is not None:
            cursor_out["last_key"] = scan_frontier["last_key"]
            # The cycle completed only if the scan reached the end of the ordered list
            # without truncation — a truncated run's frontier resumes next run.
            cursor_out["reached_end"] = not stats["truncated"]
        return _finish(stats, ctx.vision_budget, max_vision_calls,
                       floor_plan_budget=ctx.floor_plan_budget,
                       floor_plan_calls=floor_plan_calls)

    # The decision tree per pair lives in resolve_pair (shared by the candidate-priority
    # drain + the real-time path); this is just the full-scan driver over street groups.
    if cursor_out is not None:
        stats["scan_groups_total"] = len(ordered_keys)
        stats["scan_groups_scanned"] = 0

    def _advance(members: list[Any], street_key: str) -> None:
        _group_scanned(members)
        scan_frontier["last_key"] = street_key
        if cursor_out is not None:
            stats["scan_groups_scanned"] += 1

    for street_key in ordered_keys:
        members = groups[street_key]
        # Real-time (dirty) drain: the load is SCOPED to the dirty properties' street
        # groups (restrict_street_groups), so it carries each dirty property's existing
        # PEERS while staying O(dirty); this filter then resolves only groups that
        # actually contain a dirty/just-ready property — the correctness gate under the
        # scoped load (a group reaches here only if its key was claimed, so this is a
        # safety re-assertion, not the primary scope). No fragile SQL street-key replay.
        if only_groups_with_property_ids is not None and not any(
            m.property_id in only_groups_with_property_ids for m in members
        ):
            _advance(members, street_key)
            continue
        pair_iter: Any
        if len(members) > max_group_size:
            # OVERSIZED group: bounded value-ordered processing (best MAX_GROUP_PAIRS
            # pairs, dirty/cross-source/price-similar first) instead of the historical
            # whole-group skip — which silently dropped every pair on busy streets AND
            # (on dirty runs) cleared those properties' queue rows as if handled.
            total_pairs = len(members) * (len(members) - 1) // 2
            pairs = prioritized_group_pairs(
                members, cap=MAX_GROUP_PAIRS, classify=ctx.classify,
                priority_property_ids=only_groups_with_property_ids)
            stats["oversized_groups"] += 1
            stats["skipped_oversized"] += max(0, total_pairs - len(pairs))
            LOG.info("OVERSIZED group key=%s size=%d: processing %d of %d pairs",
                     street_key, len(members), len(pairs), total_pairs)
            pair_iter = pairs
        else:
            pair_iter = ((members[i], members[j])
                         for i in range(len(members))
                         for j in range(i + 1, len(members)))
        # The group's listing ids let the pHash probes batch ONE round trip per
        # (group, exclusion-profile) instead of one per pair (_phash_pairs_cached).
        # Above PHASH_BATCH_MAX_MEMBERS the batch itself is the hazard (members^2
        # IN-list) — fall back to per-pair probes, bounded by MAX_GROUP_PAIRS.
        group_sids = (tuple(sorted({m.sreality_id for m in members}))
                      if len(members) <= PHASH_BATCH_MAX_MEMBERS else None)
        for a, b in pair_iter:
            if ctx.pairs_left <= 0:
                LOG.info("PAIR cap reached; deferring remainder to next run")
                stats["truncated"] = 1  # scan did NOT finish; completed groups still clear
                stats["truncated_cause"] = "pair_cap"
                return finalize()
            # Wall-clock budget: per-pair cold vision can outrun the job timeout,
            # which SIGKILLs the run before it writes results. Stop cleanly so the run
            # row + inline-committed merges persist and the next run resumes with a
            # warm cache (mirrors the detail drain's --max-seconds).
            if deadline is not None and time.monotonic() >= deadline:
                LOG.info(
                    "TIME budget reached; finalizing cleanly at pairs_considered=%d",
                    stats["pairs_considered"],
                )
                stats["truncated"] = 1  # scan did NOT finish; completed groups still clear
                stats["truncated_cause"] = "deadline"
                return finalize()
            resolve_pair(conn, a, b, street_key=street_key, ctx=ctx, group_sids=group_sids)
        if ctx.pairs_left <= 0:
            # Budget died ON this group's final pair: conservative boundary — the group
            # does NOT count as scanned (re-decided next run) and the run is truncated,
            # exactly like the historical i-loop cap check.
            stats["truncated"] = 1
            stats["truncated_cause"] = "pair_cap"
            return finalize()
        # Reached only when the group's pair scan COMPLETED with budget to spare — the
        # deadline/pair-cap returns above exit before advancing the frontier past a
        # half-scanned group.
        _advance(members, street_key)

    return finalize()


def _finish(stats: dict[str, int], vision_budget: list[int], max_vision_calls: int,
            *, floor_plan_budget: list[int] | None = None,
            floor_plan_calls: int | None = None) -> dict[str, int]:
    used = max_vision_calls - vision_budget[0]
    # When the floor-plan gate ran on its OWN budget (free mode: floor_plan_calls set and the
    # counter is a DISTINCT list) add its spend so vision_calls stays the TOTAL paid vision
    # count. When aliased (non-free: floor_plan_budget IS vision_budget) its calls are already
    # counted above — the `is not` identity check prevents double-counting.
    if (floor_plan_calls is not None and floor_plan_budget is not None
            and floor_plan_budget is not vision_budget):
        used += floor_plan_calls - floor_plan_budget[0]
    stats["vision_calls"] = used
    return stats


def _breaker_open(error_count: list[int] | None) -> bool:
    """True once this run has seen VISION_ERROR_BREAKER paid-call failures — the
    builders then stop calling out (each doomed call costs seconds of wall-clock)
    and serve warm-cache reads only, so a dead key / exhausted credit degrades to
    cache-only instead of burning the whole run budget on errors."""
    return error_count is not None and error_count[0] >= VISION_ERROR_BREAKER


def _count_vision_error(error_count: list[int] | None) -> None:
    if error_count is None:
        return
    error_count[0] += 1
    if error_count[0] == VISION_ERROR_BREAKER:
        LOG.warning("VISION breaker OPEN after %d errors: paid calls disabled for the "
                    "rest of the run (cache reads still served)", VISION_ERROR_BREAKER)


def _build_classify_fn(
    conn: Any, *, prefer_clip: bool = False, clip_model: str | None = None,
    clip_counter: list[int] | None = None, error_count: list[int] | None = None,
) -> Any:
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.clip_dedup import clip_room_grouping
    from toolkit.image_classification import cached_classification, classify_listing_images
    llm = LLMClient(conn, providers=get_providers())
    classify_model = llm.resolve_model("llm_room_classify_model")

    def _fn(sreality_id: int) -> dict[str, Any] | None:
        # Prefer the FREE CLIP room tags; fall back to the paid LLM classify only
        # for a listing CLIP hasn't tagged yet (during the backfill ramp).
        if prefer_clip and clip_model:
            grouping = clip_room_grouping(conn, sreality_id=sreality_id, model=clip_model)
            if grouping is not None:
                if clip_counter is not None:
                    clip_counter[0] += 1
                return {"data": {"images": [
                    {"image_id": iid, "room_type": rt}
                    for rt, ids in grouping.items() for iid in ids
                ]}}
        if _breaker_open(error_count):
            state, rooms = cached_classification(
                conn, sreality_id=sreality_id, model=classify_model)
            if state != "classified" or rooms is None:
                return None
            return {"data": {"images": [
                {"image_id": iid, "room_type": rt}
                for rt, ids in rooms.items() for iid in ids
            ]}}
        try:
            return classify_listing_images(conn, llm, sreality_id=sreality_id)
        except Exception as exc:  # noqa: BLE001 - one bad listing must not kill the run
            _count_vision_error(error_count)
            LOG.warning("classify %s failed: %s", sreality_id, exc)
            return None
    return _fn


def _build_compare_fn(conn: Any, *, error_count: list[int] | None = None) -> Any:
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.visual_match import cached_visual_verdict, compare_listings_visually
    llm = LLMClient(conn, providers=get_providers())
    default_model = llm.resolve_model("llm_visual_match_model")

    def _fn(a: int, b: int, room_type: str, ids_a: list[int], ids_b: list[int],
            model: str | None = None) -> dict[str, Any] | None:
        if _breaker_open(error_count):
            v = cached_visual_verdict(
                conn, sreality_id_a=a, sreality_id_b=b, room_type=room_type,
                model=model or default_model)
            return {"verdict": v, "rationale": None, "cache_hit": True} if v is not None else None
        try:
            res = compare_listings_visually(
                conn, llm, sreality_id_a=a, sreality_id_b=b,
                room_type=room_type, image_ids_a=ids_a, image_ids_b=ids_b,
                model=model,
            )
            return res["data"]
        except Exception as exc:  # noqa: BLE001 - one bad pair must not kill the run
            _count_vision_error(error_count)
            LOG.warning("visual compare %s/%s room=%s failed: %s", a, b, room_type, exc)
            return None
    return _fn


def _build_site_plan_fn(conn: Any, *, error_count: list[int] | None = None) -> Any:
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.visual_match import cached_site_plan_verdict, compare_listing_site_plans
    llm = LLMClient(conn, providers=get_providers())
    site_plan_model = llm.resolve_model("llm_site_plan_match_model")

    def _fn(a: int, b: int, ids_a: list[int], ids_b: list[int]) -> dict[str, Any] | None:
        if _breaker_open(error_count):
            v = cached_site_plan_verdict(
                conn, sreality_id_a=a, sreality_id_b=b, model=site_plan_model)
            return {"verdict": v, "rationale": None, "cache_hit": True} if v is not None else None
        try:
            res = compare_listing_site_plans(
                conn, llm, sreality_id_a=a, sreality_id_b=b,
                image_ids_a=ids_a, image_ids_b=ids_b,
            )
            return res["data"]
        except Exception as exc:  # noqa: BLE001 - one bad pair must not kill the run
            _count_vision_error(error_count)
            LOG.warning("site-plan compare %s/%s failed: %s", a, b, exc)
            return None
    return _fn


def _build_floor_plan_fn(conn: Any, *, error_count: list[int] | None = None) -> Any:
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.visual_match import cached_floor_plan_verdict, compare_listing_floor_plans
    llm = LLMClient(conn, providers=get_providers())
    floor_plan_model = llm.resolve_model("llm_floor_plan_match_model")

    def _fn(a: int, b: int, ids_a: list[int], ids_b: list[int]) -> dict[str, Any] | None:
        if _breaker_open(error_count):
            v = cached_floor_plan_verdict(
                conn, sreality_id_a=a, sreality_id_b=b, model=floor_plan_model)
            return {"verdict": v, "rationale": None, "cache_hit": True} if v is not None else None
        try:
            res = compare_listing_floor_plans(
                conn, llm, sreality_id_a=a, sreality_id_b=b,
                image_ids_a=ids_a, image_ids_b=ids_b,
            )
            return res["data"]
        except Exception as exc:  # noqa: BLE001 - one bad pair must not kill the run
            _count_vision_error(error_count)
            LOG.warning("floor-plan compare %s/%s failed: %s", a, b, exc)
            return None
    return _fn


def _build_cache_only_floor_plan_fn(conn: Any) -> Any:
    """A $0 floor_plan_fn that ONLY reads the batch-warmed verdict cache (never the LLM),
    so even the FREE scheduled run can apply the floor-plan gate on warmed verdicts and
    DEFER the rest — the same warm-then-consume pattern as the other vision tools, light
    enough for the free run (a single app_settings read, no provider). The model MUST be
    resolved via the SAME `LLMClient.resolve_model` the batch warm-up (submit_dedup_batch
    `_warm_floor_plan`) and the live `_build_floor_plan_fn` use — the verdict cache is keyed
    on the model string, so any divergence here would permanently cache-miss the free run
    (defer forever). `LLMClient(conn)` needs no providers for a pure resolve_model read."""
    from api.llm_client import LLMClient
    from toolkit.visual_match import cached_floor_plan_verdict
    model = LLMClient(conn).resolve_model("llm_floor_plan_match_model")

    def _fn(a: int, b: int, ids_a: list[int], ids_b: list[int]) -> dict[str, Any] | None:
        v = cached_floor_plan_verdict(conn, sreality_id_a=a, sreality_id_b=b, model=model)
        return {"verdict": v, "rationale": None, "cache_hit": True} if v is not None else None
    return _fn


def _effective_vision_cap(*, free: bool, cache_only: bool, compare_budget: int,
                          max_vision_calls: int) -> int:
    """The COMPARE/site-plan vision-budget cap handed to run_engine, by mode. Cache-only
    reads are free, so a large cap keeps the budget from throttling them. In --free mode the
    forensic room/site-plan compares consume THIS pool, capped by --compare-budget
    (0 = pHash-only free run, no forensic compares — the historical --free behaviour); the
    floor-plan validation gate runs on its OWN separate budget (run_engine's floor_plan_calls
    = the dedup_floor_plan_budget), so a small compare cap can't starve the plan gate and a
    large plan budget can't inflate the compare spend. Non-free modes keep the shared
    max_vision_calls pool (the gate aliases into it, as before)."""
    if cache_only:
        return 10_000_000
    if free:
        return compare_budget
    return max_vision_calls


def _build_cache_only_fns(
    conn: Any, *, prefer_clip: bool = False, clip_model: str | None = None,
    clip_counter: list[int] | None = None,
) -> tuple[Any, Any, Any, Any]:
    """classify/compare/site-plan/floor-plan fns that ONLY READ the warm caches (the
    ones the batch lane filled at 50% off) and NEVER call the LLM — so the engine
    applies already-paid-for verdicts for $0. Un-warmed listings/rooms return None and
    the pair stays queued until the batch lane warms it. This is the cost-efficient
    consume half: the batch lane is the sole (discounted) payer. CLIP room tags
    (free) are preferred for the room grouping when present."""
    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider
    from toolkit.clip_dedup import clip_room_grouping
    from toolkit.image_classification import cached_classification
    from toolkit.visual_match import (
        cached_floor_plan_verdict,
        cached_site_plan_verdict,
        cached_visual_verdict,
    )

    llm = LLMClient(conn, providers={"anthropic": AnthropicProvider()})
    classify_model = llm.resolve_model("llm_room_classify_model")
    compare_model = llm.resolve_model("llm_visual_match_model")
    site_plan_model = llm.resolve_model("llm_site_plan_match_model")
    floor_plan_model = llm.resolve_model("llm_floor_plan_match_model")

    def classify_fn(sreality_id: int) -> dict[str, Any] | None:
        if prefer_clip and clip_model:
            grouping = clip_room_grouping(conn, sreality_id=sreality_id, model=clip_model)
            if grouping is not None:
                if clip_counter is not None:
                    clip_counter[0] += 1
                return {"data": {"images": [
                    {"image_id": iid, "room_type": rt}
                    for rt, ids in grouping.items() for iid in ids
                ]}}
        state, rooms = cached_classification(
            conn, sreality_id=sreality_id, model=classify_model)
        if state != "classified" or rooms is None:
            return None  # not fully warmed -> wait for the batch lane
        images = [
            {"image_id": iid, "room_type": rt}
            for rt, ids in rooms.items() for iid in ids
        ]
        return {"data": {"images": images}}

    def compare_fn(a: int, b: int, room_type: str, ids_a: list[int], ids_b: list[int],
                   model: str | None = None) -> dict[str, Any] | None:
        v = cached_visual_verdict(
            conn, sreality_id_a=a, sreality_id_b=b, room_type=room_type,
            model=model or compare_model)
        return {"verdict": v, "rationale": None, "cache_hit": True} if v is not None else None

    def site_plan_fn(a: int, b: int, ids_a: list[int], ids_b: list[int]) -> dict[str, Any] | None:
        v = cached_site_plan_verdict(
            conn, sreality_id_a=a, sreality_id_b=b, model=site_plan_model)
        return {"verdict": v, "rationale": None, "cache_hit": True} if v is not None else None

    def floor_plan_fn(a: int, b: int, ids_a: list[int], ids_b: list[int]) -> dict[str, Any] | None:
        v = cached_floor_plan_verdict(
            conn, sreality_id_a=a, sreality_id_b=b, model=floor_plan_model)
        return {"verdict": v, "rationale": None, "cache_hit": True} if v is not None else None

    return classify_fn, compare_fn, site_plan_fn, floor_plan_fn


def _visual_autodismiss_enabled(conn: Any) -> bool:
    """Operator toggle for auto-dismissing confident forensic 'different' verdicts
    (app_settings.dedup_forensics_autodismiss_enabled). Default on."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = 'dedup_forensics_autodismiss_enabled'"
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return True
    v = row[0]
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


def _clip_settings(conn: Any) -> dict[str, Any]:
    """The CLIP-tier knobs (all default OFF / safe, so merging this changes nothing
    until the operator flips them via app_settings — no redeploy). `clip_model` is
    the taxonomy's model id, matching what the tagging backfill stored."""
    from scraper.clip_tagger import load_taxonomy
    from toolkit.dedup_settings import default_for

    def _val(key: str) -> Any:  # stored value, else the registry default
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = cur.fetchone()
        return row[0] if row and row[0] is not None else default_for(key)

    def _flag(key: str) -> bool:
        v = _val(key)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        return bool(v)

    def _num(key: str) -> float:
        v = _val(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default_for(key))

    return {
        "prefer_clip": _flag("dedup_prefer_clip_tags"),
        "cosine_enabled": _flag("dedup_clip_cosine_enabled"),
        "bands": CosineBands(
            haiku_min=_num("dedup_cosine_haiku_min"),
            sonnet_min=_num("dedup_cosine_sonnet_min"),
        ),
        "render_min": _num("dedup_render_exclude_min"),
        "haiku_model": _val("dedup_visual_match_model_haiku"),
        "clip_model": load_taxonomy()["model"],
    }


def _write_run_row(conn: Any, stats: dict[str, int], *, run_kind: str,
                   started_at: Any, runner: str = "actions") -> None:
    """One run row at end of run. `run_kind` ('full' | 'candidates' | 'dirty') and the
    run-level `truncated` (stats['truncated'], stamped on EVERY row — migration 262) are
    what make a chronically deadline-cut FULL SCAN visible: the 2026-07 audit found every
    6h scan silently covering ~9% of the market with nothing recording it. `started_at`
    is the real run start (the column default otherwise equals ended_at — no durations).
    `runner` distinguishes GH Actions runs from the realtime worker's dedup lane."""
    params = {**stats, "run_kind": run_kind, "started_at": started_at, "runner": runner,
              "truncated": int(stats.get("truncated", 0) or 0)}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dedup_engine_runs (
                started_at, ended_at, run_kind, truncated,
                eligible, flagged_location, flagged_disposition,
                pairs_considered, rejected, auto_address, auto_phash, auto_visual,
                queued, vision_calls, auto_dismissed, floor_plan_deferred, clip_deferred,
                clip_classified, clip_cosine_calls, routed_haiku, routed_sonnet,
                dirty_queue_depth, dirty_claimed, dirty_cleared, dirty_truncated,
                skipped_unresolved, skipped_oversized, oversized_groups, vision_errors,
                truncated_cause, scan_groups_total, scan_groups_scanned,
                dirty_age_p95_seconds, dirty_pruned, runner
            ) VALUES (%(started_at)s, now(), %(run_kind)s, %(truncated)s,
                %(eligible)s, %(flagged_location)s, %(flagged_disposition)s,
                %(pairs_considered)s, %(rejected)s, %(auto_address)s, %(auto_phash)s,
                %(auto_visual)s, %(queued)s, %(vision_calls)s, %(auto_dismissed)s,
                %(floor_plan_deferred)s, %(clip_deferred)s,
                %(clip_classified)s, %(clip_cosine_calls)s, %(routed_haiku)s,
                %(routed_sonnet)s, %(dirty_queue_depth)s, %(dirty_claimed)s,
                %(dirty_cleared)s, %(dirty_truncated)s,
                %(skipped_unresolved)s, %(skipped_oversized)s, %(oversized_groups)s,
                %(vision_errors)s, %(truncated_cause)s, %(scan_groups_total)s,
                %(scan_groups_scanned)s, %(dirty_age_p95_seconds)s, %(dirty_pruned)s,
                %(runner)s)
            """,
            params,
        )


# Keys _merge_dirty_stats must NOT sum across the two dirty sub-passes: the market
# gauges are per-lane "not measured" NULLs on scoped runs (migration 265 — dashboards
# read them from full-scan rows, and folding the geo sub-pass's scoped `eligible` count
# into the street NULL would fake a market gauge), and truncated / truncated_cause get
# explicit OR / first-cause handling below.
_DIRTY_STATS_NO_SUM = ("eligible", "flagged_location", "flagged_disposition",
                       "scan_groups_total", "scan_groups_scanned",
                       "truncated", "truncated_cause")


def _merge_dirty_stats(street: dict[str, Any], geo: dict[str, Any]) -> dict[str, Any]:
    """Fold the geo sub-pass's stats into the street sub-pass's for the SINGLE
    run_kind='dirty' row: the pair/merge/queue/vision counters become BOTH-family
    totals, `truncated` is OR'd (either sub-pass cut short keeps the claim honest),
    `truncated_cause` keeps the street pass's cause when both truncated."""
    out = dict(street)
    for k, v in geo.items():
        if k in _DIRTY_STATS_NO_SUM:
            continue
        cur = out.get(k)
        if isinstance(v, int) and isinstance(cur, int):
            out[k] = cur + v
        elif cur is None and v is not None:
            out[k] = v
    out["truncated"] = 1 if (street.get("truncated") or geo.get("truncated")) else 0
    out["truncated_cause"] = street.get("truncated_cause") or geo.get("truncated_cause")
    return out


def run_dirty_pass(
    conn: Any, *, max_dirty: int, max_pairs: int, engine_kw: dict[str, Any],
    runner: str = "actions", shadow: bool = False, started_at: Any = None,
    stamp_stats: Any = None,
) -> dict[str, int] | None:
    """One bounded real-time dirty pass: prune → claim (newest-first) → per-FAMILY
    scoped resolve (the street groups first, then the claimed properties' geo CELLS —
    both through the one resolve_pair brain) → per-family incremental clear → ONE run
    row (run_kind='dirty'; its pair/merge counters cover BOTH families). The reusable
    core shared by the GH Actions --dirty cron and the realtime worker's dedup lane
    (`runner` tags the row). Returns the run stats, or None when the queue was empty —
    no run row is written then, so a worker polling every minute never spams
    dedup_engine_runs.

    `engine_kw` is the shared run_engine configuration (vision fns, budgets, flags) the
    caller assembled; the geo sub-pass derives its kwargs from the SAME dict (same fns,
    same shared wall-clock deadline — street runs first, geo gets the remainder) with
    enqueue_unresolved forced ON (geo has no rule-B and its pairs never merge on the
    deterministic geo signal, so the tier-'geo' /dedup queue is its only surfacing
    mechanism). `stamp_stats` lets the caller fold its own counters (clip
    classifications, vision errors) into the stats before the row is written."""
    from datetime import datetime, timezone

    pruned = _prune_stale_dedup_dirty(conn)
    if pruned:
        LOG.info("DIRTY drain: pruned %d stale rows (older than %dh)",
                 pruned, _DEDUP_DIRTY_TTL_HOURS)
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        cutoff = cur.fetchone()[0]
    claimed = _claim_dedup_dirty(conn, cutoff, limit=max_dirty)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dedup_dirty_properties")
        queue_depth = int(cur.fetchone()[0])
    LOG.info("DIRTY drain: %d claimed (cap=%d, queue depth=%d)",
             len(claimed), max_dirty, queue_depth)
    if not claimed:
        return None
    age_p95 = _dirty_queue_age_p95_seconds(conn, cutoff)
    only_groups = set(claimed)
    # Per-family eligibility of the claim, queried ONCE — what the per-family clear
    # below keys on (and what keeps a street-only pid clearable when the geo sub-pass
    # is deadline-skipped, and vice versa).
    family = _claimed_family_eligibility(conn, only_groups)
    # Scope the eligible LOAD to the claimed properties' street groups (O(dirty), not
    # O(market)) via the STORED street_name_key. The peers in those groups are loaded
    # too, so a dirty property still re-decides against its whole group; only_groups
    # keeps the RESOLVE filtered to dirty-containing groups — the correctness gate the
    # scoped load is layered under.
    dirty_street_groups = _claimed_street_groups(conn, only_groups)
    LOG.info("DIRTY drain: scoped load to %d street-id + %d name groups",
             len(dirty_street_groups[0]), len(dirty_street_groups[1]))
    pair_audit: list[dict[str, Any]] = []
    dirty_resolved_street: set[int] = set()
    stats = run_engine(
        conn, audit=pair_audit, max_pairs=max_pairs,
        only_groups_with_property_ids=only_groups,
        restrict_street_groups=dirty_street_groups,
        resolved_property_ids=dirty_resolved_street,
        priority_property_order=claimed,
        **engine_kw,
    )
    # GEO sub-pass: the same claim's geo-family work — the claimed properties' stored
    # geo_cell_key cells, loaded WITH PEERS and resolved through the same resolve_pair
    # brain (run_engine(geo=True) maps the deterministic geo signal auto_merge →
    # candidate, so pHash / forensic-High stay the only merge gates). Runs AFTER the
    # street sub-pass on the SHARED deadline; when the street pass exhausted it, the
    # geo work defers whole (its pids keep their claim — the per-family clear below
    # never drops un-run geo work).
    dirty_resolved_geo: set[int] = set()
    geo_cells = _claimed_geo_cells(conn, only_groups)
    if geo_cells:
        deadline = engine_kw.get("deadline")
        if deadline is not None and time.monotonic() >= deadline:
            LOG.info("DIRTY drain: wall-clock budget exhausted by the street sub-pass; "
                     "%d geo cells deferred (their claims stay)", len(geo_cells))
            stats["truncated"] = 1
            if not stats.get("truncated_cause"):
                stats["truncated_cause"] = "deadline"
        else:
            from toolkit.dedup_settings import read_setting
            geo_area_max_pct = float(read_setting(conn, "dedup_geo_area_max_pct"))
            # enqueue_unresolved is forced ON (see the docstring); restrict_property_ids
            # is neutralized — it is the street/candidate scope, mutually exclusive
            # with the cell scope.
            geo_kw = {**engine_kw, "enqueue_unresolved": True,
                      "restrict_property_ids": None}
            LOG.info("DIRTY drain: geo sub-pass over %d cells", len(geo_cells))
            geo_stats = run_engine(
                conn, audit=pair_audit, max_pairs=max_pairs,
                geo=True, geo_area_max_pct=geo_area_max_pct,
                restrict_geo_cells=geo_cells,
                only_groups_with_property_ids=only_groups,
                resolved_property_ids=dirty_resolved_geo,
                priority_property_order=claimed,
                **geo_kw,
            )
            stats = _merge_dirty_stats(stats, geo_stats)
    if stamp_stats is not None:
        stamp_stats(stats)
    # Queue-health gauges (migrations 255/258/271): depth + slice at run start, whole-queue
    # age p95 (the starvation signal), and rows the TTL prune evicted this pass.
    stats["dirty_queue_depth"] = queue_depth
    stats["dirty_claimed"] = len(claimed)
    stats["dirty_age_p95_seconds"] = age_p95
    stats["dirty_pruned"] = pruned
    if not shadow:
        # PER-FAMILY incremental clear: a claimed property clears only when EVERY
        # family it is eligible for was resolved this run (`dirty_resolved_street` /
        # `dirty_resolved_geo`, tracked per-group in run_engine) — a street-only pid
        # clears on the street resolve alone, a geo-only pid on the geo resolve, a
        # both-family pid needs both, and a neither-eligible pid clears immediately
        # (queue hygiene; the ineligible publish sweep owns its publication). A
        # deadline/pair-cap-truncated (or deadline-skipped geo) run thus still clears
        # the slice it finished — monotonic progress every run — while unfinished
        # properties keep their claim and re-drain next pass, newest-first.
        truncated = bool(stats.get("truncated"))
        stats["dirty_truncated"] = 1 if truncated else 0
        clearable = {
            pid for pid in only_groups
            if (not family.get(pid, (False, False))[0] or pid in dirty_resolved_street)
            and (not family.get(pid, (False, False))[1] or pid in dirty_resolved_geo)
        }
        cleared = _clear_dedup_dirty(conn, clearable, cutoff)
        stats["dirty_cleared"] = cleared
        LOG.info("DIRTY drain: cleared %d/%d claimed (resolved per-family; truncated=%s)",
                 cleared, len(claimed), truncated)
        started = started_at or datetime.now(timezone.utc)
        _write_run_row(conn, stats, run_kind="dirty", started_at=started, runner=runner)
        _write_pair_audit(conn, started, pair_audit)
    return stats


# A real-time dirty pass claims a small newest-first slice; the pair cap is a runaway
# backstop only (the deadline + slice are the real bounds), so the scheduled 200000 fits.
_REALTIME_MAX_PAIRS = 200000


def build_free_engine_kw(
    conn: Any, *, compare_budget: int, floor_plan_budget: int,
    max_room_attempts: int = 4, deadline: float | None = None,
    clip_counter: list[int] | None = None, vision_errors: list[int] | None = None,
    enqueue_unresolved: bool = False,
) -> dict[str, Any]:
    """Assemble run_engine's --free configuration (the exact fn/budget wiring main()'s
    free branch builds): pHash-first, bounded live forensics capped at compare_budget
    PAID calls, the floor-plan gate on its own budget, CLIP-cosine routing when enabled,
    enqueue_unresolved off by default (the street --free posture — run_dirty_pass's geo
    sub-pass derives an enqueue-ON variant from the same dict, since the tier-'geo'
    queue is geo's only surfacing mechanism). Shared by the CLI --free path (via main)
    and the realtime worker's dedup lane so the two can't drift. clip_counter /
    vision_errors are the shared observability counters the caller folds into the run
    row."""
    from toolkit.dedup_settings import read_setting
    auto_merge_enabled = _auto_merge_enabled(conn)
    autodismiss = _visual_autodismiss_enabled(conn)
    clip = _clip_settings(conn)
    inconclusive_to_review = bool(
        read_setting(conn, "dedup_floor_plan_inconclusive_to_review"))
    ck = {"prefer_clip": clip["prefer_clip"], "clip_model": clip["clip_model"],
          "clip_counter": clip_counter, "error_count": vision_errors}

    classify_fn = compare_fn = site_plan_fn = floor_plan_fn = None
    if auto_merge_enabled and compare_budget > 0:
        classify_fn = _build_classify_fn(conn, **ck)
        compare_fn = _build_compare_fn(conn, error_count=vision_errors)
        site_plan_fn = _build_site_plan_fn(conn, error_count=vision_errors)
    if auto_merge_enabled:
        floor_plan_fn = (
            _build_floor_plan_fn(conn, error_count=vision_errors) if floor_plan_budget > 0
            else _build_cache_only_floor_plan_fn(conn))

    cosine_fn = None
    bands = None
    model_for = None
    if clip["cosine_enabled"] and compare_fn is not None:
        from toolkit.clip_dedup import room_pair_cosine
        bands = clip["bands"]
        model_for = {"haiku": clip["haiku_model"], "sonnet": None}
        _cm = clip["clip_model"]

        def cosine_fn(ids_a: list[int], ids_b: list[int]) -> float | None:
            return room_pair_cosine(conn, image_ids_a=ids_a, image_ids_b=ids_b, model=_cm)

    return dict(
        classify_fn=classify_fn, compare_fn=compare_fn, site_plan_fn=site_plan_fn,
        floor_plan_fn=floor_plan_fn, cosine_fn=cosine_fn, bands=bands,
        model_for=model_for, render_min=clip["render_min"],
        inconclusive_to_review=inconclusive_to_review,
        max_vision_calls=compare_budget, max_room_attempts=max_room_attempts,
        floor_plan_calls=floor_plan_budget,
        auto_merge_enabled=auto_merge_enabled, autodismiss=autodismiss,
        enqueue_unresolved=enqueue_unresolved, deadline=deadline,
        nonbyt_attr_merge=bool(read_setting(conn, "dedup_nonbyt_attr_merge_enabled")),
        clip_model=clip["clip_model"],
    )


def run_realtime_dirty_pass(
    conn: Any, *, max_dirty: int, compare_budget: int, floor_plan_budget: int,
    max_seconds: float, runner: str = "worker",
) -> dict[str, int] | None:
    """One free-mode real-time dirty pass for the always-on worker: assemble the --free
    engine config, then delegate to run_dirty_pass (prune → newest-first claim → scoped
    resolve in claim order → incremental clear → run row tagged `runner`). Returns None
    on an empty queue (no run row). The single call the worker's dedup lane makes."""
    clip_counter = [0]
    vision_errors = [0]
    deadline = time.monotonic() + max_seconds if max_seconds > 0 else None
    engine_kw = build_free_engine_kw(
        conn, compare_budget=compare_budget, floor_plan_budget=floor_plan_budget,
        deadline=deadline, clip_counter=clip_counter, vision_errors=vision_errors)
    return run_dirty_pass(
        conn, max_dirty=max_dirty, max_pairs=_REALTIME_MAX_PAIRS, engine_kw=engine_kw,
        runner=runner,
        stamp_stats=lambda s: s.update({
            "clip_classified": clip_counter[0], "vision_errors": vision_errors[0]}),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pairs", type=int, default=2000,
                        help="Max visual candidate pairs examined per run.")
    parser.add_argument("--max-vision-calls", type=int, default=200,
                        help="Cap forensic vision calls per run (0 = pHash-only, no LLM).")
    parser.add_argument("--max-room-attempts", type=int, default=4,
                        help="Max like-room forensic comparisons per candidate pair.")
    parser.add_argument("--max-seconds", type=int, default=0,
                        help="Wall-clock budget; stop + finalize cleanly before the "
                             "job timeout SIGKILLs the run (0 = no limit).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Cheap: report eligible counts + street groups and EXIT "
                             "(no funnel). For the full preview use --shadow.")
    parser.add_argument("--shadow", action="store_true",
                        help="Run the FULL pipeline (funnel + vision) but WRITE NOTHING "
                             "— previews what a live run would merge / dismiss / reconcile / queue.")
    parser.add_argument("--no-autodismiss", action="store_true",
                        help="Disable auto-dismissing confident visual 'different' verdicts "
                             "(overrides the app_settings toggle).")
    parser.add_argument("--cache-only", action="store_true",
                        help="Consume only ALREADY-WARMED vision verdicts (no LLM, $0): "
                             "applies cached merge/dismiss results the batch lane paid for; "
                             "un-warmed pairs stay queued. The cost-efficient drain half.")
    parser.add_argument("--free", action="store_true",
                        help="FREE mode: pHash merges + reconcile + reject/gate "
                             "dismissals, NO paid all-rooms classify/compare. Un-vision'd "
                             "candidate pairs are skipped (NOT queued as placeholders), so the "
                             "review queue doesn't inflate. The ONE bounded exception is the "
                             "floor-plan validation gate (see --floor-plan-budget) — a "
                             "targeted safety check on the small set of would-merge both-plan pairs.")
    parser.add_argument("--candidates", action="store_true",
                        help="Candidate-priority drain: re-decide ONLY the properties already in "
                             "still-proposed /dedup candidates (O(queue), not a full market scan), so "
                             "the manual queue self-clears regardless of where the full scan's "
                             "deadline falls. Composes with --free / --cache-only / the floor-plan "
                             "budget — same decision tree (resolve_pair), scoped work-list.")
    parser.add_argument("--dirty", action="store_true",
                        help="Real-time dirty drain: re-decide ONLY the street groups AND geo "
                             "cells that contain a just-dedup-ready property "
                             "(dedup_dirty_properties, enqueued when a listing's images get "
                             "CLIP-tagged), so a new cross-portal listing merges within minutes — "
                             "both families, one queue, one resolve_pair brain. Scoped load "
                             "(peers present) + O(dirty) pair-work; race-free per-family "
                             "claim/clear. Composes with --free / the floor-plan budget.")
    parser.add_argument("--max-dirty", type=int, default=10000, dest="max_dirty",
                        help="Bound the --dirty claim to the N NEWEST dedup-ready properties "
                             "(newest-first — the real-time SLO serves the freshest listings even "
                             "under a backlog; the stale tail TTL-evicts to the full scan, and the "
                             "per-group incremental clear advances whatever slice fits the budget). "
                             "The scheduled hourly run passes 3000; raise it for a one-off backlog "
                             "blitz dispatch.")
    parser.add_argument("--floor-plan-budget", type=int, default=None, dest="floor_plan_budget",
                        help="Override app_settings.dedup_floor_plan_budget for this run: the cap on "
                             "inline cold Sonnet floor-plan checks (a paid call on a free run; "
                             "it fires solely on pairs the engine WOULD merge — pHash matches / "
                             "visual Highs). Its OWN budget, separate from --compare-budget. Beyond "
                             "the cap, both-plan pairs DEFER to the next run. 0 = cache-only ($0): "
                             "consume only warmed verdicts. Unset = use the setting (default 10000). "
                             "NB the budget is the count of PAID calls — it is not 'free', the run "
                             "mode is.")
    parser.add_argument("--compare-budget", type=int, default=0, dest="compare_budget",
                        help="FREE mode only: cap on PAID forensic room/site-plan compares "
                             "(cache hits don't count) so a --free scheduled run auto-merges the "
                             "different-photo cross-portal pairs the pHash fast-path can't reach — "
                             "closing the auto_visual=0 gap (--free left compare_fn=None). 0 "
                             "(default) = today's pHash-only free run. Compares are CLIP-cosine-routed "
                             "(Haiku / Sonnet / skip via CosineBands) and enqueue_unresolved stays "
                             "off, so the /dedup manual queue never inflates. Separate from "
                             "--floor-plan-budget. Ignored outside --free.")
    parser.add_argument("--geo", action="store_true",
                        help="ALSO run the FULL geo pass for single-dwelling families "
                             "(dum/pozemek/komercni/ostatni) the street+disposition engine "
                             "can't reach, through the SAME free-first flow. Forces it on "
                             "regardless of the dedup_geo_enabled setting; the scheduled run "
                             "includes it whenever that setting is on. Ignored on --dirty, "
                             "which runs its own geo sub-pass scoped to the claimed cells.")
    parser.add_argument("--geo-only", action="store_true",
                        help="Run ONLY the geo pass (skip the street engine).")
    parser.add_argument("--geo-max-pairs", type=int, default=20000,
                        help="Max geo candidate pairs examined per run.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    import psycopg

    from datetime import datetime, timezone
    # Real run start — passed as the run row's started_at (the column's default-now()
    # otherwise stamps it at INSERT time, i.e. equal to ended_at, losing durations) and
    # as the pair audit's run_at. Captured before any DB work so setup/claim time counts.
    run_at = datetime.now(timezone.utc)

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        if args.dry_run:
            counts = _eligibility_counts(conn)
            keys = _load_eligible(conn)
            groups = _group_by_street(keys)
            multi = sum(1 for m in groups.values() if len(m) > 1)
            LOG.info(
                "ENGINE dry-run eligible=%d flagged_location=%d flagged_disposition=%d "
                "street_groups=%d multi_listing_groups=%d; exit",
                counts["eligible"], counts["flagged_location"],
                counts["flagged_disposition"], len(groups), multi,
            )
            return 0

        deadline = time.monotonic() + args.max_seconds if args.max_seconds > 0 else None

        from toolkit.dedup_settings import read_setting
        # Geo path (houses/land/commercial) is its OWN scheduled pass — the dedicated --geo-only
        # cron with a full budget — NOT bolted onto the street full-scan / candidate-drain. It used
        # to auto-append there whenever dedup_geo_enabled was on, and produced ZERO candidates/merges
        # because it was (a) DEADLINE-STARVED on the full scan: it ran *after* the street pass on the
        # shared --max-seconds, which the ~100K-eligible street scan exhausted; and (b) a NO-OP on the
        # candidate drain: it inherited the street pass's apartment `restrict`, so
        # _load_geo_eligible(restrict=apartment candidates) loaded no single-dwelling rows. So geo runs
        # ONLY on an explicit flag now: --geo-only (the scheduled cron, gated by the dedup_geo_enabled
        # master switch) runs geo ALONE with its own budget; --geo forces it onto any non-dirty run
        # ad-hoc (ignores the setting, for debugging). The real-time dirty drain never gets THIS full
        # pass — run_dirty_pass runs its own geo sub-pass scoped to the claimed properties' cells.
        geo_enabled = bool(read_setting(conn, "dedup_geo_enabled"))
        run_geo = _should_run_geo(
            geo=args.geo, geo_only=args.geo_only, geo_enabled=geo_enabled, dirty=args.dirty)
        if args.geo_only and not run_geo:
            LOG.info("GEO-only run but dedup_geo_enabled is off — nothing to do.")
            return 0
        geo_area_max_pct = float(read_setting(conn, "dedup_geo_area_max_pct"))

        # ----- Shared setup (settings + vision fns + caps) — used by BOTH the street and
        # geo passes, so they run the identical free-first flow (only the candidate filter
        # differs). Built once regardless of --geo-only. -----
        auto_merge_enabled = _auto_merge_enabled(conn)
        autodismiss = _visual_autodismiss_enabled(conn) and not args.no_autodismiss
        clip = _clip_settings(conn)
        # The inline floor-plan cap (app_settings.dedup_floor_plan_budget, default
        # 10000); --floor-plan-budget overrides it for an ad-hoc run.
        floor_plan_budget = (
            args.floor_plan_budget if args.floor_plan_budget is not None
            else int(read_setting(conn, "dedup_floor_plan_budget")))
        inconclusive_to_review = bool(
            read_setting(conn, "dedup_floor_plan_inconclusive_to_review"))
        clip_counter = [0]
        # Shared across all four paid-fn builders (street + geo passes): total vision/LLM
        # ERRORS this run. Trips the breaker at VISION_ERROR_BREAKER; persisted per run
        # row as vision_errors (migration 271) — the credit-outage tripwire.
        vision_errors = [0]
        # Free CLIP room tags (preferred when on); the counter tracks how many
        # listings were served from CLIP rather than the paid LLM classify.
        ck = {"prefer_clip": clip["prefer_clip"], "clip_model": clip["clip_model"],
              "clip_counter": clip_counter, "error_count": vision_errors}
        LOG.info(
            "ENGINE auto_merge_enabled=%s autodismiss=%s prefer_clip=%s cosine=%s "
            "shadow=%s cache_only=%s free=%s geo=%s",
            auto_merge_enabled, autodismiss, clip["prefer_clip"],
            clip["cosine_enabled"], args.shadow, args.cache_only, args.free, run_geo,
        )

        # Real-time dirty drain: the claim/scoped-load/clear/run-row cycle lives in
        # run_dirty_pass (shared with the realtime worker's dedup lane) and runs after
        # the shared fn/engine_kw assembly below. An empty queue returns None there —
        # building the fns first costs a handful of resolve_model SELECTs, nothing more.
        classify_fn = None
        compare_fn = None
        site_plan_fn = None
        floor_plan_fn = None
        if args.free:
            # FREE mode: no PAID all-rooms classify/compare by default -> pHash / rule-B /
            # reconcile / reject-gate only. TWO bounded paid exceptions, each on its OWN budget:
            #  (1) --compare-budget > 0 (W2): build the LIVE classify/compare/site-plan fns so
            #      the different-photo cross-portal pairs the pHash fast-path can't reach get a
            #      forensic room compare and auto-merge on a High — capped at compare_budget PAID
            #      calls (cache hits free), CLIP-cosine-routed, deadline-bounded. classify stays
            #      ~free via CLIP tags (dedup_prefer_clip_tags). enqueue_unresolved stays off
            #      (below), so the /dedup manual queue never inflates. This closes the steady-state
            #      auto_visual=0 gap: --free previously left compare_fn=None on EVERY scheduled run.
            #  (2) the floor-plan validation gate: a positive floor-plan budget gives it the LIVE
            #      fn (its single Sonnet check on would-merge both-plan pairs, Option C); budget 0
            #      = the $0 cache-only fn (consume warmed verdicts, defer the rest).
            if auto_merge_enabled and args.compare_budget > 0:
                classify_fn = _build_classify_fn(conn, **ck)
                compare_fn = _build_compare_fn(conn, error_count=vision_errors)
                site_plan_fn = _build_site_plan_fn(conn, error_count=vision_errors)
            floor_plan_fn = (
                _build_floor_plan_fn(conn, error_count=vision_errors) if floor_plan_budget > 0
                else _build_cache_only_floor_plan_fn(conn)
            )
        elif auto_merge_enabled and args.cache_only:
            # Cost-efficient consume: read warm caches only, never call the LLM.
            classify_fn, compare_fn, site_plan_fn, floor_plan_fn = _build_cache_only_fns(
                conn, **ck)
        elif auto_merge_enabled and args.max_vision_calls > 0:
            classify_fn = _build_classify_fn(conn, **ck)
            compare_fn = _build_compare_fn(conn, error_count=vision_errors)
            site_plan_fn = _build_site_plan_fn(conn, error_count=vision_errors)
            floor_plan_fn = _build_floor_plan_fn(conn, error_count=vision_errors)
        elif auto_merge_enabled:
            # pHash fast-path still needs room labels to gate on interior shots.
            classify_fn = _build_classify_fn(conn, **ck)
        # When auto-merge is off the engine never reaches the visual step, so we
        # skip building the (LLM-backed) classify/compare fns entirely.

        # Stage 4b: the CLIP cosine recall tier (default OFF). When on it picks
        # the forensic model per room from the stored-embedding cosine — 'sonnet'
        # routes via the default model (model_for['sonnet'] = None).
        cosine_fn = None
        bands = None
        model_for = None
        if clip["cosine_enabled"] and compare_fn is not None:
            from toolkit.clip_dedup import room_pair_cosine
            bands = clip["bands"]
            model_for = {"haiku": clip["haiku_model"], "sonnet": None}
            _cm = clip["clip_model"]

            def cosine_fn(ids_a: list[int], ids_b: list[int]) -> float | None:
                return room_pair_cosine(
                    conn, image_ids_a=ids_a, image_ids_b=ids_b, model=_cm)

        # Cache-only calls are free, so don't let the vision budget / room cap throttle
        # consumption — try every warmed room of every warmed pair. In --free mode this pool
        # bounds the forensic compares (--compare-budget); the floor-plan gate has its OWN
        # budget (floor_plan_calls below), so the two never starve each other.
        eff_max_vision = _effective_vision_cap(
            free=args.free, cache_only=args.cache_only,
            compare_budget=args.compare_budget,
            max_vision_calls=args.max_vision_calls)
        eff_max_rooms = 99 if args.cache_only else args.max_room_attempts

        # Candidate-priority drain: scope the scan to the properties in DUE still-proposed
        # /dedup candidates (never looked at / backoff elapsed / fresh CLIP evidence —
        # migration 272) so the queue self-clears in O(due), not O(queue) re-chewed every
        # pass. An EMPTY set (nothing due) loads nothing — a clean no-op, NOT a full scan.
        restrict = (
            _proposed_candidate_property_ids(
                conn, redecide_hours=float(
                    read_setting(conn, "dedup_candidate_redecide_hours")))
            if args.candidates else None)
        if args.candidates:
            LOG.info("CANDIDATE drain: %d properties across the DUE proposed queue",
                     len(restrict or set()))

        # Shared kwargs every pass passes to run_engine — the free-first flow itself.
        # floor_plan_calls: a value ONLY in --free mode gives the plan gate its own separate
        # budget (so --compare-budget bounds compares independently); None everywhere else
        # aliases the gate back onto the shared vision pool (byte-identical to pre-W2).
        engine_kw: dict[str, Any] = dict(
            classify_fn=classify_fn, compare_fn=compare_fn, site_plan_fn=site_plan_fn,
            floor_plan_fn=floor_plan_fn, cosine_fn=cosine_fn, bands=bands,
            model_for=model_for, render_min=clip["render_min"],
            inconclusive_to_review=inconclusive_to_review,
            max_vision_calls=eff_max_vision, max_room_attempts=eff_max_rooms,
            floor_plan_calls=(floor_plan_budget if args.free else None),
            auto_merge_enabled=auto_merge_enabled, autodismiss=autodismiss,
            enqueue_unresolved=not args.free, dry_run=args.shadow, deadline=deadline,
            restrict_property_ids=restrict,
            nonbyt_attr_merge=bool(read_setting(conn, "dedup_nonbyt_attr_merge_enabled")),
            clip_model=clip["clip_model"],
        )

        if not args.geo_only:
            if args.dirty:
                stats = run_dirty_pass(
                    conn, max_dirty=args.max_dirty, max_pairs=args.max_pairs,
                    engine_kw=engine_kw, runner="actions", shadow=args.shadow,
                    started_at=run_at,
                    stamp_stats=lambda s: s.update({
                        "clip_classified": clip_counter[0],
                        "vision_errors": vision_errors[0],
                    }),
                )
                if stats is None:
                    LOG.info("DIRTY drain: queue empty; nothing to do")
                    return 0
            else:
                pair_audit: list[dict[str, Any]] = []
                # Full-scan CURSOR: only the plain scheduled full scan rotates a persistent
                # frontier over the market (migration 261) — the dirty/candidate drains have
                # their own work-lists and stay cursor-free. cycle_started_at falls back to
                # this run's start when a fresh cycle begins; on a crash nothing is persisted,
                # which only makes the cycle stamp LATER (prune less) — conservative-safe.
                full_scan = not args.candidates and restrict is None
                cursor_out: dict[str, Any] | None = None
                scan_state: dict[str, Any] = {"cursor_key": None, "cycle_started_at": None}
                cycle_started_at: Any = None
                if full_scan:
                    scan_state = _load_scan_state(conn)
                    cycle_started_at = scan_state["cycle_started_at"] or run_at
                    cursor_out = {}
                    LOG.info("CURSOR full scan resuming after %r (cycle started %s)",
                             scan_state["cursor_key"], cycle_started_at)
                stats = run_engine(
                    conn, audit=pair_audit, max_pairs=args.max_pairs,
                    scan_cursor=scan_state["cursor_key"], cursor_out=cursor_out,
                    **engine_kw,
                )
                stats["clip_classified"] = clip_counter[0]
                stats["vision_errors"] = vision_errors[0]
                if not args.shadow:
                    if full_scan and cursor_out is not None:
                        reached_end = bool(cursor_out.get("reached_end"))
                        # A truncated run that scanned nothing keeps the previous frontier
                        # (never regress the cursor to the top on a degenerate run).
                        new_cursor = (None if reached_end
                                      else (cursor_out.get("last_key") or scan_state["cursor_key"]))
                        _save_scan_state(
                            conn, "street", cursor_key=new_cursor,
                            cycle_started_at=cycle_started_at, completed=reached_end)
                        LOG.info("CURSOR saved: %s",
                                 "cycle COMPLETED (TTL eviction re-armed)" if reached_end
                                 else f"frontier at {new_cursor!r}")
                    run_kind = "candidates" if args.candidates else "full"
                    _write_run_row(conn, stats, run_kind=run_kind, started_at=run_at)
                    _write_pair_audit(conn, run_at, pair_audit)
            LOG.info(
                "ENGINE %s eligible=%s auto_address=%d auto_phash=%d auto_visual=%d "
                "auto_dismissed=%d floor_plan_deferred=%d clip_deferred=%d reconciled=%d queued=%d "
                "skipped_unresolved=%d rejected=%d prior_dismissed_skips=%d "
                "pairs=%d vision_calls=%d",
                "shadow" if args.shadow else "done",
                stats["eligible"], stats["auto_address"], stats["auto_phash"],
                stats["auto_visual"], stats["auto_dismissed"],
                stats.get("floor_plan_deferred", 0), stats.get("clip_deferred", 0),
                stats["reconciled"],
                stats["queued"], stats["skipped_unresolved"], stats["rejected"],
                stats.get("skipped_prior_dismissed", 0),
                stats["pairs_considered"], stats["vision_calls"],
            )

        if run_geo:
            # Same free-first flow over geo-keyed single-dwelling families; geo=True swaps
            # the loader + candidate filter (classify_geo_pair, ±area tolerance) + queue
            # tier. Geo decisions land in dedup_pair_audit (decision history), the
            # tier='geo' candidate queue, and (since migration 265) the lane's OWN
            # run_kind='geo' run row — see the _write_run_row call below.
            geo_audit: list[dict[str, Any]] = []
            geo_started_at = datetime.now(timezone.utc)
            geo_clip_base = clip_counter[0]
            geo_err_base = vision_errors[0]
            # Geo ALWAYS enqueues its unresolved pairs (rule #15 (E): single-dwelling geo signals
            # never auto-merge on proximity alone, so "everything else queues for review"),
            # independent of --free. The street path's --free enqueue suppression (don't inflate
            # the queue with un-vision'd cross-source pairs that the warmer/pHash will resolve)
            # does NOT apply to geo: geo has no warmer and cross-portal houses share no photos, so
            # the queue is geo's ONLY surfacing mechanism — suppressing it would silently drop every
            # geo dup. The scheduled run is paid (auto-merges the confident ones via the facade
            # compare first); an ad-hoc --geo-only --free still surfaces the co-located candidates.
            geo_kw = {**engine_kw, "enqueue_unresolved": True}
            # Geo full-scan CURSOR (lane='geo', migration 261 — the table is lane-keyed for
            # exactly this): the scheduled geo backstop FULL-LOADS the market each run, so
            # without a frontier it head-restarted at the top every run and the tail was
            # structurally never reached (the pre-cursor street pathology). Geo groups key
            # on the stored listings.geo_cell_key — plain strings that sort lexically like
            # street keys, so run_engine's key-agnostic cursor branch applies unchanged.
            # Mirrors the street full-scan branch above: scoped runs (--candidates + --geo)
            # keep insertion order and no state is persisted; a crash persists nothing,
            # which only makes the cycle stamp LATER — conservative-safe.
            geo_full_scan = not args.candidates and restrict is None
            geo_cursor_out: dict[str, Any] | None = None
            geo_scan_state: dict[str, Any] = {"cursor_key": None, "cycle_started_at": None}
            geo_cycle_started_at: Any = None
            if geo_full_scan:
                geo_scan_state = _load_scan_state(conn, "geo")
                geo_cycle_started_at = geo_scan_state["cycle_started_at"] or geo_started_at
                geo_cursor_out = {}
                LOG.info("CURSOR geo scan resuming after %r (cycle started %s)",
                         geo_scan_state["cursor_key"], geo_cycle_started_at)
            geo_stats = run_engine(
                conn, audit=geo_audit, max_pairs=args.geo_max_pairs,
                geo=True, geo_area_max_pct=geo_area_max_pct,
                scan_cursor=geo_scan_state["cursor_key"], cursor_out=geo_cursor_out,
                **geo_kw,
            )
            geo_stats["clip_classified"] = clip_counter[0] - geo_clip_base
            geo_stats["vision_errors"] = vision_errors[0] - geo_err_base
            if not args.shadow:
                if geo_full_scan and geo_cursor_out is not None:
                    geo_reached_end = bool(geo_cursor_out.get("reached_end"))
                    # A truncated run that scanned nothing keeps the previous frontier
                    # (never regress the cursor to the top on a degenerate run).
                    geo_new_cursor = (
                        None if geo_reached_end
                        else (geo_cursor_out.get("last_key")
                              or geo_scan_state["cursor_key"]))
                    _save_scan_state(
                        conn, "geo", cursor_key=geo_new_cursor,
                        cycle_started_at=geo_cycle_started_at, completed=geo_reached_end)
                    LOG.info("CURSOR geo saved: %s",
                             "cycle COMPLETED" if geo_reached_end
                             else f"frontier at {geo_new_cursor!r}")
                # The geo lane writes its OWN run row (run_kind='geo', migration 262/265) —
                # it previously wrote none, so a chronically truncating geo scan was
                # invisible. Its `eligible` is the GEO lane's count; the street gauge
                # pickers exclude it by run_kind, so it never pollutes the /dedup gauges.
                # started_at is the GEO pass's own start (on a combined run, run_at would
                # bill the whole street pass to the geo row's duration).
                _write_run_row(conn, geo_stats, run_kind="geo", started_at=geo_started_at)
                _write_pair_audit(conn, run_at, geo_audit)
            LOG.info(
                "GEO %s eligible=%d auto_phash=%d auto_visual=%d auto_dismissed=%d "
                "floor_plan_deferred=%d queued=%d skipped_unresolved=%d rejected=%d "
                "pairs=%d vision_calls=%d area_max=%.2f",
                "shadow" if args.shadow else "done",
                geo_stats["eligible"], geo_stats["auto_phash"], geo_stats["auto_visual"],
                geo_stats["auto_dismissed"], geo_stats.get("floor_plan_deferred", 0),
                geo_stats["queued"], geo_stats["skipped_unresolved"], geo_stats["rejected"],
                geo_stats["pairs_considered"], geo_stats["vision_calls"], geo_area_max_pct,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
