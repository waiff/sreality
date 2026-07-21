"""R2 read cutover: rebuild the maintenance-walker partial indexes on the surrogate.

Five partial indexes on `listings` are keyed `btree(sreality_id)` (one is
`(source, sreality_id)`), and the enrichment scripts that page through them
cursor on the same column. Post-Gate-2 a new non-sreality listing has
sreality_id NULL, so it is INVISIBLE to every one of these walkers — geocoding,
street resolution and geo_cell keying silently stop for exactly the new rows,
forever, with no error. That in turn starves dedup (no street / no geo_cell =
no blocking key = the listing never reaches a dedup pass).

Rebuilds each index on `id` with the SAME partial predicate, CONCURRENTLY, then
drops the legacy twin. Legacy indexes are dropped LAST and only once the
replacement is confirmed valid, so the walkers are never left unindexed — a
plain `sreality_id`-ordered scan over 563k rows would be a seq scan under the
always-on writer.

Why a script and not a plain migration: CREATE INDEX CONCURRENTLY cannot run
inside a transaction block, and a non-concurrent CREATE INDEX takes a SHARE lock
on the whole hot table. Same reasoning (and the same helpers) as
apply_r2_phase_d_prep.py / apply_r2_constraints.py.

    python -m scripts.apply_r2_maintenance_indexes --dry-run
    python -m scripts.apply_r2_maintenance_indexes

Requires SUPABASE_DB_URL (+ SUPABASE_DB_SESSION_URL). Safe to re-run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from scraper import db
from scripts.apply_r2_constraints import _with_lock_retry
from scripts.apply_r2_phase_d_prep import _build_index_concurrently, _index_ready

log = logging.getLogger("apply_r2_maintenance_indexes")

# `new` mirrors `legacy` exactly except for the keyed column(s). Predicates are
# copied verbatim from the live definitions (pg_indexes) — a drifted predicate
# would silently change which rows the walker sees.
INDEXES: list[dict[str, Any]] = [
    {
        "new": "listings_geocode_candidates_id_idx",
        "legacy": "listings_geocode_candidates_idx",
        "cols": "(id)",
        "where": "WHERE geom IS NULL AND locality IS NOT NULL "
                 "AND geocode_attempted_at IS NULL",
    },
    {
        "new": "listings_street_name_key_null_id_idx",
        "legacy": "listings_street_name_key_null_idx",
        "cols": "(id)",
        "where": "WHERE street_name_key IS NULL AND street IS NOT NULL AND street <> ''",
    },
    {
        "new": "listings_geo_cell_key_byt_null_id_idx",
        "legacy": "listings_geo_cell_key_byt_null_idx",
        "cols": "(id)",
        "where": "WHERE geo_cell_key IS NULL AND category_main = 'byt' "
                 "AND geom IS NOT NULL AND obec_id IS NOT NULL",
    },
    {
        "new": "listings_geo_cell_key_null_id_idx",
        "legacy": "listings_geo_cell_key_null_idx",
        "cols": "(id)",
        "where": "WHERE geo_cell_key IS NULL "
                 "AND category_main = ANY (ARRAY['dum','pozemek','komercni','ostatni']) "
                 "AND geom IS NOT NULL AND obec_id IS NOT NULL",
    },
    {
        "new": "listings_source_active_street_id_idx",
        "legacy": "listings_source_active_street_idx",
        "cols": "(source, id)",
        "where": "WHERE street IS NULL AND is_active",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-legacy", action="store_true",
                        help="Build the replacements but do NOT drop the legacy "
                             "indexes (use while the old-cursor code is still live).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    with db.connect_session() as conn:
        for spec in INDEXES:
            new, legacy = spec["new"], spec["legacy"]
            if args.dry_run:
                log.info("%-42s new=%s legacy=%s", legacy,
                         "ok" if _index_ready(conn, new) else "todo",
                         "present" if _index_ready(conn, legacy) else "gone")
                continue
            _build_index_concurrently(
                conn, new,
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {new} ON listings "
                f"{spec['cols']} {spec['where']}",
            )

        if args.dry_run or args.keep_legacy:
            log.info("done (legacy indexes left in place)")
            return 0

        # Drop legacy ONLY after every replacement is confirmed valid — never
        # leave a walker with no usable index.
        missing = [s["new"] for s in INDEXES if not _index_ready(conn, s["new"])]
        if missing:
            raise RuntimeError(f"replacements not ready, refusing to drop legacy: {missing}")
        for spec in INDEXES:
            legacy = spec["legacy"]
            if not _index_ready(conn, legacy):
                continue
            log.info("%s: dropping legacy", legacy)
            _with_lock_retry(conn, f"DROP INDEX CONCURRENTLY IF EXISTS {legacy}", legacy)

    log.info("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
