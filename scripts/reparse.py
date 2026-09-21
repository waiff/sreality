"""ONE re-parse seam: replay a portal's CURRENT parser over the state we already stored.

A parser fix only ever reaches rows the scraper touches NEXT. This is the seam that carries
it back to the rows already in `listings` — for every portal, through the portal's own
`parse_detail` / `parse_advert` / `parse_listing`, never a second copy of its key order or
its grammar (rule 21). It replaces the one-off `backfill_*.py` family (~2 new ones a
quarter) and absorbs `scripts/reextract.py`'s three load-bearing mechanics: a declared
registry of what may be healed, the `--allow-snapshot-deferral` gate for a HASHED column,
and the import-time assertion that the registry still agrees with `_HASH_FIELDS`.

**THE SUBSTRATE IS DECLARED ONCE PER PORTAL, AS DATA** (`SUBSTRATE` below). Two kinds, and
the split is measured, not assumed:

  * `portal_raw_pages.html` (page_kind='detail') for the seven HTML portals — bazos,
    ceskereality, idnes, maxima, mmreality, realitymix, remax. UNIQUE(source,
    source_id_native, page_kind), upserted latest-wins in the SAME `write_details`
    transaction that writes the listings row, so it cannot lag the row it produced: the
    stored page IS the page the current columns were parsed from, which is why replaying it
    can never regress a price the portal has since changed. Coverage measured 2026-09-21:
    100 % of listings on all seven, INACTIVE rows included (bazos 153,040/153,040, idnes
    249,338/249,338, ceskereality 101,081, realitymix 85,369, remax 14,125, mmreality
    14,787, maxima 556).
  * `listings.raw_json` for sreality (the v1 estate object) and bezrealitky (the GraphQL
    advert). Neither stages a body at all — `portal_raw_pages` holds ZERO detail rows for
    both — so raw_json is not a preference there, it is the only substrate, and each has a
    public entry point that takes that object (`parser.parse_listing`,
    `bezrealitky_parser.parse_advert`).

`portal_raw_payloads` is NOT this seam's substrate and the two are easy to confuse: that one
is the append-on-change R2-backed archive with a 7-day per-listing floor, whose staleness
made an earlier heal skip 89 % of its population (examined=3000, body_stale=2672).

**raw_json is INSUFFICIENT on the HTML portals, and idnes proves it.** Its amenity booleans
are DOM icons; `raw_json['params']` stores `{label: text}`, and an icon-only cell has no
text, so it serialises as JSON null. Measured over the 3,000 newest active idnes byt rows:
`params` carries 'výtah' on 1,328, the text value is non-null on 0, `has_lift` is true on
1,328 and false on 0 — True survives only as key-presence and False is unrepresentable. A
raw_json re-derive there yields None for every one of them; the page yields the truth.

**A chosen set of columns, never "all of them".** `--fields` is required. The seam writes
exactly the columns named and reads the rest only to leave them alone, so healing
`has_lift` can never restate a price. Only `LISTING_COLUMNS` can be named, minus
`published_at` and `source_url` — both preserve-if-null at the ingest boundary because an
absent value there means "the parser could not read it", never "the portal withdrew it",
and a heal must not be the one path that clears them.

Every write obeys the standing heal rules (R9):

  * **No `listing_snapshots` row, ever.** The hashed columns ARE in a ScrapedListing's
    content hash, so this is the sanctioned rule-2 exception the backfill family recorded
    four times over: correcting OUR OWN reading of the SAME state we already stored is a
    data-quality fix, not a change the portal published. Each healed LIVE row's next
    successful detail fetch computes a hash differing from its latest snapshot and appends
    exactly ONE genuine snapshot, spread over the normal cadence — deferred, never skipped.
    An inactive row is never refetched, so the heal is the only write it gets, and its
    column and its last snapshot disagree from then on. `--allow-snapshot-deferral` is
    required for a hashed column so that is an explicit choice.
  * **Never blanks a stored value.** A column the re-derive cannot produce is not a change
    and is not written. A shape drift must never turn a stored fact into NULL — and on the
    page substrate that rule is what makes a partial parse failure harmless.
  * **Never touches `last_seen_at`** (rule 4): a replay is not a sighting.
  * **Enqueues `dirty_properties` in the SAME statement** (rule 20), so the mark cannot
    survive a rolled-back write and the rollup/read model catch up on the next pass.
  * **Idempotent by comparison, never by arithmetic.** Every value is rounded to its
    column's scale before it is compared AND before it is written, so a second pass reports
    `changed=0` rather than re-writing the same number.

Batched by `listings.id` (keyset, resumable with `--after`), one statement per page inside a
transaction with `SET LOCAL statement_timeout` + `lock_timeout`, replayed through
`scripts.backfill_support.execute_with_lock_retry`; a WRITING pass first waits for a
`rebuild_%` gap. DML, never DDL.

Dry-run is the DEFAULT and reports per-field would-change counts plus examples.

Required env: SUPABASE_DB_URL. Nothing else — no fetch, no R2, no network.

    python -m scripts.reparse --source idnes --fields has_lift,cellar
    python -m scripts.reparse --source ceskereality --fields area_m2,estate_area \
        --write --allow-snapshot-deferral
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from scraper import db
from scraper.db import LISTING_COLUMNS, _LISTING_COLUMN_PGTYPE
from scraper.scraped_listing import ScrapedListing, _HASH_FIELDS
from scripts.backfill_support import (
    _STATEMENT_TIMEOUT_SQL,
    execute_with_lock_retry,
    wait_for_rebuild_gap,
)

LOG = logging.getLogger("reparse")

PAGE = "page"
RAW_JSON = "raw_json"


@dataclass(frozen=True)
class PortalSubstrate:
    """Where a portal's stored state lives, and the entry point that reads it.

    `takes_category` says the entry point accepts the row's stored category as an
    argument. It is not a style choice: bazos, ceskereality and idnes declare those
    parameters without defaults. bazos and ceskereality treat them as a fallback behind
    the page's own breadcrumb; idnes passes them STRAIGHT THROUGH, so its two category
    columns come from the index walk and this seam cannot re-derive them. remax and maxima
    default them to None and read the page instead, so nothing is passed and their
    categories DO re-derive.
    """

    kind: str
    module: str
    entry: str
    takes_category: bool


SUBSTRATE: dict[str, PortalSubstrate] = {
    "bazos": PortalSubstrate(PAGE, "scraper.bazos_parser", "parse_detail", True),
    "ceskereality": PortalSubstrate(PAGE, "scraper.ceskereality_parser", "parse_detail", True),
    "idnes": PortalSubstrate(PAGE, "scraper.idnes_parser", "parse_detail", True),
    "maxima": PortalSubstrate(PAGE, "scraper.maxima_parser", "parse_detail", False),
    "mmreality": PortalSubstrate(PAGE, "scraper.mmreality_parser", "parse_detail", False),
    "realitymix": PortalSubstrate(PAGE, "scraper.realitymix_parser", "parse_detail", False),
    "remax": PortalSubstrate(PAGE, "scraper.remax_parser", "parse_detail", False),
    "bezrealitky": PortalSubstrate(RAW_JSON, "scraper.bezrealitky_parser", "parse_advert", False),
    "sreality": PortalSubstrate(RAW_JSON, "scraper.parser", "parse_listing", False),
}

# Both are preserve-if-null at the ingest boundary (scraper.db._PRESERVE_IF_NULL_COLUMNS):
# an absent value means "the parser could not read it", never "the portal withdrew it".
# Clearing or restating either is a deliberate act, never a heal's.
_NOT_HEALABLE: frozenset[str] = frozenset({"published_at", "source_url"})

HEALABLE: tuple[str, ...] = tuple(c for c in LISTING_COLUMNS if c not in _NOT_HEALABLE)

# DECLARED, then checked against the hash contract below. Writing one of these defers a
# snapshot to the row's next detail fetch, which `--allow-snapshot-deferral` makes explicit;
# `area_basis` is the one healable column outside the hash (a provenance stamp, migration
# 423), so it churns nothing. If a column joins _HASH_FIELDS later, this set stops agreeing
# and the module refuses to import rather than silently downgrading the guarantee.
HASHED_COLUMNS: frozenset[str] = frozenset({
    "category_main", "category_type", "price_czk", "price_unit", "area_m2", "disposition",
    "floor", "total_floors", "has_balcony", "has_parking", "has_lift", "building_type",
    "condition", "energy_rating", "estate_area", "usable_area", "garden_area",
    "category_sub_cb", "subtype", "furnished", "terrace", "cellar", "garage",
    "parking_lots", "ownership", "description",
})

_expected_hashed = frozenset(HEALABLE) & frozenset(_HASH_FIELDS)
if HASHED_COLUMNS != _expected_hashed:
    raise RuntimeError(
        "reparse HASHED_COLUMNS drifted from _HASH_FIELDS: "
        f"missing={sorted(_expected_hashed - HASHED_COLUMNS)} "
        f"extra={sorted(HASHED_COLUMNS - _expected_hashed)}"
    )

# Every numeric LISTING_COLUMN is an area at numeric(*,1), so round HALF-UP (Postgres's own
# numeric rounding, not round()'s banker's) before comparing AND before writing — otherwise
# a cell reading "86,19 m2" re-diffs 86.2 on every pass and the heal never converges.
_NUMERIC_SCALE = 1

# The unnest cast per column type. `numeric` travels as double precision (the array psycopg
# builds from Python floats) and the UPDATE casts it into the column — the sibling heals'
# shape, and the values are already at column scale.
_ARRAY_TYPE: dict[str, str] = {
    "numeric": "double precision",
    "integer": "integer",
    "boolean": "boolean",
    "text": "text",
}

# ONE statement per page: the columns and the property enqueue in a single CTE, so the dirty
# mark cannot survive a rolled-back write (rule 20). `listing_snapshots` and `last_seen_at`
# appear nowhere in it, by construction rather than by guard.
_UPDATE_SQL_TEMPLATE = """
    WITH fresh AS (
        SELECT * FROM unnest(
            %(ids)s::bigint[]{casts}
        ) AS t(id{cols})
    ), updated AS (
        UPDATE listings AS l
        SET {sets}
        FROM fresh AS u
        WHERE l.id = u.id
        RETURNING l.property_id
    )
    INSERT INTO dirty_properties (property_id)
    SELECT DISTINCT property_id FROM updated WHERE property_id IS NOT NULL
    ON CONFLICT (property_id) DO UPDATE SET marked_at = now()
