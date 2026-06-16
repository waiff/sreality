"""Backfill listings.street (+ house_number/zip) across the HTML/JSON portals.

idnes/maxima/remax/bezrealitky historically stored NO street; bazos stored a
polluted one ("ul. Teplého Nabízíme"). This re-derives the field from data we
ALREADY have — the stored `locality` (idnes/maxima), `raw_json->>'address'`
(remax), the advert JSON (bezrealitky), or by re-cleaning the existing value
(bazos). NO re-fetch, NO LLM, NO Mapy spend; street/house_number/zip are out of
the content hash so NO snapshot is written (a data-quality fix, not a new
real-world state — same posture as the bazos coords/locality backfills).

Per-source rules mirror the live parsers (scraper.street):
  idnes        first comma-segment of locality   (foreign + okres guarded)
  maxima       last comma-segment, morphology     (village vs street)
  remax        leading segment of data-address    (town cross-checked)
  bezrealitky  structured street/houseNumber/zip  (the full triple)
  bazos        re-clean the existing street value

The fabrication guard uses the row's geo-derived obec/okres/region (migration
140) — the strongest signal that a candidate is a town, not a street — and the
selection requires obec IS NOT NULL, which excludes foreign listings outright.

Idempotent + resumable: every processed row is stamped
(`raw_json.portal_street_backfill = true`) so it drops out next run whether or
not it improved; writes commit per batch (autocommit). Rerun until "pending=0".
A changed-street row enqueues its property into dirty_properties so the */5
recompute propagates the group-best street up to properties.street.

Usage:  python -m scripts.backfill_portal_streets [--source idnes] --limit 40000
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

from scraper import db
from scraper.street import clean_street, street_from_locality

LOG = logging.getLogger("backfill_portal_streets")

_SOURCES: tuple[str, ...] = ("idnes", "maxima", "remax", "bezrealitky", "bazos")

# Rows with the input we need that haven't been processed this pass. obec IS NOT
# NULL (CZ-resolved coordinate, migration 140) excludes foreign listings — the
# dominant idnes fabrication vector — before the helper's guards even run.
_INPUT_PREDICATE: dict[str, str] = {
    "idnes":       "l.locality IS NOT NULL AND l.obec IS NOT NULL AND l.street IS NULL",
    "maxima":      "l.locality IS NOT NULL AND l.obec IS NOT NULL AND l.street IS NULL",
    "remax":       "l.raw_json->>'address' IS NOT NULL AND l.obec IS NOT NULL AND l.street IS NULL",
    "bezrealitky": "l.raw_json->>'street' IS NOT NULL AND l.street IS NULL",
    "bazos":       "l.street IS NOT NULL",
}

_SELECT_SQL = """
    SELECT l.sreality_id, l.property_id, l.locality, l.district, l.street,
           l.obec, l.okres, l.region,
           l.raw_json->>'address' AS address,
           l.raw_json->>'street' AS adv_street,
           l.raw_json->>'houseNumber' AS adv_house_number,
           l.raw_json->>'zip' AS adv_zip
    FROM listings l
    WHERE l.source = %(source)s AND l.is_active
      AND l.raw_json->>'portal_street_backfill' IS NULL
      AND ({predicate})
    ORDER BY l.sreality_id
    LIMIT %(limit)s
"""

_COUNT_SQL = """
    SELECT count(*) FROM listings l
    WHERE l.source = %(source)s AND l.is_active
      AND l.raw_json->>'portal_street_backfill' IS NULL
      AND ({predicate})
"""

# COALESCE(new, existing): a derived value overrides only when present, so a
# town-only row (street -> NULL) keeps NULL and a re-clean never nulls out a
# value the cleaner couldn't parse. geom untouched -> no admin-geo re-derive, no
# snapshot. The marker stamps every processed row so it leaves the selection.
_UPDATE_SQL = """
    UPDATE listings
    SET street       = COALESCE(%(street)s, street),
        house_number = COALESCE(%(house_number)s, house_number),
        zip          = COALESCE(%(zip)s, zip),
        raw_json     = raw_json || '{"portal_street_backfill": true}'::jsonb
    WHERE sreality_id = %(id)s
