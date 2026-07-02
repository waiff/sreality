"""One-off backfill: heal idnes areas truncated by the spaced-thousands bug.

Before the `_AREA_RE` fix, an idnes title area rendered with a Czech thousands
group ("Prodej pole 2 403 m²") parsed from INSIDE the number — `area_m2` stored
403 instead of 2403 (8k+ rows, mostly pozemek; every Kč/m² figure computed from
them was wrong). This re-parses each suspect listing's already-staged detail
HTML (`portal_raw_pages` — NO re-fetch of idnes) with the fixed parser and
updates the four area columns in place where the re-parse differs.

Suspects are selected by two signatures: a spaced-thousands area in the stored
title, OR the truncation fingerprint (`area_m2 = estate_area mod 1000` with
`estate_area >= 1000` — the title path truncated while the unspaced <dl> value
parsed whole).

Price is deliberately NOT rewritten number-for-number (a staged page can lag
the live one): the only price write is the per-m²-masquerade heal — stored
price non-NULL while the fixed parser refuses the text as a unit price — which
prod verification found ZERO instances of (all 5,773 sub-100k pozemek prices
matched the portal's own absolute <strong> price). Any other price discrepancy
is logged for operator review, never written.

This writes NO snapshot (rule #2 governs source-content changes; correcting our
own mis-parse of the SAME staged state is a data-quality fix — the
backfill_bazos_coords posture). The area columns ARE in the ScrapedListing
content hash, so each healed listing's NEXT successful detail refetch computes
a hash differing from its latest snapshot and appends ONE genuine snapshot —
bounded, correct, self-limiting.

Idempotent + resumable: every processed row is stamped
(`raw_json.area_reparse_v1 = true`) so it drops out of the next selection;
writes commit per row (autocommit). The marker vanishes on the next refetch —
by then the row was parsed with the fixed parser, so a re-selection no-ops.

Usage:  python -m scripts.backfill_idnes_areas --limit 20000
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from scraper import db
from scraper.idnes_parser import _PRICE_PER_M2_RE, _PRICE_RUN_RE, parse_detail

LOG = logging.getLogger("backfill_idnes_areas")

_AREA_COLS = ("area_m2", "usable_area", "estate_area", "garden_area")

# Metadata only — the (large) HTML is fetched per row so memory stays flat.
_SELECT_SQL = """
    SELECT l.sreality_id, l.source_id_native, l.source_url,
           l.category_main, l.category_type, l.property_id,
           l.price_czk, l.area_m2, l.usable_area, l.estate_area, l.garden_area,
           l.raw_json->>'price_text' AS price_text
    FROM listings l
    WHERE l.source = 'idnes'
      AND l.raw_json->>'area_reparse_v1' IS NULL
      AND (
            (l.raw_json->>'title') ~ '\\d [0-9]{3}([,.][0-9]+)? ?m'
         OR (l.area_m2 IS NOT NULL AND l.estate_area >= 1000 AND l.area_m2 < 1000
             AND l.area_m2 = mod(l.estate_area::int, 1000))
      )
      AND EXISTS (
          SELECT 1 FROM portal_raw_pages pr
          WHERE pr.source = 'idnes' AND pr.source_id_native = l.source_id_native
            AND pr.page_kind = 'detail'
      )
    ORDER BY l.is_active DESC, l.sreality_id
    LIMIT %(limit)s
"""

_COUNT_SQL = """
    SELECT count(*) FROM listings l
    WHERE l.source = 'idnes'
      AND l.raw_json->>'area_reparse_v1' IS NULL
      AND (
            (l.raw_json->>'title') ~ '\\d [0-9]{3}([,.][0-9]+)? ?m'
         OR (l.area_m2 IS NOT NULL AND l.estate_area >= 1000 AND l.area_m2 < 1000
             AND l.area_m2 = mod(l.estate_area::int, 1000))
      )
"""

_FETCH_HTML_SQL = """
    SELECT html FROM portal_raw_pages
    WHERE source = 'idnes' AND source_id_native = %(native)s AND page_kind = 'detail'
    ORDER BY fetched_at DESC NULLS LAST
    LIMIT 1