"""

# portal_raw_pages is UNIQUE(source, source_id_native, page_kind), so the detail body is one
# row — no LATERAL, no newest-of pick. Its own source_url is the URL the body was fetched
# from, which is what the parser derives its native id from.
_SELECT_PAGE_SQL_TEMPLATE = """
    SELECT l.id, l.property_id, l.category_main, l.category_type,
           coalesce(p.source_url, l.source_url) AS ref,
           p.html AS body, {stored}
    FROM listings l
    JOIN portal_raw_pages p
      ON p.source = l.source AND p.source_id_native = l.source_id_native
     AND p.page_kind = 'detail' AND p.html IS NOT NULL
    WHERE l.source = %(source)s AND l.id > %(after)s::bigint{gap}
    ORDER BY l.id LIMIT %(page)s::int
"""

_SELECT_RAW_SQL_TEMPLATE = """
    SELECT l.id, l.property_id, l.category_main, l.category_type,
           l.source_url AS ref,
           l.raw_json AS body, {stored}
    FROM listings l
    WHERE l.source = %(source)s AND l.id > %(after)s::bigint{gap}
      AND l.raw_json IS NOT NULL
    ORDER BY l.id LIMIT %(page)s::int
"""

_COUNT_SQL_TEMPLATE = """
    SELECT count(*) AS total, count(*) FILTER (WHERE is_active) AS active
    FROM listings WHERE source = %(source)s{gap}
