"""Ingest the ČÚZK RÚIAN "Adresní místa" address points into `address_points`.

Implements docs/design/street-coverage-ruian.md. The whole-country structured
CSV (one ZIP, ~3M points, CC-BY, regenerated on the last day of each month):

    https://vdp.cuzk.gov.cz/vymenny_format/csv/{YYYYMMDD}_strukt_ADR.csv.zip

Windows-1250, semicolon-separated, stable column order:

    0 Kód ADM | 1 Kód obce | 2 Název obce | 3 Kód MOMC | 4 Název MOMC |
    5 Kód obvodu Prahy | 6 Název obvodu Prahy | 7 Kód části obce |
    8 Název části obce | 9 Kód ulice | 10 Název ulice | 11 Typ SO |
    12 Číslo domovní | 13 Číslo orientační | 14 Znak č.o. | 15 PSČ |
    16 Souřadnice Y | 17 Souřadnice X | 18 Platí Od

Only STREET-BEARING points are stored (single-street villages have no street and
can't resolve one). Coordinates are S-JTSK / Křovák (EPSG:5514) written as
POSITIVE magnitudes; PostGIS wants them negative, so we transform
`ST_SetSRID(ST_MakePoint(-Y,-X), 5514)` -> 4326 at insert. Full replace each run.

Loaded in bounded batches (ST_Transform per row in the INSERT) so no single
statement trips the statement timeout. Stdlib only (urllib / zipfile / csv / io),
psycopg for the DB.

Usage:  python -m scripts.ingest_address_points [--date YYYYMMDD] [--max-back 6] [--dry-run]
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
from typing import Iterator

from scraper import db

LOG = logging.getLogger("ingest_address_points")

_URL = "https://vdp.cuzk.gov.cz/vymenny_format/csv/{date}_strukt_ADR.csv.zip"
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


def _candidate_dates(start: str | None, max_back: int) -> list[str]:
    if start:
        return [start]
    cur = _month_end(datetime.date.today())
    out: list[str] = []
    for _ in range(max_back):
        out.append(cur.strftime("%Y%m%d"))
        cur = _month_end(cur)
    return out


def _download(date: str) -> tuple[str, bytes] | None:
    url = _URL.format(date=date)
    req = urllib.request.Request(url, headers={"User-Agent": "sreality-ruian-ingest/1"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return url, resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _num(value: str) -> float | None:
    v = (value or "").strip().replace(",", ".")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int(value: str) -> int | None:
    try:
        return int((value or "").strip())
    except ValueError:
        return None


def _rows(zip_bytes: bytes) -> Iterator[tuple]:
    """Yield (id, street, house_number, obec_id, y, x) for street-bearing rows
    with coordinates. S-JTSK Y/X kept positive here; negated in SQL."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(name) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="cp1250", newline=""),
                                delimiter=";")
            next(reader, None)  # skip the Czech header row
            for row in reader:
                if len(row) < 18:
                    continue
                street = (row[10] or "").strip()
                if not street:
                    continue  # street NOT NULL — single-street villages dropped
                y, x = _num(row[16]), _num(row[17])
                adm = _int(row[0])
                if y is None or x is None or adm is None:
                    continue
                yield (adm, street, (row[12] or "").strip() or None, _int(row[1]), y, x)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="strukt_ADR file date YYYYMMDD (default: latest month-end)")
    parser.add_argument("--max-back", type=int, default=6)
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

    blob = used_date = None
    for date in _candidate_dates(args.date, args.max_back):
        LOG.info("RUIAN trying %s", _URL.format(date=date))
        got = _download(date)
        if got is not None:
            url, blob = got
            used_date = date
            LOG.info("RUIAN downloaded %s (%.1f MB)", url, len(blob) / 1e6)
            break
    if blob is None:
        LOG.error("RUIAN no strukt_ADR file found in the last %d month-ends", args.max_back)
        return 1

    if args.dry_run:
        n = 0
        for _ in _rows(blob):
            n += 1
        LOG.info("RUIAN dry-run street_bearing_rows=%d date=%s", n, used_date)
        return 0

    inserted = 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE address_points")
        batch: list[tuple] = []
        for r in _rows(blob):
            batch.append(r)
            if len(batch) >= _BATCH:
                with conn.cursor() as cur:
                    cur.executemany(_INSERT_SQL, batch)
                inserted += len(batch)
                batch = []
                if inserted % 200000 == 0:
                    LOG.info("RUIAN inserted=%d", inserted)
        if batch:
            with conn.cursor() as cur:
                cur.executemany(_INSERT_SQL, batch)
            inserted += len(batch)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM address_points")
            total = cur.fetchone()[0]
    LOG.info("RUIAN done date=%s inserted=%d total=%d", used_date, inserted, total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