"""

_STAMP_SQL = """
    UPDATE listings
    SET raw_json = raw_json || '{"area_reparse_v1": true}'::jsonb
    WHERE sreality_id = %(id)s
"""


def _num_eq(a: object, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) < 1e-6


def _is_per_m2_price_text(text: str | None) -> bool:
    if not text:
        return False
    m = _PRICE_RUN_RE.search(text)
    return bool(m and _PRICE_PER_M2_RE.match(text[m.end():]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20000,
                        help="Max listings processed this run.")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stop claiming and exit cleanly.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Reparse and report what would change; write nothing.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    start = time.monotonic()
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_COUNT_SQL)
            pending = int(cur.fetchone()[0])
        LOG.info("BACKFILL pending=%d limit=%d dry_run=%s", pending, args.limit, args.dry_run)

        with conn.cursor() as cur:
            cur.execute(_SELECT_SQL, {"limit": args.limit})
            rows = cur.fetchall()

        healed = stamped = errors = price_flags = 0
        dirty: list[int] = []
        for i, row in enumerate(rows):
            (sid, native, url, cmain, ctype, prop_id,
             price_czk, *stored_areas, price_text) = row
            if args.max_seconds and time.monotonic() - start > args.max_seconds:
                LOG.info("BACKFILL stopping: --max-seconds reached")
                break
            with conn.cursor() as cur:
                cur.execute(_FETCH_HTML_SQL, {"native": native})
                html_row = cur.fetchone()
            if html_row is None:
                if not args.dry_run:
                    with conn.cursor() as cur:
                        cur.execute(_STAMP_SQL, {"id": sid})
                stamped += 1
                continue
            try:
                listing = parse_detail(
                    html_row[0], source_url=url,
                    category_main=cmain, category_type=ctype, geocoder=None,
                )
            except Exception as exc:  # noqa: BLE001 - a bad staged page must not abort the run
                LOG.warning("BACKFILL parse error id=%d: %s", sid, exc)
                if not args.dry_run:
                    with conn.cursor() as cur:
                        cur.execute(_STAMP_SQL, {"id": sid})
                errors += 1
                continue

            changed: dict[str, float | int | None] = {}
            for col, stored in zip(_AREA_COLS, stored_areas):
                new = getattr(listing, col)
                if not _num_eq(stored, new):
                    changed[col] = new

            if price_czk is not None and listing.price_czk is None \
                    and _is_per_m2_price_text(listing.raw.get("price_text")):
                changed["price_czk"] = None
            elif listing.price_czk is not None and price_czk is not None \
                    and listing.price_czk != price_czk:
                # A different NUMBER means the staged page lags the live row —
                # not this backfill's call. Surface it, never write it.
                LOG.warning("BACKFILL price differs id=%d stored=%s reparse=%s (skipped)",
                            sid, price_czk, listing.price_czk)
                price_flags += 1

            if args.dry_run:
                if changed:
                    LOG.info("BACKFILL would heal id=%d %s", sid, changed)
                    healed += 1
                else:
                    stamped += 1
                continue

            with conn.cursor() as cur:
                if changed:
                    sets = ", ".join(f"{col} = %({col})s" for col in changed)
                    cur.execute(
                        f"UPDATE listings SET {sets}, "
                        "raw_json = raw_json || '{\"area_reparse_v1\": true}'::jsonb "
                        "WHERE sreality_id = %(id)s",
                        {**changed, "id": sid},
                    )
                    healed += 1
                    if prop_id is not None:
                        dirty.append(prop_id)
                else:
                    cur.execute(_STAMP_SQL, {"id": sid})
                    stamped += 1

            if len(dirty) >= 500:
                db.mark_properties_dirty(conn, dirty)
                dirty = []
            if (i + 1) % 200 == 0:
                LOG.info("BACKFILL progress=%d/%d healed=%d unchanged=%d errors=%d",
                         i + 1, len(rows), healed, stamped, errors)

        if dirty and not args.dry_run:
            db.mark_properties_dirty(conn, dirty)

    LOG.info("BACKFILL done processed=%d healed=%d unchanged=%d errors=%d price_flags=%d",
             healed + stamped + errors, healed, stamped, errors, price_flags)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