"""

_BATCH_GUARDS: tuple[str, ...] = (
    "SET LOCAL statement_timeout = '120s'",
    "SET LOCAL lock_timeout = '30s'",
)


def _gap_sql(fields: tuple[str, ...], *, missing: bool, prefix: str = "") -> str:
    """The `--missing` narrowing: rows where every chosen column is still NULL.

    The cheap, complete arm for a column a portal has NEVER filled (the gap class). Without
    it the walk is the whole corpus and `_moved` decides, which is complete by construction
    but reads every stored body to do it.
    """
    if not missing:
        return ""
    return " AND (" + " AND ".join(f"{prefix}{c} IS NULL" for c in fields) + ")"


def _select_sql(source: str, fields: tuple[str, ...], *, missing: bool) -> str:
    template = (_SELECT_PAGE_SQL_TEMPLATE if SUBSTRATE[source].kind == PAGE
                else _SELECT_RAW_SQL_TEMPLATE)
    return template.format(
        stored=", ".join(f"l.{c}" for c in fields),
        gap=_gap_sql(fields, missing=missing, prefix="l."),
    )


def _count_sql(fields: tuple[str, ...], *, missing: bool) -> str:
    return _COUNT_SQL_TEMPLATE.format(gap=_gap_sql(fields, missing=missing))


def _update_sql(fields: tuple[str, ...]) -> str:
    casts = "".join(
        f",\n            %({c})s::{_ARRAY_TYPE[_LISTING_COLUMN_PGTYPE[c]]}[]" for c in fields)
    return _UPDATE_SQL_TEMPLATE.format(
        casts=casts,
        cols="".join(f", {c}" for c in fields),
        sets=",\n            ".join(f"{c} = u.{c}" for c in fields),
    )


def _derive(source: str, *, body: Any, ref: str | None,
            category_main: str | None, category_type: str | None) -> dict[str, Any]:
    """The portal's OWN parse entry point over the row's stored substrate.

    Imported lazily and CALLED, never re-implemented — the same function that portal's
    drain runs on a fresh fetch. Returns every LISTING_COLUMN the parse produced.
    """
    spec = SUBSTRATE[source]
    entry = getattr(importlib.import_module(spec.module), spec.entry)
    if spec.kind == RAW_JSON:
        produced = entry(body)
    else:
        kwargs: dict[str, Any] = {"source_url": ref or ""}
        if spec.takes_category:
            kwargs["category_main"] = category_main
            kwargs["category_type"] = category_type
        produced = entry(body, **kwargs)
    if isinstance(produced, ScrapedListing):
        return {c: getattr(produced, c) for c in LISTING_COLUMNS}
    return {c: produced.get(c) for c in LISTING_COLUMNS}


def _at_column_scale(column: str, value: Any) -> Any:
    """The value the column will read back — what a comparison has to be made against."""
    if value is None:
        return None
    pgtype = _LISTING_COLUMN_PGTYPE[column]
    if pgtype == "numeric":
        return float(Decimal(str(value)).quantize(
            Decimal(f"1e-{_NUMERIC_SCALE}"), rounding=ROUND_HALF_UP))
    if pgtype == "integer":
        return int(value)
    if pgtype == "boolean":
        return bool(value)
    text = str(value).strip()
    # An empty string is not a value the re-derive produced; treat it as absent so the
    # never-blank rule covers it too.
    return text or None


def _merged(produced: dict[str, Any], stored: dict[str, Any],
            fields: tuple[str, ...]) -> dict[str, Any]:
    """What this seam would write: the re-derive at column scale, never blanking a row.

    `sane_price_czk` + `sane_listing_numerics` are the SAME boundary functions
    `upsert_listing` runs, so a value the column cannot hold becomes absent here exactly as
    it would on a live detail write — never a 22003 that aborts the batch.
    """
    obj = dict(produced)
    obj["price_czk"] = db.sane_price_czk(obj.get("price_czk"))
    db.sane_listing_numerics(obj)
    fresh: dict[str, Any] = {}
    for column in fields:
        value = _at_column_scale(column, obj.get(column))
        fresh[column] = _at_column_scale(column, stored[column]) if value is None else value
    return fresh


def _moved(stored: dict[str, Any], fresh: dict[str, Any],
           fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(c for c in fields if _at_column_scale(c, stored[c]) != fresh[c])


def _fields_arg(raw: str) -> tuple[str, ...]:
    fields = tuple(dict.fromkeys(f.strip() for f in raw.split(",") if f.strip()))
    unknown = [f for f in fields if f not in HEALABLE]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"not re-derivable: {unknown}. Choose from: {', '.join(HEALABLE)}")
    if not fields:
        raise argparse.ArgumentTypeError("name at least one column")
    return fields


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=sorted(SUBSTRATE),
                        help="Portal to replay. Its substrate is declared, not chosen.")
    parser.add_argument("--fields", required=True, type=_fields_arg,
                        help="Comma-separated listings columns to re-derive. Required: the "
                             "seam writes exactly these and leaves every other column alone.")
    parser.add_argument("--missing", action="store_true",
                        help="Only rows where EVERY chosen column is NULL (the gap class).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings EXAMINED this run. Default: all of them.")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="Rows per keyset page, and per UPDATE statement. A page "
                             "substrate detoasts one body per row, so this is smaller than "
                             "the sibling heals' 500.")
    parser.add_argument("--after", type=int, default=0,
                        help="Resume from this listings.id cursor (exclusive).")
    parser.add_argument("--max-seconds", type=float, default=9000.0,
                        help="Wall-clock budget; stop at a page boundary and exit cleanly.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report what would change; write nothing (the default).")
    parser.add_argument("--write", dest="dry_run", action="store_false",
                        help="Actually write. Without it this only reports.")
    parser.add_argument(
        "--allow-snapshot-deferral", action="store_true",
        help="Required to WRITE a column in _HASH_FIELDS: acknowledges that setting it "
             "changes the content hash, so one snapshot per LIVE listing is appended on "
             "its next natural detail scrape (deferred, never skipped) and an inactive "
             "row never gets one — see the module docstring.")
    parser.add_argument("--rebuild-wait-seconds", type=float, default=900.0,
                        help="How long a WRITING pass waits for a read-model rebuild to "
                             "finish before starting anyway. A dry run never waits.")
    parser.add_argument("--examples", type=int, default=3,
                        help="Per-field before/after examples to print in the report.")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _report(source: str, fields: tuple[str, ...], counts: Counter,
            per_field: Counter, examples: dict[str, list[str]], *, dry_run: bool) -> None:
    verb = "would change" if dry_run else "changed"
    LOG.info("REPARSE %-14s examined=%d %s=%d unchanged=%d parse_errors=%d",
             source, counts["examined"], verb, counts["changed"], counts["unchanged"],
             counts["parse_errors"])
    for column in fields:
        LOG.info("REPARSE   %-16s %s=%d", column, verb, per_field[column])
        for line in examples.get(column, ()):
            LOG.info("REPARSE     %s", line)


def main() -> int:
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    fields: tuple[str, ...] = args.fields
    hashed = tuple(c for c in fields if c in HASHED_COLUMNS)
    if hashed and not args.dry_run and not args.allow_snapshot_deferral:
        print(f"ERROR: {list(hashed)} are in _HASH_FIELDS, so writing them changes the "
              "content hash and defers one snapshot per live listing to its next detail "
              "scrape. Re-run with --allow-snapshot-deferral to acknowledge.",
              file=sys.stderr)
        return 2

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    source: str = args.source
    select_sql = _select_sql(source, fields, missing=args.missing)
    update_sql = _update_sql(fields)
    counts: Counter = Counter()
    per_field: Counter = Counter()
    examples: dict[str, list[str]] = {c: [] for c in fields}
    cursor = args.after
    complete = False
    start = time.monotonic()

    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                # A page read projects one detail body per row; the cluster default is 120 s.
                cur.execute(_STATEMENT_TIMEOUT_SQL)
                cur.execute(_count_sql(fields, missing=args.missing), {"source": source})
                total, active = cur.fetchone()
            LOG.info("REPARSE %s substrate=%s candidates=%d (%d active) fields=%s",
                     source, SUBSTRATE[source].kind, total, active, ",".join(fields))

            if not args.dry_run:
                wait_for_rebuild_gap(conn, args.rebuild_wait_seconds)

            while True:
                page = args.batch_size
                if args.limit is not None:
                    remaining = args.limit - counts["examined"]
                    if remaining <= 0:
                        break
                    page = min(page, remaining)

                with conn.cursor() as cur:
                    cur.execute(select_sql,
                                {"source": source, "after": cursor, "page": page})
                    rows = cur.fetchall()
                if not rows:
                    complete = True
                    break

                updates: list[tuple[int, dict[str, Any]]] = []
                for row in rows:
                    listing_id, _property_id, cat_main, cat_type, ref, body = row[:6]
                    stored = dict(zip(fields, row[6:]))
                    counts["examined"] += 1
                    try:
                        produced = _derive(source, body=body, ref=ref,
                                           category_main=cat_main, category_type=cat_type)
                    except Exception as exc:  # noqa: BLE001 — one bad body, not the run
                        counts["parse_errors"] += 1
                        LOG.debug("REPARSE parse failed id=%s: %s", listing_id, exc)
                        continue
                    fresh = _merged(produced, stored, fields)
                    moved = _moved(stored, fresh, fields)
                    if not moved:
                        counts["unchanged"] += 1
                        continue
                    counts["changed"] += 1
                    for column in moved:
                        per_field[column] += 1
                        if len(examples[column]) < args.examples:
                            examples[column].append(
                                f"id={listing_id} {column}: {stored[column]!r} -> "
                                f"{fresh[column]!r}")
                    updates.append((int(listing_id), fresh))

                if updates and not args.dry_run:
                    params: dict[str, Any] = {"ids": [i for i, _ in updates]}
                    for column in fields:
                        params[column] = [f[column] for _, f in updates]
                    try:
                        enqueued = execute_with_lock_retry(
                            conn, update_sql, params, label="REPARSE " + source,
                            setup=_BATCH_GUARDS)
                    except Exception:
                        LOG.error("REPARSE source=%s page FAILED; nothing past id=%d was "
                                  "written. Resume with --after %d", source, cursor, cursor)
                        raise
                    LOG.debug("REPARSE enqueued %d dirty properties", enqueued)

                cursor = int(rows[-1][0])
                LOG.info("REPARSE progress source=%s examined=%d changed=%d cursor=%d",
                         source, counts["examined"], counts["changed"], cursor)
                if len(rows) < page:
                    complete = True
                    break
                if args.max_seconds and time.monotonic() - start > args.max_seconds:
                    LOG.info("REPARSE stopping: --max-seconds reached after=%d", cursor)
                    break
    finally:
        _report(source, fields, counts, per_field, examples, dry_run=args.dry_run)
        LOG.info("REPARSE done source=%s elapsed=%.1fs dry_run=%s",
                 source, time.monotonic() - start, args.dry_run)
        if not complete:
            LOG.warning("REPARSE INCOMPLETE source=%s — rows above id=%d were never "
                        "examined; resume with --after %d", source, cursor, cursor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
