"""Phase A driver — runs `discover_condition_markers` over a stratified
sample of listings and writes the per-listing extractions into
`listing_marker_extractions`. Resumable: any (sreality_id, snapshot_id)
that already has a row is skipped, so the script can be re-run
incrementally after a network blip or a cost-cap stop.

Usage (typically via .github/workflows/discover_condition_markers.yml):
    python scripts/discover_condition_markers.py \
        --limit 2000 --n-images 5 --max-cost-usd 80

Stratification spreads the sample across:
  * category_main x category_type (six pairs)
  * `condition` text enum (~11 values incl. NULL)
  * district (locality_district_id)
  * within each cell, a price-quartile shuffle so a cell isn't
    dominated by one price band.

Six picks per stratification cell is the cap; total caps at --limit.

Required env vars: SUPABASE_DB_URL, ANTHROPIC_API_KEY, and the four
R2_* vars (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
R2_BUCKET_NAME) when --n-images > 0. Without R2 the script still
runs but every call gets n_images=0.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

LOG = logging.getLogger("discover_condition_markers")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=2000,
        help="Maximum number of listings to extract markers for (default 2000).",
    )
    parser.add_argument(
        "--n-images", type=int, default=5,
        help="Number of R2-stored images to include per listing (default 5; 0 disables).",
    )
    parser.add_argument(
        "--per-cell-cap", type=int, default=6,
        help="Maximum picks per (cat × cat_type × condition × district × price quartile) cell.",
    )
    parser.add_argument(
        "--max-cost-usd", type=float, default=80.0,
        help="Stop early when this run's accumulated llm_calls.cost_usd exceeds this cap.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the stratified sample but do not call the LLM.",
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

    import psycopg

    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.condition_markers import (
        DiscoveryError,
        discover_condition_markers,
    )

    started_at = time.monotonic()
    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        sample = _select_stratified_sample(
            conn,
            limit=args.limit,
            per_cell_cap=args.per_cell_cap,
        )
        LOG.info(
            "DISCOVERY sample selected total=%d limit=%d per_cell_cap=%d",
            len(sample), args.limit, args.per_cell_cap,
        )

        if args.dry_run:
            for sid in sample[:20]:
                LOG.info("DISCOVERY sample[head] sreality_id=%d", sid)
            return 0

        pending = _filter_already_extracted(conn, sample)
        LOG.info(
            "DISCOVERY pending=%d already_done=%d",
            len(pending), len(sample) - len(pending),
        )

        providers = get_providers()
        llm_client = LLMClient(conn, providers=providers)

        scored = 0
        errors = 0
        cost_so_far = 0.0
        for i, sid in enumerate(pending, start=1):
            if cost_so_far >= args.max_cost_usd:
                LOG.warning(
                    "DISCOVERY cost cap reached cost=$%.4f cap=$%.2f stopping early",
                    cost_so_far, args.max_cost_usd,
                )
                break
            try:
                result = discover_condition_markers(
                    conn, llm_client,
                    sreality_id=sid,
                    n_images=args.n_images,
                )
            except DiscoveryError as exc:
                errors += 1
                LOG.warning(
                    "DISCOVERY id=%d skipped error=%s", sid, exc,
                )
                continue
            except Exception as exc:
                errors += 1
                LOG.exception("DISCOVERY id=%d crashed: %s", sid, exc)
                continue

            scored += 1
            cost = result["data"].get("cost_usd") or 0.0
            if not result["data"].get("cache_hit"):
                cost_so_far += float(cost)
            if i % 25 == 0 or i == len(pending):
                LOG.info(
                    "DISCOVERY progress=%d/%d scored=%d errors=%d cost_so_far=$%.4f",
                    i, len(pending), scored, errors, cost_so_far,
                )

    elapsed = time.monotonic() - started_at
    LOG.info(
        "DISCOVERY done scored=%d errors=%d cost=$%.4f elapsed=%.1fs",
        scored, errors, cost_so_far, elapsed,
    )
    return 0


def _select_stratified_sample(
    conn: Any, *, limit: int, per_cell_cap: int,
) -> list[int]:
    """Pick sreality_ids spread across cat × cat_type × condition × district × price-quartile.

    NTILE(4) over price_czk inside (category_main, category_type) gives
    even quartile bands per category pair, so the cell coordinates are
    naturally aligned to "how much one usually pays for that kind of
    listing". md5(sreality_id::text) is a deterministic shuffle inside
    the cell — same script run twice picks the same listings.
    """
    sql = (
        "WITH ranked AS ( "
        "  SELECT l.sreality_id, "
        "         row_number() OVER ( "
        "           PARTITION BY l.category_main, l.category_type, "
        "                        l.condition, "
        "                        coalesce(l.locality_district_id, -1), "
        "                        NTILE(4) OVER ( "
        "                          PARTITION BY l.category_main, l.category_type "
        "                          ORDER BY l.price_czk "
        "                        ) "
        "           ORDER BY md5(l.sreality_id::text) "
        "         ) AS rn "
        "  FROM listings l "
        "  WHERE l.is_active = true "
        "    AND l.last_seen_at > now() - interval '60 days' "
        "    AND l.price_czk IS NOT NULL "
        "    AND EXISTS (SELECT 1 FROM listing_snapshots s "
        "                WHERE s.sreality_id = l.sreality_id) "
        ") "
        "SELECT sreality_id FROM ranked "
        "WHERE rn <= %s "
        "ORDER BY md5(sreality_id::text) "
        "LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (per_cell_cap, limit))
        return [int(r[0]) for r in cur.fetchall()]


def _filter_already_extracted(conn: Any, sample: list[int]) -> list[int]:
    """Skip listings whose LATEST snapshot already has a marker extraction row."""
    if not sample:
        return []
    sql = (
        "WITH latest AS ( "
        "  SELECT sreality_id, MAX(id) AS snapshot_id "
        "  FROM listing_snapshots "
        "  WHERE sreality_id = ANY(%s) "
        "  GROUP BY sreality_id "
        ") "
        "SELECT l.sreality_id FROM latest l "
        "LEFT JOIN listing_marker_extractions me "
        "  ON me.sreality_id = l.sreality_id "
        " AND me.snapshot_id = l.snapshot_id "
        "WHERE me.id IS NULL"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sample,))
        pending_set = {int(r[0]) for r in cur.fetchall()}
    return [sid for sid in sample if sid in pending_set]


if __name__ == "__main__":
    sys.exit(main())
