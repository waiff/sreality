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
     (5% area guard); rule C contradictions are rejected; the rest are visual
     candidates.
  2. For each candidate pair, the layered visual confirmation (rule D):
       a. pHash fast-path — >=2 near-identical INTERIOR image pairs -> merge,
          no LLM (interior gate needs room classification, so it shares the
          classify step).
       b. room-aware forensic comparison in priority order, stop at first High.
     A High verdict (or the pHash fast-path) merges; everything else queues
     (rule E).

Bounded + cached: per-run caps on candidate pairs visually examined, room
attempts per pair, and total vision calls; classification + comparison are
cached so re-runs are nearly free. Writes one `dedup_engine_runs` row.

Runnable as `python -m scripts.dedup_engine`. Required env: SUPABASE_DB_URL
(+ ANTHROPIC_API_KEY / R2_* for the visual layer; absent these the engine still
does rule-A/B/C work and the pHash fast-path degrades to a logged skip).
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
    ListingKey,
    classify_pair,
    decide_phash_fastpath,
    rooms_in_priority,
    street_group_keys,
    verdict_is_merge,
)
from toolkit.image_classification import INTERIOR_ROOM_TYPES, SITE_PLAN_ROOM_TYPE
from toolkit.property_identity import MergeError, merge_properties

LOG = logging.getLogger("dedup_engine")

# A street group bigger than this is almost certainly a whole street/development
# rather than one building's units; the O(n^2) pairing would explode and the
# matches would be low-value. Skip + log (no silent truncation).
MAX_GROUP_SIZE = 40


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


def _merge_pair(conn: Any, a: ListingKey, b: ListingKey, reason: str, markers: dict[str, Any]) -> bool:
    """Merge the two listings' properties (older survives). Returns True on success.

    The in-memory ListingKeys hold the property_id as loaded at run start; a
    merge earlier this run may have retired one of them. merge_properties raises
    MergeError on a non-active survivor/retired, which we catch and skip — the
    daily re-run sees the settled state and completes the chain. The job is
    idempotent and converges over runs, so a deferred chain merge is harmless.
    """
    if a.property_id is None or b.property_id is None or a.property_id == b.property_id:
        return False
    # Survivor = the older property (smaller id is a stable proxy for first_seen).
    survivor, retired = sorted((a.property_id, b.property_id))
    try:
        merge_properties(
            conn, survivor_id=survivor, retired_id=retired,
            reason=reason, source="auto", confidence=markers.get("confidence"),
            markers=markers,
        )
        return True
    except MergeError as exc:
        LOG.warning("merge %s<-%s skipped: %s", survivor, retired, exc)
        return False


def _enqueue_candidate(conn: Any, a: ListingKey, b: ListingKey, markers: dict[str, Any]) -> None:
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
            (lo, hi, "street_disposition", markers.get("confidence"), Jsonb(markers)),
        )


def _phash_interior_identical_pairs(
    conn: Any, a_id: int, b_id: int, interior_image_ids_a: list[int], interior_image_ids_b: list[int],
) -> int:
    """Count INTERIOR image pairs within the identical-Hamming threshold.

    Restricted to the classifier-selected interior image ids of each listing, so
    a shared facade render / floor plan can't trigger the fast-path.
    """
    if not interior_image_ids_a or not interior_image_ids_b:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM images ia JOIN images ib ON true "
            "WHERE ia.id = ANY(%s) AND ib.id = ANY(%s) "
            "AND ia.phash IS NOT NULL AND ib.phash IS NOT NULL "
            "AND bit_count((ia.phash # ib.phash)::bit(64)) <= %s",
            (interior_image_ids_a, interior_image_ids_b, PHASH_IDENTICAL_MAX),
        )
        return int(cur.fetchone()[0])


