"""Plan / receipt for `apply_migration.yml`: what one migration file declares,
and — after the apply — whether every declared object exists in the LIVE catalog.

Reuses the parser and the probe SQL of verify_pipeline's `migration_drift`
check, so "applied" here means exactly what the 6-hourly alarm would accept.
The `supabase_migrations.schema_migrations` ledger is printed for information
only; it is not the oracle (see scripts/migration_objects.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from scripts.migration_objects import MigrationObject, _strip_noise, parse_objects
from scripts.verify_pipeline import _MIGRATION_OBJECT_PROBE_SQL, _SAFE_IDENT


def statement_count(sql: str) -> int:
    return sum(1 for part in _strip_noise(sql).split(";") if part.strip())


def declared(path: Path) -> list[MigrationObject]:
    return parse_objects(path.read_text(encoding="utf-8"))


def probe(conn: Any, objects: list[MigrationObject]) -> list[tuple[str, str, bool]]:
    safe = [o for o in objects if _SAFE_IDENT.match(o.ident)]
    if not safe:
        return []
    rows = conn.execute(
        _MIGRATION_OBJECT_PROBE_SQL,
        {"kinds": [o.kind for o in safe], "idents": [o.ident for o in safe]},
    ).fetchall()
    return [(kind, ident, bool(present)) for kind, ident, present in rows]


def plan(path: Path) -> int:
    objects = declared(path)
    print(f"{path.name}: {statement_count(path.read_text(encoding='utf-8'))} statement(s), "
          f"{len(objects)} probeable object(s)")
    for o in objects:
        print(f"  declares {o}")
    if not objects:
        print("  (nothing probeable — the receipt can only confirm psql exit 0)")
    return 0


def verify(path: Path) -> int:
    from scraper import db

    objects = declared(path)
    with db.connect() as conn:
        results = probe(conn, objects)
        try:
            tail = conn.execute(
                "select version, name from supabase_migrations.schema_migrations "
                "order by version desc limit 3"
            ).fetchall()
            print("ledger tail (informational):", tail)
        except Exception as exc:  # the ledger is optional and not an oracle
            print("ledger unavailable:", exc)
    absent = [(k, i) for k, i, present in results if not present]
    for kind, ident, present in results:
        print(f"  {'PRESENT' if present else 'ABSENT '} {kind}:{ident}")
    if absent:
        print(f"::error::{len(absent)} declared object(s) missing from the live catalog")
        return 1
    print(f"receipt OK: {len(results)} object(s) present")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", metavar="FILE")
    mode.add_argument("--verify", metavar="FILE")
    args = ap.parse_args(argv)
    return plan(Path(args.plan)) if args.plan else verify(Path(args.verify))


if __name__ == "__main__":
    sys.exit(main())
