"""One-off heal: re-parse the areas five portals truncated on a spaced thousands group.

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
                                 number) — but the archived capture proves its spec cells
                                 DO ("Plocha parcely: 1 063 m²", NBSP)
    maxima            540 rows   87 land rows, max 987 m² — the same signature
    bazos          ~12,400 rows  8.4 % of the newest 8,000 rows carry the exact
                                 fingerprint; its land rows show the same signature

`scraper.area.parse_area_text` fixes every future write. This fixes the rows already
stored, by re-parsing each listing's own archived detail body with the fixed parser —
the same page, read correctly — and writing back only the area columns.

**THE SUBSTRATE IS `portal_raw_payloads`, NOT `portal_raw_pages`.** The
`backfill_idnes_areas` precedent read the latest-wins staging table; since migration 382
the append-on-change archive is where a listing's body actually lives, and since 406 the
bytes are in R2 with the metadata row in Postgres. So the read is: latest successful
`detail` payload per (source, source_id_native) → `location_data.page_readers.load_bodies`
→ `payloads.decode_body`, exactly the path the hourly claim lane takes. A spilled row with
no store configured is an ERROR (`IntakeRefused`), never a skipped page: mining only the
database-resident rows would report coverage over a corpus that is almost entirely in the
bucket. One bad object costs one listing, never the batch.

**THE ARCHIVE CAN LAG THE LIVE ROW, so a stale body is skipped, not applied.** The payload
writer holds a per-listing TIME FLOOR (`LOCATION_PAYLOAD_MIN_APPEND_INTERVAL_DAYS`, 7 by
default): a body that CHANGED inside that window is discarded, so ~3.2 % of rows (≈6k)
have a `listing_snapshots` row newer than their newest archived body. Re-parsing one of
those would revert a seller's edit to a week-old page. Any listing whose newest snapshot
post-dates the body's `last_observed_at` is therefore counted `body_stale` and left alone.

**Selection is complete by construction, and touches no wide column.** A truncation keeps
the last three digits, so the stored value is ALWAYS under 1000 — unless those digits are
"000" ("10 000 m²" → 0), in which case `scraper.db.sane_listing_numerics` NULLed the
placeholder at the write boundary and the row carries no area at all. Hence three arms:

    any of area_m2 / estate_area / usable_area / garden_area is non-NULL and < 1000
    OR all four are NULL

A row whose every stored area is >= 1000 cannot be a truncation and is skipped. That is a
CAN-BE-WRONG set, not a will-change set: it matches ~99 % of these portals' rows (a 60 m²
flat is legitimately under 1000) while only ~18 % actually move — the per-source report
says both. The `raw_json->>'title'` spaced-number probe the idnes backfill used is
deliberately NOT an arm: it adds nothing the third arm does not already cover, and a
predicate over `raw_json` detoasts every candidate row — the cost that killed the first
live dispatch of `backfill_mmreality_areas` on the cluster's 120 s statement_timeout.

**The page read is per source, and it arms its own statement timeout.** The paging SELECT
measured 40.5 s for the first page and the count 13.6 s against the cluster's 120 s
default — close enough to it that a slower day is a cancelled run, so `db.connect()` is
followed by `backfill_support._STATEMENT_TIMEOUT_SQL` (600 s) exactly as the sibling
backfills do. Sources are then walked ONE AT A TIME with the keyset on `id`, so a page
never pays for the other portals' rows.

**It writes NO `listing_snapshots` row.** The area columns ARE in a `ScrapedListing`'s
content hash, so this is the sanctioned exception rule 2 already recognises (the
`backfill_idnes_areas` / `backfill_land_headline_area` precedent): correcting OUR OWN
mis-parse of the SAME stored page is a data-quality fix, not a change the portal
published. What follows is bounded and correct — each healed LIVE row's next successful
detail fetch computes a hash differing from its latest snapshot and appends exactly ONE
genuine snapshot, spread over the normal cadence. An inactive row is never refetched, so
the heal is the only write it gets. Price is never touched: a staged body can lag the live
row, and a price discrepancy is not this job's call.

**It never blanks a stored value.** A column the re-parse cannot produce is not a change
and is not written: a parser shape drift must never turn an area into NULL. One
consequence is deliberate — a parcel beyond `area_m2`'s `numeric(7,1)` ceiling leaves the
row's existing headline exactly where it was (the one rule declines to stamp a basis for a
value the column cannot hold), while `estate_area` (numeric(9,1)) is healed beside it.

**W17's land heal is subsumed, not repeated.** `backfill_land_headline_area` copied
`estate_area` into `area_m2` for land rows — and on these portals `estate_area` was itself
truncated, so the copy propagated the wrong number. Re-parsing fixes both columns from the
same page in the same statement; there is no separate step.

Idempotent: every value is rounded to its column's scale (all five area columns are
`numeric(*,1)`) BEFORE it is compared and before it is written, so a page reading
"86,19 m²" does not rewrite 86.2 as 86.19 on every pass. A healed row may stay in the
selection — a 60 m² flat's `usable_area` is legitimately under 1000 — which is why
`--after` exists rather than a `raw_json` marker; stamping one would rewrite the widest
TOAST column on the hottest table for every row examined.

Batched by `listings.id`: each page is a keyset read, and ONE statement per page updates
the rows and enqueues their properties into `dirty_properties` in the same CTE — inside a
transaction with `SET LOCAL statement_timeout` + `lock_timeout`, replayed through the
shared lock-retry rail, so the enqueue can never survive a rolled-back write (rule 20).
DML, never DDL: no ACCESS EXCLUSIVE, so it cannot head-block a writer. A WRITING pass
waits for a `rebuild_%` gap first, bounded, then proceeds anyway.

Usage:  python -m scripts.backfill_area_spaced_thousands --dry-run
        python -m scripts.backfill_area_spaced_thousands --write --sources ceskereality
Required: SUPABASE_DB_URL, plus the R2_* vars (the bodies live in the bucket).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from scraper import db
from scraper.scraped_listing import ScrapedListing
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

# The area columns this job owns. `area_basis` rides along because it is the provenance
# stamp for `area_m2` and must never describe a value the row no longer holds.
_AREA_COLS: tuple[str, ...] = ("area_m2", "usable_area", "estate_area", "garden_area")

# Every area column is `numeric(*,1)` — area_m2 (7,1), the other three (9,1) — so the
# database rounds on the way in. Round HERE too, or a page reading "86,19 m2" compares its
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

# ONE source per read, keyset on id: the page never pays for the other portals' rows.
# Narrow on purpose — a cheap primary-key walk that detoasts nothing. `raw_json` is NOT
# projected: the parser is handed the archived body, not our stored derivative.
_SELECT_SQL = f"""
    SELECT id, source, source_id_native, source_url, category_main, category_type,
           property_id, area_m2, usable_area, estate_area, garden_area, area_basis
    FROM listings
    WHERE source = %(source)s
      AND id > %(after)s::bigint
      AND source_id_native IS NOT NULL
      AND {_SUSPECT}
    ORDER BY id LIMIT %(page)s::int
