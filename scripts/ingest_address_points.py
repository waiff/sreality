"""Ingest the ČÚZK RÚIAN "Adresní místa" address points into `address_points`.

Implements docs/design/street-coverage-ruian.md. ČÚZK publishes the denormalized
address points (street name + house number + obec code + coordinate in one row)
as PER-OBEC CSV zips — there is no single national denormalized dump (the national
`strukt_ADR` export is normalized FK-linkage tables with no names/coords). So we
iterate every obec (the ~6,250 obec ids in admin_boundaries, which ARE the RÚIAN
Kód obce):

    https://vdp.cuzk.gov.cz/vymenny_format/csv/{YYYYMMDD}_OB_{obec}_ADR.csv.zip

Each is Windows-1250, semicolon-separated, stable 19-column order:

    0 Kód ADM | 1 Kód obce | 2 Název obce | 3 Kód MOMC | 4 Název MOMC |
    5 Kód obvodu Prahy | 6 Název obvodu Prahy | 7 Kód části obce |
    8 Název části obce | 9 Kód ulice | 10 Název ulice | 11 Typ SO |
    12 Číslo domovní | 13 Číslo orientační | 14 Znak č.o. | 15 PSČ |
    16 Souřadnice Y | 17 Souřadnice X | 18 Platí Od

Only STREET-BEARING points are stored (single-street villages have no street and
can't resolve one). Coordinates are S-JTSK / Křovák (EPSG:5514), POSITIVE
magnitudes; transformed `ST_SetSRID(ST_MakePoint(-Y,-X), 5514)` -> 4326 at insert.
Full replace each run. Downloads run on a small thread pool; rows are inserted
in bounded batches (DB serialized in the main thread). Stdlib only + psycopg.

Usage:  python -m scripts.ingest_address_points [--date YYYYMMDD] [--workers 12] [--dry-run]
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import io
import logging
import os
import sys
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from scraper import db

LOG = logging.getLogger("ingest_address_points")

_URL = "https://vdp.cuzk.gov.cz/vymenny_format/csv/{date}_OB_{obec}_ADR.csv.zip"
_PROBE_OBEC = 554782  # Praha — used to find the published month-end file date
_BATCH = 10000

_INSERT_SQL = """
    INSERT INTO address_points (id, street, house_number, obec_id, geom)
    VALUES (%s, %s, %s, %s,
            ST_Transform(ST_SetSRID(ST_MakePoint(-%s, -%s), 5514), 4326))
    ON CONFLICT (id) DO UPDATE SET
      street = EXCLUDED.street, house_number = EXCLUDED.house_number,
      obec_id = EXCLUDED.obec_id, geom = EXCLUDED.geom