"""


def derive(source: str, row: dict[str, Any]) -> tuple[str | None, str | None, str | None, bool]:
    """Per-source (street, house_number, zip, improved) from stored fields."""
    geo = (row["obec"], row["okres"], row["region"])
    if source == "idnes":
        s = street_from_locality(row["locality"], position="first", geo_names=geo)
        return s, None, None, s is not None
    if source == "maxima":
        s = street_from_locality(
            row["locality"], position="last", require_morphology=True, geo_names=geo
        )
        return s, None, None, s is not None
    if source == "remax":
        s = street_from_locality(
            row["address"], position="first",
            geo_names=(row["locality"], row["district"], *geo),
        )
        return s, None, None, s is not None
    if source == "bezrealitky":
        s = clean_street(row["adv_street"])
        hn = (row["adv_house_number"] or "").strip() or None
        zp = (row["adv_zip"] or "").strip() or None
        return s, hn, zp, (s is not None or hn is not None or zp is not None)
    if source == "bazos":
        s = clean_street(row["street"])
        return s, None, None, (s is not None and s != row["street"])
    raise ValueError(f"unknown source {source!r}")


_COLS = ("sreality_id", "property_id", "locality", "district", "street",
         "obec", "okres", "region", "address", "adv_street",
         "adv_house_number", "adv_zip")


def process_source(conn: Any, source: str, limit: int, deadline: float | None) -> dict[str, int]:
    predicate = _INPUT_PREDICATE[source]
    with conn.cursor() as cur:
        cur.execute(_COUNT_SQL.format(predicate=predicate), {"source": source})
        pending = int(cur.fetchone()[0])
    LOG.info("BACKFILL source=%s pending=%d limit=%d", source, pending, limit)
    if pending == 0:
        return {"updated": 0, "skipped": 0, "remaining": 0}

    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL.format(predicate=predicate),
                    {"source": source, "limit": limit})
        rows = [dict(zip(_COLS, r)) for r in cur.fetchall()]

    updated = skipped = 0
    dirty: list[int] = []
    for i, row in enumerate(rows):
        if deadline is not None and time.monotonic() > deadline:
            LOG.info("BACKFILL source=%s stopping: --max-seconds reached", source)
            break
        street, hn, zp, improved = derive(source, row)
        with conn.cursor() as cur:
            cur.execute(_UPDATE_SQL, {
                "id": row["sreality_id"], "street": street,
                "house_number": hn, "zip": zp,
            })
        if improved:
            updated += 1
            if row["property_id"] is not None:
                dirty.append(row["property_id"])
        else:
            skipped += 1
        if len(dirty) >= 500:
            db.mark_properties_dirty(conn, dirty)
            dirty = []
        if (i + 1) % 500 == 0:
            LOG.info("BACKFILL source=%s progress=%d/%d updated=%d skipped=%d",
                     source, i + 1, len(rows), updated, skipped)
    if dirty:
        db.mark_properties_dirty(conn, dirty)

    with conn.cursor() as cur:
        cur.execute(_COUNT_SQL.format(predicate=predicate), {"source": source})
        remaining = int(cur.fetchone()[0])
    LOG.info("BACKFILL source=%s done updated=%d skipped=%d remaining=%d",
             source, updated, skipped, remaining)
    return {"updated": updated, "skipped": skipped, "remaining": remaining}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=_SOURCES, default=None,
                        help="Limit to one portal; default processes all.")
    parser.add_argument("--limit", type=int, default=40000,
                        help="Max listings processed per source this run.")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stop claiming and exit cleanly.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the pending counts and exit without writing.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    sources = (args.source,) if args.source else _SOURCES
    start = time.monotonic()
    deadline = start + args.max_seconds if args.max_seconds else None

    with db.connect() as conn:
        if args.dry_run:
            for source in sources:
                with conn.cursor() as cur:
                    cur.execute(_COUNT_SQL.format(predicate=_INPUT_PREDICATE[source]),
                                {"source": source})
                    LOG.info("BACKFILL source=%s pending=%d", source, int(cur.fetchone()[0]))
            return 0

        totals = {"updated": 0, "skipped": 0, "remaining": 0}
        for source in sources:
            if deadline is not None and time.monotonic() > deadline:
                LOG.info("BACKFILL stopping before source=%s: --max-seconds reached", source)
                break
            res = process_source(conn, source, args.limit, deadline)
            for k in totals:
                totals[k] += res[k]

    LOG.info("BACKFILL all-done updated=%d skipped=%d remaining=%d",
             totals["updated"], totals["skipped"], totals["remaining"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
