"""One-off backfill: stamp `listings.area_basis` where the stored columns PROVE it.

Migration 423 added the column with no backfill, so it fills only as rows are
detail-drained: 169,248 of 701,704 rows (24.1%), and — the part that matters —
**zero rows anywhere carry `plot`**, so every gate written on `area_basis` alone
is inert today and reads "not land" for every parcel in the database.

This does NOT re-derive the headline area. `scraper.area.derive_headline_area`
remains the single definition of both the precedence and the token vocabulary,
and this script CALLS it — feeding it the one measure the stored columns prove
was the winner, and writing back whatever it returns. What is new here is only
the INFERENCE of which input won, which is genuinely new knowledge about an
already-stored row, not a second copy of the derivation.

Three inferences are exact. Everything else is declined and counted:

  * LAND. `derive_headline_area`'s land arm stamps `plot` on whichever measure
    the page carried, and that value IS `area_m2`. So for `category_main =
    'pozemek'` the answer needs no portal input at all — it is a function of
    `area_m2` alone. 71,353 rows, every portal, taking `plot` off zero.
  * NO AREA. `area_m2 IS NULL` means every input was falsy, so the function
    returned `(None, None)` and the stamp must be NULL. This is what corrects
    the 8 rows that today carry a basis `derive_headline_area` cannot produce
    (7 sreality `pozemek` with `area_basis='usable'` and no area at all).
  * THE USABLE COLUMN, on the six portals where it is not collapsed. For those,
    `usable_area` stores ONLY the `usable` argument, so `area_m2 = usable_area`
    proves the first arm won. ~109,469 rows.
  * bazos, whose only arm is `fallback` (it writes no `usable_area` at all), so
    a non-land row with an area is `unknown` by construction. 61,041 rows.

DECLINED, deliberately — this is the whole discipline of the stamp. idnes and
ceskereality store a COLLAPSED `usable_area` (`užitná ?? podlahová ?? plocha`
and `plocha užitná ?? užitná plocha ?? plocha`), so `area_m2 = usable_area`
there proves only "one of three labels won". Writing `usable` would FABRICATE a
provenance claim on ~183,000 rows. The rest — a `total`/`floor`/title arm that
no column stores — is the same problem in smaller numbers. All of it is
recoverable later by re-parsing `portal_raw_pages` (100% detail coverage on all
seven HTML portals), which is a re-parse project, not a stamp.

sreality LAND stays NULL and that is correct, not a gap: `area_m2 IS NULL` on
all 39,371 of its `pozemek` rows (the parser offers only `usable=`, which
sreality does not populate for land) while the parcel sits in `estate_area`. A
basis describes `area_m2`; with no `area_m2` there is nothing to stamp. Moving
`estate_area` into `area_m2` would be a value change on a hashed column — a
different project. This is exactly why any land gate must keep OR-ing on
`category_main = 'pozemek'` rather than trusting `area_basis` alone.

Writes NOTHING but `listings.area_basis`. That column is in `_LISTING_FIELDS`
and NOT in `_HASH_FIELDS`, so this appends ZERO `listing_snapshots` rows — and
neither trigger on `listings` fires, both being `UPDATE OF geom` / `UPDATE OF
geom, obec_id, category_main, category_type`. It also does not touch
`dirty_properties`: the singleton rollup in `scraper/db.py` does not mirror
`area_basis` onto `properties` at all (all 686,291 rows are NULL there), so
there is nothing for a property recompute to pick up.

This is DML, never DDL — no ACCESS EXCLUSIVE, so it cannot head-block a writer
the way an `alter table` on this table can. It still declines to start while a
`rebuild_%` is active, so it is not competing with the read-model rebuilds for
I/O on the same table.

The read is KEYSET-PAGINATED and narrow — id, source, category_main, area_m2,
usable_area, area_basis — naming no wide column, so unlike its two W2 siblings
it detoasts nothing and each page is a cheap primary-key walk. Writes go back in
batched `UPDATE … FROM (VALUES …)` statements rather than one round-trip per
row. Idempotent WITHOUT a marker: a stamped row leaves the selection by
construction, and a declined row is re-examined and declined again at zero
writes. `--after` resumes from a `listings.id` cursor; a pass that stopped early
logs `BACKFILL INCOMPLETE` with that cursor, and silence means done.

Usage:  python -m scripts.backfill_area_basis --dry-run
        python -m scripts.backfill_area_basis --write
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter

from scraper import db
from scraper.area import LAND_CATEGORIES, derive_headline_area

LOG = logging.getLogger("backfill_area_basis")

# The portals whose `usable_area` column stores ONLY the `usable` argument, so
# `area_m2 = usable_area` proves the first arm won. idnes and ceskereality are
# absent on purpose: both collapse usable/floor/total into that one column
# before it is stored, so there the equality proves nothing. bazos is absent
# because it writes no `usable_area` at all — it is handled by _FALLBACK_ONLY.
_UNCOLLAPSED_USABLE: frozenset[str] = frozenset({
    "sreality", "bezrealitky", "mmreality", "remax", "realitymix", "maxima",
})

# Portals whose parser passes `fallback` and nothing else, so a non-land row
# with an area can only have come from that arm.
_FALLBACK_ONLY: frozenset[str] = frozenset({"bazos"})

LAND, NO_AREA, USABLE_COL, FALLBACK, DECLINED = (
    "land", "no-area", "usable-column", "fallback-only", "declined")

_SELECT_SQL = """
    SELECT l.id, l.source, l.category_main, l.area_m2, l.usable_area, l.area_basis
    FROM listings l
    WHERE l.id > %(after)s::bigint
      AND (l.area_basis IS NULL
           OR (l.category_main = 'pozemek' AND l.area_basis IS DISTINCT FROM 'plot'))
    ORDER BY l.id
    LIMIT %(page)s::int
