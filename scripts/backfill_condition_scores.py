"""Phase B2 driver — backfill condition scores for active listings.

Walks active listings whose latest snapshot doesn't yet have a row in
`listing_condition_scores`, scores each one via
`toolkit.condition_scoring.score_listing_condition`, and persists the
cache row plus the two `listings.*_condition_level` columns in one
transaction.

Resumable: scored rows drop out of the next selection (the WHERE
clause excludes any sreality_id whose latest snapshot already has a
score). Cost-capped via `--max-cost-usd` — when the per-run accumulated
LLM spend crosses the cap, the loop exits cleanly with the in-flight
listings preserved.

Region scope: scoring targets the operator-enabled kraje stored in
`app_settings.condition_scoring_enabled_region_ids` (admin_boundaries
kraj ids, edited from the Settings page). `--region-ids` overrides that
list for a one-off run; listings outside the effective list — or with
`region_id` NULL — are parked, not selected. An empty effective list
pauses scoring.

Usage (typically via .github/workflows/backfill_condition_scores.yml):

    python -m scripts.backfill_condition_scores \\
        --region-ids 27,43,108 \\
        --limit 500 \\
        --max-cost-usd 10

Required env vars: SUPABASE_DB_URL, ANTHROPIC_API_KEY. R2_* are only
needed when `--n-images > 0`; without them the scorer silently
degrades to text-only.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

LOG = logging.getLogger("backfill_condition_scores")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region-ids", default="",
        help=(
            "Comma-separated list of admin_boundaries kraj ids to "
            "include. Empty = the Settings-page enabled list "
            "(app_settings.condition_scoring_enabled_region_ids)."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=500,
        help="Maximum number of listings to score in this run (default 500).",
    )
    parser.add_argument(
        "--n-images", type=int, default=0,
        help="Number of R2-stored images to include per listing (default 0).",
    )
    parser.add_argument(
        "--max-cost-usd", type=float, default=10.0,
        help="Stop early when this run's accumulated LLM cost exceeds this cap.",
    )
    parser.add_argument(
        "--max-age-days", type=int, default=30,
        help=(
            "Only score listings whose last_seen_at is within this many "
            "days (default 30). Set to 0 to disable the freshness "
            "filter entirely — useful for one-off backfills reaching "
            "older listings."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the pending listings (first 20) and exit without calling the LLM.",
    )
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
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2

    region_ids = _parse_region_ids(args.region_ids)
    LOG.info(
        "SCORE config region_ids=%s limit=%d n_images=%d "
        "max_cost_usd=%.2f max_age_days=%d dry_run=%s",
        region_ids or "from-settings", args.limit, args.n_images,
        args.max_cost_usd, args.max_age_days, args.dry_run,
    )

    import psycopg

    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider
    from toolkit.condition_scoring import (
        ScoringError,
        propagate_condition_levels,
        score_listing_condition,
    )

    started_at = time.monotonic()
    with psycopg.connect(
        db_url, autocommit=True, prepare_threshold=None,
    ) as conn:
        if not args.dry_run:
            # Copy already-paid scores to cross-portal siblings first, so the
            # selection below never re-bills a property a sibling already covers.
            reused = propagate_condition_levels(conn)
            LOG.info("PROPAGATE reused=%d", reused)

        pending = _select_pending(
            conn,
            region_ids=region_ids,
            max_age_days=args.max_age_days,
            limit=args.limit,
        )
        LOG.info("SCORE pending=%d cap=%d", len(pending), args.limit)

        if args.dry_run:
            for sid in pending[:20]:
                LOG.info("SCORE sample sreality_id=%d", sid)
            LOG.info("SCORE dry-run; exit")
            return 0

        if not pending:
            LOG.info("SCORE nothing to backfill; done")
            return 0

        providers = {"anthropic": AnthropicProvider()}
        llm_client = LLMClient(conn, providers=providers)

        scored = 0
        errors = 0
        cost_so_far = 0.0
        for i, sid in enumerate(pending, start=1):
            if cost_so_far >= args.max_cost_usd:
                LOG.warning(
                    "SCORE cost cap reached cost=$%.4f cap=$%.2f stopping early",
                    cost_so_far, args.max_cost_usd,
                )
                break
            try:
                result = score_listing_condition(
                    conn, llm_client,
                    sreality_id=sid,
                    n_images=args.n_images,
                )
            except ScoringError as exc:
                errors += 1
                LOG.warning("SCORE id=%d skipped error=%s", sid, exc)
                continue
            except Exception as exc:
                errors += 1
                LOG.exception("SCORE id=%d crashed: %s", sid, exc)
                continue

            scored += 1
            cost = result["data"].get("cost_usd") or 0.0
            if not result["data"].get("cache_hit"):
                cost_so_far += float(cost)
            if i % 25 == 0 or i == len(pending):
                LOG.info(
                    "SCORE progress=%d/%d scored=%d errors=%d cost_so_far=$%.4f",
                    i, len(pending), scored, errors, cost_so_far,
                )

    elapsed = time.monotonic() - started_at
    LOG.info(
        "SCORE done scored=%d errors=%d cost=$%.4f elapsed=%.1fs",
        scored, errors, cost_so_far, elapsed,
    )
    return 0


def _parse_region_ids(raw: str) -> list[int]:
    """Parse a comma-separated string of region IDs into a list of ints.

    Empty / whitespace-only input returns []. Invalid entries cause a
    hard exit with a clear message — better than silently dropping
    a typo'd region.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            print(
                f"ERROR: --region-ids contains non-integer entry: {part!r}",
                file=sys.stderr,
            )
            sys.exit(2)
    return out


