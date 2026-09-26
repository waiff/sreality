"""Back up what a destructive migration drops, to R2, before the migration is applied.

Run by the backup_before_drop workflow (CLAUDE.md rule 1 / the `database` skill: back up first,
then the operator's OK to apply). R2 and never a workflow artifact: the repository is public,
so an artifact is downloadable by anyone signed in to GitHub. A target is a whole relation (`table`: pg_dump, schema + data) or a column set
(`table:key,col[,col...]`: a CSV of the key and the columns, only rows where one of them is
set, so an absent row restores as the NULL it was). Uses SUPABASE_DB_SESSION_URL when set:
pg_dump and COPY want a stable session, which the transaction pooler does not give.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg import sql

from scraper.db import connect_session
from scraper.image_storage import R2Client

LOG = logging.getLogger(__name__)

_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TARGET = re.compile(r"^([a-z_][a-z0-9_]*)(?::([a-z_][a-z0-9_]*(?:,[a-z_][a-z0-9_]*)+))?$")


@dataclass(frozen=True)
class Target:
    table: str
    columns: tuple[str, ...]  # empty: the whole relation; else the key first

    @property
    def filename(self) -> str:
        return f"{self.table}.{'csv' if self.columns else 'sql'}.gz"


def parse_targets(specs: list[str]) -> list[Target]:
    """`table` or `table:key,col[,col...]`, lowercase identifiers only, one per table."""
    targets: list[Target] = []
    for spec in specs:
        m = _TARGET.match(spec)
        if not m:
            raise ValueError(f"bad target {spec!r}: want `table` or `table:key,col[,col...]`")
        targets.append(Target(m.group(1), tuple(m.group(2).split(",")) if m.group(2) else ()))
    tables = [t.table for t in targets]
    if len(set(tables)) != len(tables):
        raise ValueError(f"a table is named twice: {tables}")
    return targets


def column_copy_sql(target: Target) -> sql.Composed:
    key, *cols = target.columns
    return sql.SQL(
        "COPY (SELECT {cols} FROM public.{table} WHERE {any_set} ORDER BY {key})"
        " TO STDOUT WITH (FORMAT csv, HEADER)"
    ).format(
        cols=sql.SQL(", ").join(map(sql.Identifier, target.columns)),
        table=sql.Identifier(target.table),
        any_set=sql.SQL(" OR ").join(
            sql.SQL("{} IS NOT NULL").format(sql.Identifier(c)) for c in cols
        ),
        key=sql.Identifier(key),
    )


def _dump_columns(db_url: str, target: Target) -> tuple[bytes, int]:
    """Gzipped CSV of the column set, plus its row count read back out of the CSV itself."""
    buf = bytearray()
    with connect_session(db_url) as conn, conn.cursor() as cur:
        with cur.copy(column_copy_sql(target)) as copy:
            for chunk in copy:
                buf += chunk
    rows = sum(1 for _ in csv.reader(io.StringIO(buf.decode()))) - 1
    return gzip.compress(bytes(buf)), rows


def _dump_table(db_url: str, target: Target) -> tuple[bytes, int]:
    """Gzipped pg_dump of one relation, plus its COPY row count read out of the dump."""
    proc = subprocess.run(
        ["pg_dump", db_url, "--no-owner", "--no-privileges", "--table", f"public.{target.table}"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip())
    return gzip.compress(proc.stdout), _copy_rows(proc.stdout)


def _copy_rows(dump: bytes) -> int:
    """Rows between `COPY ... FROM stdin;` and its terminating `\\.` line."""
    rows, in_copy = 0, False
    for line in dump.split(b"\n"):
        if in_copy:
            if line == b"\\.":
                in_copy = False
            else:
                rows += 1
        elif line.startswith(b"COPY ") and line.endswith(b"FROM stdin;"):
            in_copy = True
    return rows


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="names the backup: R2 backups/<label>/<UTC date>/")
    parser.add_argument("targets", nargs="+", help="`table` or `table:key,col[,col...]`")
    args = parser.parse_args(argv)

    if not _LABEL.match(args.label):
        parser.error(f"bad label {args.label!r}: a-z 0-9 and -")
    try:
        targets = parse_targets(args.targets)
    except ValueError as exc:
        parser.error(str(exc))

    db_url = os.environ.get("SUPABASE_DB_SESSION_URL") or os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        LOG.error("SUPABASE_DB_SESSION_URL / SUPABASE_DB_URL not set")
        return 1

    prefix = f"backups/{args.label}/{datetime.now(timezone.utc):%Y-%m-%d}"
    r2 = R2Client.from_env()

    failures: list[str] = []
    for target in targets:
        dump = _dump_columns if target.columns else _dump_table
        try:
            data, rows = dump(db_url, target)
        except Exception as exc:  # noqa: BLE001 -- every target is attempted, then the run fails
            LOG.error("FAILED %s: %s", target.table, exc)
            failures.append(target.table)
            continue
        key = f"{prefix}/{target.filename}"
        r2.upload_bytes(key, data, content_type="application/gzip")
        if r2.object_size(key) != len(data):
            LOG.error("FAILED %s: %s did not read back at %d bytes", target.table, key, len(data))
            failures.append(target.table)
            continue
        LOG.info("%s -> %s (%d rows, %d bytes gzipped)", target.table, key, rows, len(data))

    if failures:
        LOG.error("Failed: %s", ", ".join(failures))
        return 1
    LOG.info("Backup complete: %d target(s) under %s", len(targets), prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
