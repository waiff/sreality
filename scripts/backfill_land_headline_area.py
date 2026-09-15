"""One-off heal: the parcel a land listing always published becomes its headline area.

`listings.area_m2` is POLYMORPHIC by design (rule 23) — the interior measure for a
dwelling, THE PLOT for `pozemek` — and `scraper.area.derive_headline_area` is the one
rule that picks it. Until W17 three parsers never handed that rule the plot at all:
sreality passed only `usable_area` (which it does not publish on land), bezrealitky only
`surface`, idnes only the three interior labels plus the title. Each of them DID store
the parcel, in `estate_area`. The result, measured on production 2026-09-15:

    sreality      44,237 of 44,237 land rows   area_m2 NULL, estate_area set
    idnes          5,292 of 45,500
    bezrealitky    2,654 of  2,667
    ------------------------------------------------------------------
    52,183 rows (~32.7k of them active)

Those rows have no per-m² price (the measure divides by `area_m2`), and no area for any
consumer that reads the headline — including the NEW DEDUP path C candidate rule, which
W17 moved onto `area_m2` precisely so there is ONE answer to "which area is this
listing's area". The parser side of W17 fixes every future write; this fixes the rows
already stored. It is the same value the same page already gave us, moved into the column
the one rule would put it in today.

    area_m2 = estate_area, area_basis = 'plot'
    WHERE category_main IN <LAND_CATEGORIES>
      AND (area_m2 IS NULL OR area_m2 <= 0)
      AND estate_area > 0 AND estate_area < MAX_AREA_M2

ALL rows, active or not: coverage is every listing, and an inactive land row is still read
by history, statistics and the dedup simulation. 3,016 land rows carry no area from their
portal at all — an honest gap, left alone.

**THE CEILING IS PART OF THE POPULATION, not a convenience.** `area_m2` is `numeric(7,1)`
and `estate_area` is `numeric(9,1)`, so 20 pending rows (sreality 11, idnes 9; largest
16,809,800 m²) hold a parcel the headline column cannot store — `SET area_m2 = estate_area`
on one of them raises 22003 and, since a numeric overflow is not a lock fight, aborts the
whole batch rather than being replayed. The first sits at rank ~4,741 in id order, INSIDE
the first page, so without the bound this job heals nothing at all. Those rows are MEANT to
have no headline: the ingest path NULLs an out-of-range `area_m2` the same way
(`scraper.db.sane_listing_numerics`), and since W17 `derive_headline_area` declines the
measure outright rather than stamping a basis for a value the row will not hold. The bound
is `scraper.area.MAX_AREA_M2`, pinned to `scraper.db._NUMERIC_ABS_MAX["area_m2"]` by test —
never re-spelled here. Both counts are reported, per portal, in dry-run AND write modes.

**It writes NO `listing_snapshots` row**, and for most of the population nothing ever will.
`area_m2` is in the content hash of a `ScrapedListing`, so this is the sanctioned exception
rule 2 already recognises (the `backfill_idnes_areas` precedent): correcting OUR OWN
mis-parse of the SAME stored page is a data-quality fix, not a change the portal published.
What follows differs by portal, and the difference is worth stating:

  * idnes + bezrealitky hash the PARSED fields, so W17's parser change (not this heal)
    makes the next successful detail fetch compute a hash that differs from the row's
    latest snapshot: exactly ONE genuine snapshot per live row, spread over the normal
    cadence. 7,946 rows at most, and only the active ones.
  * sreality hashes the RAW payload (`scraper.hashing.content_hash(raw)`), which did not
    change — so no snapshot is ever appended for its 44,237 rows. The heal is the only
    write they get; later refetches re-derive the same value and rewrite the column
    silently. An inactive row of any portal is never refetched either.

Idempotent BY CONSTRUCTION, with no marker: a healed row fails the WHERE for ever after,
so the selection empties itself and a re-run is a no-op. `--after` resumes from a
`listings.id` cursor, which advances ONLY past a page that was actually written — a failed
page is re-read, never skipped. A pass that stops early logs `BACKFILL INCOMPLETE` with
that cursor.

Batched by id: each pass reads one keyset page of matching ids (5,000 by default) and
writes them in ONE `UPDATE ... WHERE id = ANY(...)` that re-asserts the whole predicate, so
a row a concurrent detail drain healed first is simply not rewritten. Every batch runs in
its own transaction with `SET LOCAL statement_timeout` + `lock_timeout` — on these
autocommit connections a `SET LOCAL` only binds inside a transaction — and replays through
the shared lock-retry rail, whose window is sized for the ~90 s of row locks the hourly
MF-yield recompute holds on `listings` twice an hour. This is DML, never DDL: no ACCESS
EXCLUSIVE, so it cannot head-block a writer. A WRITING pass waits for a `rebuild_%` gap
first, bounded, then proceeds anyway — the contention is I/O, never a lock conflict; a dry
run writes nothing and never waits.

`properties.area_m2` mirrors a SINGLETON property's child row, so every healed listing's
property is enqueued into `dirty_properties` (rule 20) and the `*/5` incremental
maintenance pass recomputes it. Nothing else derives from `area_m2` at write time.

Usage:  python -m scripts.backfill_land_headline_area --dry-run
        python -m scripts.backfill_land_headline_area --write
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from scraper import db
from scraper.area import LAND_CATEGORIES, MAX_AREA_M2
from scripts.backfill_support import execute_with_lock_retry, wait_for_rebuild_gap

LOG = logging.getLogger("backfill_land_headline_area")

# Rendered from the one vocabulary, never re-spelled: the categories whose headline area
# IS the parcel, and the first value the headline column cannot store.
_LAND_IN = ", ".join(f"'{c}'" for c in sorted(LAND_CATEGORIES))
_MISSING_HEADLINE = (
    f"category_main IN ({_LAND_IN}) AND (area_m2 IS NULL OR area_m2 <= 0) AND estate_area > 0"
)

# The one predicate, spelled once and shared by the count, the page read and the write —
# the write re-asserts it so a concurrently-healed row is never rewritten.
_PENDING = f"{_MISSING_HEADLINE} AND estate_area < {MAX_AREA_M2:.1f}"

# The rows the ceiling excludes: a parcel `numeric(7,1)` cannot hold. Counted and named,
# never silently dropped — and never written, because `SET area_m2 = estate_area` on one
# of them raises 22003 and aborts the batch.
_UNSTORABLE = f"{_MISSING_HEADLINE} AND estate_area >= {MAX_AREA_M2:.1f}"

_COUNT_BY_SOURCE_SQL = f"""
    SELECT source,
           count(*) FILTER (WHERE {_PENDING}) AS pending,
           count(*) FILTER (WHERE {_PENDING} AND is_active) AS pending_active,
           count(*) FILTER (WHERE {_UNSTORABLE}) AS unstorable,
           max(estate_area) FILTER (WHERE {_UNSTORABLE}) AS largest_unstorable
    FROM listings
    WHERE {_MISSING_HEADLINE}
    GROUP BY source ORDER BY pending DESC, source
