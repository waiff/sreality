"""Shape gate for migration 564 — the operator's own Browse merges as engine data (E299).

Offline, no DB. The generic RLS/grant rails see every statement; this checks what they cannot
know: the copy is 560's derivation verbatim (so the groups and the rulings 560 wrote describe
the same pairs), it only READS `public`, it is idempotent, the verdict link is a nullable
additive column backfilled from exactly the two note shapes the code has ever written for a
merge, and the lane reads only columns the table has. Executed against a replayed schema in
tests/test_operator_merges_live.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from autodedup import labels_sql

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "564_autodedup_operator_merges.sql"
_COPY_560 = _ROOT / "migrations" / "560_one_merge_one_undo.sql"


def _code(path: Path = _MIGRATION) -> str:
    body = "\n".join(line.split("--")[0] for line in
                     path.read_text(encoding="utf-8").lower().splitlines())
    return re.sub(r"\s+", " ", body)


def _cte(code: str, name: str) -> str:
    start = code.index(f"{name} as (")
    depth = 0
    for index in range(code.index("(", start), len(code)):
        depth += {"(": 1, ")": -1}.get(code[index], 0)
        if depth == 0:
            return code[start:index + 1]
    raise AssertionError(name)


def test_one_table_in_the_engine_schema_locked_down_with_no_foreign_key() -> None:
    code = _code()
    created = set(re.findall(r"create table (?:if not exists )?([a-z0-9_.]+) \(", code))
    assert created == {"autodedup.operator_merges"}
    assert "references" not in code
    assert "alter table autodedup.operator_merges enable row level security;" in code
    assert "revoke all on autodedup.operator_merges from anon, authenticated;" in code
    assert "grant " not in code


def test_the_copy_reuses_560s_derivation_verbatim() -> None:
    mine, theirs = _code(), _code(_COPY_560)
    for name in ("live", "origin", "group_properties", "members", "placed"):
        assert _cte(mine, name) == _cte(theirs, name), name
    # The group filter is 560's too: an operator merge with at least one live ledger row.
    groups = _cte(mine, "operator_groups")
    assert "where e.source = 'operator'" in groups
    assert "having bool_or(e.undone_at is null)" in groups
    # ... and a pair is 560's pair: different origin sides, one property now.
    pairs = _cte(mine, "pair_counts")
    assert "b.side <> a.side" in pairs and "b.now_on = a.now_on" in pairs


def test_public_is_only_read() -> None:
    code = _code()
    writes = re.findall(r"\b(insert into|update|delete from|truncate|alter table)\s+([a-z0-9_.]+)",
                        code)
    targets = {table for _verb, table in writes}
    assert targets <= {"autodedup.operator_merges", "autodedup.verdicts"}, targets
    assert "property_merge_events" in code  # the one read, once


def test_the_copy_and_the_backfill_are_idempotent() -> None:
    code = _code()
    assert "on conflict (merge_group_id) do nothing;" in code
    backfill = code[code.index("update autodedup.verdicts v"):]
    assert "v.operator_merge_group_id is null" in backfill
    assert "v.kind = 'pair'" in backfill and "v.verdict = 'same'" in backfill
    assert "create index if not exists" in code
    assert "add column if not exists operator_merge_group_id uuid;" in code


def test_the_verdict_link_is_nullable_and_additive() -> None:
    code = _code()
    column = re.search(r"add column if not exists operator_merge_group_id ([^;]*);", code)
    assert column is not None
    assert column.group(1).strip() == "uuid"  # no NOT NULL, no default, no foreign key
    assert "drop " not in code


def test_the_backfill_matches_the_notes_the_code_writes() -> None:
    """The two shapes are 560's copy and `merge_property_set` since 559 — matched exactly,
    once, so no reader after this migration ever parses a note."""
    raw = _MIGRATION.read_text(encoding="utf-8")
    assert "'operator merge ' || m.merge_group_id::text || ' (copied by migration 560)'" in raw
    assert "'operator merge ' || m.merge_group_id::text)" in raw
    assert "'operator merge ' || p.merge_group_id::text || ' (copied by migration 560)'" in \
        _COPY_560.read_text(encoding="utf-8")
    identity = (_ROOT / "toolkit" / "property_identity.py").read_text(encoding="utf-8")
    assert 'note=f"operator merge {group}"' in identity


def test_the_member_arrays_must_align_and_status_is_closed() -> None:
    code = _code()
    assert ("cardinality(member_ids) = cardinality(member_sides) and cardinality(member_ids) "
            "= cardinality(member_property_ids)") in code
    assert "check (status in ('live', 'undone', 'withdrawn'))" in code
    assert "check (source in ('browse'))" in code


def test_the_lane_reads_only_columns_the_table_has() -> None:
    table = _code().split("create table if not exists autodedup.operator_merges (")[1] \
        .split(");")[0]
    select = " ".join(labels_sql.OPERATOR_MERGES_SQL.split())
    columns = select.split("select ")[1].split(" from ")[0]
    for column in (c.strip().split(".")[-1] for c in columns.split(",")):
        assert re.search(rf"\b{column}\b", table), column
    assert "operator_merge_group_id" in labels_sql.OPERATOR_MERGE_LINKS_SQL
    assert "operator_merge_group_id" in labels_sql.OPERATOR_MERGES_PRESENT_SQL