"""

_COUNT_SQL = """
    SELECT count(*) FROM listings
    WHERE area_basis IS NULL
       OR (category_main = 'pozemek' AND area_basis IS DISTINCT FROM 'plot')
"""

# One statement per batch of stamps instead of one per row: at ~241k writes the
# round-trip alone would dominate the run.
_UPDATE_SQL = """
    UPDATE listings AS l
    SET area_basis = v.basis
    FROM (SELECT * FROM unnest(%(ids)s::bigint[], %(bases)s::text[]) AS t(id, basis)) AS v
    WHERE l.id = v.id
"""

# rebuild_browse_list() holds AccessShareLock on these tables for 5-10 minutes
# per run and is the heaviest reader of `listings`; starting a 460k-row sweep
# underneath it just makes both slower.
_REBUILD_ACTIVE_SQL = """
    SELECT count(*) FROM pg_stat_activity
    WHERE state = 'active' AND query LIKE %(pattern)s AND pid <> pg_backend_pid()
"""

# Bound as a VALUE, not spelled into the query: a literal `%` in an executed SQL
# string is a psycopg placeholder hazard, and `tests/test_sql_placeholders.py`
# guards against exactly that. `\_` escapes the underscore so this matches
# `rebuild_browse_list` / `rebuild_properties_map_mv` and not `rebuildXlist`.
_REBUILD_LIKE = r"%rebuild\_%"

_STATEMENT_TIMEOUT_SQL = "SET statement_timeout = '600s'"


def provable_basis(
    source: str | None,
    category_main: str | None,
    area_m2: float | None,
    usable_area: float | None,
) -> tuple[str | None, str]:
    """The basis `derive_headline_area` DID return, where the columns prove it.

    Returns `(basis, reason)`. `reason == DECLINED` means the stored columns
    cannot identify which measure won — the caller must leave the row alone.
    Every other reason is a proof, and its basis (which may legitimately be
    None) is what the function itself returns for that input.
    """
    if category_main in LAND_CATEGORIES:
        # The land arm takes the first truthy of (total, usable, floor,
        # fallback) and stamps 'plot' on it — and that value is what became
        # area_m2. Which arm supplied it cannot change the answer, so feeding
        # area_m2 as `total` reproduces the real call exactly.
        return derive_headline_area(
            category_main=category_main, total=area_m2)[1], LAND
    if area_m2 is None:
        # Every measure was falsy, so the function returned (None, None).
        return None, NO_AREA
    if source in _FALLBACK_ONLY:
        return derive_headline_area(
            category_main=category_main, fallback=area_m2)[1], FALLBACK
    if (source in _UNCOLLAPSED_USABLE
            and usable_area is not None
            and abs(float(area_m2) - float(usable_area)) < 1e-6):
        return derive_headline_area(
            category_main=category_main, usable=usable_area)[1], USABLE_COL
    return None, DECLINED


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings examined this run. Default: the whole table.")
    parser.add_argument("--batch-size", type=int, default=5000,
                        help="Rows per keyset page. The read names no wide "
                             "column, so this detoasts nothing.")
    parser.add_argument("--after", type=int, default=0,
                        help="Resume from this listings.id cursor (exclusive).")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stop claiming and exit cleanly.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report what would change; write nothing (the default).")
    parser.add_argument("--write", dest="dry_run", action="store_false",
                        help="Actually write. Without it this script only reports.")
    parser.add_argument("--ignore-rebuild", action="store_true",
                        help="Start even while a read-model rebuild is active.")
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
    examined = stamped = cleared = 0
    by_reason: Counter[str] = Counter()
    by_written: Counter[str] = Counter()
    declined_by_source: Counter[str] = Counter()
    cursor = args.after
    exhausted = False

    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_STATEMENT_TIMEOUT_SQL)
                cur.execute(_REBUILD_ACTIVE_SQL, {"pattern": _REBUILD_LIKE})
                active = int(cur.fetchone()[0])
                if active and not args.ignore_rebuild:
                    LOG.warning("BACKFILL refusing to start: %d rebuild_%% "
                                "statement(s) active. Retry later, or pass "
                                "--ignore-rebuild.", active)
                    return 3
                cur.execute(_COUNT_SQL)
                pending = int(cur.fetchone()[0])
            LOG.info("BACKFILL pending=%d batch=%d after=%d dry_run=%s",
                     pending, args.batch_size, args.after, args.dry_run)

            while True:
                page = args.batch_size
                if args.limit is not None:
                    remaining = args.limit - examined
                    if remaining <= 0:
                        break
                    page = min(page, remaining)
                with conn.cursor() as cur:
                    cur.execute(_SELECT_SQL, {"after": cursor, "page": page})
                    rows = cur.fetchall()
                if not rows:
                    exhausted = True
                    break

                ids: list[int] = []
                bases: list[str | None] = []
                for listing_id, source, cmain, area_m2, usable_area, stored in rows:
                    cursor = listing_id
                    examined += 1
                    basis, reason = provable_basis(source, cmain, area_m2, usable_area)
                    by_reason[reason] += 1
                    if reason == DECLINED:
                        declined_by_source[str(source)] += 1
                        continue
                    if basis == stored:
                        continue
                    ids.append(listing_id)
                    bases.append(basis)
                    by_written[f"{basis or 'NULL'}:{reason}"] += 1
                    if basis is None:
                        cleared += 1
                    else:
                        stamped += 1
                    if args.verbose:
                        LOG.debug("BACKFILL id=%d %s -> %s (%s)",
                                  listing_id, stored, basis, reason)

                if ids and not args.dry_run:
                    with conn.cursor() as cur:
                        cur.execute(_UPDATE_SQL, {"ids": ids, "bases": bases})

                LOG.info("BACKFILL progress examined=%d stamped=%d cleared=%d "
                         "declined=%d cursor=%d",
                         examined, stamped, cleared, by_reason[DECLINED], cursor)

                if len(rows) < page:
                    exhausted = True
                    break
                if args.max_seconds and time.monotonic() - start > args.max_seconds:
                    LOG.info("BACKFILL stopping: --max-seconds reached after=%d", cursor)
                    break
    finally:
        for key, n in sorted(by_written.items(), key=lambda kv: -kv[1]):
            LOG.info("BACKFILL would_write %-28s %7d", key, n)
        for key, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            LOG.info("BACKFILL reason     %-28s %7d", key, n)
        for key, n in sorted(declined_by_source.items(), key=lambda kv: -kv[1]):
            LOG.info("BACKFILL declined   %-28s %7d", key, n)
        LOG.info("BACKFILL done examined=%d stamped=%d cleared=%d declined=%d "
                 "cursor=%d exhausted=%s dry_run=%s",
                 examined, stamped, cleared, by_reason[DECLINED], cursor,
                 exhausted, args.dry_run)
        if not exhausted:
            LOG.warning("BACKFILL INCOMPLETE — rows above id=%d were never "
                        "examined; resume with --after %d", cursor, cursor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