"""

# The latest SUCCESSFUL detail body per listing, with the clock the staleness gate reads.
# `http_status IS NULL` ranks as successful exactly as `location_data.payloads` ranks it —
# rows written before migration 403 added the column carry no status and are not failures.
_PAYLOADS_SQL = """
    SELECT DISTINCT ON (p.source, p.source_id_native)
           p.source, p.source_id_native, p.id, p.last_observed_at
    FROM portal_raw_payloads p
    WHERE p.page_kind = 'detail'
      AND p.source = %(source)s
      AND p.source_id_native = ANY(%(natives)s::text[])
      AND (p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299)
    ORDER BY p.source, p.source_id_native, p.version_seq DESC NULLS LAST,
             p.last_observed_at DESC, p.id DESC
"""

# The newest genuine content change we recorded for each listing on the page. Keyed on the
# REKEYED identity (`listing_id`, migration 320) — the same key the live snapshot read uses
# since 333, whose index serves this — not the legacy `sreality_id`, which is NULL on whole
# portals since the identity refactor.
_SNAPSHOTS_SQL = """
    SELECT listing_id, max(scraped_at) FROM listing_snapshots
    WHERE listing_id = ANY(%(ids)s::bigint[])
    GROUP BY listing_id
"""

# ONE statement per page: the areas and the property enqueue in a single CTE, so the dirty
# mark cannot survive a rolled-back write (rule 20 — `db.mark_properties_dirty` documents
# that it nests in the caller's transaction, and the lock-retry rail owns this one).
# Every column is set from the SAME re-parse of the SAME body, with a value the re-parse
# could not produce carried over from the row itself — this never writes NULL over a
# stored area. Only rows where at least one area NUMBER moved are in the arrays at all.
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


def _parser_for(source: str) -> Callable[..., ScrapedListing]:
    """The portal's own `parse_detail`, imported lazily so one bad portal is one portal."""
    if source == "ceskereality":
        from scraper.ceskereality_parser import parse_detail
    elif source == "realitymix":
        from scraper.realitymix_parser import parse_detail
    elif source == "remax":
        from scraper.remax_parser import parse_detail
    elif source == "maxima":
        from scraper.maxima_parser import parse_detail
    elif source == "bazos":
        from scraper.bazos_parser import parse_detail
    else:
        raise ValueError(f"no detail parser wired for source {source!r}")
    return parse_detail


