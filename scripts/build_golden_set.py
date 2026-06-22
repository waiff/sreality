"""Build the dedup golden set (migration 223): labeled listing pairs for eval.

POSITIVES — operator/engine-confirmed same-property pairs from property_merge_events
(the moved listing paired with the survivor property's representative listing).
NEGATIVES — the apartment coordinate trap as ground truth: two active apartments at the
SAME geom (same obec + rounded lat/lng) with a DIFFERENT leading room count are distinct
units, never the same property.

Set-based INSERT...SELECT only (never per-row over the pooler). Idempotent via
ON CONFLICT DO NOTHING. Run: `python -m scripts.build_golden_set` (env: SUPABASE_DB_URL).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

LOG = logging.getLogger("build_golden_set")

# Bound the negative sample so the self-join can't run away; positives are naturally
# bounded by the merge history (~13k) so they're taken in full.
NEGATIVE_LIMIT = 6000

_POSITIVES_SQL = """
    INSERT INTO dedup_golden_pairs
        (left_sreality_id, right_sreality_id, is_same, category_main, stratum, basis)
    SELECT LEAST(e.listing_id, sp.repr_listing_id),
           GREATEST(e.listing_id, sp.repr_listing_id),
           true, l.category_main, 'positive', 'merge_event'
    FROM property_merge_events e
    JOIN properties sp ON sp.id = e.survivor_property_id AND sp.repr_listing_id IS NOT NULL
    JOIN listings l ON l.sreality_id = e.listing_id
    WHERE e.undone_at IS NULL
      AND e.listing_id <> sp.repr_listing_id
    ON CONFLICT (left_sreality_id, right_sreality_id) DO NOTHING
"""

# Different leading room count (`left(disposition,1)`) sidesteps the 2+kk~2+1 loose
# equivalence — these are unambiguously different units sharing one building's pin.
_NEGATIVES_SQL = """
    INSERT INTO dedup_golden_pairs
        (left_sreality_id, right_sreality_id, is_same, category_main, stratum, basis)
    SELECT lo, hi, false, 'byt', 'negative', 'distinct_disposition_same_coord'
    FROM (
        SELECT LEAST(a.sreality_id, b.sreality_id) AS lo,
               GREATEST(a.sreality_id, b.sreality_id) AS hi
        FROM (
            SELECT sreality_id, obec_id, disposition,
                   round(ST_Y(geom::geometry)::numeric, 5) AS lat,
                   round(ST_X(geom::geometry)::numeric, 5) AS lng
            FROM listings
            WHERE is_active = true AND geom IS NOT NULL
              AND category_main = 'byt' AND disposition IS NOT NULL AND obec_id IS NOT NULL
        ) a
        JOIN (
            SELECT sreality_id, obec_id, disposition,
                   round(ST_Y(geom::geometry)::numeric, 5) AS lat,
                   round(ST_X(geom::geometry)::numeric, 5) AS lng
            FROM listings
            WHERE is_active = true AND geom IS NOT NULL
              AND category_main = 'byt' AND disposition IS NOT NULL AND obec_id IS NOT NULL
        ) b
          ON a.obec_id = b.obec_id AND a.lat = b.lat AND a.lng = b.lng
         AND a.sreality_id < b.sreality_id
         AND left(a.disposition, 1) <> left(b.disposition, 1)
        LIMIT %(limit)s
    ) p
    ON CONFLICT (left_sreality_id, right_sreality_id) DO NOTHING
"""

_COUNTS_SQL = """
    SELECT stratum, count(*) FROM dedup_golden_pairs GROUP BY stratum ORDER BY stratum
"""


def build(conn: Any, *, negative_limit: int = NEGATIVE_LIMIT) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(_POSITIVES_SQL)
        pos = cur.rowcount or 0
        cur.execute(_NEGATIVES_SQL, {"limit": negative_limit})
        neg = cur.rowcount or 0
        cur.execute(_COUNTS_SQL)
        totals = {row[0]: int(row[1]) for row in cur.fetchall()}
    return {"inserted_positive": pos, "inserted_negative": neg, **{f"total_{k}": v for k, v in totals.items()}}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    import psycopg

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        stats = build(conn)
    LOG.info("GOLDEN SET %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
