"""One-off heal: re-derive the areas five portals truncated on a spaced thousands group.

Until W19, `ceskereality`, `realitymix`, `remax`, `maxima` and `bazos` each carried a
private copy of the naive area regex — it matched the FIRST bare digit run before an `m²`,
so the Czech thousands format every one of them renders ("5 870 m²", "1 063 m²" with a
no-break space) parsed from INSIDE the number and stored 870 / 63. Measured on production
2026-09-17:

    ceskereality   98,126 rows   17,207 carry the truncation fingerprint (stored area =
                                 title area mod 1000); of 19,088 land rows NOT ONE has an
                                 area_m2 of 1000 or more (max 999)
    realitymix     83,051 rows   13,164 fingerprints, 1,828 correct (its spec cells are
                                 unspaced, so only the TITLE fallback truncates)
    remax          13,797 rows   not fingerprintable from titles (they carry no spaced
                                 number) — but its spec cells render an NBSP thousands
                                 group ("Plocha parcely: 1 063 m²")
    maxima            540 rows   87 land rows, max 987 m² — the same signature
    bazos          ~12,400 rows  8.4 % of the newest 8,000 rows carry the exact
                                 fingerprint; its land rows show the same signature

`scraper.area.parse_area_text` fixes every future write. This fixes the rows already
stored — from the row's OWN page fields, without fetching or reading anything else.

**THE SUBSTRATE IS `listings.raw_json`, THE PARSER'S LATEST READING OF THE LIVE PAGE.**
Each of these parsers stores the detail page's spec cells verbatim under
`raw_json['params']` ("plocha pozemku" -> "5 870 m²") plus `raw_json['title']`; bazos, which
has no spec table, stores the ad title and keeps its body in `listings.description`. Those
are exactly the strings the naive regex mis-read, so re-reading them with the fixed grammar
IS the heal. Three properties fall out of the substrate rather than being engineered:

  * **No fetch, no object store, no R2.** An earlier cut re-parsed the archived body out of
    `portal_raw_payloads` + the bucket. It worked, and it was wrong in one decisive way: the
    payload writer holds a 7-day per-listing floor, so the archive lags the live row, and a
    gate that refused to apply a stale body skipped **89 % of the population** on the first
    production dry run (examined=3000, would change=85, body_stale=2672). `raw_json` cannot
    lag: it is rewritten by the same transaction that writes the areas.
  * **No staleness gate at all.** There is no older-page-versus-newer-row question to ask.
  * **The portal's own key precedence, spelled once.** Each parser exposes
    `areas_from_params(params, title=, category_main=)` (bazos: `areas_from_text`), the
    SAME function its `parse_detail` calls, returning `scraper.area.PortalAreas`. The heal
    calls it with the stored params. A second copy of a key order is the same defect as a
    second copy of the number grammar, one level up (rule 21).

**Selection is complete by construction, and carries no `raw_json` predicate.** A
truncation keeps the last three digits, so the stored value is ALWAYS under 1000 — unless
those digits are "000" ("10 000 m²" → 0), in which case `scraper.db.sane_listing_numerics`
NULLed the placeholder at the write boundary and the row carries no area at all. Hence
three arms:

    any of area_m2 / estate_area / usable_area / garden_area is non-NULL and < 1000
    OR all four are NULL

A row whose every stored area is >= 1000 cannot be a truncation and is skipped. That is a
CAN-BE-WRONG set, not a will-change set: it matches ~99 % of these portals' rows (a 60 m²
flat is legitimately under 1000) while only a minority actually move — the per-source
report says both. `raw_json` is PROJECTED per page (500 rows at a time, which is a fine
read) and never appears in a WHERE: a predicate over it would detoast every candidate row,
the cost that killed the first live dispatch of `backfill_mmreality_areas` on the cluster's
120 s statement_timeout.

**The page read is per source, and it arms its own statement timeout.** The paging SELECT
measured 40.5 s and the count 13.6 s against that same 120 s default, so `db.connect()` is
followed by `backfill_support._STATEMENT_TIMEOUT_SQL` (600 s) exactly as the sibling
backfills do, and the sources are walked ONE AT A TIME with the keyset on `id`, so a page
never pays for the other portals' rows.

**It writes NO `listing_snapshots` row.** The area columns ARE in a `ScrapedListing`'s
content hash, so this is the sanctioned exception rule 2 already recognises (the
`backfill_idnes_areas` / `backfill_land_headline_area` precedent): correcting OUR OWN
mis-parse of the SAME page we already read is a data-quality fix, not a change the portal
published. Each healed LIVE row's next successful detail fetch computes a hash differing
from its latest snapshot and appends exactly ONE genuine snapshot, spread over the normal
cadence; an inactive row is never refetched, so the heal is the only write it gets. Price
is never touched.

**It never blanks a stored value.** A column the re-derive cannot produce is not a change
and is not written: a shape drift must never turn an area into NULL. One consequence is
deliberate — a parcel beyond `area_m2`'s `numeric(7,1)` ceiling leaves the row's existing
headline exactly where it was (the one rule declines to stamp a basis for a value the
column cannot hold), while `estate_area` (numeric(9,1)) is healed beside it. The live
simulation over 6,000 rows agrees: every change grows or fills a value, none shrinks one.

**W17's land heal is subsumed, not repeated.** `backfill_land_headline_area` copied
`estate_area` into `area_m2` for land rows — and on these portals `estate_area` was itself
truncated, so the copy propagated the wrong number. The re-derive fixes both columns from
the same fields in the same statement; there is no separate step.

Idempotent: every value is rounded to its column's scale (all five area columns are
`numeric(*,1)`) BEFORE it is compared and before it is written, HALF-UP like Postgres's own
numeric rounding, so a cell reading "86,19 m²" does not rewrite 86.2 on every pass. A healed
row may stay in the selection — a 60 m² flat's `usable_area` is legitimately under 1000 —
which is why `--after` exists rather than a `raw_json` marker; stamping one would rewrite
the widest TOAST column on the hottest table for every row examined.

Batched by `listings.id`: each page is a keyset read, and ONE statement per page updates
the rows and enqueues their properties into `dirty_properties` in the same CTE — inside a
transaction with `SET LOCAL statement_timeout` + `lock_timeout`, replayed through the
shared lock-retry rail, so the enqueue can never survive a rolled-back write (rule 20).
DML, never DDL: no ACCESS EXCLUSIVE, so it cannot head-block a writer. A WRITING pass
waits for a `rebuild_%` gap first, bounded, then proceeds anyway.

Usage:  python -m scripts.backfill_area_spaced_thousands --dry-run
        python -m scripts.backfill_area_spaced_thousands --write --sources ceskereality
Required: SUPABASE_DB_URL. Nothing else — no R2, no network.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from scraper import db
from scraper.area import PortalAreas
from scripts.backfill_support import (
    _STATEMENT_TIMEOUT_SQL,
    execute_with_lock_retry,
    wait_for_rebuild_gap,
)

LOG = logging.getLogger("backfill_area_spaced_thousands")

# The five portals that carried the naive copy. idnes was healed in 2026-08 by
# `backfill_idnes_areas`; sreality / bezrealitky / mmreality take typed numbers off a JSON
# API and never ran the regex at all.
DEFAULT_SOURCES: tuple[str, ...] = (
    "ceskereality", "realitymix", "remax", "maxima", "bazos")

# bazos publishes no spec table: its areas live in the ad's free text, so its page fields
# are the title plus `listings.description` rather than `raw_json['params']`.
TEXT_SOURCES: frozenset[str] = frozenset({"bazos"})

# The area columns this job owns. `area_basis` rides along because it is the provenance
# stamp for `area_m2` and must never describe a value the row no longer holds.
_AREA_COLS: tuple[str, ...] = ("area_m2", "usable_area", "estate_area", "garden_area")

# Every area column is `numeric(*,1)` — area_m2 (7,1), the other three (9,1) — so the
# database rounds on the way in. Round HERE too, or a cell reading "86,19 m2" compares its
# 86.19 against the stored 86.2 and rewrites the row on every single pass.
_AREA_SCALE = 1

_SUSPECT = """(
        (area_m2 IS NOT NULL AND area_m2 < 1000)
     OR (usable_area IS NOT NULL AND usable_area < 1000)
     OR (estate_area IS NOT NULL AND estate_area < 1000)
     OR (garden_area IS NOT NULL AND garden_area < 1000)
     OR (area_m2 IS NULL AND usable_area IS NULL
         AND estate_area IS NULL AND garden_area IS NULL)
    )"""

_COUNT_BY_SOURCE_SQL = f"""
    SELECT source,
           count(*) AS total,
           count(*) FILTER (WHERE {_SUSPECT}) AS suspects,
           count(*) FILTER (WHERE {_SUSPECT} AND is_active) AS suspects_active
    FROM listings
    WHERE source = ANY(%(sources)s::text[])
    GROUP BY source ORDER BY suspects DESC, source
