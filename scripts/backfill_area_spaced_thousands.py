"""One-off heal: re-parse the areas four portals truncated on a spaced thousands group.

Until W19, `ceskereality`, `realitymix`, `remax` and `maxima` each carried a private
copy of the naive area regex — it matched the FIRST bare digit run before an `m²`, so
the Czech thousands format every one of them renders ("5 870 m²", "1 063 m²" with a
no-break space) parsed from INSIDE the number and stored 870 / 63. Measured on
production 2026-09-17:

    ceskereality   98,126 rows   17,207 carry the truncation fingerprint (stored area =
                                 title area mod 1000); of 19,088 land rows NOT ONE has an
                                 area_m2 of 1000 or more (max 999)
    realitymix     83,051 rows   13,164 fingerprints, 1,828 correct (its spec cells are
                                 unspaced, so only the TITLE fallback truncates)
    remax          13,797 rows   not fingerprintable from titles (they carry no spaced
                                 number) — but the archived capture proves its spec cells
                                 DO ("Plocha parcely: 1 063 m²", NBSP)
    maxima            540 rows   87 land rows, max 987 m² — the same signature

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

**Selection is complete by construction, and touches no wide column.** A truncation keeps
the last three digits, so the stored value is ALWAYS under 1000 — unless those digits are
"000" ("10 000 m²" → 0), in which case `scraper.db.sane_listing_numerics` NULLed the
placeholder at the write boundary and the row carries no area at all. Hence three arms:

    any of area_m2 / estate_area / usable_area / garden_area is non-NULL and < 1000
    OR all four are NULL

A row whose every stored area is >= 1000 cannot be a truncation and is skipped. The
`raw_json->>'title'` spaced-number probe the idnes backfill used is deliberately NOT an
arm here: it adds nothing the third arm does not already cover, and a predicate over
`raw_json` detoasts every candidate row — the cost that killed the first live dispatch of
`backfill_mmreality_areas` on the cluster's 120 s statement_timeout.

**It writes NO `listing_snapshots` row.** The area columns ARE in a `ScrapedListing`'s
content hash, so this is the sanctioned exception rule 2 already recognises (the
`backfill_idnes_areas` / `backfill_land_headline_area` precedent): correcting OUR OWN
mis-parse of the SAME stored page is a data-quality fix, not a change the portal
published. What follows is bounded and correct — each healed LIVE row's next successful
detail fetch computes a hash differing from its latest snapshot and appends exactly ONE
genuine snapshot, spread over the normal cadence. An inactive row is never refetched, so
the heal is the only write it gets. Price is never touched: a staged body can lag the live
row, and a price discrepancy is not this job's call.

**W17's land heal is subsumed, not repeated.** `backfill_land_headline_area` copied
`estate_area` into `area_m2` for land rows — and on these four portals `estate_area` was
itself truncated, so the copy propagated the wrong number. Re-parsing fixes both columns
from the same page in the same UPDATE; there is no separate step.

Idempotent: a healed row re-parses to the same values and writes nothing (it may stay in
the selection — a 60 m² flat's `usable_area` is legitimately under 1000 — which is why
`--after` exists rather than a `raw_json` marker; stamping one would rewrite the widest
TOAST column on the hottest table for every row examined). Batched by `listings.id`: each
page is a keyset read, one `UPDATE ... FROM unnest(...)` per page inside its own
transaction with `SET LOCAL statement_timeout` + `lock_timeout`, replayed through the
shared lock-retry rail. DML, never DDL: no ACCESS EXCLUSIVE, so it cannot head-block a
writer. A WRITING pass waits for a `rebuild_%` gap first, bounded, then proceeds anyway.

Healed listings are enqueued into `dirty_properties` (rule 20) so the singleton
`properties` area mirror is recomputed.

Usage:  python -m scripts.backfill_area_spaced_thousands --dry-run
        python -m scripts.backfill_area_spaced_thousands --write --max-seconds 3000
Required: SUPABASE_DB_URL, plus the R2_* vars (the bodies live in the bucket).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from typing import Any, Callable

from scraper import db
from scraper.scraped_listing import ScrapedListing
from scripts.backfill_support import execute_with_lock_retry, wait_for_rebuild_gap

LOG = logging.getLogger("backfill_area_spaced_thousands")

# The four portals that carried the naive copy. bazos held a fifth (folded onto the
# shared grammar in the same PR) but is NOT healed here: its area comes from free ad
# text, so the corpus was never measured and a re-parse would move numbers nobody has
# counted. idnes was healed in 2026-08 by `backfill_idnes_areas`.
DEFAULT_SOURCES: tuple[str, ...] = ("ceskereality", "realitymix", "remax", "maxima")

# The area columns this job owns. `area_basis` rides along because it is the provenance
# stamp for `area_m2` and must never describe a value the row no longer holds.
_AREA_COLS: tuple[str, ...] = ("area_m2", "usable_area", "estate_area", "garden_area")

# A truncation always leaves a value under 1000, or a "000" tail the write boundary
# NULLed. Spelled once, shared by the count and the page read. No `raw_json` here: a
# predicate over it detoasts every candidate row.
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

# Narrow on purpose — a cheap primary-key walk that detoasts nothing. `raw_json` is NOT
# projected: the parser is handed the archived body, not our stored derivative.
_SELECT_SQL = f"""
    SELECT id, source, source_id_native, source_url, category_main, category_type,
           property_id, area_m2, usable_area, estate_area, garden_area
    FROM listings
    WHERE id > %(after)s::bigint
      AND source = ANY(%(sources)s::text[])
      AND source_id_native IS NOT NULL
      AND {_SUSPECT}
    ORDER BY id LIMIT %(page)s::int
