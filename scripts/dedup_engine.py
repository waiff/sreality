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
from typing import Any

from toolkit.dedup_engine import (
    PHASH_IDENTICAL_MAX,
    PHASH_MIN_IDENTICAL_PAIRS,
    CosineBands,
    ListingKey,
    _area_pct_diff,
    _haversine_m,
    _price_match,
    classify_geo_pair,
    classify_pair,
    decide_phash_fastpath,
    decide_visual_dismiss,
    geo_cell_key,
    profile_for,
    rooms_in_priority,
    route_by_cosine,
    street_group_keys,
    verdict_is_merge,
)
from toolkit.image_classification import SITE_PLAN_ROOM_TYPE
from toolkit.property_identity import MergeError, merge_properties

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
    ORDER BY l.obec_id NULLS LAST, l.street_id NULLS LAST, lower(l.street), l.disposition
"""


def _load_eligible(conn: Any) -> list[ListingKey]:
    """One ListingKey per (listing, grouping key): a row with both a canonical
    street_id and a street name is dual-keyed into its 'id:' and 'name:' groups
    so cross-portal rows keyed differently can still meet (run_engine dedups
    the listing pairs that surface in both groups)."""
    with conn.cursor() as cur:
        cur.execute(_ELIGIBLE_SQL)
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
            (lo, hi, tier, markers.get("confidence"), Jsonb(markers)),
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
    ORDER BY l.obec_id, l.category_main, l.category_type
"""


def _load_geo_eligible(conn: Any) -> list[ListingKey]:
    """One ListingKey per geo-eligible single-dwelling listing, keyed by its geo cell
    (so the existing _group_by_street groups them). Carries lat/lng/price for the geo
    classifier; disposition/floor/street_id are unused on this path."""
    with conn.cursor() as cur:
        cur.execute(_GEO_ELIGIBLE_SQL)
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