"""

# Narrow on purpose: id, source and the property to enqueue. Naming no wide column keeps
# each page a cheap primary-key walk that detoasts nothing.
_SELECT_SQL = f"""
    SELECT id, source, property_id FROM listings
    WHERE id > %(after)s::bigint AND {_PENDING}
    ORDER BY id LIMIT %(page)s::int
"""

_UPDATE_SQL = f"""
    UPDATE listings SET area_m2 = estate_area, area_basis = 'plot'
    WHERE id = ANY(%(ids)s::bigint[]) AND {_PENDING}
"""

# A batch of 5,000 single-row updates on a hot table: generous enough to finish, tight
# enough that a wedged batch surfaces instead of parking a backend for ten minutes. The
# lock wait is bounded well under it, so losing a lock fight is a REPLAY (the shared rail's
# four attempts and 15 s × attempt backoff outlast the ~90 s MF-yield lock hold) rather
# than a statement_timeout that burns the whole budget first.
_BATCH_GUARDS: tuple[str, ...] = (
    "SET LOCAL statement_timeout = '120s'",
    "SET LOCAL lock_timeout = '30s'",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings healed this run. Default: all of them.")
    parser.add_argument("--batch-size", type=int, default=5000,
                        help="Rows per keyset page, and per UPDATE statement.")
    parser.add_argument("--after", type=int, default=0,
                        help="Resume from this listings.id cursor (exclusive).")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stop at a batch boundary and exit cleanly.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report what would change; write nothing (the default).")
    parser.add_argument("--write", dest="dry_run", action="store_false",
                        help="Actually write. Without it this script only reports.")
    parser.add_argument("--rebuild-wait-seconds", type=float, default=900.0,
                        help="How long a WRITING pass waits for a read-model rebuild to "
                             "finish before starting anyway. A dry run never waits.")
    parser.add_argument("--ignore-rebuild", action="store_true",
                        help="Start immediately, without waiting for a rebuild gap at all.")
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
    selected = healed = batches = 0
    cursor = args.after          # advanced ONLY past a page that was actually written
    exhausted = False

    try:
        with db.connect() as conn:
            # A dry run takes no lock and writes nothing, so it has nothing to be polite
            # about — waiting would only make the report arrive up to 15 minutes late.
            if not args.dry_run and not args.ignore_rebuild:
                wait_for_rebuild_gap(conn, args.rebuild_wait_seconds)

            with conn.cursor() as cur:
                cur.execute(_COUNT_BY_SOURCE_SQL)
                counts = cur.fetchall()
            for source, pending, pending_active, unstorable, largest in counts:
                LOG.info("BACKFILL pending %-14s %7d rows (%d active)",
                         source, pending, pending_active)
                if unstorable:
                    LOG.info("BACKFILL excluded %-13s %7d rows whose parcel exceeds "
                             "area_m2's numeric(7,1) ceiling (largest %s m2) — they are "
                             "MEANT to have no headline area", source, unstorable, largest)
            LOG.info("BACKFILL pending total=%d excluded_total=%d batch=%d after=%d dry_run=%s",
                     sum(int(r[1]) for r in counts), sum(int(r[3] or 0) for r in counts),
                     args.batch_size, args.after, args.dry_run)

            while True:
                page = args.batch_size
                if args.limit is not None:
                    remaining = args.limit - selected
                    if remaining <= 0:
                        break
                    page = min(page, remaining)
                with conn.cursor() as cur:
                    cur.execute(_SELECT_SQL, {"after": cursor, "page": page})
                    rows = cur.fetchall()
                if not rows:
                    exhausted = True
                    break

                ids = [int(r[0]) for r in rows]
                properties = sorted({int(r[2]) for r in rows if r[2] is not None})
                if args.verbose:
                    LOG.debug("BACKFILL page ids=%d..%d sources=%s",
                              ids[0], ids[-1], sorted({str(r[1]) for r in rows}))

                if not args.dry_run:
                    try:
                        written = execute_with_lock_retry(
                            conn, _UPDATE_SQL, {"ids": ids},
                            label="BACKFILL land headline area", setup=_BATCH_GUARDS,
                        )
                        # `properties.area_m2` mirrors a singleton's child row (rule 20).
                        if properties:
                            db.mark_properties_dirty(conn, properties)
                    except Exception:
                        # The cursor has NOT moved: this page is re-read on resume, never
                        # skipped. Name both ends so the operator can see what was left.
                        LOG.error("BACKFILL page ids=%d..%d FAILED; nothing past id=%d was "
                                  "written. Resume with --after %d", ids[0], ids[-1],
                                  cursor, cursor)
                        raise
                    healed += written
                else:
                    # The dry run counts what the write would attempt; it cannot see a row
                    # a concurrent drain heals between the read and a write that never runs.
                    healed += len(ids)

                cursor = ids[-1]
                selected += len(ids)
                batches += 1
                LOG.info("BACKFILL progress selected=%d healed=%d batches=%d cursor=%d",
                         selected, healed, batches, cursor)

                if len(rows) < page:
                    exhausted = True
                    break
                if args.max_seconds and time.monotonic() - start > args.max_seconds:
                    LOG.info("BACKFILL stopping: --max-seconds reached after=%d", cursor)
                    break
    finally:
        LOG.info("BACKFILL done selected=%d healed=%d batches=%d cursor=%d exhausted=%s "
                 "dry_run=%s", selected, healed, batches, cursor, exhausted, args.dry_run)
        if not exhausted:
            LOG.warning("BACKFILL INCOMPLETE — rows above id=%d were never healed; "
                        "resume with --after %d", cursor, cursor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
