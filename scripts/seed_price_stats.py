"""Seed an example price-stats dataset so the pipeline has something to run.

Idempotent on `slug`. Datasets are normally created via the Datasets UI / the
FastAPI service; this just gives a fresh deploy a starting dataset matching the
legacy ceny-nemovitosti config (byty / velmi dobrý / panel / osobní / 30–80 m²).

  python -m scripts.seed_price_stats
"""

from __future__ import annotations

import logging

from scraper.price_stats_db import connect

LOG = logging.getLogger(__name__)

EXAMPLE = {
    "slug": "byty-velmi-dobry-panel-osobni-30-80",
    "name": "Byty · velmi dobrý · panel · osobní · 30–80 m²",
    "description": "Legacy ceny-nemovitosti default filter set.",
    "category_main_cb": 1,
    "building_condition": "1",
    "building_type": "5",
    "ownership": "1",
    "usable_area_from": 30,
    "usable_area_to": 80,
    "distance": 0,
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO price_stat_datasets (
                slug, name, description, category_main_cb, building_condition,
                building_type, ownership, usable_area_from, usable_area_to,
                distance, created_by
            ) VALUES (
                %(slug)s, %(name)s, %(description)s, %(category_main_cb)s,
                %(building_condition)s, %(building_type)s, %(ownership)s,
                %(usable_area_from)s, %(usable_area_to)s, %(distance)s, 'seed'
            )
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
            """,
            EXAMPLE,
        )
        row = cur.fetchone()
    LOG.info("seeded dataset %s (id=%s)", EXAMPLE["slug"], row[0] if row else "exists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
