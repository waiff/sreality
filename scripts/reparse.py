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
    14,787, maxima 556). ONE measured exception to "the page the row was parsed from":
    bazos bodies staged before PR #1451 taught the client to recognise a deleted advert are
    the portal's gone-ad page, stamped 200 like any other (migration 519's population) —
    53 of the 300 oldest INACTIVE bazos rows carry it. They parse to all-None today (that
    page has no breadcrumb category segment), so never-blank is what contains them; a wave
    healing a column the gone page's own chrome CAN produce must select against them first.
  * `listings.raw_json` for sreality (the v1 estate object) and bezrealitky (the GraphQL
    advert). Neither stages a body at all — `portal_raw_pages` holds ZERO detail rows for
    both — so raw_json is not a preference there, it is the only substrate, and each has a
    public entry point that takes that object (`parser.parse_listing`,
    `bezrealitky_parser.parse_advert`). **The sreality arm does not reach that portal's
    oldest rows**: `parse_listing` derives the id from `hash_id`/`id`, and rows stored
    before the client unwrapped the estate object hold the WRAPPED response
    (`_embedded`/`items`/`locality`/`seo`…), which carries neither key and raises. Measured
    2026-09-21: 234 of 1,000 rows at id ≤ 1,000 carry a usable key, 609/1,001 at id ≈ 30k,
    597/1,001 at id ≈ 60k, 1,001/1,001 from id ≈ 90k up. Those rows are counted as
    `parse_errors`, the report WARNs on them, and no heal can reach them.

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
    column and its last snapshot disagree from then on. **sreality is the exception, and
    it is the biggest portal**: it hashes the RAW payload (`scraper.hashing.content_hash`,
    `scraper/main.py`), which a column heal never touches, so no snapshot is EVER appended
    there — column and history diverge permanently, live rows included. That is the
    asymmetry `docs/architecture.md` already records for the W17 land heal's 44,237 rows.
    `--allow-snapshot-deferral` is required for a hashed column so that is an explicit
    choice, and it states the consequence that portal will actually have.
  * **Never blanks a stored value.** A column the re-derive cannot produce is not a change
    and is not written. A shape drift must never turn a stored fact into NULL — and on the
    page substrate that rule is what makes a partial parse failure harmless.
  * **Never touches `last_seen_at`** (rule 4): a replay is not a sighting.
  * **Writes only rows that still hold what it read.** The re-derive runs in Python between
    the SELECT and the UPDATE while the drain keeps writing underneath, so every chosen
    column carries an `IS NOT DISTINCT FROM` predicate on the value this pass read: a row
    a fresh detail write changed in that window is left alone instead of being reverted to
    a stale reading. It matters here more than on the sibling heals because the seam writes
    no snapshot and no `last_seen_at` — a revert would leave no trace anywhere. A skipped
    row is simply healed by the next pass.
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
from scraper.db import (
    LISTING_COLUMNS,
    _LISTING_COLUMN_PGTYPE,
    _PRESERVE_IF_NULL_COLUMNS,
)
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

# Derived from the two definitions that already exist, never restated (rule 21): a column
# preserve-if-null at the ingest boundary is not a heal's to clear (an absent value there
# means "the parser could not read it", never "the portal withdrew it"), and a column in the
# content hash defers a snapshot to the row's next detail fetch, which
# `--allow-snapshot-deferral` makes explicit. `area_basis` is the one healable column
# outside the hash (a provenance stamp, migration 423), so healing it churns nothing. A
# column joining either definition extends the gate here on its own.
HEALABLE: tuple[str, ...] = tuple(
    c for c in LISTING_COLUMNS if c not in _PRESERVE_IF_NULL_COLUMNS)

HASHED_COLUMNS: frozenset[str] = frozenset(HEALABLE) & frozenset(_HASH_FIELDS)

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
# appear nowhere in it, by construction rather than by guard. The `was_` arrays carry what
# the SELECT read, so the write is a compare-and-set: a row another writer moved while this
# page was being parsed keeps that writer's value instead of being reverted.
_UPDATE_SQL_TEMPLATE = """
    WITH fresh AS (
        SELECT * FROM unnest(
            %(ids)s::bigint[]{casts}
        ) AS t(id{cols})
    ), updated AS (
        UPDATE listings AS l
        SET {sets}
        FROM fresh AS u
        WHERE l.id = u.id{cas}
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
    # The fresh values travel as the wire type psycopg builds from Python; the `was_` values
    # came straight out of the column and go back in its own type, so the compare is exact.
    casts = "".join(
        f",\n            %({c})s::{_ARRAY_TYPE[_LISTING_COLUMN_PGTYPE[c]]}[]" for c in fields)
    casts += "".join(
        f",\n            %(was_{c})s::{_LISTING_COLUMN_PGTYPE[c]}[]" for c in fields)
    return _UPDATE_SQL_TEMPLATE.format(
        casts=casts,
        cols="".join(f", {c}" for c in fields) + "".join(f", was_{c}" for c in fields),
        sets=",\n            ".join(f"{c} = u.{c}" for c in fields),
        cas="".join(
            f"\n          AND l.{c} IS NOT DISTINCT FROM u.was_{c}" for c in fields),
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
        help="Required to WRITE a column in _HASH_FIELDS: acknowledges what history does "
             "afterwards. On the eight portals hashing the PARSED fields, one snapshot per "
             "LIVE listing is appended on its next natural detail scrape (deferred, never "
             "skipped) and an inactive row never gets one. On sreality the hash is the RAW "
             "payload, which a column heal does not touch, so NO snapshot is ever appended "
             "and the column diverges from its history for good.")
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
    # A clean exit must not read as "this portal is done" when the substrate never reached
    # part of it — sreality's oldest rows hold the pre-unwrap payload and always raise.
    if counts["parse_errors"]:
        share = 100.0 * counts["parse_errors"] / max(counts["examined"], 1)
        LOG.warning("REPARSE %s: %d of %d rows examined (%.1f%%) could not be parsed from "
                    "their stored substrate and were NOT healed — re-run with --verbose to "
                    "see which. No heal can reach them.",
                    source, counts["parse_errors"], counts["examined"], share)


def main() -> int:
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    source: str = args.source
    fields: tuple[str, ...] = args.fields
    hashed = tuple(c for c in fields if c in HASHED_COLUMNS)
    if hashed and not args.dry_run and not args.allow_snapshot_deferral:
        # sreality is the one portal whose drain hashes the RAW payload
        # (scraper/main.py) instead of the parsed fields (scraper/db.write_details), so
        # its consequence is the opposite of every other portal's and has to be said.
        consequence = (
            "sreality hashes the RAW payload, which this heal does not touch, so NO "
            "snapshot is ever appended — the column and its history diverge permanently, "
            "live rows included."
            if source == "sreality" else
            "writing them changes the content hash and defers one snapshot per live "
            "listing to its next detail scrape; an inactive row never gets one."
        )
        print(f"ERROR: {list(hashed)} are in _HASH_FIELDS. On {source}, {consequence} "
              "Re-run with --allow-snapshot-deferral to acknowledge.", file=sys.stderr)
        return 2

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

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

                updates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
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
                    updates.append((int(listing_id), fresh, stored))

                if updates and not args.dry_run:
                    params: dict[str, Any] = {"ids": [i for i, _, _ in updates]}
                    for column in fields:
                        params[column] = [f[column] for _, f, _ in updates]
                        params[f"was_{column}"] = [s[column] for _, _, s in updates]
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
