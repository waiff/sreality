"""Populate app_settings.llm_condition_rubric + llm_condition_marker_dictionary.

Migration 072 seeds the two app_settings rows with empty {} placeholders
because the full rubric (~7 KB) and the curated marker dictionary
(~217 KB) would make the migration file huge. This script reads the
committed JSON files and UPDATEs both rows with their full content.

The app_settings_history trigger (migration 020) preserves every
prior value, so re-running this script after the operator re-aggregates
the marker dictionary or tweaks the rubric is safe and auditable.

Usage:
    python -m scripts.seed_condition_settings
    python -m scripts.seed_condition_settings --rubric path --dictionary path
    python -m scripts.seed_condition_settings --dry-run

Required env: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

LOG = logging.getLogger("seed_condition_settings")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rubric", default="data/condition_rubric_v1.json",
        help="Path to the rubric JSON file.",
    )
    parser.add_argument(
        "--dictionary", default="data/condition_markers_curated.json",
        help="Path to the curated marker dictionary JSON file.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print sizes and exit without UPDATEing.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    rubric = _load_json(Path(args.rubric))
    dictionary = _load_json(Path(args.dictionary))
    if rubric is None or dictionary is None:
        return 2

    LOG.info(
        "rubric: %d top-level keys, %d building_levels, %d apartment_levels",
        len(rubric), len(rubric.get("building_levels", [])),
        len(rubric.get("apartment_levels", [])),
    )
    LOG.info(
        "dictionary: %d building clusters, %d apartment clusters",
        len(dictionary.get("building", [])),
        len(dictionary.get("apartment", [])),
    )

    if args.dry_run:
        LOG.info("dry-run; skipping UPDATEs")
        return 0

    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        _update_setting(
            conn,
            key="llm_condition_rubric",
            value=Jsonb(rubric),
            description_when_seed=(
                "The 5-level rubric (data/condition_rubric_v1.json) "
                "injected verbatim into the scorer's system prompt."
            ),
        )
        _update_setting(
            conn,
            key="llm_condition_marker_dictionary",
            value=Jsonb(dictionary),
            description_when_seed=(
                "The curated marker dictionary "
                "(data/condition_markers_curated.json) injected "
                "verbatim into the scorer's system prompt."
            ),
        )

    LOG.info("done")
    return 0


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        print(f"ERROR: {path} not found.", file=sys.stderr)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: {path} is not valid JSON: {exc}", file=sys.stderr)
        return None


def _update_setting(
    conn,
    *,
    key: str,
    value,
    description_when_seed: str,
) -> None:
    sql = (
        "UPDATE app_settings "
        "SET value = %s, updated_by = 'seed_condition_settings', "
        "    description = COALESCE(NULLIF(description, ''), %s) "
        "WHERE key = %s "
        "RETURNING key, length(value::text) AS value_len"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (value, description_when_seed, key))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"app_settings row {key!r} not found — run migration 072 first."
        )
    LOG.info("updated app_settings.%s (value_len=%d)", row[0], row[1])


if __name__ == "__main__":
    sys.exit(main())