"""

# The latest SUCCESSFUL detail body per listing. `http_status IS NULL` ranks as successful
# exactly as `location_data.payloads` ranks it — rows written before migration 403 added
# the column carry no status and are not failures.
_PAYLOADS_SQL = """
    SELECT DISTINCT ON (p.source, p.source_id_native)
           p.source, p.source_id_native, p.id
    FROM portal_raw_payloads p
    WHERE p.page_kind = 'detail'
      AND p.source = ANY(%(sources)s::text[])
      AND p.source_id_native = ANY(%(natives)s::text[])
      AND (p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299)
    ORDER BY p.source, p.source_id_native, p.version_seq DESC NULLS LAST,
             p.last_observed_at DESC, p.id DESC
"""

# One statement per page. Every column is set from the SAME re-parse of the SAME body, so
# a column that did not change is rewritten with its own value; only rows where at least
# one AREA NUMBER moved are in the arrays at all.
_UPDATE_SQL = """
    UPDATE listings AS l
    SET area_m2 = u.area_m2,
        area_basis = u.area_basis,
        usable_area = u.usable_area,
        estate_area = u.estate_area,
        garden_area = u.garden_area
    FROM (
        SELECT * FROM unnest(
            %(ids)s::bigint[],
            %(area_m2)s::double precision[],
            %(area_basis)s::text[],
            %(usable_area)s::double precision[],
            %(estate_area)s::double precision[],
            %(garden_area)s::double precision[]
        ) AS t(id, area_m2, area_basis, usable_area, estate_area, garden_area)
    ) AS u
    WHERE l.id = u.id
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


def _reparsed_areas(listing: ScrapedListing) -> dict[str, Any]:
    """The five area values this job writes, after the shared write-boundary guards.

    `sane_listing_numerics` is the SAME function the ingest path runs, so a parcel the
    column cannot hold becomes NULL here exactly as it would on a live detail write —
    never a 22003 that aborts the batch.
    """
    obj: dict[str, Any] = {col: getattr(listing, col) for col in _AREA_COLS}
    db.sane_listing_numerics(obj)
    obj["area_basis"] = listing.area_basis if obj.get("area_m2") is not None else None
    return obj


def _num_eq(stored: Any, fresh: Any) -> bool:
    if stored is None or fresh is None:
        return stored is None and fresh is None
    return abs(float(stored) - float(fresh)) < 1e-6


def _changed(stored: tuple[Any, ...], fresh: dict[str, Any]) -> bool:
    """True when an area NUMBER moved. A bare `area_basis` stamp is not this job's work
    (`scripts/backfill_area_basis.py` owns that) and must not churn a write of its own."""
    return any(not _num_eq(value, fresh[col]) for col, value in zip(_AREA_COLS, stored))


def _report(counts: dict[str, Counter], *, dry_run: bool) -> None:
    verb = "would change" if dry_run else "changed"
    for source in sorted(counts):
        c = counts[source]
        LOG.info("BACKFILL %-14s suspects=%d %s=%d unchanged=%d body_missing=%d "
                 "parse_errors=%d", source, c["suspects"], verb, c["changed"],
                 c["unchanged"], c["body_missing"], c["parse_errors"])