def _phash_identical_pairs(conn: Any, a_id: int, b_id: int) -> int:
    """Count near-identical image pairs (Hamming <= PHASH_IDENTICAL_MAX) across ALL
    stored images of the two listings — no classify needed, so this runs BEFORE the
    LLM stage. A development sharing one stock facade/plan yields 1 such pair; an
    actual re-post of the same listing shares many. The PHASH_MIN_IDENTICAL_PAIRS
    count threshold is what separates them (validated against the dismissed-pairs set),
    which is why this can drop the old interior-only gate (which needed the classifier).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM images ia JOIN images ib ON true "
            "WHERE ia.sreality_id = %s AND ib.sreality_id = %s "
            "AND ia.phash IS NOT NULL AND ib.phash IS NOT NULL "
            "AND bit_count((ia.phash # ib.phash)::bit(64)) <= %s",
            (a_id, b_id, PHASH_IDENTICAL_MAX),
        )
        return int(cur.fetchone()[0])


def _both_have_site_plan(conn: Any, a_id: int, b_id: int) -> bool:
    """True if BOTH listings already have a classified site/situation plan.

    The pHash fast-path runs before classify, so it would otherwise bypass the
    site-plan development guard (rule C, 'different_unit' -> queue). When both sides
    carry a site plan, defer the pair to the visual stage so that guard adjudicates
    rather than pHash auto-merging two units of one development.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE i.sreality_id = %(a)s) > 0 "
            "   AND count(*) FILTER (WHERE i.sreality_id = %(b)s) > 0 "
            "FROM images i JOIN image_room_classifications c ON c.image_id = i.id "
            "WHERE i.sreality_id IN (%(a)s, %(b)s) AND c.room_type = %(sp)s",
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
    vision_budget: list[int],
    max_room_attempts: int,
    autodismiss: bool = True,
    cosine_fn: Any = None,
    bands: Any = None,
    model_for: dict[str, str] | None = None,
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

    # Room-aware forensic comparison, priority order, stop at first High.
    rooms_a = {i["room_type"] for i in imgs_a}
    rooms_b = {i["room_type"] for i in imgs_b}
    common = rooms_a & rooms_b
    by_room_a = _group_ids_by_room(imgs_a)
    by_room_b = _group_ids_by_room(imgs_b)

    priority = rooms_in_priority(common)
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
            return {
                "action": "auto_merge", "reason": "visual_match",
                "room_type": room, "verdict": last_verdict, "rationale": last_rationale,
                "cosine": cos,
            }

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
            (r for r in rooms_in_priority(set(room_verdicts))
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


def run_engine(
    conn: Any,
    *,
    classify_fn: Any = None,
    compare_fn: Any = None,
    site_plan_fn: Any = None,
    cosine_fn: Any = None,
    bands: Any = None,
    model_for: dict[str, str] | None = None,
    audit: list[dict[str, Any]] | None = None,
    max_pairs: int = 2000,
    max_vision_calls: int = 200,
    max_room_attempts: int = 4,
    auto_merge_enabled: bool = True,
    autodismiss: bool = True,
    enqueue_unresolved: bool = True,
    dry_run: bool = False,
    deadline: float | None = None,
) -> dict[str, int]:
    """Run the full pipeline once. classify_fn/compare_fn are injectable for tests.

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
    stats = _eligibility_counts(conn)
    stats.update({
        "pairs_considered": 0, "rejected": 0,
        "auto_address": 0, "auto_phash": 0, "auto_visual": 0,
        "queued": 0, "vision_calls": 0, "skipped_same_source": 0,
        "auto_dismissed": 0, "reconciled": 0, "skipped_unresolved": 0,
        "clip_classified": 0, "clip_cosine_calls": 0,
        "routed_haiku": 0, "routed_sonnet": 0,
    })

    stats["reconciled"] = _reconcile_stale_candidates(conn, dry_run=dry_run)

    keys = _load_eligible(conn)
    groups = _group_by_street(keys)
    vision_budget = [max_vision_calls]
    seen_property_pairs: set[tuple[int, int]] = set()
    seen_listing_pairs: set[tuple[int, int]] = set()
    merged_pairs: set[tuple[int, int]] = set()
    dismissed_pairs: set[tuple[int, int]] = set()
    pairs_left = max_pairs

    def finalize() -> dict[str, int]:
        # Resolve every candidate the engine acted on this run (no-op for pairs
        # without a proposed row); a no-op set-based UPDATE when nothing collected.
        if not dry_run:
            _resolve_candidates(conn, merged_pairs, "merged")
            _resolve_candidates(conn, dismissed_pairs, "dismissed")
        return _finish(stats, vision_budget, max_vision_calls)

    for street_key, members in groups.items():
        if len(members) > MAX_GROUP_SIZE:
            LOG.info("SKIP large street group key=%s size=%d", street_key, len(members))
            continue
        for i in range(len(members)):
            if pairs_left <= 0:
                break
            for j in range(i + 1, len(members)):
                if pairs_left <= 0:
                    LOG.info("PAIR cap reached; deferring remainder to next run")
                    return finalize()
                # Wall-clock budget: the cold-cache classification flood (one
                # uncapped vision call per newly-eligible listing) can outrun the
                # job timeout, which SIGKILLs the run before it writes results.
                # Stop cleanly so the run row + the inline-committed merges persist
                # and the next run resumes with a warm cache (mirrors the detail
                # drain's --max-seconds).
                if deadline is not None and time.monotonic() >= deadline:
                    LOG.info(
                        "TIME budget reached; finalizing cleanly at pairs_considered=%d",
                        stats["pairs_considered"],
                    )
                    return finalize()
                a, b = members[i], members[j]
                # Dual-keyed listings appear in their 'id:' AND 'name:' groups;
                # classify each listing pair once (first group wins).
                lpair = (
                    min(a.sreality_id, b.sreality_id),
                    max(a.sreality_id, b.sreality_id),
                )
                if lpair in seen_listing_pairs:
                    continue
                seen_listing_pairs.add(lpair)
                decision = classify_pair(a, b)
                cp = _canon_pair(a, b)
                if decision.action == "reject":
                    stats["rejected"] += 1
                    # A pair the current rules reject is a deterministic non-match:
                    # dismiss any stale proposed candidate for it (recall-neutral).
                    if cp is not None:
                        dismissed_pairs.add(cp)
                    continue

                # Re-pointing happens at the property grain; skip a property pair
                # we already acted on this run (merges mutate property_id live).
                if cp is None:
                    continue
                if cp in seen_property_pairs:
                    continue
                seen_property_pairs.add(cp)

                if decision.action == "auto_merge":  # rule B exact address
                    factors = _factors(
                        "address", reason="address_exact", street_key=street_key,
                        house_number=a.house_number, floor=a.floor)
                    if auto_merge_enabled:
                        mg = None if dry_run else _merge_pair(
                            conn, a, b, "address_exact", {**factors, "confidence": 0.99})
                        if dry_run or mg:
                            stats["auto_address"] += 1
                            merged_pairs.add(cp)
                            _audit(audit, a, b, "address", "merged", factors,
                                   source="engine", merge_group_id=mg)
                    else:
                        if not dry_run:
                            _enqueue_candidate(
                                conn, a, b,
                                {**factors, "reason": "auto_merge_off:address_exact",
                                 "confidence": 0.99},
                            )
                        stats["queued"] += 1
                    continue

                # pHash fast-path (FREE, BEFORE classify, ALL sources). A strong raw
                # photo match (>= PHASH_MIN_IDENTICAL_PAIRS near-identical pairs over any
                # images) is a same-property signal that needs no LLM. The pair already
                # passed rule C (no house#/floor/area/unit-token contradiction), so a
                # match here auto-merges. Runs before the cross-source gate so identical-
                # photo re-posts (incl. same-source, which the gate would otherwise drop)
                # merge for free — and cross-posted cross-source pairs skip classify AND
                # compare. (Replaces the old interior-gated fast-path that needed classify.)
                phash_pairs = _phash_identical_pairs(conn, a.sreality_id, b.sreality_id)
                if decide_phash_fastpath(phash_pairs) and not _both_have_site_plan(
                    conn, a.sreality_id, b.sreality_id
                ):
                    factors = _factors("phash", reason="image_phash",
                                       street_key=street_key, phash_pairs=phash_pairs)
                    if auto_merge_enabled:
                        mg = None if dry_run else _merge_pair(
                            conn, a, b, "image_phash",
                            {**factors, "tier": "street_disposition", "confidence": 0.97})
                        if dry_run or mg:
                            stats["auto_phash"] += 1
                            merged_pairs.add(cp)
                            _audit(audit, a, b, "phash", "merged", factors,
                                   source="engine", merge_group_id=mg)
                    else:
                        if not dry_run:
                            _enqueue_candidate(conn, a, b, {
                                **factors, "tier": "street_disposition",
                                "reason": "auto_merge_off:image_phash", "confidence": 0.97})
                        stats["queued"] += 1
                    continue

                # Cross-source gate: the paid visual layer (classify + forensic compare)
                # exists to match one portal's listing against ANOTHER portal's — same-source
                # pairs buy nothing there (73/74 historical visual auto-merges were
                # cross-source). Rule B above already auto-merges exact same-source relists for
                # free; a same-source non-exact pair is skipped (no LLM, no queue), which cut
                # ~36% of candidate pairs off the visual stage at ~1.4% recall cost.
                if a.source == b.source:
                    stats["skipped_same_source"] += 1
                    # Same-source non-exact: the engine won't pursue this visually
                    # (cross-source gate). Dismiss any stale proposed candidate for
                    # it — recall-neutral per the gate's accepted tradeoff.
                    dismissed_pairs.add(cp)
                    continue

                # rule C candidate -> rule D visual
                pairs_left -= 1
                stats["pairs_considered"] += 1
                if not auto_merge_enabled:
                    # Auto-merge off: queue for manual review without spending vision.
                    # No forensic verdict here, but pHash already ran — carry it so the
                    # Needs-review card still shows the one similarity signal we have.
                    if not dry_run:
                        _enqueue_candidate(conn, a, b, {
                            **_factors("candidate", reason="auto_merge_off",
                                       street_key=street_key, phash_pairs=phash_pairs),
                            "tier": "street_disposition", "confidence": 0.6,
                        })
                    stats["queued"] += 1
                    continue
                outcome = _resolve_visual(
                    conn, a, b, classify_fn=classify_fn, compare_fn=compare_fn,
                    site_plan_fn=site_plan_fn,
                    vision_budget=vision_budget, max_room_attempts=max_room_attempts,
                    autodismiss=autodismiss,
                    cosine_fn=cosine_fn, bands=bands, model_for=model_for, stats=stats,
                )
                # ONE factor set per pair — fed to BOTH the terminal audit `detail`
                # (merged/dismissed) AND, when queued, the candidate `markers_matched`,
                # so Decision history and Needs-review show identical detail. phash_pairs
                # is carried even on the visual path (it's 0/1 here — the fast-path didn't
                # fire — which is itself the "photos differ, escalated to vision" signal).
                factors = _factors(
                    "visual", reason=outcome.get("reason"), street_key=street_key,
                    verdict=outcome.get("verdict"), room_type=outcome.get("room_type"),
                    rationale=outcome.get("rationale"), cosine=outcome.get("cosine"),
                    phash_pairs=phash_pairs)
                markers = {**factors, "tier": "street_disposition",
                           "confidence": 0.97 if outcome["action"] == "auto_merge" else 0.6}
                # pHash already ran (pre-classify); the visual stage only auto-merges via
                # a High forensic verdict, and auto-dismisses on a confident "different".
                if outcome["action"] == "auto_merge":
                    mg = None if dry_run else _merge_pair(
                        conn, a, b, outcome["reason"], markers)
                    if dry_run or mg:
                        stats["auto_visual"] += 1
                        merged_pairs.add(cp)
                        _audit(audit, a, b, "visual", "merged", factors,
                               source="engine", merge_group_id=mg)
                elif outcome["action"] == "dismiss":
                    stats["auto_dismissed"] += 1
                    dismissed_pairs.add(cp)
                    _audit(audit, a, b, "visual", "dismissed", factors, source="engine")
                elif enqueue_unresolved:
                    if not dry_run:
                        _enqueue_candidate(conn, a, b, markers)
                    stats["queued"] += 1
                    # NOT audited — a queued pair IS the candidate; its factor detail
                    # lives in markers_matched (Needs-review reads it). Auditing queued
                    # re-logged the same pair every run (the duplicate-row bug).
                else:
                    # Free mode: don't pile un-vision'd pairs into the review queue
                    # (they'd just be 'no photos compared' placeholders). pHash /
                    # rule-B / reconcile already ran; this pair is left for a future
                    # run (free pHash as coverage grows, or vision if re-enabled).
                    stats["skipped_unresolved"] += 1

    return finalize()


def _finish(stats: dict[str, int], vision_budget: list[int], max_vision_calls: int) -> dict[str, int]:
    stats["vision_calls"] = max_vision_calls - vision_budget[0]
    return stats


def run_geo_candidates(
    conn: Any,
    *,
    max_pairs: int = 20000,
    geo_auto_merge_enabled: bool = False,
    dry_run: bool = False,
    deadline: float | None = None,
) -> dict[str, int]:
    """Geo path: find duplicate single-dwelling properties (houses/land/commercial) the
    street+disposition engine structurally can't see. Blocks by geo cell, classifies
    each co-located pair deterministically (classify_geo_pair — no LLM), and QUEUES the
    matches into property_identity_candidates (tier 'geo_<family>').

    `geo_auto_merge_enabled` is the P2 switch: when False (P1) even a strong house signal
    is queued for the operator, never auto-merged — so this pass cannot false-merge. Only
    `dum` is ever eligible to auto-merge (its profile); land/commercial/other are always
    queue-only by profile.
    """
    keys = _load_geo_eligible(conn)
    groups = _group_by_street(keys)
    stats: dict[str, int] = {
        "geo_eligible": len({k.sreality_id for k in keys}), "geo_cells": len(groups),
        "geo_pairs": 0, "geo_candidates": 0, "geo_auto": 0, "geo_rejected": 0,
        "geo_skipped_large_cell": 0,
    }
    seen_property_pairs: set[tuple[int, int]] = set()
    seen_listing_pairs: set[tuple[int, int]] = set()
    pairs_left = max_pairs

    for cell, members in groups.items():
        if len(members) > MAX_GEO_GROUP_SIZE:
            stats["geo_skipped_large_cell"] += 1
            LOG.info("SKIP large geo cell key=%s size=%d", cell, len(members))
            continue
        for i in range(len(members)):
            if pairs_left <= 0:
                break
            for j in range(i + 1, len(members)):
                if pairs_left <= 0:
                    LOG.info("GEO pair cap reached; deferring remainder to next run")
                    return stats
                if deadline is not None and time.monotonic() >= deadline:
                    LOG.info("GEO time budget reached; finalizing at geo_pairs=%d", stats["geo_pairs"])
                    return stats
                a, b = members[i], members[j]
                lpair = (min(a.sreality_id, b.sreality_id), max(a.sreality_id, b.sreality_id))
                if lpair in seen_listing_pairs:
                    continue
                seen_listing_pairs.add(lpair)
                profile = profile_for(a.category_main)
                decision = classify_geo_pair(a, b, profile)
                if decision.action == "reject":
                    stats["geo_rejected"] += 1
                    continue
                cp = _canon_pair(a, b)
                if cp is None or cp in seen_property_pairs:
                    continue
                seen_property_pairs.add(cp)
                pairs_left -= 1
                stats["geo_pairs"] += 1
                dist = (
                    _haversine_m(a.lat, a.lng, b.lat, b.lng)
                    if None not in (a.lat, a.lng, b.lat, b.lng) else None
                )
                area_ratio = _area_pct_diff(a.area_m2, b.area_m2)
                house_no_match = (
                    a.house_number is not None and b.house_number is not None
                    and a.house_number.strip().lower() == b.house_number.strip().lower()
                )
                tier = f"geo_{profile.family}"
                markers = {
                    "tier": tier, "reason": decision.reason,
                    "coord_distance_m": round(dist, 1) if dist is not None else None,
                    "area_ratio": round(area_ratio, 4) if area_ratio is not None else None,
                    "price_match": _price_match(a.price_czk, b.price_czk),
                    "house_number_match": house_no_match,
                    "confidence": (
                        0.9 if decision.action == "auto_merge"
                        else (0.7 if decision.reason == "geo_strong" else 0.5)
                    ),
                }
                if decision.action == "auto_merge" and geo_auto_merge_enabled:
                    if dry_run or _merge_pair(conn, a, b, "geo_exact", markers):
                        stats["geo_auto"] += 1
                else:
                    if not dry_run:
                        _enqueue_candidate(conn, a, b, markers, tier=tier)
                    stats["geo_candidates"] += 1
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


def _build_cache_only_fns(
    conn: Any, *, prefer_clip: bool = False, clip_model: str | None = None,
    clip_counter: list[int] | None = None,
) -> tuple[Any, Any, Any]:
    """classify/compare/site-plan fns that ONLY READ the warm caches (the ones the
    batch lane filled at 50% off) and NEVER call the LLM — so the engine applies
    already-paid-for verdicts for $0. Un-warmed listings/rooms return None and the
    pair stays queued until the batch lane warms it. This is the cost-efficient
    consume half: the batch lane is the sole (discounted) payer. CLIP room tags
    (free) are preferred for the room grouping when present."""
    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider
    from toolkit.clip_dedup import clip_room_grouping
    from toolkit.image_classification import cached_classification
    from toolkit.visual_match import cached_site_plan_verdict, cached_visual_verdict

    llm = LLMClient(conn, providers={"anthropic": AnthropicProvider()})
    classify_model = llm.resolve_model("llm_room_classify_model")
    compare_model = llm.resolve_model("llm_visual_match_model")
    site_plan_model = llm.resolve_model("llm_site_plan_match_model")

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

    return classify_fn, compare_fn, site_plan_fn


def _visual_autodismiss_enabled(conn: Any) -> bool:
    """Operator toggle for auto-dismissing confident visual 'different' verdicts
    (app_settings.dedup_visual_autodismiss_enabled). Default on."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = 'dedup_visual_autodismiss_enabled'"
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
                queued, vision_calls, auto_dismissed,
                clip_classified, clip_cosine_calls, routed_haiku, routed_sonnet
            ) VALUES (now(), %(eligible)s, %(flagged_location)s, %(flagged_disposition)s,
                %(pairs_considered)s, %(rejected)s, %(auto_address)s, %(auto_phash)s,
                %(auto_visual)s, %(queued)s, %(vision_calls)s, %(auto_dismissed)s,
                %(clip_classified)s, %(clip_cosine_calls)s, %(routed_haiku)s,
                %(routed_sonnet)s)
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
                        help="FREE mode ($0, no LLM at all): pHash + exact-address merges + "
                             "reconcile + reject/gate dismissals only. Un-vision'd cross-source "
                             "pairs are skipped (NOT queued as placeholders), so the review queue "
                             "doesn't inflate. Captures every photo-sharing dup; different-photo "
                             "dups are left for a future run (more free pHash, or vision if enabled).")
    parser.add_argument("--geo", action="store_true",
                        help="ALSO run the geo candidate pass for single-dwelling families "
                             "(dum/pozemek/komercni/ostatni) the street+disposition engine "
                             "can't reach. QUEUE-ONLY in P1 — never auto-merges.")
    parser.add_argument("--geo-only", action="store_true",
                        help="Run ONLY the geo candidate pass (skip the street engine).")
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

        if not args.geo_only:
            auto_merge_enabled = _auto_merge_enabled(conn)
            autodismiss = _visual_autodismiss_enabled(conn) and not args.no_autodismiss
            clip = _clip_settings(conn)
            clip_counter = [0]
            # Free CLIP room tags (preferred when on); the counter tracks how many
            # listings were served from CLIP rather than the paid LLM classify.
            ck = {"prefer_clip": clip["prefer_clip"], "clip_model": clip["clip_model"],
                  "clip_counter": clip_counter}
            LOG.info(
                "ENGINE auto_merge_enabled=%s autodismiss=%s prefer_clip=%s cosine=%s "
                "shadow=%s cache_only=%s free=%s",
                auto_merge_enabled, autodismiss, clip["prefer_clip"],
                clip["cosine_enabled"], args.shadow, args.cache_only, args.free,
            )

            classify_fn = None
            compare_fn = None
            site_plan_fn = None
            if args.free:
                # FREE mode: no vision fns at all -> pHash / rule-B / reconcile /
                # reject-gate only ($0); un-vision'd pairs are skipped, not queued.
                pass
            elif auto_merge_enabled and args.cache_only:
                # Cost-efficient consume: read warm caches only, never call the LLM.
                classify_fn, compare_fn, site_plan_fn = _build_cache_only_fns(conn, **ck)
            elif auto_merge_enabled and args.max_vision_calls > 0:
                classify_fn = _build_classify_fn(conn, **ck)
                compare_fn = _build_compare_fn(conn)
                site_plan_fn = _build_site_plan_fn(conn)
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
            # throttle consumption — try every warmed room of every warmed pair.
            eff_max_vision = 10_000_000 if args.cache_only else args.max_vision_calls
            eff_max_rooms = 99 if args.cache_only else args.max_room_attempts

            from datetime import datetime, timezone
            run_at = datetime.now(timezone.utc)
            pair_audit: list[dict[str, Any]] = []
            stats = run_engine(
                conn, classify_fn=classify_fn, compare_fn=compare_fn,
                site_plan_fn=site_plan_fn,
                cosine_fn=cosine_fn, bands=bands, model_for=model_for,
                audit=pair_audit,
                max_pairs=args.max_pairs, max_vision_calls=eff_max_vision,
                max_room_attempts=eff_max_rooms,
                auto_merge_enabled=auto_merge_enabled,
                autodismiss=autodismiss,
                enqueue_unresolved=not args.free,
                dry_run=args.shadow,
                deadline=deadline,
            )
            stats["clip_classified"] = clip_counter[0]
            if not args.shadow:
                _write_run_row(conn, stats)
                _write_pair_audit(conn, run_at, pair_audit)
            LOG.info(
                "ENGINE %s eligible=%d auto_address=%d auto_phash=%d auto_visual=%d "
                "auto_dismissed=%d reconciled=%d queued=%d skipped_unresolved=%d rejected=%d "
                "skipped_same_source=%d pairs=%d vision_calls=%d",
                "shadow" if args.shadow else "done",
                stats["eligible"], stats["auto_address"], stats["auto_phash"],
                stats["auto_visual"], stats["auto_dismissed"], stats["reconciled"],
                stats["queued"], stats["skipped_unresolved"], stats["rejected"],
                stats.get("skipped_same_source", 0),
                stats["pairs_considered"], stats["vision_calls"],
            )

        if args.geo or args.geo_only:
            # P1: geo_auto_merge_enabled is hard-OFF — every family queues for the
            # operator; nothing auto-merges until the golden set calibrates it (P2).
            geo_stats = run_geo_candidates(
                conn, max_pairs=args.geo_max_pairs,
                geo_auto_merge_enabled=False, dry_run=args.shadow, deadline=deadline,
            )
            LOG.info(
                "GEO %s eligible=%d cells=%d pairs=%d candidates=%d auto=%d rejected=%d "
                "skipped_large_cell=%d",
                "shadow" if args.shadow else "done",
                geo_stats["geo_eligible"], geo_stats["geo_cells"], geo_stats["geo_pairs"],
                geo_stats["geo_candidates"], geo_stats["geo_auto"], geo_stats["geo_rejected"],
                geo_stats["geo_skipped_large_cell"],
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