def _classify_or_none(classify_fn: Any, sreality_id: int) -> list[dict[str, Any]] | None:
    if classify_fn is None:
        return None
    try:
        res = classify_fn(sreality_id)
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
) -> dict[str, Any]:
    """Rule D for one candidate pair. Returns a dict describing the outcome.

    {action: 'auto_merge'|'queue', reason, room_type?, verdict?, rationale?,
     phash_pairs}. Mutates vision_budget[0] as forensic calls are spent.
    """
    imgs_a = _classify_or_none(classify_fn, a.sreality_id)
    imgs_b = _classify_or_none(classify_fn, b.sreality_id)
    if imgs_a is None or imgs_b is None:
        return {"action": "queue", "reason": "no_images", "phash_pairs": 0}

    # Development guard (runs FIRST): if both listings carry a site/situation
    # plan, check whether they highlight the same unit. A 'different_unit'
    # verdict is the strongest "same project, distinct property" signal — QUEUE
    # for the operator, never auto-merge. (Never auto-rejects: same_unit /
    # inconclusive fall through to the normal confirmation below.)
    site_a = [i["image_id"] for i in imgs_a if i["room_type"] == SITE_PLAN_ROOM_TYPE]
    site_b = [i["image_id"] for i in imgs_b if i["room_type"] == SITE_PLAN_ROOM_TYPE]
    if site_a and site_b and site_plan_fn is not None and vision_budget[0] > 0:
        vision_budget[0] -= 1
        sp = site_plan_fn(a.sreality_id, b.sreality_id, site_a, site_b)
        if sp is not None and sp.get("verdict") == "different_unit":
            return {
                "action": "queue", "reason": "site_plan_different_unit",
                "verdict": sp["verdict"], "rationale": sp.get("rationale"),
                "phash_pairs": 0,
            }

    # Layer 1: interior pHash fast-path.
    interior_a = [i["image_id"] for i in imgs_a if i["room_type"] in INTERIOR_ROOM_TYPES]
    interior_b = [i["image_id"] for i in imgs_b if i["room_type"] in INTERIOR_ROOM_TYPES]
    phash_pairs = _phash_interior_identical_pairs(
        conn, a.sreality_id, b.sreality_id, interior_a, interior_b,
    )
    if decide_phash_fastpath(phash_pairs):
        return {"action": "auto_merge", "reason": "image_phash", "phash_pairs": phash_pairs}

    if compare_fn is None:
        return {"action": "queue", "reason": "vision_unavailable", "phash_pairs": phash_pairs}

    # Layer 3: room-aware forensic comparison, priority order, stop at first High.
    rooms_a = {i["room_type"] for i in imgs_a}
    rooms_b = {i["room_type"] for i in imgs_b}
    common = rooms_a & rooms_b
    by_room_a = _group_ids_by_room(imgs_a)
    by_room_b = _group_ids_by_room(imgs_b)

    tried = 0
    last_verdict = None
    last_rationale = None
    for room in rooms_in_priority(common):
        if tried >= max_room_attempts or vision_budget[0] <= 0:
            break
        tried += 1
        vision_budget[0] -= 1
        verdict_obj = compare_fn(a.sreality_id, b.sreality_id, room, by_room_a[room], by_room_b[room])
        if verdict_obj is None:
            continue
        last_verdict, last_rationale = verdict_obj["verdict"], verdict_obj.get("rationale")
        if verdict_is_merge(last_verdict):
            return {
                "action": "auto_merge", "reason": "visual_match",
                "room_type": room, "verdict": last_verdict, "rationale": last_rationale,
                "phash_pairs": phash_pairs,
            }

    return {
        "action": "queue", "reason": "visual_inconclusive",
        "verdict": last_verdict, "rationale": last_rationale, "phash_pairs": phash_pairs,
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


def run_engine(
    conn: Any,
    *,
    classify_fn: Any = None,
    compare_fn: Any = None,
    site_plan_fn: Any = None,
    max_pairs: int = 2000,
    max_vision_calls: int = 200,
    max_room_attempts: int = 4,
    auto_merge_enabled: bool = True,
    deadline: float | None = None,
) -> dict[str, int]:
    """Run the full pipeline once. classify_fn/compare_fn are injectable for tests.

    classify_fn(sreality_id) -> classify_listing_images envelope.
    compare_fn(a, b, room_type, ids_a, ids_b) -> {verdict, rationale} | None.
    site_plan_fn(a, b, ids_a, ids_b) -> {verdict, rationale} | None (development
    guard: verdict ∈ same_unit|different_unit|inconclusive).

    When auto_merge_enabled is False (the operator's /dedup toggle), the engine
    still finds candidates but queues every one for manual review instead of
    auto-merging — and skips the forensic vision step (no LLM spend).
    """
    stats = _eligibility_counts(conn)
    stats.update({
        "pairs_considered": 0, "rejected": 0,
        "auto_address": 0, "auto_phash": 0, "auto_visual": 0,
        "queued": 0, "vision_calls": 0, "skipped_same_source": 0,
    })

    keys = _load_eligible(conn)
    groups = _group_by_street(keys)
    vision_budget = [max_vision_calls]
    seen_property_pairs: set[tuple[int, int]] = set()
    seen_listing_pairs: set[tuple[int, int]] = set()
    pairs_left = max_pairs

    for street_key, members in groups.items():
        if len(members) > MAX_GROUP_SIZE:
            LOG.info("SKIP large street group key=%s size=%d", street_key, len(members))
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if pairs_left <= 0:
                    LOG.info("PAIR cap reached; deferring remainder to next run")
                    return _finish(stats, vision_budget, max_vision_calls)
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
                    return _finish(stats, vision_budget, max_vision_calls)
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
                if decision.action == "reject":
                    stats["rejected"] += 1
                    continue

                # Re-pointing happens at the property grain; skip a property pair
                # we already acted on this run (merges mutate property_id live).
                if a.property_id is None or b.property_id is None:
                    continue
                ppair = tuple(sorted((a.property_id, b.property_id)))
                if ppair in seen_property_pairs:
                    continue
                seen_property_pairs.add(ppair)

                if decision.action == "auto_merge":  # rule B exact address
                    markers = {"reason": decision.reason, "confidence": 0.99,
                               "street_key": street_key, "house_number": a.house_number,
                               "floor": a.floor}
                    if auto_merge_enabled:
                        if _merge_pair(conn, a, b, "address_exact", markers):
                            stats["auto_address"] += 1
                    else:
                        _enqueue_candidate(
                            conn, a, b,
                            {**markers, "reason": "auto_merge_off:address_exact"},
                        )
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
                    continue

                # rule C candidate -> rule D visual
                pairs_left -= 1
                stats["pairs_considered"] += 1
                if not auto_merge_enabled:
                    # Auto-merge off: queue for manual review without spending vision.
                    _enqueue_candidate(conn, a, b, {
                        "tier": "street_disposition", "street_key": street_key,
                        "reason": "auto_merge_off", "confidence": 0.6,
                    })
                    stats["queued"] += 1
                    continue
                outcome = _resolve_visual(
                    conn, a, b, classify_fn=classify_fn, compare_fn=compare_fn,
                    site_plan_fn=site_plan_fn,
                    vision_budget=vision_budget, max_room_attempts=max_room_attempts,
                )
                markers = {
                    "tier": "street_disposition", "street_key": street_key,
                    "reason": outcome.get("reason"), "phash_pairs": outcome.get("phash_pairs", 0),
                    "verdict": outcome.get("verdict"), "rationale": outcome.get("rationale"),
                    "room_type": outcome.get("room_type"),
                    "confidence": 0.97 if outcome["action"] == "auto_merge" else 0.6,
                }
                if outcome["action"] == "auto_merge":
                    merged = _merge_pair(conn, a, b, outcome["reason"], markers)
                    if merged and outcome["reason"] == "image_phash":
                        stats["auto_phash"] += 1
                    elif merged:
                        stats["auto_visual"] += 1
                else:
                    _enqueue_candidate(conn, a, b, markers)
                    stats["queued"] += 1

    return _finish(stats, vision_budget, max_vision_calls)


def _finish(stats: dict[str, int], vision_budget: list[int], max_vision_calls: int) -> dict[str, int]:
    stats["vision_calls"] = max_vision_calls - vision_budget[0]
    return stats


def _build_classify_fn(conn: Any) -> Any:
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.image_classification import classify_listing_images
    llm = LLMClient(conn, providers=get_providers())

    def _fn(sreality_id: int) -> dict[str, Any]:
        return classify_listing_images(conn, llm, sreality_id=sreality_id)
    return _fn


def _build_compare_fn(conn: Any) -> Any:
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.visual_match import compare_listings_visually
    llm = LLMClient(conn, providers=get_providers())

    def _fn(a: int, b: int, room_type: str, ids_a: list[int], ids_b: list[int]) -> dict[str, Any] | None:
        try:
            res = compare_listings_visually(
                conn, llm, sreality_id_a=a, sreality_id_b=b,
                room_type=room_type, image_ids_a=ids_a, image_ids_b=ids_b,
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


def _write_run_row(conn: Any, stats: dict[str, int]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dedup_engine_runs (
                ended_at, eligible, flagged_location, flagged_disposition,
                pairs_considered, rejected, auto_address, auto_phash, auto_visual,
                queued, vision_calls
            ) VALUES (now(), %(eligible)s, %(flagged_location)s, %(flagged_disposition)s,
                %(pairs_considered)s, %(rejected)s, %(auto_address)s, %(auto_phash)s,
                %(auto_visual)s, %(queued)s, %(vision_calls)s)
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
                        help="Report eligible counts + street groups and exit without writing.")
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

        auto_merge_enabled = _auto_merge_enabled(conn)
        LOG.info("ENGINE auto_merge_enabled=%s", auto_merge_enabled)

        classify_fn = None
        compare_fn = None
        site_plan_fn = None
        if auto_merge_enabled and args.max_vision_calls > 0:
            classify_fn = _build_classify_fn(conn)
            compare_fn = _build_compare_fn(conn)
            site_plan_fn = _build_site_plan_fn(conn)
        elif auto_merge_enabled:
            # pHash fast-path still needs room labels to gate on interior shots.
            classify_fn = _build_classify_fn(conn)
        # When auto-merge is off the engine never reaches the visual step, so we
        # skip building the (LLM-backed) classify/compare fns entirely.

        deadline = time.monotonic() + args.max_seconds if args.max_seconds > 0 else None
        stats = run_engine(
            conn, classify_fn=classify_fn, compare_fn=compare_fn,
            site_plan_fn=site_plan_fn,
            max_pairs=args.max_pairs, max_vision_calls=args.max_vision_calls,
            max_room_attempts=args.max_room_attempts,
            auto_merge_enabled=auto_merge_enabled,
            deadline=deadline,
        )
        _write_run_row(conn, stats)

    LOG.info(
        "ENGINE done eligible=%d auto_address=%d auto_phash=%d auto_visual=%d "
        "queued=%d rejected=%d skipped_same_source=%d pairs=%d vision_calls=%d",
        stats["eligible"], stats["auto_address"], stats["auto_phash"],
        stats["auto_visual"], stats["queued"], stats["rejected"],
        stats.get("skipped_same_source", 0),
        stats["pairs_considered"], stats["vision_calls"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