def _load_bodies(cur: Any, payload_ids: list[int], store: Any) -> dict[int, bytes]:
    from location_data.page_readers import load_bodies

    bodies, _from_r2 = load_bodies(cur, payload_ids, store=store)
    return bodies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                        help="Comma-separated portals to heal.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings EXAMINED this run. Default: all of them.")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Rows per keyset page — also the R2 fan-out and UPDATE width.")
    parser.add_argument("--after", type=int, default=0,
                        help="Resume from this listings.id cursor (exclusive).")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stop at a page boundary and exit cleanly.")
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
    cursor = args.after          # advanced ONLY past a page that was actually written
    exhausted = False

    try:
        with db.connect() as conn:
            if not args.dry_run and not args.ignore_rebuild:
                wait_for_rebuild_gap(conn, args.rebuild_wait_seconds)

            with conn.cursor() as cur:
                cur.execute(_COUNT_BY_SOURCE_SQL, {"sources": sources})
                for source, total, suspects, suspects_active in cur.fetchall():
                    LOG.info("BACKFILL pending %-14s %7d suspects of %d rows (%d active)",
                             source, suspects, total, suspects_active)

            while True:
                page = args.batch_size
                if args.limit is not None:
                    remaining = args.limit - examined
                    if remaining <= 0:
                        break
                    page = min(page, remaining)

                with conn.cursor() as cur:
                    cur.execute(_SELECT_SQL,
                                {"after": cursor, "sources": sources, "page": page})
                    rows = cur.fetchall()
                if not rows:
                    exhausted = True
                    break

                natives = sorted({str(r[2]) for r in rows})
                with conn.cursor() as cur:
                    cur.execute(_PAYLOADS_SQL, {"sources": sources, "natives": natives})
                    payload_by_key = {(s, n): int(pid) for s, n, pid in cur.fetchall()}
                    bodies = _load_bodies(cur, sorted(payload_by_key.values()), store)

                updates: list[tuple[int, dict[str, Any]]] = []
                properties: set[int] = set()
                for (lid, source, native, url, cmain, ctype, prop_id,
                     *stored) in rows:
                    counter = counts[source]
                    counter["suspects"] += 1
                    body = bodies.get(payload_by_key.get((source, native), -1))
                    if body is None:
                        counter["body_missing"] += 1
                        continue
                    try:
                        listing = _parse(source, _decode_html(body), source_url=url,
                                         category_main=cmain, category_type=ctype)
                    except Exception as exc:  # noqa: BLE001 - one bad page, never the run
                        LOG.warning("BACKFILL parse error id=%s source=%s: %s",
                                    lid, source, exc)
                        counter["parse_errors"] += 1
                        continue
                    fresh = _reparsed_areas(listing)
                    if not _changed(tuple(stored), fresh):
                        counter["unchanged"] += 1
                        continue
                    counter["changed"] += 1
                    if args.verbose:
                        LOG.debug("BACKFILL heal id=%s source=%s %s -> %s", lid, source,
                                  dict(zip(_AREA_COLS, stored)), fresh)
                    updates.append((int(lid), fresh))
                    if prop_id is not None:
                        properties.add(int(prop_id))

                if updates and not args.dry_run:
                    params: dict[str, Any] = {"ids": [i for i, _ in updates]}
                    for col in (*_AREA_COLS, "area_basis"):
                        params[col] = [f[col] for _, f in updates]
                    try:
                        written = execute_with_lock_retry(
                            conn, _UPDATE_SQL, params,
                            label="BACKFILL area spaced thousands", setup=_BATCH_GUARDS,
                        )
                        if properties:
                            db.mark_properties_dirty(conn, sorted(properties))
                    except Exception:
                        LOG.error("BACKFILL page ids=%d..%d FAILED; nothing past id=%d "
                                  "was written. Resume with --after %d",
                                  int(rows[0][0]), int(rows[-1][0]), cursor, cursor)
                        raise
                    healed += written
                elif updates:
                    healed += len(updates)

                cursor = int(rows[-1][0])
                examined += len(rows)
                pages += 1
                LOG.info("BACKFILL progress examined=%d healed=%d pages=%d cursor=%d",
                         examined, healed, pages, cursor)

                if len(rows) < page:
                    exhausted = True
                    break
                if args.max_seconds and time.monotonic() - start > args.max_seconds:
                    LOG.info("BACKFILL stopping: --max-seconds reached after=%d", cursor)
                    break
    finally:
        _report(counts, dry_run=args.dry_run)
        LOG.info("BACKFILL done examined=%d healed=%d pages=%d cursor=%d exhausted=%s "
                 "dry_run=%s", examined, healed, pages, cursor, exhausted, args.dry_run)
        if not exhausted:
            LOG.warning("BACKFILL INCOMPLETE — rows above id=%d were never examined; "
                        "resume with --after %d", cursor, cursor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
