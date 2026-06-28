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
     (classify_pair). Rule B exact-address pairs auto-merge immediately
     (5% area guard); rule C contradictions are rejected; the rest are candidates.
  2. For each candidate pair:
       a. pHash fast-path (FREE, no LLM, runs FIRST, all sources) — >=2
          near-identical image pairs (any image) -> auto-merge. Catches
          identical-photo re-posts before paying for any classify/compare.
       b. cross-source gate — same-source pairs that pHash didn't resolve are
          skipped (the paid visual layer only pays off cross-portal).
       c. layered visual confirmation (rule D), cross-source only: classify ->
          room-aware forensic comparison in priority order, stop at first High.
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
    geo_cell_key,
    phash_excluded_tags_for,
    phash_render_exclude_for,
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
_ELIGIBLE_SQL = """
    SELECT
      l.sreality_id, l.property_id, l.source,
      l.street, l.street_id, l.disposition, l.house_number, l.floor, l.area_m2,
      left(l.description, 600) AS description,
      l.category_type, l.category_main, l.obec_id
    FROM listings l
    JOIN properties p ON p.id = l.property_id AND p.status = 'active'
    WHERE l.street IS NOT NULL AND l.street <> ''
      AND l.disposition IS NOT NULL
      {filter}
    ORDER BY l.obec_id NULLS LAST, l.street_id NULLS LAST, lower(l.street), l.disposition
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
      (_claimed_street_groups), so a dirty property's group still carries its
      existing peers for re-decision. obec-bounded ⇒ complete; the load filter folds
      a NULL obec to -1 to mirror _claimed_street_groups. (None on both = full scan.)"""
    params: dict[str, Any] = {}
    flt = ""
    if restrict_property_ids is not None:  # an EMPTY set restricts to nothing (not all)
        flt = "AND l.property_id = ANY(%(pids)s)"
        params["pids"] = list(restrict_property_ids)
    elif restrict_street_groups is not None:
        # An empty (street_ids, name_keys) restricts to nothing: ANY('{}') is false
        # and IN (empty) is false, so the OR yields no rows (not all).
        street_ids, name_keys = restrict_street_groups
        flt = (
            "AND (l.street_id = ANY(%(sids)s::bigint[]) "
            "OR (coalesce(l.obec_id, -1), l.street_name_key) IN "
            "(SELECT o, k FROM unnest(%(obecs)s::bigint[], %(keys)s::text[]) AS t(o, k)))"
        )
        params["sids"] = list(street_ids)
        params["obecs"] = [o for o, _ in name_keys]
        params["keys"] = [k for _, k in name_keys]
    with conn.cursor() as cur:
        cur.execute(_ELIGIBLE_SQL.format(filter=flt), params)
        rows = cur.fetchall()
    keys: list[ListingKey] = []
    for r in rows:
        raw_street_id = int(r[4]) if r[4] is not None else None
        street_id = raw_street_id if raw_street_id is not None and raw_street_id > 0 else None
        obec_id = int(r[12]) if r[12] is not None else None
        for street_key in street_group_keys(r[3], raw_street_id, obec_id):
            keys.append(ListingKey(
                sreality_id=int(r[0]),
                property_id=int(r[1]) if r[1] is not None else None,
                source=r[2],
                street_key=street_key,
                disposition=r[5],
                house_number=r[6],
                floor=int(r[7]) if r[7] is not None else None,
                area_m2=float(r[8]) if r[8] is not None else None,
                description=r[9],
                category_type=r[10],
                category_main=r[11],
                street_id=street_id,
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


def _proposed_candidate_property_ids(conn: Any) -> set[int]:
    """Every property that appears in a still-`proposed` /dedup candidate — the work-list
    for the candidate-priority drain. The two properties of a candidate share a street +
    disposition, so scoping `_load_eligible` to this set re-forms exactly those pairs in
    their street group and re-decides them via resolve_pair, without a full market scan."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT left_property_id, right_property_id FROM property_identity_candidates "
            "WHERE status = 'proposed'")
        rows = cur.fetchall()
    out: set[int] = set()
    for left, right in rows:
        if left is not None:
            out.add(int(left))
        if right is not None:
            out.add(int(right))
    return out


# --- real-time dedup queue drain (dedup_dirty_properties, migration 242; the writer-side
#     enqueue is scraper.db.mark_properties_dedup_dirty_for_images, mirroring dirty_properties)
def _claim_dedup_dirty(conn: Any, cutoff: Any, limit: int | None = None) -> set[int]:
    """Property ids dirtied at/before `cutoff` (claim slice). A row re-dirtied AFTER cutoff
    (marked_at > cutoff via a writer's ON CONFLICT) is neither claimed nor cleared — it
    survives to the next pass (race-free + terminating, mirrors recompute's dirty drain).

    `limit` bounds the slice to the N OLDEST dirty properties (FIFO). The drain MUST be
    bounded like every sibling drain: a tagging backlog (a new portal, a retag campaign) can
    enqueue most of the market at once, and an unbounded claim then resolves O(market) groups
    per hourly run — it never completes within the time budget, so it never clears, so the queue
    only grows (and the huge claim + full load can drop the pooled connection mid-run). With a
    bound each run completes-and-clears its slice and the backlog drains over successive runs."""
    sql = "SELECT property_id FROM dedup_dirty_properties WHERE marked_at <= %s ORDER BY marked_at"
    params: list[Any] = [cutoff]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {int(r[0]) for r in cur.fetchall()}


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


def _enqueue_candidate(
    conn: Any, a: ListingKey, b: ListingKey, markers: dict[str, Any],
    *, tier: str = "street_disposition",
) -> None:
    # `markers["tier"]` is the source of truth for the COLUMN (resolve_pair sets it to
    # ctx.tier — 'street_disposition' or 'geo'); the `tier` kwarg is only the fallback for
    # the rare markers dict that omits it (the street-only rule-B auto_merge_off path).
    from psycopg.types.json import Jsonb
    if a.property_id is None or b.property_id is None or a.property_id == b.property_id:
        return
    lo, hi = sorted((a.property_id, b.property_id))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO property_identity_candidates
                (left_property_id, right_property_id, tier, confidence, markers_matched)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (left_property_id, right_property_id) DO NOTHING
            """,
            (lo, hi, markers.get("tier", tier), markers.get("confidence"), Jsonb(markers)),
        )


# --- geo path (single-dwelling families) ------------------------------------
# Disposition-less houses/land/commercial that the street pass can't reach. Blocked
# by geo cell (one obec + a rounded coordinate). ACTIVE only for P1 (the operator's
# pain is active duplicate cards); inactive-for-history is a later concern. The
# NOT(street AND disposition) clause hands the rare disposition-bearing non-apartment
# to the street pass instead, so the two passes never double-handle a pair.
_GEO_ELIGIBLE_SQL = """
    SELECT l.sreality_id, l.property_id, l.source, l.house_number,
           coalesce(l.area_m2, l.estate_area, l.usable_area) AS area,
           left(l.description, 600) AS description,
           l.category_type, l.category_main, l.obec_id, l.price_czk,
           ST_Y(l.geom::geometry) AS lat, ST_X(l.geom::geometry) AS lng
    FROM listings l
    JOIN properties p ON p.id = l.property_id AND p.status = 'active'
    WHERE l.is_active = true
      AND l.category_main IN ('dum', 'pozemek', 'komercni', 'ostatni')
      AND l.geom IS NOT NULL
      AND l.obec_id IS NOT NULL
      AND coalesce(l.area_m2, l.estate_area, l.usable_area) IS NOT NULL
      AND NOT (l.street IS NOT NULL AND l.street <> '' AND l.disposition IS NOT NULL)
      {filter}
    ORDER BY l.obec_id, l.category_main, l.category_type
"""


def _load_geo_eligible(conn: Any,
                       restrict_property_ids: set[int] | None = None) -> list[ListingKey]:
    """One ListingKey per geo-eligible single-dwelling listing, keyed by its geo cell
    (so the existing _group_by_street groups them). Carries lat/lng/price for the geo
    classifier; disposition/floor/street_id are unused on this path.

    `restrict_property_ids` scopes to those properties (the candidate drain), exactly like
    the street `_load_eligible` — an EMPTY set restricts to nothing (not all)."""
    params: dict[str, Any] = {}
    flt = ""
    if restrict_property_ids is not None:
        flt = "AND l.property_id = ANY(%(pids)s)"
        params["pids"] = list(restrict_property_ids)
    with conn.cursor() as cur:
        cur.execute(_GEO_ELIGIBLE_SQL.format(filter=flt), params)
        rows = cur.fetchall()
    keys: list[ListingKey] = []
    for r in rows:
        lat = float(r[10]) if r[10] is not None else None
        lng = float(r[11]) if r[11] is not None else None
        obec_id = int(r[8]) if r[8] is not None else None
        cell = geo_cell_key(obec_id, lat, lng, r[7], r[6])
        if cell is None:
            continue
        keys.append(ListingKey(
            sreality_id=int(r[0]),
            property_id=int(r[1]) if r[1] is not None else None,
            source=r[2], street_key=cell, disposition="",
            house_number=r[3], floor=None,
            area_m2=float(r[4]) if r[4] is not None else None,
            description=r[5], category_type=r[6], category_main=r[7],
            street_id=None, lat=lat, lng=lng,
            price_czk=int(r[9]) if r[9] is not None else None,
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
    inconclusive_to_review: bool = True,
) -> str:
    """The floor-plan validation on a pair the engine WOULD merge (pHash or visual).
    Returns 'merge' | 'dismiss' | 'queue' | 'defer'. It only adds conservatism, and
    crucially distinguishes "a human must decide" (queue) from "validate it later"
    (defer):
      * both sides carry a floor plan + a Sonnet verdict is available -> the verdict
        decides: 'different_layout' -> 'dismiss'; 'inconclusive' -> 'queue' when
        `inconclusive_to_review` (default on, app_settings.dedup_floor_plan_inconclusive_to_review),
        else 'merge'; 'same_layout' -> 'merge' (auto-confirm — the verdict weighs layout +
        the OCR'd unit/area/floor labels). When a side carries SEVERAL plans the verdict is
        N×N (migration 243): one call sees every labelled plan of both, 'same_layout' if ANY
        A-plan matches ANY B-plan, 'different_layout' only if NONE do — so a matching plan
        among several can never be missed into a wrong dismiss;
      * both sides carry a plan but the verdict isn't available yet (no fn / no budget /
        cache-miss) -> 'defer': skip this run and re-try next, once the batch lane warms
        the verdict — NOT the operator queue (this pair is automatable, not a human call);
      * exactly ONE side has a plan -> 'queue' (manual review: no plan-to-plan compare);
      * neither side has a plan -> 'merge' (existing path unchanged).
    """
    ids_a = _floor_plan_image_ids(conn, a_id)
    ids_b = _floor_plan_image_ids(conn, b_id)
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
        if verdict == "inconclusive" and inconclusive_to_review:
            return "queue"
        return "merge"
    if ids_a or ids_b:
        return "queue"
    return "merge"


def _both_have_site_plan(conn: Any, a_id: int, b_id: int) -> bool:
    """True if BOTH listings carry a site/situation plan (CLIP tag OR LLM
    classification), so the pHash fast-path defers to the site-plan development
    guard instead of auto-merging two units of one development.

    Prefers the full-inventory CLIP image_clip_tags, falling back to the LLM
    image_room_classifications cache — mirroring _floor_plan_image_ids. The LLM
    classifier never tagged a single dum/pozemek/komercni site plan, so reading
    CLIP is what lets this guard fire on house/land developments (where shared
    masterplans drive most false merges), not just the ~1% of classified flats.
    """
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
        return bool(cur.fetchone()[0])


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
    max_room_attempts: int,
    autodismiss: bool = True,
    cosine_fn: Any = None,
    bands: Any = None,
    model_for: dict[str, str] | None = None,
    render_min: float = RENDER_SCORE_EXCLUDE_MIN,
    inconclusive_to_review: bool = True,
    tag_overrides: dict[str, list[str]] | None = None,
    stats: dict[str, int] | None = None,
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
            # Floor-plan validation gate (migration 234): a different floor plan overrides
            # even a High forensic verdict (dismiss); a one-sided plan -> manual queue; an
            # unwarmed both-plan verdict -> defer (re-try next run once the batch warms it,
            # NOT queue). same/inconclusive/none -> the auto-merge stands.
            fp = _floor_plan_gate(
                conn, a.sreality_id, b.sreality_id,
                floor_plan_fn=floor_plan_fn, vision_budget=vision_budget,
                inconclusive_to_review=inconclusive_to_review)
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


def _write_pair_audit(
    conn: Any, run_at: Any, records: list[dict[str, Any]],
) -> None:
    if not records:
        return
    import json
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
                for r in records
            ],
        )


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
    # mutable run state
    stats: dict[str, int] = field(default_factory=dict)
    vision_budget: list[int] = field(default_factory=lambda: [0])
    audit: list[dict[str, Any]] | None = None
    seen_listing_pairs: set[tuple[int, int]] = field(default_factory=set)
    seen_property_pairs: set[tuple[int, int]] = field(default_factory=set)
    merged_pairs: set[tuple[int, int]] = field(default_factory=set)
    dismissed_pairs: set[tuple[int, int]] = field(default_factory=set)
    pairs_left: int = 10 ** 9


def resolve_pair(conn: Any, a: ListingKey, b: ListingKey, *, street_key: str,
                 ctx: _RunContext) -> None:
    """Decide ONE eligible same-street pair and apply the outcome (merge / dismiss /
    queue / defer / skip), mutating `ctx`. The single source of truth for the dedup
    decision tree — rule A/B/C, the pHash fast-path + floor-plan gate, the cross-source
    gate, and rule-D forensic visual — shared by every driver. Returns nothing; its
    effects are the DB writes (merge/enqueue) plus ctx mutations the caller persists."""
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

    # Tagging-readiness gate: a listing must be FULLY CLIP-tagged before the engine decides on
    # it. An incompletely-tagged listing's floor-plan / room images may still be in the tag
    # queue, so the floor-plan gate would mis-read 'one-sided' (a pending plan looks absent — the
    # false floor_plan_review queue) and the visual flow would under-pair rooms. DEFER (no
    # re-queue needed: a pending image already has clip_tagged_at IS NULL, so `clip_tag.yml` will
    # tag it — re-queuing here would only cycle a terminally-undecodable image). The clip_tag job
    # enqueues the property into dedup_dirty_properties once its LAST image is tagged, so the
    # hourly --dirty drain re-decides it within minutes — then both sides are complete and a real
    # two-sided floor-plan compare merges on matching plans. Always on whenever CLIP is the tagger
    # (clip_model set) — there is no opt-out (it replaced the retired dedup_clip_only setting).
    if ctx.clip_model and _clip_incomplete(conn, [a.sreality_id, b.sreality_id], ctx.clip_model):
        stats["clip_deferred"] += 1
        return

    # Rule B (exact address) is RETIRED (2026-06): it was the only auto-merge path with false
    # merges (6.7% later unmerged — two units at one address — vs 0% for pHash/visual). Exact
    # address is not unit-conclusive, so classify_pair now returns it as a CANDIDATE: the pair
    # flows through the pHash fast-path + forensic visual + floor-plan gate below, like any
    # street+disposition pair.

    # pHash fast-path (FREE, BEFORE classify, ALL sources). A strong raw photo match
    # (>= PHASH_MIN_IDENTICAL_PAIRS near-identical pairs over the listings' images) is a
    # same-property signal that needs no LLM. The pair already passed rule C, so a match
    # here auto-merges. Runs before the cross-source gate so identical-photo re-posts
    # (incl. same-source, which the gate would otherwise drop) merge for free — and
    # cross-posted cross-source pairs skip classify AND compare. For byt, known-exterior/
    # shared images are excluded from the count so a development's reused renders can't
    # reach the >=2 threshold; other categories count any image. A single near-identical
    # KITCHEN/BATHROOM match also qualifies (distinctive override) — but ONLY for byt: a
    # house's facade/garden is shared across a development's units, so distinctive_rooms_for
    # returns an empty set for non-byt families and the override is skipped (require >=2).
    _rmin = phash_render_exclude_for(a.category_main, ctx.render_min)
    phash_pairs = _phash_identical_pairs(
        conn, a.sreality_id, b.sreality_id,
        phash_excluded_tags_for(a.category_main), render_exclude_min=_rmin)
    _distinctive_rooms = distinctive_rooms_for(a.category_main)
    distinctive = (
        bool(_distinctive_rooms)
        and phash_pairs < PHASH_MIN_IDENTICAL_PAIRS
        and _phash_distinctive_match(
            conn, a.sreality_id, b.sreality_id,
            rooms=_distinctive_rooms, render_exclude_min=_rmin))
    if decide_phash_fastpath(phash_pairs, distinctive) and not _both_have_site_plan(
        conn, a.sreality_id, b.sreality_id
    ):
        factors = _factors("phash", reason="image_phash",
                           street_key=street_key, phash_pairs=phash_pairs,
                           phash_distinctive=distinctive)
        if not ctx.auto_merge_enabled:
            if not ctx.dry_run:
                _enqueue_candidate(conn, a, b, {
                    **factors, "tier": ctx.tier,
                    "reason": "auto_merge_off:image_phash", "confidence": 0.97})
            stats["queued"] += 1
            return
        # Floor-plan validation gate (migration 234): a different floor plan DISMISSES, a
        # one-sided plan goes to MANUAL queue, an unwarmed both-plan verdict DEFERS (skip,
        # re-try next run once the batch warms it — never the manual queue); otherwise the
        # pHash merge proceeds.
        fp = _floor_plan_gate(
            conn, a.sreality_id, b.sreality_id,
            floor_plan_fn=ctx.floor_plan_fn, vision_budget=ctx.vision_budget,
            inconclusive_to_review=ctx.inconclusive_to_review)
        if fp == "dismiss":
            stats["auto_dismissed"] += 1
            ctx.dismissed_pairs.add(cp)
            _audit(ctx.audit, a, b, "phash", "dismissed",
                   {**factors, "reason": "floor_plan_different_layout"},
                   source="engine")
            return
        if fp == "queue":
            if not ctx.dry_run:
                _enqueue_candidate(conn, a, b, {
                    **factors, "tier": ctx.tier,
                    "reason": "floor_plan_review", "confidence": 0.6})
            stats["queued"] += 1
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
            })
        stats["queued"] += 1
        return
    # (Tagging readiness is enforced once, up front — see the _clip_incomplete gate at the top
    # of resolve_pair — so every pair reaching the visual stage is fully CLIP-tagged.)
    outcome = _resolve_visual(
        conn, a, b, classify_fn=ctx.classify_fn, compare_fn=ctx.compare_fn,
        site_plan_fn=ctx.site_plan_fn, floor_plan_fn=ctx.floor_plan_fn,
        vision_budget=ctx.vision_budget, max_room_attempts=ctx.max_room_attempts,
        autodismiss=ctx.autodismiss,
        cosine_fn=ctx.cosine_fn, bands=ctx.bands, model_for=ctx.model_for,
        render_min=ctx.render_min, inconclusive_to_review=ctx.inconclusive_to_review,
        tag_overrides=ctx.tag_overrides,
        stats=stats,
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
        _audit(ctx.audit, a, b, "visual", "dismissed", factors, source="engine")
    elif outcome["action"] == "defer":
        # Floor-plan verdict not warmed yet -> skip, re-try next run (the batch lane
        # warms it). NOT the manual queue.
        stats["floor_plan_deferred"] += 1
    elif ctx.enqueue_unresolved:
        if not ctx.dry_run:
            _enqueue_candidate(conn, a, b, markers)
        stats["queued"] += 1
        # NOT audited — a queued pair IS the candidate; its factor detail lives in
        # markers_matched (Needs-review reads it). Auditing queued re-logged the same
        # pair every run (the duplicate-row bug).
    else:
        # Free mode: don't pile un-vision'd pairs into the review queue (they'd just be
        # 'no photos compared' placeholders). pHash / rule-B / reconcile already ran; this
        # pair is left for a future run (free pHash as coverage grows, or vision if
        # re-enabled).
        stats["skipped_unresolved"] += 1


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
    max_room_attempts: int = 4,
    auto_merge_enabled: bool = True,
    autodismiss: bool = True,
    enqueue_unresolved: bool = True,
    dry_run: bool = False,
    deadline: float | None = None,
    restrict_property_ids: set[int] | None = None,
    restrict_street_groups: tuple[set[int], set[tuple[int, str]]] | None = None,
    only_groups_with_property_ids: set[int] | None = None,
    geo: bool = False,
    geo_area_max_pct: float | None = None,
    clip_model: str | None = None,
) -> dict[str, int]:
    """Run the full pipeline once. classify_fn/compare_fn are injectable for tests.

    geo=True runs the SAME flow over single-dwelling families (house/land/commercial)
    keyed by geo-proximity instead of street+disposition: the only differences are the
    candidate loader (_load_geo_eligible), the candidate filter (classify_geo_pair, mapped
    auto_merge→candidate so the free-first visual flow is the sole merge gate), and the
    queue tier ('geo'). Everything else — pHash → cosine → forensic compare (facade /
    site-plan priority via room_priority_for) → floor/site-plan gate — is shared.

    classify_fn(sreality_id) -> classify_listing_images envelope.
    compare_fn(a, b, room_type, ids_a, ids_b) -> {verdict, rationale} | None.
    site_plan_fn(a, b, ids_a, ids_b) -> {verdict, rationale} | None (development
    guard: verdict ∈ same_unit|different_unit|inconclusive).

    Self-healing: each run also RESOLVES stale proposed candidates rather than
    letting them pile up in the operator queue — it dismisses a pair the current
    rules reject (deterministic non-match), one the cross-source gate skips, or a
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
    if geo:
        keys = _load_geo_eligible(conn, restrict_property_ids=restrict_property_ids)
        stats = {
            "eligible": len({k.sreality_id for k in keys}),
            "flagged_location": 0, "flagged_disposition": 0,
        }
    else:
        keys = _load_eligible(
            conn, restrict_property_ids=restrict_property_ids,
            restrict_street_groups=restrict_street_groups)
        stats = _eligibility_counts(conn)
    stats.update({
        "pairs_considered": 0, "rejected": 0,
        "auto_address": 0, "auto_phash": 0, "auto_visual": 0,
        "queued": 0, "vision_calls": 0, "skipped_same_source": 0,
        "auto_dismissed": 0, "reconciled": 0, "skipped_unresolved": 0,
        "floor_plan_deferred": 0, "clip_deferred": 0, "truncated": 0,
        "clip_classified": 0, "clip_cosine_calls": 0,
        "routed_haiku": 0, "routed_sonnet": 0,
        # Observability: the --dirty path stamps these post-claim (NULL on other run modes).
        "dirty_queue_depth": None, "dirty_claimed": None,
    })

    if not geo:
        stats["reconciled"] = _reconcile_stale_candidates(conn, dry_run=dry_run)

    groups = _group_by_street(keys)
    max_group_size = MAX_GEO_GROUP_SIZE if geo else MAX_GROUP_SIZE
    # The candidate FILTER + queue tier are the geo path's only divergence; the geo classify
    # maps auto_merge → candidate so a deterministic geo signal never merges on its own — the
    # shared free-first visual flow is the sole merge gate.
    classify = _make_geo_classify(geo_area_max_pct) if geo else classify_pair
    from toolkit.dedup_priorities import load_tag_priority_overrides
    tag_overrides = load_tag_priority_overrides(conn)
    ctx = _RunContext(
        classify=classify, tier=("geo" if geo else "street_disposition"),
        tag_overrides=tag_overrides, clip_model=clip_model,
        classify_fn=classify_fn, compare_fn=compare_fn, site_plan_fn=site_plan_fn,
        floor_plan_fn=floor_plan_fn, cosine_fn=cosine_fn, bands=bands, model_for=model_for,
        auto_merge_enabled=auto_merge_enabled, autodismiss=autodismiss,
        enqueue_unresolved=enqueue_unresolved, dry_run=dry_run, render_min=render_min,
        inconclusive_to_review=inconclusive_to_review, max_room_attempts=max_room_attempts,
        stats=stats, vision_budget=[max_vision_calls], audit=audit, pairs_left=max_pairs,
    )

    def finalize() -> dict[str, int]:
        # Resolve every candidate the engine acted on this run (no-op for pairs
        # without a proposed row); a no-op set-based UPDATE when nothing collected.
        if not dry_run:
            _resolve_candidates(conn, ctx.merged_pairs, "merged")
            _resolve_candidates(conn, ctx.dismissed_pairs, "dismissed")
        return _finish(stats, ctx.vision_budget, max_vision_calls)

    # The decision tree per pair lives in resolve_pair (shared by the candidate-priority
    # drain + the real-time path); this is just the full-scan driver over street groups.
    for street_key, members in groups.items():
        if len(members) > max_group_size:
            LOG.info("SKIP large group key=%s size=%d", street_key, len(members))
            continue
        # Real-time (dirty) drain: the load is SCOPED to the dirty properties' street
        # groups (restrict_street_groups), so it carries each dirty property's existing
        # PEERS while staying O(dirty); this filter then resolves only groups that
        # actually contain a dirty/just-ready property — the correctness gate under the
        # scoped load (a group reaches here only if its key was claimed, so this is a
        # safety re-assertion, not the primary scope). No fragile SQL street-key replay.
        if only_groups_with_property_ids is not None and not any(
            m.property_id in only_groups_with_property_ids for m in members
        ):
            continue
        for i in range(len(members)):
            if ctx.pairs_left <= 0:
                stats["truncated"] = 1  # pair cap exhausted between groups — also truncation
                break
            for j in range(i + 1, len(members)):
                if ctx.pairs_left <= 0:
                    LOG.info("PAIR cap reached; deferring remainder to next run")
                    stats["truncated"] = 1  # scan did NOT finish — dirty drain keeps its claim
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
                    stats["truncated"] = 1  # scan did NOT finish — dirty drain keeps its claim
                    return finalize()
                resolve_pair(conn, members[i], members[j], street_key=street_key, ctx=ctx)

    return finalize()


def _finish(stats: dict[str, int], vision_budget: list[int], max_vision_calls: int) -> dict[str, int]:
    stats["vision_calls"] = max_vision_calls - vision_budget[0]
    return stats


def _build_classify_fn(
    conn: Any, *, prefer_clip: bool = False, clip_model: str | None = None,
    clip_counter: list[int] | None = None,
) -> Any:
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.clip_dedup import clip_room_grouping
    from toolkit.image_classification import classify_listing_images
    llm = LLMClient(conn, providers=get_providers())

    def _fn(sreality_id: int) -> dict[str, Any]:
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
        return classify_listing_images(conn, llm, sreality_id=sreality_id)
    return _fn


def _build_compare_fn(conn: Any) -> Any:
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.visual_match import compare_listings_visually
    llm = LLMClient(conn, providers=get_providers())

    def _fn(a: int, b: int, room_type: str, ids_a: list[int], ids_b: list[int],
            model: str | None = None) -> dict[str, Any] | None:
        try:
            res = compare_listings_visually(
                conn, llm, sreality_id_a=a, sreality_id_b=b,
                room_type=room_type, image_ids_a=ids_a, image_ids_b=ids_b,
                model=model,
            )
            return res["data"]
        except Exception as exc:  # noqa: BLE001 - one bad pair must not kill the run
            LOG.warning("visual compare %s/%s room=%s failed: %s", a, b, room_type, exc)
            return None
    return _fn


def _build_site_plan_fn(conn: Any) -> Any:
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.visual_match import compare_listing_site_plans
    llm = LLMClient(conn, providers=get_providers())

    def _fn(a: int, b: int, ids_a: list[int], ids_b: list[int]) -> dict[str, Any] | None:
        try:
            res = compare_listing_site_plans(
                conn, llm, sreality_id_a=a, sreality_id_b=b,
                image_ids_a=ids_a, image_ids_b=ids_b,
            )
            return res["data"]
        except Exception as exc:  # noqa: BLE001 - one bad pair must not kill the run
            LOG.warning("site-plan compare %s/%s failed: %s", a, b, exc)
            return None
    return _fn


def _build_floor_plan_fn(conn: Any) -> Any:
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.visual_match import compare_listing_floor_plans
    llm = LLMClient(conn, providers=get_providers())

    def _fn(a: int, b: int, ids_a: list[int], ids_b: list[int]) -> dict[str, Any] | None:
        try:
            res = compare_listing_floor_plans(
                conn, llm, sreality_id_a=a, sreality_id_b=b,
                image_ids_a=ids_a, image_ids_b=ids_b,
            )
            return res["data"]
        except Exception as exc:  # noqa: BLE001 - one bad pair must not kill the run
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


def _effective_vision_cap(*, free: bool, cache_only: bool, floor_plan_budget: int,
                          max_vision_calls: int) -> int:
    """The vision-budget cap handed to run_engine, by mode. Cache-only reads are free, so a
    large cap keeps the budget from throttling them. In --free mode the ONLY vision consumer
    is the floor-plan validation gate: a positive floor_plan_budget caps its inline COLD
    floor-plan checks; 0 selects the cache-only floor_plan_fn (which never makes a cold
    call) -> a large cap so a zero budget can't pre-empt the gate before it reads the warm
    cache (the gate defers when vision_budget<=0)."""
    if cache_only:
        return 10_000_000
    if free:
        return floor_plan_budget if floor_plan_budget > 0 else 10_000_000
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


def _write_run_row(conn: Any, stats: dict[str, int]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dedup_engine_runs (
                ended_at, eligible, flagged_location, flagged_disposition,
                pairs_considered, rejected, auto_address, auto_phash, auto_visual,
                queued, vision_calls, auto_dismissed, floor_plan_deferred, clip_deferred,
                clip_classified, clip_cosine_calls, routed_haiku, routed_sonnet,
                dirty_queue_depth, dirty_claimed
            ) VALUES (now(), %(eligible)s, %(flagged_location)s, %(flagged_disposition)s,
                %(pairs_considered)s, %(rejected)s, %(auto_address)s, %(auto_phash)s,
                %(auto_visual)s, %(queued)s, %(vision_calls)s, %(auto_dismissed)s,
                %(floor_plan_deferred)s, %(clip_deferred)s,
                %(clip_classified)s, %(clip_cosine_calls)s, %(routed_haiku)s,
                %(routed_sonnet)s, %(dirty_queue_depth)s, %(dirty_claimed)s)
            """,
            stats,
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
                        help="FREE mode: pHash + exact-address merges + reconcile + reject/gate "
                             "dismissals, NO paid all-rooms classify/compare. Un-vision'd "
                             "cross-source pairs are skipped (NOT queued as placeholders), so the "
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
                        help="Real-time dirty drain: re-decide ONLY the street groups that contain a "
                             "just-dedup-ready property (dedup_dirty_properties, enqueued when a "
                             "listing's images get CLIP-tagged), so a new cross-portal listing merges "
                             "within minutes. Scoped load (peers present) + O(dirty) pair-work; "
                             "race-free claim/clear. Composes with --free / the floor-plan budget.")
    parser.add_argument("--max-dirty", type=int, default=10000, dest="max_dirty",
                        help="Bound the --dirty claim to the N OLDEST dedup-ready properties (FIFO). "
                             "Keeps each hourly run complete-and-clearing so a tagging backlog (new "
                             "portal / retag campaign) that enqueues most of the market drains over "
                             "successive runs instead of an unbounded claim that never completes. "
                             "Raise it for a one-off backlog blitz dispatch.")
    parser.add_argument("--floor-plan-budget", type=int, default=None, dest="floor_plan_budget",
                        help="Override app_settings.dedup_floor_plan_budget for this run: the cap on "
                             "inline cold Sonnet floor-plan checks (the only paid call on a free run; "
                             "it fires solely on pairs the engine WOULD merge — pHash matches / "
                             "visual Highs). Beyond the cap, both-plan pairs DEFER to the next run. "
                             "0 = cache-only ($0): consume only warmed verdicts. Unset = use the "
                             "setting (default 10000). NB the budget is the count of PAID calls — "
                             "it is not 'free', the run mode is.")
    parser.add_argument("--geo", action="store_true",
                        help="ALSO run the geo pass for single-dwelling families "
                             "(dum/pozemek/komercni/ostatni) the street+disposition engine "
                             "can't reach, through the SAME free-first flow. Forces it on "
                             "regardless of the dedup_geo_enabled setting; the scheduled run "
                             "includes it whenever that setting is on. Skipped on --dirty.")
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
        # Geo path (houses/land/commercial): the scheduled run includes it when the operator
        # flips dedup_geo_enabled; --geo / --geo-only force it ad-hoc. Default off. It runs on
        # the FULL scan + the (bounded, candidate-scoped) candidate drain, but NOT the
        # real-time dirty drain — geo isn't dirty-scoped, so running it there would do a full
        # unscoped scan every :45 tick. Geo real-time is a later enhancement.
        geo_enabled = bool(read_setting(conn, "dedup_geo_enabled"))
        run_geo = (args.geo or args.geo_only or geo_enabled) and not args.dirty
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
        # Free CLIP room tags (preferred when on); the counter tracks how many
        # listings were served from CLIP rather than the paid LLM classify.
        ck = {"prefer_clip": clip["prefer_clip"], "clip_model": clip["clip_model"],
              "clip_counter": clip_counter}
        LOG.info(
            "ENGINE auto_merge_enabled=%s autodismiss=%s prefer_clip=%s cosine=%s "
            "shadow=%s cache_only=%s free=%s geo=%s",
            auto_merge_enabled, autodismiss, clip["prefer_clip"],
            clip["cosine_enabled"], args.shadow, args.cache_only, args.free, run_geo,
        )

        # Real-time dirty drain: claim the dedup-ready properties at/before a cutoff
        # BEFORE building the (LLM-backed) fns, so an empty queue exits without that work.
        # The clear after the run is gated on a NON-truncated run, so a deadline-cut run
        # keeps its whole claim (re-drained next pass) and never loses unprocessed work.
        only_groups = None
        dirty_street_groups: tuple[set[int], set[tuple[int, str]]] | None = None
        dirty_cutoff = None
        if args.dirty:
            with conn.cursor() as cur:
                cur.execute("SELECT now()")
                dirty_cutoff = cur.fetchone()[0]
            only_groups = _claim_dedup_dirty(conn, dirty_cutoff, limit=args.max_dirty)
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM dedup_dirty_properties")
                queue_depth = int(cur.fetchone()[0])
            LOG.info("DIRTY drain: %d claimed (cap=%d, queue depth=%d)",
                     len(only_groups), args.max_dirty, queue_depth)
            if not only_groups:
                LOG.info("DIRTY drain: queue empty; nothing to do")
                return 0
            # Scope the eligible LOAD to the claimed properties' street groups
            # (O(dirty), not O(market)) via the STORED street_name_key. The peers in
            # those groups are loaded too, so a dirty property still re-decides
            # against its existing group; only_groups (below) keeps the RESOLVE
            # filtered to dirty-containing groups, so the scoped load is a pure perf
            # optimization layered under that correctness gate.
            dirty_street_groups = _claimed_street_groups(conn, only_groups)
            LOG.info("DIRTY drain: scoped load to %d street-id + %d name groups",
                     len(dirty_street_groups[0]), len(dirty_street_groups[1]))

        classify_fn = None
        compare_fn = None
        site_plan_fn = None
        floor_plan_fn = None
        if args.free:
            # FREE mode: no PAID all-rooms classify/compare -> pHash / rule-B /
            # reconcile / reject-gate only. The ONE bounded paid exception is the
            # floor-plan validation gate: with a positive floor-plan budget it
            # gets the LIVE fn and pays its single Sonnet check inline for the small set
            # of would-merge both-plan pairs (auto-confirm / auto-dismiss, Option C),
            # capped by the budget (beyond it, pairs DEFER to the next run). Budget 0 =
            # the $0 cache-only fn: consume only batch-warmed verdicts, defer the rest.
            floor_plan_fn = (
                _build_floor_plan_fn(conn) if floor_plan_budget > 0
                else _build_cache_only_floor_plan_fn(conn)
            )
        elif auto_merge_enabled and args.cache_only:
            # Cost-efficient consume: read warm caches only, never call the LLM.
            classify_fn, compare_fn, site_plan_fn, floor_plan_fn = _build_cache_only_fns(
                conn, **ck)
        elif auto_merge_enabled and args.max_vision_calls > 0:
            classify_fn = _build_classify_fn(conn, **ck)
            compare_fn = _build_compare_fn(conn)
            site_plan_fn = _build_site_plan_fn(conn)
            floor_plan_fn = _build_floor_plan_fn(conn)
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

        # Cache-only calls are free, so don't let the vision budget / room cap
        # throttle consumption — try every warmed room of every warmed pair. In --free
        # mode the cap bounds the inline floor-plan checks (see _effective_vision_cap).
        eff_max_vision = _effective_vision_cap(
            free=args.free, cache_only=args.cache_only,
            floor_plan_budget=floor_plan_budget,
            max_vision_calls=args.max_vision_calls)
        eff_max_rooms = 99 if args.cache_only else args.max_room_attempts

        from datetime import datetime, timezone
        run_at = datetime.now(timezone.utc)
        # Candidate-priority drain: scope the scan to the properties in still-proposed
        # /dedup candidates so the queue self-clears in O(queue), not O(market). An
        # EMPTY set (no candidates) loads nothing — a clean no-op, NOT a full scan.
        restrict = (_proposed_candidate_property_ids(conn) if args.candidates else None)
        if args.candidates:
            LOG.info("CANDIDATE drain: %d properties across the proposed queue",
                     len(restrict or set()))

        # Shared kwargs every pass passes to run_engine — the free-first flow itself.
        engine_kw: dict[str, Any] = dict(
            classify_fn=classify_fn, compare_fn=compare_fn, site_plan_fn=site_plan_fn,
            floor_plan_fn=floor_plan_fn, cosine_fn=cosine_fn, bands=bands,
            model_for=model_for, render_min=clip["render_min"],
            inconclusive_to_review=inconclusive_to_review,
            max_vision_calls=eff_max_vision, max_room_attempts=eff_max_rooms,
            auto_merge_enabled=auto_merge_enabled, autodismiss=autodismiss,
            enqueue_unresolved=not args.free, dry_run=args.shadow, deadline=deadline,
            restrict_property_ids=restrict,
            clip_model=clip["clip_model"],
        )

        if not args.geo_only:
            pair_audit: list[dict[str, Any]] = []
            stats = run_engine(
                conn, audit=pair_audit, max_pairs=args.max_pairs,
                only_groups_with_property_ids=only_groups,
                restrict_street_groups=dirty_street_groups, **engine_kw,
            )
            stats["clip_classified"] = clip_counter[0]
            if args.dirty:
                # Record the queue depth at run start + this run's slice, so the /dedup +
                # Health dashboards can see whether the backlog is draining (a stall that
                # otherwise stays invisible — see migration 255).
                stats["dirty_queue_depth"] = queue_depth
                stats["dirty_claimed"] = len(only_groups)
            if not args.shadow:
                _write_run_row(conn, stats)
                _write_pair_audit(conn, run_at, pair_audit)
                # Clear the claim ONLY on a run that finished the scan. A truncated run
                # (deadline / pair-cap) didn't reach every dirty group, so keep the whole
                # claim — it re-drains next pass; a few already-resolved pairs re-run cheaply
                # (classify_pair rejects 'already_merged'), but NO unprocessed dirty property
                # is silently dropped. (Oversized groups are unresolvable everywhere, so a
                # finished run rightly clears them — the daily full scan handles them if they
                # shrink below MAX_GROUP_SIZE.)
                if args.dirty and not stats.get("truncated"):
                    cleared = _clear_dedup_dirty(conn, only_groups, dirty_cutoff)
                    LOG.info("DIRTY drain: cleared %d drained properties", cleared)
            LOG.info(
                "ENGINE %s eligible=%d auto_address=%d auto_phash=%d auto_visual=%d "
                "auto_dismissed=%d floor_plan_deferred=%d clip_deferred=%d reconciled=%d queued=%d "
                "skipped_unresolved=%d rejected=%d "
                "skipped_same_source=%d pairs=%d vision_calls=%d",
                "shadow" if args.shadow else "done",
                stats["eligible"], stats["auto_address"], stats["auto_phash"],
                stats["auto_visual"], stats["auto_dismissed"],
                stats.get("floor_plan_deferred", 0), stats.get("clip_deferred", 0),
                stats["reconciled"],
                stats["queued"], stats["skipped_unresolved"], stats["rejected"],
                stats.get("skipped_same_source", 0),
                stats["pairs_considered"], stats["vision_calls"],
            )

        if run_geo:
            # Same free-first flow over geo-keyed single-dwelling families; geo=True swaps
            # the loader + candidate filter (classify_geo_pair, ±area tolerance) + queue
            # tier. NO separate dedup_engine_runs row — the dashboard reads the latest single
            # row (ORDER BY started_at DESC LIMIT 1); a geo row (small geo eligible,
            # auto_address=0) would hide the street pass's headline. Geo decisions still land
            # in dedup_pair_audit (decision history) + the tier='geo' candidate queue.
            geo_audit: list[dict[str, Any]] = []
            geo_stats = run_engine(
                conn, audit=geo_audit, max_pairs=args.geo_max_pairs,
                geo=True, geo_area_max_pct=geo_area_max_pct, **engine_kw,
            )
            if not args.shadow:
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
