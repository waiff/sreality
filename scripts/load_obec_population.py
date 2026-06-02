"""Load ČSÚ municipality population into admin_boundaries.population (all obce).

Reads the committed ČSÚ DataStat export (data/csu_population.json, OBY02AT02 —
download from https://data.csu.gov.cz/datastat/data/VYBER/OBY02AT02) and upserts
each obec's population by id (the ČSÚ municipality code IS admin_boundaries.id).
Unlike scripts/seed_curated_cities.py (which only populates the 206 curated
cities' city_population), this covers EVERY obec, which is what the
"within X km of a municipality with population > N" proximity filter needs.

Idempotent: a re-run with an unchanged file writes nothing (the `is distinct
from` guard). Requires SUPABASE_DB_URL. Run via .github/workflows/load_obec_population.yml
or locally.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.csu_population import DATASTAT_URL, load_population_by_code  # noqa: E402

JSON_PATH = ROOT / "data" / "csu_population.json"
LOG = logging.getLogger("load_obec_population")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="load_obec_population",
        description=f"Load obec population into admin_boundaries (source: {DATASTAT_URL})",
    )
    p.add_argument("--json", default=str(JSON_PATH))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    by_code = load_population_by_code(Path(args.json))
    rows = [(cid, pop, yr) for cid, (pop, yr) in by_code.items()]
    LOG.info("Parsed %d obce from %s", len(rows), args.json)
    if args.dry_run:
        LOG.info("Dry-run: would upsert %d obec population rows", len(rows))
        return 0

    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        LOG.error("SUPABASE_DB_URL is not set")
        return 2

    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.cursor() as cur, conn.transaction():
            cur.execute(
                "create temp table _obec_pop (id bigint, pop int, yr int) "
                "on commit drop"
            )
            with cur.copy("copy _obec_pop (id, pop, yr) from stdin") as copy:
                for r in rows:
                    copy.write_row(r)
            cur.execute(
                """
                update admin_boundaries b
                   set population = p.pop, population_year = p.yr
                  from _obec_pop p
                 where b.id = p.id
                   and b.level = 'obec'
                   and (b.population is distinct from p.pop
                        or b.population_year is distinct from p.yr)
                """
            )
            updated = cur.rowcount or 0
    LOG.info("Upserted population for %d obce (changed rows)", updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
