"""One-off backfill: heal mmreality areas that stored the PLOT as the headline.

Before the W1 fix, mmreality's own `or` chain read `totalArea or usableArea`.
For a HOUSE mmreality's `totalArea` is the PARCEL, so 3,588 active `dum` rows
carried a median `area_m2` of 905 m2 against 130 m2 sitting unused in
`usableArea` — and a median Kc/m2 of 5,699 against ~46,000 on every other
portal. The parcel was thrown away at the same moment it was being misused as
the denominator: `count(estate_area)` was ZERO across all 11,218 mmreality rows.

So the heal is a re-derive, not a refetch: mmreality stores its whole source
object in `raw_json` (`dict(obj)` in the parser), so every input the fixed
parser needs is already staged. This re-runs the SAME decision the parser now
makes — `scraper.area.derive_headline_area` on the typed measures, plus the
parser's `estate_area` chain — and writes the four area columns plus
`area_basis` where the re-derive disagrees.

`area_basis` is written on every processed row, not only the healed ones: it is
NULL on all of mmreality today (migration 423 added the column, nothing
backfilled it), it is a provenance stamp that never changes `area_m2`, and it
is deliberately OUT of `_HASH_FIELDS` — so stamping it churns no snapshot.

This writes NO snapshot (rule #2 governs source-content changes; correcting our
own mis-parse of the SAME staged state is a data-quality fix — the
backfill_idnes_areas / backfill_bazos_coords posture). `area_m2`, `estate_area`
and `usable_area` ARE in the content hash, so each healed listing's NEXT
successful detail refetch computes a hash differing from its latest snapshot and
appends ONE genuine snapshot — bounded, correct, self-limiting.

Keyed on the surrogate `listings.id`, NOT `sreality_id` — the one place this
diverges from backfill_idnes_areas. Since the identity refactor's Gate 2,
`sreality_id` is NULL on 1,464 of 11,218 mmreality rows, 543 of them damaged
`dum`: a sreality_id-keyed clone would heal 3,058 of the 3,601 and leave the
rest broken with no signal that it had.

Idempotent + resumable: every processed row is stamped
(`raw_json.area_reparse_v2 = true`) so it drops out of the next selection;
writes commit per row (autocommit). The marker vanishes on the next refetch — by
then the row was parsed with the fixed parser, so a re-selection no-ops.

Usage:  python -m scripts.backfill_mmreality_areas --dry-run
        python -m scripts.backfill_mmreality_areas --limit 20000
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from typing import Any, Mapping

from scraper import db
from scraper.area import derive_headline_area
from scraper.mmreality_parser import _to_float

LOG = logging.getLogger("backfill_mmreality_areas")

_COLS: tuple[str, ...] = (
    "area_m2", "area_basis", "usable_area", "estate_area", "garden_area",
)

# Only the measure keys are projected out of raw_json: the whole object averages
# 17 kB across 11,218 rows (182 MB), which a fetchall would hold in memory as
# parsed dicts for no gain.
_RAW_KEYS: tuple[str, ...] = (
    "usableArea", "totalArea", "landArea", "plotArea", "gardenArea",
)
_RAW_PROJECTION = ",\n           ".join(
    f"l.raw_json->>'{k}' AS raw_{k}" for k in _RAW_KEYS
)

_SELECT_SQL = f"""
    SELECT l.id, l.category_main, l.property_id,
           l.area_m2, l.area_basis, l.usable_area, l.estate_area, l.garden_area,
           {_RAW_PROJECTION}
    FROM listings l
    WHERE l.source = 'mmreality'
      AND l.raw_json->>'area_reparse_v2' IS NULL
    ORDER BY l.is_active DESC, l.id
    LIMIT %(limit)s::int
"""

_COUNT_SQL = """
    SELECT count(*) FROM listings
    WHERE source = 'mmreality' AND raw_json->>'area_reparse_v2' IS NULL
"""

_STAMP_SQL = """
    UPDATE listings
    SET raw_json = raw_json || '{"area_reparse_v2": true}'::jsonb
    WHERE id = %(id)s::bigint