_ENABLED_REGIONS_KEY = "condition_scoring_enabled_region_ids"


def _enabled_region_ids(conn: Any) -> list[int]:
    """Operator-enabled admin_boundaries kraj ids from app_settings.

    Missing row or empty array -> [] (scoring paused).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s",
            (_ENABLED_REGIONS_KEY,),
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        return []
    try:
        return [int(v) for v in row[0]]
    except (TypeError, ValueError):
        # A malformed value (e.g. hand-edited via the generic Settings JSON
        # editor) pauses scoring instead of crashing the scheduled job.
        LOG.warning("invalid %s value %r — treating as paused", _ENABLED_REGIONS_KEY, row[0])
        return []


def _select_pending(
    conn: Any,
    *,
    region_ids: list[int],
    max_age_days: int,
    limit: int,
) -> list[int]:
    """Active listings whose latest snapshot doesn't yet have a score row.

    The `latest_snapshot` CTE materialises (sreality_id, max_snapshot_id)
    once, then the LEFT JOIN on listing_condition_scores filters out
    listings whose latest snapshot already has a cache row. Ordering
    by last_seen_at DESC scores the freshest listings first — most
    user-facing relevance per dollar spent.

    Region scope: `region_ids` (the CLI override) wins when non-empty;
    otherwise the Settings-page enabled list
    (app_settings.condition_scoring_enabled_region_ids). The clause gates
    on `l.region_id` — the geo-derived admin_boundaries kraj id, present
    for every portal — with NO null passthrough: a listing outside the
    enabled kraje, or without a region yet, is parked, not scored. An
    empty effective list pauses scoring entirely.

    Sibling reuse: a listing is skipped while a same-property sibling
    already holds a GENUINE score (condition_levels_propagated_from IS
    NULL) — propagate_condition_levels copies that score over instead of
    paying the LLM twice. The latest-snapshot/cache-row predicate is
    per-listing, so the scored sibling itself still re-scores when its
    own snapshot changes.

    `max_age_days <= 0` drops the freshness clause entirely — score
    every active listing regardless of when it was last seen. The
    `is_active = true` guard still applies. Useful for one-off
    backfills that want to reach older listings the scraper hasn't
    walked recently.
    """
    effective_region_ids = region_ids or _enabled_region_ids(conn)
    if not effective_region_ids:
        LOG.info("condition scoring paused: no enabled regions")
        return []

    freshness_clause = (
        " AND l.last_seen_at > now() - %s::interval"
        if max_age_days > 0 else ""
    )
    sql = (
        "WITH latest_snapshot AS ( "
        "  SELECT sreality_id, MAX(id) AS snapshot_id "
        "  FROM listing_snapshots GROUP BY sreality_id "
        ") "
        "SELECT l.sreality_id "
        "FROM listings l "
        "JOIN latest_snapshot ls ON ls.sreality_id = l.sreality_id "
        "LEFT JOIN listing_condition_scores cs "
        "  ON cs.sreality_id = ls.sreality_id "
        " AND cs.snapshot_id = ls.snapshot_id "
        "WHERE l.is_active = true "
        + freshness_clause +
        "  AND cs.id IS NULL "
        "  AND l.region_id = ANY(%s::bigint[]) "
        "  AND (l.property_id IS NULL OR NOT EXISTS ( "
        "    SELECT 1 FROM listings sib "
        "    WHERE sib.property_id = l.property_id "
        "      AND sib.sreality_id <> l.sreality_id "
        "      AND sib.condition_levels_propagated_from IS NULL "
        "      AND (sib.building_condition_level IS NOT NULL "
        "           OR sib.apartment_condition_level IS NOT NULL) "
        "  )) "
        "ORDER BY l.last_seen_at DESC "
        "LIMIT %s"
    )
    params: tuple[Any, ...]
    if max_age_days > 0:
        params = (f"{max_age_days} days", effective_region_ids, limit)
    else:
        params = (effective_region_ids, limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [int(r[0]) for r in cur.fetchall()]


if __name__ == "__main__":
    sys.exit(main())
