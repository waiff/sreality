"""Shape gate for migration 492 (the NEW DEDUP Level-0 candidate store, schema dedup_sim).

Offline, no DB. The generic RLS/grant rails see every statement here; this file checks
what a generic rail cannot know: the pair identity is (inputs_id, lo, hi) with lo < hi,
the pair row holds no foreign key into `listings` (the sim schema is dropped wholesale at
Wave 8), the evidence is typed columns rather than a per-row jsonb, and `simulation_runs`
now admits a `lane`-triggered run.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "492_new_dedup_candidate_store.sql"

_TABLES = ("candidate_inputs", "candidate_generations", "candidate_pairs")


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    body = "\n".join(line.split("--")[0] for line in _sql().lower().splitlines())
    return re.sub(r"\s+", " ", body)


def _table(name: str) -> str:
    return _code().split(f"create table if not exists dedup_sim.{name} (")[1].split(");")[0]


def test_migration_file_exists() -> None:
    assert _MIGRATION.is_file(), f"expected {_MIGRATION}"


def test_every_table_lives_in_dedup_sim() -> None:
    code = _code()
    for table in _TABLES:
        assert f"create table if not exists dedup_sim.{table} (" in code, table
    # Nothing here may be created in public: the whole store is droppable evidence.
    assert not re.search(r"create table if not exists (public\.)?candidate_", code)


def test_pair_identity_is_inputs_lo_hi_with_lo_below_hi() -> None:
    pairs = _table("candidate_pairs")
    assert "primary key (inputs_id, listing_id_lo, listing_id_hi)" in pairs
    assert "check (listing_id_lo < listing_id_hi)" in pairs
    # Rung is an attribute of the pair, never part of its identity: one rung per path.
    assert "rung text not null" in pairs
    assert "primary key (inputs_id, listing_id_lo, listing_id_hi, rung" not in pairs


def test_pair_rows_hold_no_foreign_key_into_production() -> None:
    pairs = _table("candidate_pairs")
    assert "references listings" not in pairs
    assert "references public." not in pairs
    # The only reference is INTO the sim schema (the parameter set).
    assert "references dedup_sim.candidate_inputs (id)" in pairs


def test_pair_evidence_is_typed_columns_not_jsonb() -> None:
    pairs = _table("candidate_pairs")
    assert "jsonb" not in pairs
    for col in (
        "block_key text not null",
        "category_type text",
        "disposition text",
        "area_lo numeric",
        "area_hi numeric",
        "area_diff_pct numeric",
        "floor_lo integer",
        "floor_hi integer",
        "floor_checked boolean not null default false",
        "first_seen_at timestamptz not null default now()",
        "last_seen_at timestamptz not null default now()",
    ):
        assert col in pairs, col


def test_inputs_are_unique_per_path_and_fingerprint() -> None:
    inputs = _table("candidate_inputs")
    assert "unique (path, fingerprint)" in inputs
    assert "check (path in ('a', 'b', 'c'))" in inputs
    assert "inputs jsonb not null" in inputs
    assert "generator_version text not null" in inputs


def test_generation_links_run_and_inputs_and_mirrors_run_statuses() -> None:
    gen = _table("candidate_generations")
    assert "references dedup_sim.simulation_runs (id)" in gen
    assert "references dedup_sim.candidate_inputs (id)" in gen
    assert "check (status in ('pending', 'running', 'success', 'failed'))" in gen
    assert "progress jsonb" in gen and "stats jsonb" in gen


def test_simulation_runs_admits_lane_triggered_runs() -> None:
    code = _code()
    assert "drop constraint if exists simulation_runs_triggered_by_check" in code
    assert "check (triggered_by in ('ui', 'api', 'lane'))" in code


def test_security_posture_rls_on_and_browser_roles_revoked() -> None:
    code = _code()
    for table in _TABLES:
        assert f"alter table dedup_sim.{table} enable row level security" in code, table
        assert f"revoke all on dedup_sim.{table} from anon, authenticated" in code, table


def test_no_secondary_index_on_the_pair_table() -> None:
    # Every index on a 10^8-row table is gigabytes, and one on generation_id would defeat
    # HOT updates on every re-run; the PK prefix (inputs_id) serves every read.
    code = _code()
    on_pairs = re.findall(r"create (?:unique )?index (?:if not exists )?(\w+) on dedup_sim\.candidate_pairs", code)
    assert on_pairs == []


def test_no_alter_adds_a_foreign_key_into_production() -> None:
    code = _code()
    for m in re.finditer(r"alter table dedup_sim\.candidate_pairs[^;]*;", code):
        assert "references" not in m.group(0)