"""


def derive(category_main: str | None, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Re-run the fixed parser's area decision over one staged mmreality object.

    Mirrors scraper.mmreality_parser.parse_detail exactly: for a house
    `totalArea` is the parcel, so it is never offered to the resolver as a
    `total` (a house with no `usableArea` must land NULL rather than a parcel
    stamped 'total') and is routed to `estate_area` instead.
    """
    is_house = category_main == "dum"
    total_area = _to_float(raw.get("totalArea"))
    usable = _to_float(raw.get("usableArea"))
    area_m2, area_basis = derive_headline_area(
        category_main=category_main,
        usable=usable,
        total=None if is_house else total_area,
    )
    return {
        "area_m2": area_m2,
        "area_basis": area_basis,
        "usable_area": usable,
        "estate_area": (
            _to_float(raw.get("landArea"))
            or _to_float(raw.get("plotArea"))
            or (total_area if is_house else None)
        ),
        "garden_area": _to_float(raw.get("gardenArea")),
    }


def _eq(stored: object, new: object) -> bool:
    if stored is None or new is None:
        return stored is None and new is None
    if isinstance(new, str):
        return str(stored) == new
    return abs(float(stored) - float(new)) < 1e-6


def changed_columns(
    category_main: str | None, raw: Mapping[str, Any], stored: Mapping[str, Any]
) -> dict[str, Any]:
    """The subset of _COLS whose stored value disagrees with the re-derive."""
    fresh = derive(category_main, raw)
    return {c: fresh[c] for c in _COLS if not _eq(stored[c], fresh[c])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20000,
                        help="Max listings processed this run.")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stop claiming and exit cleanly.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report what would change; write nothing (the default).")
    parser.add_argument("--write", dest="dry_run", action="store_false",
                        help="Actually write. Without it this script only reports.")
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

        healed = stamped = 0
        by_category: Counter[str] = Counter()
        by_column: Counter[str] = Counter()
        dirty: list[int] = []
        for i, row in enumerate(rows):
            if args.max_seconds and time.monotonic() - start > args.max_seconds:
                LOG.info("BACKFILL stopping: --max-seconds reached")
                break
            listing_id, cmain, prop_id = row[0], row[1], row[2]
            stored = dict(zip(_COLS, row[3:8]))
            raw = dict(zip(_RAW_KEYS, row[8:]))
            changed = changed_columns(cmain, raw, stored)

            if changed:
                healed += 1
                by_category[f"{cmain}:{'+'.join(sorted(changed))}"] += 1
                for col in changed:
                    by_column[col] += 1
            else:
                stamped += 1

            if args.dry_run:
                if changed and args.verbose:
                    LOG.debug("BACKFILL would heal id=%d %s", listing_id, changed)
                continue

            with conn.cursor() as cur:
                if changed:
                    sets = ", ".join(f"{col} = %({col})s" for col in changed)
                    cur.execute(
                        f"UPDATE listings SET {sets}, "
                        "raw_json = raw_json || '{\"area_reparse_v2\": true}'::jsonb "
                        "WHERE id = %(id)s::bigint",
                        {**changed, "id": listing_id},
                    )
                    if prop_id is not None:
                        dirty.append(prop_id)
                else:
                    cur.execute(_STAMP_SQL, {"id": listing_id})

            if len(dirty) >= 500:
                db.mark_properties_dirty(conn, dirty)
                dirty = []
            if (i + 1) % 500 == 0:
                LOG.info("BACKFILL progress=%d/%d healed=%d unchanged=%d",
                         i + 1, len(rows), healed, stamped)

        if dirty and not args.dry_run:
            db.mark_properties_dirty(conn, dirty)

    for key, n in sorted(by_category.items(), key=lambda kv: -kv[1]):
        LOG.info("BACKFILL by_change %-44s %6d", key, n)
    for col, n in sorted(by_column.items(), key=lambda kv: -kv[1]):
        LOG.info("BACKFILL by_column %-16s %6d", col, n)
    LOG.info("BACKFILL done processed=%d healed=%d unchanged=%d dry_run=%s",
             healed + stamped, healed, stamped, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