"""

# ONE source per read, keyset on id: the page never pays for the other portals' rows. The
# page fields ARE projected (that is the substrate) but never predicated on — `_SUSPECT`
# touches only the four narrow numeric columns, so the planner never detoasts a row it is
# about to reject.
_SELECT_SQL = f"""
    SELECT id, property_id, category_main,
           area_m2, usable_area, estate_area, garden_area, area_basis,
           raw_json->'params' AS params, raw_json->>'title' AS title,
           NULL::text AS ad_text
    FROM listings
    WHERE source = %(source)s
      AND id > %(after)s::bigint
      AND {_SUSPECT}
    ORDER BY id LIMIT %(page)s::int
"""

# bazos's half: the ad body instead of a spec table. Spelled as its own statement rather
# than projected for everyone, so four portals never detoast `description` for nothing.
_SELECT_TEXT_SQL = f"""
    SELECT id, property_id, category_main,
           area_m2, usable_area, estate_area, garden_area, area_basis,
           NULL::jsonb AS params, raw_json->>'title' AS title,
           description AS ad_text
    FROM listings
    WHERE source = %(source)s
      AND id > %(after)s::bigint
      AND {_SUSPECT}
    ORDER BY id LIMIT %(page)s::int
"""

# ONE statement per page: the areas and the property enqueue in a single CTE, so the dirty
# mark cannot survive a rolled-back write (rule 20 — `db.mark_properties_dirty` documents
# that it nests in the caller's transaction, and the lock-retry rail owns this one).
# Every column is set from the SAME re-derive, with a value the re-derive could not produce
# carried over from the row itself — this never writes NULL over a stored area. Only rows
# where at least one area NUMBER moved are in the arrays at all.
_UPDATE_SQL = """
    WITH fresh AS (
        SELECT * FROM unnest(
            %(ids)s::bigint[],
            %(area_m2)s::double precision[],
            %(area_basis)s::text[],
            %(usable_area)s::double precision[],
            %(estate_area)s::double precision[],
            %(garden_area)s::double precision[]
        ) AS t(id, area_m2, area_basis, usable_area, estate_area, garden_area)
    ), updated AS (
        UPDATE listings AS l
        SET area_m2 = u.area_m2,
            area_basis = u.area_basis,
            usable_area = u.usable_area,
            estate_area = u.estate_area,
            garden_area = u.garden_area
        FROM fresh AS u
        WHERE l.id = u.id
        RETURNING l.property_id
    )
    INSERT INTO dirty_properties (property_id)
    SELECT DISTINCT property_id FROM updated WHERE property_id IS NOT NULL
    ON CONFLICT (property_id) DO UPDATE SET marked_at = now()
