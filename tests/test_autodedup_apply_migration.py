"""Shape gate for migration 558 — W29's apply ledger, the chokepoint's third source and the
operator's two switches, seeded OFF.

Offline, no DB. The generic RLS/grant rails see every statement; this checks what they cannot
know: the source CHECK is WIDENED (never narrowed, never dropped without a replacement), the
ledger lives in schema `autodedup` with no foreign key into production, the idempotency
index is the one E305 names, and the seeded switches cannot drive a live run on their own.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from autodedup import apply as A

_MIGRATION = (Path(__file__).resolve().parent.parent / "migrations"
              / "558_autodedup_apply_ledger.sql")


def _code() -> str:
    body = "\n".join(line.split("--")[0] for line in
                     _MIGRATION.read_text(encoding="utf-8").lower().splitlines())
    return re.sub(r"\s+", " ", body)


def test_the_source_check_is_widened_to_the_three_sources() -> None:
    code = _code()
    assert ("check (source in ('auto', 'operator', 'autodedup')) not valid" in code)
    assert "validate constraint property_merge_events_source_check" in code
    # The drop and the add are ONE statement (a DO block), so autocommit apply never leaves
    # the table with neither constraint.
    block = code.split("do $$")[1].split("$$;")[0]
    assert "drop constraint" in block and "add constraint" in block


def test_the_ledger_is_isolated_and_posture_is_locked() -> None:
    code = _code()
    created = set(re.findall(r"create table (?:if not exists )?([a-z0-9_.]+) \(", code))
    assert created == {"autodedup.applied_merges"}
    assert "references" not in code
    assert "alter table autodedup.applied_merges enable row level security;" in code
    assert "revoke all on autodedup.applied_merges from anon, authenticated;" in code
    assert "grant " not in code


def test_idempotency_is_a_unique_live_pair() -> None:
    code = _code()
    assert ("create unique index if not exists autodedup_applied_merges_live_pair_uidx "
            "on autodedup.applied_merges (survivor_property_id, retired_property_id) "
            "where outcome = 'applied' and undone_at is null;") in code
    assert ("check (outcome in ('planned', 'applied', 'skipped', 'refused', 'failed'))"
            in code)


def test_the_sql_the_apply_path_writes_matches_the_ledger_columns() -> None:
    from autodedup import apply_sql

    table = _code().split("create table if not exists autodedup.applied_merges (")[1] \
        .split(");")[0]
    insert = apply_sql.LEDGER_INSERT_SQL.split("(")[1].split(")")[0]
    for column in (c.strip() for c in insert.split(",")):
        assert re.search(rf"\b{column}\b", table), column


def test_the_switches_are_seeded_off_and_never_overwrite_the_operator() -> None:
    code = _code()
    assert "insert into app_settings (key, value, description)" in code
    assert "on conflict (key) do nothing;" in code
    assert "'autodedup_apply_enabled', 'false'::jsonb," in code
    assert "update app_settings" not in code
    from autodedup import apply_sql

    assert "from public.app_settings" in apply_sql.SETTING_SQL


def test_the_seeded_scope_parses_and_cannot_drive_a_live_run() -> None:
    raw = _MIGRATION.read_text(encoding="utf-8")
    literal = re.search(r"'autodedup_apply_scope',\s*'(\{.*?\})'::jsonb", raw, re.S)
    assert literal is not None
    seeded = json.loads(literal.group(1))
    assert set(seeded) == set(A.SCOPE_KEYS)
    scope = A.effective_scope(seeded, {}, live=False)
    assert scope.category_types == frozenset({"prodej"}) and scope.blocks is None
    # A deal type but no area: the seed can never drive a live merge, not even with a
    # dispatch that names an area of its own.
    with pytest.raises(ValueError, match="area"):
        A.effective_scope(seeded, {"blocks": "obec:563510"}, live=True)