"""


def _month_end(d: datetime.date) -> datetime.date:
    return d.replace(day=1) - datetime.timedelta(days=1)


def _download(date: str, obec: int) -> bytes | None:
    url = _URL.format(date=date, obec=obec)
    req = urllib.request.Request(url, headers={"User-Agent": "sreality-ruian-ingest/1"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _resolve_date(start: str | None, max_back: int) -> str | None:
    """Find a published month-end by probing one known obec (Praha)."""
    if start:
        return start if _download(start, _PROBE_OBEC) is not None else None
    cur = _month_end(datetime.date.today())
    for _ in range(max_back):
        date = cur.strftime("%Y%m%d")
        if _download(date, _PROBE_OBEC) is not None:
            return date
        cur = _month_end(cur)
    return None


def _num(value: str) -> float | None:
    v = (value or "").strip().replace(",", ".")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _int(value: str) -> int | None:
    try:
        return int((value or "").strip())
    except ValueError:
        return None


def _parse(zip_bytes: bytes) -> list[tuple]:
    """Street-bearing rows (id, street, house_number, obec_id, y, x) from one
    per-obec CSV. S-JTSK Y/X kept positive; negated in SQL."""
    out: list[tuple] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
        if name is None:
            return out
        with zf.open(name) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="cp1250", newline=""),
                                delimiter=";")
            next(reader, None)  # header
            for row in reader:
                if len(row) < 18:
                    continue
                street = (row[10] or "").strip()
                if not street:
                    continue
                y, x = _num(row[16]), _num(row[17])
                adm = _int(row[0])
                if y is None or x is None or adm is None:
                    continue
                out.append((adm, street, (row[12] or "").strip() or None, _int(row[1]), y, x))
    return out


def _obec_codes(conn: Any) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM admin_boundaries WHERE level = 'obec' ORDER BY id")
        return [r[0] for r in cur.fetchall()]


def _record_revision(conn: Any, date: str, row_count: int, obec_count: int) -> None:
    """Append an address_points_revisions row, guarded on source_date so
    re-running the same published RÚIAN month does not bump the version (which
    would trigger a pointless full coord->street re-attempt). The bump is the
    LAST thing the ingest does — only after the wholesale reload committed."""
    source_date = datetime.datetime.strptime(date, "%Y%m%d").date()
    with conn.cursor() as cur:
        cur.execute("SELECT source_date FROM address_points_revisions ORDER BY revision DESC LIMIT 1")
        row = cur.fetchone()
        if row is not None and row[0] == source_date:
            LOG.info("RUIAN revision unchanged source_date=%s — no bump", source_date)
            return
        cur.execute(
            "INSERT INTO address_points_revisions (source_date, row_count, obec_count) "
            "VALUES (%s, %s, %s) RETURNING revision",
            (source_date, row_count, obec_count),
        )
        LOG.info("RUIAN revision recorded source_date=%s revision=%s row_count=%s",
                 source_date, cur.fetchone()[0], row_count)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="strukt file date YYYYMMDD (default: latest month-end)")
    parser.add_argument("--max-back", type=int, default=6)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--limit-obce", type=int, default=None, help="Process only the first N obce (testing).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Download + parse, report counts, write nothing.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    date = _resolve_date(args.date, args.max_back)
    if date is None:
        LOG.error("RUIAN no published per-obec file found in the last %d month-ends", args.max_back)
        return 1
    LOG.info("RUIAN using date=%s", date)

    with db.connect() as conn:
        codes = _obec_codes(conn)
        if args.limit_obce:
            codes = codes[:args.limit_obce]
        LOG.info("RUIAN obce=%d workers=%d", len(codes), args.workers)

        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE address_points")

        inserted = obce_ok = obce_404 = 0
        batch: list[tuple] = []

        def flush() -> None:
            nonlocal inserted, batch
            if batch and not args.dry_run:
                with conn.cursor() as cur:
                    cur.executemany(_INSERT_SQL, batch)
            inserted += len(batch)
            batch = []

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_download, date, k): k for k in codes}
            for i, fut in enumerate(as_completed(futures)):
                try:
                    blob = fut.result()
                except Exception as exc:  # noqa: BLE001 — one bad obec must not abort the run
                    LOG.warning("RUIAN obec=%s download error: %s", futures[fut], exc)
                    continue
                if blob is None:
                    obce_404 += 1
                    continue
                obce_ok += 1
                batch.extend(_parse(blob))
                if len(batch) >= _BATCH:
                    flush()
                if (i + 1) % 1000 == 0:
                    LOG.info("RUIAN progress obce=%d/%d ok=%d 404=%d inserted=%d",
                             i + 1, len(codes), obce_ok, obce_404, inserted)
        flush()

        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*), count(DISTINCT obec_id) FROM address_points")
                total, distinct_obce = cur.fetchone()
            _record_revision(conn, date, total, distinct_obce)
        else:
            total = distinct_obce = "(dry-run)"
    LOG.info("RUIAN done date=%s obce_ok=%d obce_404=%d inserted=%d total=%s distinct_obce=%s",
             date, obce_ok, obce_404, inserted, total, distinct_obce)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