def _parse(source: str, html: str, *, source_url: str | None,
           category_main: str | None, category_type: str | None) -> ScrapedListing:
    """Re-parse one archived body. realitymix resolves its own category from the page."""
    parse_detail = _parser_for(source)
    url = source_url or ""
    if source == "realitymix":
        return parse_detail(html, source_url=url)
    return parse_detail(html, source_url=url, category_main=category_main,
                        category_type=category_type)


def _decode_html(body: bytes) -> str:
    """Bytes to text, the way `location_data.html_scope` does it — never raising."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", "replace")


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


def _reparsed_areas(listing: ScrapedListing, stored: dict[str, Any]) -> dict[str, Any]:
    """The five area values this job would write, at column scale, never blanking a row.

    `sane_listing_numerics` is the SAME function the ingest path runs, so a parcel the
    column cannot hold becomes NULL here exactly as it would on a live detail write —
    never a 22003 that aborts the batch. A column the re-parse leaves NULL then falls back
    to what the row already holds: a shape drift must not delete an area, and the `plot`
    over `area_m2`'s ceiling must not blank the headline the row was carrying.
    """
    obj: dict[str, Any] = {col: getattr(listing, col) for col in _AREA_COLS}
    db.sane_listing_numerics(obj)
    fresh: dict[str, Any] = {col: _at_column_scale(obj[col]) for col in _AREA_COLS}
    produced_headline = fresh["area_m2"] is not None
    for col in _AREA_COLS:
        if fresh[col] is None:
            fresh[col] = _at_column_scale(stored[col])
    # The stamp follows the value: keep the row's own basis whenever its own headline is
    # what survives, so a declined measure never restamps a number it did not produce.
    fresh["area_basis"] = listing.area_basis if produced_headline else stored["area_basis"]
    return fresh


def _changed(stored: dict[str, Any], fresh: dict[str, Any]) -> bool:
    """True when an area NUMBER moved.

    A bare `area_basis` stamp is not this job's work (`scripts/backfill_area_basis.py`
    owns that) and must not churn a write of its own; and because `fresh` already carries
    the stored value wherever the re-parse produced none, a column the parser could not
    read is never a change either.
    """
    return any(_at_column_scale(stored[col]) != fresh[col] for col in _AREA_COLS)


def _report(counts: dict[str, Counter], *, dry_run: bool) -> None:
    verb = "would change" if dry_run else "changed"
    for source in sorted(counts):
        c = counts[source]
        LOG.info("BACKFILL %-14s examined=%d %s=%d unchanged=%d body_missing=%d "
                 "body_stale=%d parse_errors=%d", source, c["examined"], verb,
                 c["changed"], c["unchanged"], c["body_missing"], c["body_stale"],
                 c["parse_errors"])


def _load_bodies(cur: Any, payload_ids: list[int], store: Any) -> dict[int, bytes]:
    from location_data.page_readers import load_bodies

    bodies, _from_r2 = load_bodies(cur, payload_ids, store=store)
    return bodies


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                        help="Comma-separated portals to heal, walked one at a time.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings EXAMINED this run. Default: all of them.")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Rows per keyset page — also the R2 fan-out and UPDATE width.")
    parser.add_argument("--after", type=int, default=0,
                        help="Resume from this listings.id cursor (exclusive). Per source, "
                             "so pass it with a single --sources.")
    parser.add_argument("--max-seconds", type=float, default=9000.0,
                        help="Wall-clock budget; stop at a page boundary and exit cleanly. "
                             "Defaults under the runner's job timeout, because the full "
                             "corpus is ~193k bodies and does not fit one run.")
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
        print(f"ERROR: no detail parser wired for {unknown}.", file=sys.stderr)
        return 2

    from location_data import payloads

    store = payloads.open_store()
    if store is None:
        # The bodies live in R2 (threshold 2 KB since migration 406): a run without a
        # store would find essentially every payload spilled and report coverage over
        # nothing. `load_bodies` raises on the first such row; say so up front instead.
        print("ERROR: R2 is not configured (R2_* env vars unset) — the archived bodies "
              "this heal re-parses live in the bucket.", file=sys.stderr)
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
                     "legitimately under 1000) while ~18%% actually move. The per-source "
                     "report below counts both.")

            budget_spent = False
            for source in sources:
                if budget_spent:
                    break
                while True:
                    page = args.batch_size
                    if args.limit is not None:
                        remaining = args.limit - examined
                        if remaining <= 0:
                            budget_spent = True
                            break
                        page = min(page, remaining)

                    with conn.cursor() as cur:
                        cur.execute(_SELECT_SQL, {"source": source,
                                                  "after": cursors[source], "page": page})
                        rows = cur.fetchall()
                    if not rows:
                        done.add(source)
                        break

                    natives = sorted({str(r[2]) for r in rows})
                    ids = [int(r[0]) for r in rows]
                    with conn.cursor() as cur:
                        cur.execute(_PAYLOADS_SQL,
                                    {"source": source, "natives": natives})
                        payload_rows = cur.fetchall()
                        payload_by_native = {str(n): (int(pid), seen)
                                             for _s, n, pid, seen in payload_rows}
                        cur.execute(_SNAPSHOTS_SQL, {"ids": ids})
                        newest_snapshot = {int(lid): at for lid, at in cur.fetchall()}
                        bodies = _load_bodies(
                            cur, sorted(pid for pid, _ in payload_by_native.values()),
                            store)

                    updates: list[tuple[int, dict[str, Any]]] = []
                    counter = counts[source]
                    for row in rows:
                        (lid, _source, native, url, cmain, ctype, _prop_id,
                         *area_values) = row
                        stored = dict(zip((*_AREA_COLS, "area_basis"), area_values))
                        counter["examined"] += 1
                        payload = payload_by_native.get(str(native))
                        body = bodies.get(payload[0]) if payload else None
                        if body is None:
                            counter["body_missing"] += 1
                            continue
                        snapshot_at = newest_snapshot.get(int(lid))
                        if snapshot_at is not None and payload[1] is not None \
                                and snapshot_at > payload[1]:
                            # The portal changed this listing after the newest body we
                            # archived (the writer's 7-day per-listing floor discards a
                            # changed body inside the window). Re-parsing it would revert
                            # the seller's edit to a week-old page.
                            counter["body_stale"] += 1
                            continue
                        try:
                            listing = _parse(source, _decode_html(body), source_url=url,
                                             category_main=cmain, category_type=ctype)
                        except Exception as exc:  # noqa: BLE001 - one bad page, not the run
                            LOG.warning("BACKFILL parse error id=%s source=%s: %s",
                                        lid, source, exc)
                            counter["parse_errors"] += 1
                            continue
                        fresh = _reparsed_areas(listing, stored)
                        if not _changed(stored, fresh):
                            counter["unchanged"] += 1
                            continue
                        counter["changed"] += 1
                        if args.verbose:
                            LOG.debug("BACKFILL heal id=%s source=%s %s -> %s",
                                      lid, source, stored, fresh)
                        updates.append((int(lid), fresh))

                    if updates and not args.dry_run:
                        params: dict[str, Any] = {"ids": [i for i, _ in updates]}
                        for col in (*_AREA_COLS, "area_basis"):
                            params[col] = [f[col] for _, f in updates]
                        try:
                            # The rail's rowcount is the dirty-enqueue arm's; the heal
                            # count is the array it was handed.
                            enqueued = execute_with_lock_retry(
                                conn, _UPDATE_SQL, params,
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
