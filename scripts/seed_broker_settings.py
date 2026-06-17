"""Populate app_settings.broker_free_email_domains + broker_franchise_domains.

Migration 186 seeds the two rows with empty `[]` placeholders; this script loads
the committed JSON files and UPDATEs them with the full domain lists (same pattern
as scripts.seed_condition_settings). The app_settings_history trigger preserves
every prior value, so re-running after an operator edits a list is safe.

Usage:
    python -m scripts.seed_broker_settings
    python -m scripts.seed_broker_settings --dry-run

Required env: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

LOG = logging.getLogger("seed_broker_settings")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--free", default="data/broker_free_email_domains.json")
    parser.add_argument("--franchise", default="data/broker_franchise_domains.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    free = _load_domains(Path(args.free))
    franchise = _load_domains(Path(args.franchise))
    if free is None or franchise is None:
        return 2

    LOG.info("free=%d domains, franchise=%d domains", len(free), len(franchise))
    if args.dry_run:
        LOG.info("dry-run; skipping UPDATEs")
        return 0

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        _update_setting(conn, "broker_free_email_domains", Jsonb(free))
        _update_setting(conn, "broker_franchise_domains", Jsonb(franchise))

    LOG.info("done")
    return 0


def _load_domains(path: Path) -> list[str] | None:
    if not path.is_file():
        print(f"ERROR: {path} not found.", file=sys.stderr)
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: {path} is not valid JSON: {exc}", file=sys.stderr)
        return None
    domains = doc.get("domains") if isinstance(doc, dict) else doc
    if not isinstance(domains, list):
        print(f"ERROR: {path} has no 'domains' list.", file=sys.stderr)
        return None
    return sorted({str(d).strip().lower() for d in domains if str(d).strip()})


def _update_setting(conn, key: str, value) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_settings SET value = %s, updated_by = 'seed_broker_settings' "
            "WHERE key = %s RETURNING key",
            (value, key),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"app_settings row {key!r} not found — run migration 186 first.")
    LOG.info("updated app_settings.%s", row[0])


if __name__ == "__main__":
    sys.exit(main())
