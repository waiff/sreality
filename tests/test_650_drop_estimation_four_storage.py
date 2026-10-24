"""Migration 650's rails, offline: exactly the parser's storage goes, idempotently, never by CASCADE."""
# What only the text can show: the nine columns dropped are the nine the backup recipe saves, each
# drop is `if exists` (a lock timeout is re-run; the CI replay runs it too), nothing cascades (643
# must have dropped portal_health_mv first, or the apply fails), and the post-check sits inside
# the one transaction.

from __future__ import annotations

import re
from pathlib import Path

SQL = (Path(__file__).resolve().parents[1] / "migrations" / "650_drop_estimation_four_storage.sql").read_text(
    encoding="utf-8")
CODE = re.sub(r"--[^\n]*", "", SQL)
COLUMNS = {
    "estimation_runs": {"source_kind", "parse_confidence", "parse_confidence_per_field", "source_html",
                        "subject_attributes"},
    "building_runs": {"source_kind", "parse_confidence", "parse_confidence_per_field", "source_html"},
}


def _dropped_columns(table: str) -> set[str]:
    stmt = re.search(rf"alter table public\.{table}\s+(.*?);", CODE, re.S).group(1)
    return set(re.findall(r"drop column if exists (\w+)", stmt))


def test_the_nine_columns_go_and_the_backup_saves_everything_dropped() -> None:
    targets = re.search(r'-f targets="([^"]+)"', SQL).group(1).split()
    saved = {t.split(":")[0]: set(t.split(":")[1].split(",")) for t in targets if ":" in t}
    # Whole tables: parsed_url_cache goes; the two settings' current values exist nowhere else
    # (020's history trigger fires on UPDATE only); the portals row rides along.
    assert {"parsed_url_cache", "app_settings", "portals"} <= set(targets)
    for table, cols in COLUMNS.items():
        assert _dropped_columns(table) == cols, table
        assert saved[table] == cols | {"id"}, table


def test_every_drop_is_idempotent_and_nothing_cascades() -> None:
    assert "cascade" not in CODE.lower()
    drops = re.findall(r"\bdrop (?:view|table|column)\b(?! if exists)", CODE)
    assert drops == []
    assert "drop view if exists public.parsed_url_activity;" in CODE
    assert "drop table if exists public.parsed_url_cache;" in CODE


def test_the_rows_go() -> None:
    assert "delete from public.app_settings where key in ('llm_parse_model', 'llm_parse_system_prompt');" in CODE
    assert "delete from public.portals where source = 'idnes_reality';" in CODE


def test_one_transaction_checked_after() -> None:
    at = CODE.index
    assert (at("set lock_timeout = '5s';") < at("begin;") < at("drop view if exists")
            < at("delete from public.portals") < at("do $post$") < at("commit;") < at("reset lock_timeout;"))
