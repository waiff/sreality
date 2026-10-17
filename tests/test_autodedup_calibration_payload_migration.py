"""Shape gate for migration 616 — the calibration payload as zlib bytes (PROGRAM.md E963).

Offline, no DB. The generic RLS/grant rails see every statement; this file checks what they
cannot know: the migration stays inside schema `autodedup` (ruling D8), it only ADDS (the jsonb
column it retires is dropped by a later migration, with the operator's OK), the new column is a
bytea, and the lane's presence check names it, so code merged first skips its passes.
"""

from __future__ import annotations

import re
from pathlib import Path

from autodedup import incremental_sql

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "616_autodedup_rt_calibration_payload_zlib.sql"


def _code() -> str:
    body = "\n".join(line.split("--")[0]
                     for line in _MIGRATION.read_text(encoding="utf-8").lower().splitlines())
    return re.sub(r"\s+", " ", body)


def test_the_migration_touches_only_schema_autodedup() -> None:
    assert _MIGRATION.is_file()
    code = _code()
    tables = (re.findall(r"alter table (\S+)", code)
              + re.findall(r"comment on column (\S+?)\.\w+ is", code))
    assert tables and all(table == "autodedup.rt_calibration" for table in tables), tables


def test_it_is_additive_and_idempotent() -> None:
    code = _code()
    for word in ("drop ", "delete from", "truncate", "update ", " type ", "rename", "create "):
        assert word not in code, word
    assert re.findall(r"alter table \S+ (\w+ \w+)", code) == ["add column"]
    assert "add column if not exists payload_zlib bytea;" in code
    assert "not null" not in code and "default" not in code, "nullable, no default: no rewrite"
    assert "set lock_timeout = '5s';" in code and "reset lock_timeout;" in code
    assert "set local" not in code


def test_the_new_column_and_the_two_it_retires_say_so() -> None:
    code = _code()
    for column in ("payload_zlib", "payload", "artifact_url"):
        assert f"comment on column autodedup.rt_calibration.{column} is" in code, column
    assert code.count("retired by e963") == 2


def test_the_lane_skips_until_it_is_applied() -> None:
    assert "attname = 'payload_zlib'" in incremental_sql.RT_STORE_PRESENT_SQL
    assert "%(payload_zlib)s::bytea" in incremental_sql.RT_CALIBRATION_WRITE_SQL