"""

_BATCH_GUARDS: tuple[str, ...] = (
    "SET LOCAL statement_timeout = '120s'",
    "SET LOCAL lock_timeout = '30s'",
)


def _areas_for(source: str, *, params: Any, title: str | None,
               ad_text: str | None, category_main: str | None) -> PortalAreas | None:
    """The portal's OWN area derivation over the row's stored page fields.

    Imported lazily and called, never re-implemented: each of these is the same function
    the portal's `parse_detail` runs on a live page. None means the row carries no page
    fields to read at all.
    """
    if source in TEXT_SOURCES:
        from scraper.bazos_parser import ad_haystack, areas_from_text

        if not title and not ad_text:
            return None
        return areas_from_text(ad_haystack(title, ad_text),
                               category_main=category_main)

    cells = {str(k): v for k, v in params.items()
             if isinstance(v, str)} if isinstance(params, dict) else {}
    if not cells and not title:
        return None
    if source == "ceskereality":
        from scraper.ceskereality_parser import areas_from_params
    elif source == "realitymix":
        from scraper.realitymix_parser import areas_from_params
    elif source == "remax":
        from scraper.remax_parser import areas_from_params
    elif source == "maxima":
        from scraper.maxima_parser import areas_from_params
    else:
        raise ValueError(f"no area derivation wired for source {source!r}")
    return areas_from_params(cells, title=title, category_main=category_main)


def _at_column_scale(value: Any) -> float | None:
    """A float the `numeric(*,1)` column can hold exactly — what the row will read back.

    HALF-UP, like Postgres's own numeric rounding, not `round()`'s banker's rounding: on an
    exact .x5 area the two disagree (86.25 -> 86.3 there, 86.2 here), and a heal that wrote
    a value the column then rounds differently would re-diff it on the next pass.
    """
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(
        Decimal(f"1e-{_AREA_SCALE}"), rounding=ROUND_HALF_UP))


def _merged(areas: PortalAreas, stored: dict[str, Any]) -> dict[str, Any]:
    """The five values this job would write, at column scale, never blanking a row.

    `sane_listing_numerics` is the SAME function the ingest path runs, so a parcel the
    column cannot hold becomes NULL here exactly as it would on a live detail write —
    never a 22003 that aborts the batch. A column the re-derive leaves NULL then falls back
    to what the row already holds: a shape drift must not delete an area, and a `plot` over
    `area_m2`'s ceiling must not blank the headline the row was carrying.
    """
    obj: dict[str, Any] = {col: getattr(areas, col) for col in _AREA_COLS}
    db.sane_listing_numerics(obj)
    fresh: dict[str, Any] = {col: _at_column_scale(obj[col]) for col in _AREA_COLS}
    produced_headline = fresh["area_m2"] is not None
    for col in _AREA_COLS:
        if fresh[col] is None:
            fresh[col] = _at_column_scale(stored[col])
    # The stamp follows the value: keep the row's own basis whenever its own headline is
    # what survives, so a declined measure never restamps a number it did not produce.
    fresh["area_basis"] = areas.area_basis if produced_headline else stored["area_basis"]
    return fresh


def _changed(stored: dict[str, Any], fresh: dict[str, Any]) -> bool:
    """True when an area NUMBER moved.

    A bare `area_basis` stamp is not this job's work (`scripts/backfill_area_basis.py`
    owns that) and must not churn a write of its own; and because `fresh` already carries
    the stored value wherever the re-derive produced none, a column the parser could not
    read is never a change either.
    """
    return any(_at_column_scale(stored[col]) != fresh[col] for col in _AREA_COLS)


def _report(counts: dict[str, Counter], *, dry_run: bool) -> None:
    verb = "would change" if dry_run else "changed"
    for source in sorted(counts):
        c = counts[source]
        LOG.info("BACKFILL %-14s examined=%d %s=%d unchanged=%d no_fields=%d",
                 source, c["examined"], verb, c["changed"], c["unchanged"],
                 c["no_fields"])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                        help="Comma-separated portals to heal, walked one at a time.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings EXAMINED this run. Default: all of them.")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Rows per keyset page, and per UPDATE statement.")
    parser.add_argument("--after", type=int, default=0,
                        help="Resume from this listings.id cursor (exclusive). Per source, "
                             "so pass it with a single --sources.")
    parser.add_argument("--max-seconds", type=float, default=9000.0,
                        help="Wall-clock budget; stop at a page boundary and exit cleanly. "
                             "Defaults under the runner's job timeout.")
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
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in DEFAULT_SOURCES]
    if unknown:
        print(f"ERROR: no area derivation wired for {unknown}.", file=sys.stderr)
        return 2

    start = time.monotonic()
    counts: dict[str, Counter] = {s: Counter() for s in sources}
    examined = healed = pages = 0
    # Per source, advanced ONLY past a page that was actually written.
    cursors: dict[str, int] = {s: args.after for s in sources}
    done: set[str] = set()

    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                # The paging SELECT measured 40.5 s and the count 13.6 s against the
                # cluster's 120 s default — too close to it to run unguarded.
                cur.execute(_STATEMENT_TIMEOUT_SQL)

            if not args.dry_run and not args.ignore_rebuild:
                wait_for_rebuild_gap(conn, args.rebuild_wait_seconds)

            with conn.cursor() as cur:
                cur.execute(_COUNT_BY_SOURCE_SQL, {"sources": sources})
                for source, total, suspects, suspects_active in cur.fetchall():
                    LOG.info("BACKFILL pending %-14s %7d suspects of %d rows (%d active)",
                             source, suspects, total, suspects_active)
            LOG.info("BACKFILL 'suspects' is the CAN-BE-WRONG set, not the will-change "
                     "set: it matches ~99%% of these portals' rows (a 60 m2 flat is "
                     "legitimately under 1000) while a minority actually move. The "
                     "per-source report below counts both.")

            budget_spent = False
            for source in sources:
                if budget_spent:
                    break
                select_sql = (_SELECT_TEXT_SQL if source in TEXT_SOURCES else _SELECT_SQL)
                while True:
                    page = args.batch_size
                    if args.limit is not None:
                        remaining = args.limit - examined
                        if remaining <= 0:
                            budget_spent = True
                            break
                        page = min(page, remaining)

                    with conn.cursor() as cur:
                        cur.execute(select_sql, {"source": source,
                                                 "after": cursors[source], "page": page})
                        rows = cur.fetchall()
                    if not rows:
                        done.add(source)
                        break

                    ids = [int(r[0]) for r in rows]
                    updates: list[tuple[int, dict[str, Any]]] = []
                    counter = counts[source]
                    for (lid, _prop_id, category_main, *rest) in rows:
                        area_values, (params, title, ad_text) = rest[:5], rest[5:]
                        stored = dict(zip((*_AREA_COLS, "area_basis"), area_values))
                        counter["examined"] += 1
                        areas = _areas_for(source, params=params, title=title,
                                           ad_text=ad_text, category_main=category_main)
                        if areas is None:
                            counter["no_fields"] += 1
                            continue
                        fresh = _merged(areas, stored)
                        if not _changed(stored, fresh):
                            counter["unchanged"] += 1
                            continue
                        counter["changed"] += 1
                        if args.verbose:
                            LOG.debug("BACKFILL heal id=%s source=%s %s -> %s",
                                      lid, source, stored, fresh)
                        updates.append((int(lid), fresh))

                    if updates and not args.dry_run:
                        params_out: dict[str, Any] = {"ids": [i for i, _ in updates]}
                        for col in (*_AREA_COLS, "area_basis"):
                            params_out[col] = [f[col] for _, f in updates]
                        try:
                            # The rail's rowcount is the dirty-enqueue arm's; the heal
                            # count is the array it was handed.
                            enqueued = execute_with_lock_retry(
                                conn, _UPDATE_SQL, params_out,
                                label="BACKFILL area spaced thousands",
                                setup=_BATCH_GUARDS,
                            )
                        except Exception:
                            LOG.error("BACKFILL source=%s page ids=%d..%d FAILED; nothing "
                                      "past id=%d was written. Resume with --sources %s "
                                      "--after %d", source, ids[0], ids[-1],
                                      cursors[source], source, cursors[source])
                            raise
                        healed += len(updates)
                        LOG.debug("BACKFILL enqueued %d dirty properties", enqueued)
                    elif updates:
                        healed += len(updates)

                    cursors[source] = ids[-1]
                    examined += len(rows)
                    pages += 1
                    LOG.info("BACKFILL progress source=%s examined=%d healed=%d pages=%d "
                             "cursor=%d", source, examined, healed, pages,
                             cursors[source])

                    if len(rows) < page:
                        done.add(source)
                        break
                    if args.max_seconds and time.monotonic() - start > args.max_seconds:
                        LOG.info("BACKFILL stopping: --max-seconds reached source=%s "
                                 "after=%d", source, cursors[source])
                        budget_spent = True
                        break
    finally:
        _report(counts, dry_run=args.dry_run)
        LOG.info("BACKFILL done examined=%d healed=%d pages=%d dry_run=%s",
                 examined, healed, pages, args.dry_run)
        for source in sources:
            if source in done:
                continue
            LOG.warning("BACKFILL INCOMPLETE source=%s — rows above id=%d were never "
                        "examined; resume with --sources %s --after %d",
                        source, cursors[source], source, cursors[source])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
