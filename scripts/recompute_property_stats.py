"""Slice 1 driver -- recompute the canonical `properties` rollup + stats.

Two phases, both idempotent:

1. Attach stragglers. Any `listings` row with `property_id IS NULL` (an
   old-code insert on `main` that ran before the property-linking wrapper
   went live) gets its own singleton property, mirroring migration 092.
   Self-heals the transient gap the foundation migration documented.

2. Recompute every property from its children. Per property:
     is_active           = bool_or(children.is_active)   (decision #3 rollup)
     source_count        = count(children)
     distinct_site_count = count(distinct children.source)
     first/last_seen_at  = min/max across children
     repr_listing_id     = the active, most-recently-seen child
     category/area/...    + current_price_czk mirror that representative child
     price_drop_count     \\
     price_rise_count      } from the union of all children's snapshots,
     max_price_drop_pct   /  ordered by scraped_at (consecutive-step deltas)
     stats_computed_at   = now()

   For today's singleton properties this reproduces exactly what the
   insert-time wrapper (`scraper.db._ensure_singleton_property`) maintains,
   plus the price-history aggregates the wrapper does not compute.

Batched by property-id range so each statement stays well under the
transaction-pooler statement timeout. autocommit=True means each batch
commits independently -- a workflow timeout preserves completed batches.

Usage (typically via .github/workflows/recompute_property_stats.yml):

    python -m scripts.recompute_property_stats --batch-size 2000

Required env var: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Iterator
from typing import Any

LOG = logging.getLogger("recompute_property_stats")


_ATTACH_BACKFILL_NATIVE_ID_SQL = """
    UPDATE listings SET source_id_native = sreality_id::text
    WHERE source_id_native IS NULL
"""

_ATTACH_INSERT_SQL = """
    INSERT INTO properties (
        repr_listing_id, category_main, category_type, disposition,
        area_m2, district, geom, current_price_czk,
        is_active, first_seen_at, last_seen_at,
        source_count, distinct_site_count
    )
    SELECT
        l.sreality_id, l.category_main, l.category_type, l.disposition,
        l.area_m2, l.district, l.geom, l.price_czk,
        l.is_active, l.first_seen_at, l.last_seen_at, 1, 1
    FROM listings l
    WHERE l.property_id IS NULL
"""

_ATTACH_LINK_SQL = """
    UPDATE listings l
    SET property_id = p.id
    FROM properties p
    WHERE p.repr_listing_id = l.sreality_id
      AND l.property_id IS NULL
"""

_RECOMPUTE_BATCH_SQL = """
    WITH batch AS (
      SELECT id FROM properties WHERE id >= %(lo)s AND id < %(hi)s
    ),
    child_agg AS (
      SELECT
        l.property_id              AS pid,
        bool_or(l.is_active)       AS is_active,
        count(*)                   AS source_count,
        count(distinct l.source)   AS distinct_site_count,
        min(l.first_seen_at)       AS first_seen_at,
        max(l.last_seen_at)        AS last_seen_at
      FROM listings l
      JOIN batch b ON b.id = l.property_id
      GROUP BY l.property_id
    ),
    repr AS (
      SELECT DISTINCT ON (l.property_id)
        l.property_id AS pid, l.sreality_id, l.category_main, l.category_type,
        l.disposition, l.area_m2, l.district, l.geom, l.price_czk
      FROM listings l
      JOIN batch b ON b.id = l.property_id
      ORDER BY l.property_id, l.is_active DESC, l.last_seen_at DESC NULLS LAST,
               l.sreality_id DESC
    ),
    prices AS (
      SELECT
        l.property_id AS pid,
        s.price_czk,
        row_number() OVER (
          PARTITION BY l.property_id ORDER BY s.scraped_at, s.id
        ) AS rn
      FROM listing_snapshots s
      JOIN listings l ON l.sreality_id = s.sreality_id
      JOIN batch b ON b.id = l.property_id
      WHERE s.price_czk IS NOT NULL
    ),
    steps AS (
      SELECT
        pid, price_czk,
        lag(price_czk) OVER (PARTITION BY pid ORDER BY rn) AS prev
      FROM prices
    ),
    price_hist AS (
      SELECT
        pid,
        count(*) FILTER (WHERE prev IS NOT NULL AND price_czk < prev) AS drops,
        count(*) FILTER (WHERE prev IS NOT NULL AND price_czk > prev) AS rises,
        max(CASE WHEN prev IS NOT NULL AND price_czk < prev
                 THEN (prev - price_czk)::numeric / prev * 100 END)   AS max_drop_pct
      FROM steps
      GROUP BY pid
    )
    UPDATE properties p SET
      is_active           = ca.is_active,
      source_count        = ca.source_count,
      distinct_site_count = ca.distinct_site_count,
      first_seen_at       = ca.first_seen_at,
      last_seen_at        = ca.last_seen_at,
      repr_listing_id     = r.sreality_id,
      category_main       = r.category_main,
      category_type       = r.category_type,
      disposition         = r.disposition,
      area_m2             = r.area_m2,
      district            = r.district,
      geom                = r.geom,
      current_price_czk   = r.price_czk,
      price_drop_count    = coalesce(ph.drops, 0),
      price_rise_count    = coalesce(ph.rises, 0),
      max_price_drop_pct  = ph.max_drop_pct,
      stats_computed_at   = now()
    FROM child_agg ca
    JOIN repr r ON r.pid = ca.pid
    LEFT JOIN price_hist ph ON ph.pid = ca.pid
    WHERE p.id = ca.pid
"""


def _batch_ranges(max_id: int, batch_size: int) -> Iterator[tuple[int, int]]:
    """Yield half-open [lo, hi) id ranges covering 1..max_id inclusive."""
    if max_id < 1 or batch_size < 1:
        return
    for lo in range(1, max_id + 1, batch_size):
        yield lo, lo + batch_size


def _attach_stragglers(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute(_ATTACH_BACKFILL_NATIVE_ID_SQL)
        cur.execute(_ATTACH_INSERT_SQL)
        inserted = cur.rowcount or 0
        cur.execute(_ATTACH_LINK_SQL)
    return inserted


def _max_property_id(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM properties")
        return int(cur.fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size", type=int, default=2000,
        help="Properties recomputed per statement (default 2000).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report straggler + property counts and exit without writing.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.batch_size < 1:
        print("ERROR: --batch-size must be >= 1.", file=sys.stderr)
        return 2

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    import psycopg

    LOG.info(
        "RECOMPUTE config batch_size=%d dry_run=%s",
        args.batch_size, args.dry_run,
    )

    started_at = time.monotonic()
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        if args.dry_run:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM listings WHERE property_id IS NULL")
                stragglers = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM properties")
                properties = int(cur.fetchone()[0])
            LOG.info(
                "RECOMPUTE dry-run stragglers=%d properties=%d; exit",
                stragglers, properties,
            )
            return 0

        attached = _attach_stragglers(conn)
        LOG.info("RECOMPUTE stragglers attached=%d", attached)

        max_id = _max_property_id(conn)
        batches = 0
        for lo, hi in _batch_ranges(max_id, args.batch_size):
            with conn.cursor() as cur:
                cur.execute(_RECOMPUTE_BATCH_SQL, {"lo": lo, "hi": hi})
            batches += 1
            LOG.debug("RECOMPUTE batch=%d-%d done", lo, hi)

    elapsed = time.monotonic() - started_at
    LOG.info(
        "RECOMPUTE done max_property_id=%d batches=%d elapsed=%.1fs",
        max_id, batches, elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
