"""Shape gate for migration 528 (the autonomous dedup engine's store, schema autodedup).

Offline, no DB. The generic RLS/grant rails see every statement here; this file checks
what a generic rail cannot know: the program's rulings. Everything lives in schema
`autodedup` and nothing holds a foreign key into production (D-scope: the schema must be
droppable in one statement); every pair-grain table pins `listing_lo < listing_hi`, so a
swapped re-decision can never become a second row; `merges` is the reserved write ledger
of a SHADOW-MODE trial (D4); nothing reads `property_merge_events` (D7); no DDL touches
`public.images` (D8); and `iterations` — the table the SPA progress page renders — carries
exactly the ruled columns.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "528_autodedup_foundation.sql"

_TABLES = (
    "settings", "runs", "iterations",
    "listing_fp", "image_band", "phash_pop", "exploded_blocks",
    "pairs", "judgements", "must_not_link",
    "clusters", "cluster_members", "cluster_conflicts",
    "merges", "verdicts", "labels", "models", "eval_samples",
    "resolve_queue", "scan_cursor", "judge_queue",
)

# Every table that carries a listing pair, and therefore an ordering constraint.
_PAIR_TABLES = (
    "pairs", "judgements", "must_not_link", "labels", "eval_samples", "judge_queue",
    "verdicts", "cluster_conflicts", "merges",
)


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    body = "\n".join(line.split("--")[0] for line in _sql().lower().splitlines())
    return re.sub(r"\s+", " ", body)


def _table(name: str) -> str:
    return _code().split(f"create table if not exists autodedup.{name} (")[1].split(");")[0]


def test_migration_file_exists() -> None:
    assert _MIGRATION.is_file(), f"expected {_MIGRATION}"


def test_schema_is_created_and_every_table_lives_in_it() -> None:
    code = _code()
    assert "create schema if not exists autodedup;" in code
    for table in _TABLES:
        assert f"create table if not exists autodedup.{table} (" in code, table
    # Nothing may land in public: the whole store is droppable evidence.
    created = set(re.findall(r"create table (?:if not exists )?([a-z0-9_.]+) \(", code))
    assert all(name.startswith("autodedup.") for name in created), sorted(created)
    assert len(created) == len(_TABLES)


def test_no_foreign_key_into_production_anywhere() -> None:
    # `listing_id` columns are listings.id by convention, enforced in code: a FK onto an
    # 822k-row hot table buys nothing here and would block `drop schema ... cascade`.
    assert "references" not in _code()


def test_no_ddl_touches_the_shared_image_tables() -> None:
    # Ruling D8: no index or alter on public.images in the trial.
    code = _code()
    assert not re.search(r"(alter|create (unique )?index[^;]*on) (public\.)?images\b", code)


def test_every_pair_table_pins_lo_below_hi() -> None:
    for table in _PAIR_TABLES:
        body = _table(table)
        assert "listing_lo" in body and "listing_hi" in body, table
        assert "check (listing_lo < listing_hi)" in body, table


def test_pair_and_judgement_identity_is_the_unordered_pair() -> None:
    assert "primary key (listing_lo, listing_hi)" in _table("pairs")
    assert "primary key (listing_lo, listing_hi)" in _table("must_not_link")
    # A judgement is per (pair, judge version, tier) — a re-judge must not overwrite the
    # cheap tier's row with the gold tier's.
    assert "primary key (listing_lo, listing_hi, judge_version, tier)" in _table("judgements")


def test_shadow_mode_merges_is_a_reserved_ledger_with_reserved_columns() -> None:
    # Ruling D4: the trial decides and stores, and writes nothing into production.
    merges = _table("merges")
    assert "merge_group_id uuid primary key" in merges
    assert "stamped boolean not null default false" in merges
    assert "applied_merge_group uuid" in _table("pairs")
    assert "property_id bigint" in _table("clusters")


def test_nothing_reads_the_production_merge_ledger() -> None:
    # Ruling D7. The name may appear in prose stating the engine neither reads nor
    # reconciles against it; it may never appear in a statement.
    assert "property_merge_events" not in _code()


def test_iterations_carries_the_ruled_progress_page_columns() -> None:
    it = _table("iterations")
    for col in (
        "id bigserial primary key",
        "wave text not null",
        "title text not null",
        "status text not null check (status in ('running','done','failed','skipped'))",
        "approach text",
        "tools text[]",
        "sample_stats jsonb",
        "metrics jsonb",
        "cost_usd numeric(10,4) not null default 0",
        "artifacts jsonb",
        "run_id bigint",
        "notes text",
        "started_at timestamptz",
        "finished_at timestamptz",
        "created_at timestamptz not null default now()",
    ):
        assert col in it, col


def test_security_posture_rls_on_and_browser_roles_revoked() -> None:
    code = _code()
    for table in _TABLES:
        assert f"alter table autodedup.{table} enable row level security" in code, table
        assert f"revoke all on autodedup.{table} from anon, authenticated" in code, table
    assert "revoke all on schema autodedup from anon, authenticated" in code
    assert "revoke all on all sequences in schema autodedup from anon, authenticated" in code
    # Zero policies: service-role only, never a browser path.
    assert "create policy" not in code


def test_apply_contract_is_idempotent_with_a_plain_lock_timeout() -> None:
    code = _code()
    assert "set lock_timeout = '5s';" in code
    assert "set local" not in code
    # The lock-timeout retry re-runs the WHOLE file.
    assert "create table autodedup." not in code
    for m in re.finditer(r"create (?:unique )?index ([a-z0-9_]+) on ", code):
        raise AssertionError(f"index {m.group(1)} is not `if not exists`")


# --- the tier vocabulary, widened by migration 530 -------------------------------------


_TIER_MIGRATION = _ROOT / "migrations" / "530_autodedup_judge_tier_oss.sql"
_TIER_TABLES = ("judgements", "judge_queue")


def test_528_spelled_the_tier_domain_as_an_inline_check() -> None:
    """The premise migration 530 rests on: an INLINE check, which Postgres names for you and
    which a widening must therefore drop by that generated name."""
    for table in _TIER_TABLES:
        assert "tier" in _table(table)
        assert "check (tier in ('text','vision','gold'))" in _table(table)


def test_530_admits_the_oss_tier_on_both_tables() -> None:
    body = _TIER_MIGRATION.read_text(encoding="utf-8")
    for table in _TIER_TABLES:
        assert f"alter table autodedup.{table}" in body
    assert body.count("check (tier in ('text', 'vision', 'gold', 'oss'))") == len(_TIER_TABLES)
    # The lane's tiers and the store's tiers are ONE vocabulary: a tier the lane can draw and
    # the table rejects is a paid pass that stores nothing.
    from autodedup import judge_lane

    for tier in judge_lane.TIERS:
        if tier == "smoke":       # two tiers in one pass; it stores under each, never as itself
            continue
        assert f"'{tier}'" in body


def test_530_is_idempotent_and_takes_a_lock_timeout() -> None:
    body = _TIER_MIGRATION.read_text(encoding="utf-8")
    assert "set lock_timeout = '5s'" in body
    assert body.count("drop constraint if exists") == 2 * len(_TIER_TABLES)
    assert "create table" not in body and "drop table" not in body


# --- the operator verdict vocabulary, widened by migration 532 -------------------------


_VERDICT_MIGRATION = _ROOT / "migrations" / "532_autodedup_verdict_same_project.sql"
_VERDICT_VALUES = (
    "same",
    "different",
    "same_building_different_unit",
    "same_project_different_unit",
    "unsure",
)


def test_528_spelled_the_verdict_domain_as_an_inline_check() -> None:
    """The premise migration 532 rests on: an INLINE check, whose name Postgres generates —
    which is why the widening looks the name up in pg_constraint instead of guessing it."""
    body = _table("verdicts")
    assert "check (verdict in" in body
    assert "constraint autodedup_verdicts_verdict" not in body


def test_532_admits_same_project_different_unit_and_keeps_the_other_four() -> None:
    body = _VERDICT_MIGRATION.read_text(encoding="utf-8")
    for value in _VERDICT_VALUES:
        assert f"'{value}'" in body, value
    # The API's vocabulary and the store's are ONE: a value the route accepts and the table
    # rejects is a verdict the operator watches vanish.
    pytest.importorskip("fastapi")
    from api.routes import autodedup as routes

    assert set(routes.VERDICT_VALUES) == set(_VERDICT_VALUES)


def test_532_drops_the_generated_check_by_lookup_and_re_adds_a_named_one() -> None:
    body = _VERDICT_MIGRATION.read_text(encoding="utf-8").lower()
    assert "set lock_timeout = '5s'" in body
    # The idempotence mechanism: whatever check covers `verdict` is dropped, named or not.
    assert "from pg_constraint" in body and "contype = 'c'" in body
    assert "drop constraint %i" in body
    assert "add constraint autodedup_verdicts_verdict_ck" in body
    assert "create table" not in body and "drop table" not in body


# --- the operator's REASONS, added by migration 533 ------------------------------------


_REASONS_MIGRATION = _ROOT / "migrations" / "533_autodedup_verdict_reasons.sql"


def test_533_adds_the_reasons_array_idempotently_and_takes_a_lock_timeout() -> None:
    body = _REASONS_MIGRATION.read_text(encoding="utf-8").lower()
    assert "set lock_timeout = '5s'" in body
    assert "add column if not exists reasons text[] not null default '{}'" in body
    # ADDITIVE only: nothing here creates, drops or rewrites a table.
    assert "create table" not in body and "drop table" not in body
    assert "drop column" not in body and "update autodedup" not in body


def test_533_puts_NO_check_constraint_on_the_reason_vocabulary() -> None:
    """Unlike 532's `verdict` domain. The API is the only writer and validates every code, so
    a check here would buy nothing and cost a migration each time the list grows."""
    body = _REASONS_MIGRATION.read_text(encoding="utf-8").lower()
    assert "check (" not in body
    assert "add constraint" not in body


def test_the_reason_registry_is_a_closed_vocabulary_of_codes_with_czech_labels() -> None:
    """The store keeps free-form text[] — which makes the API's registry the ONLY thing
    keeping the histogram groupable. Codes are ascii and stable; labels are the operator's."""
    from autodedup import verdict_reasons

    codes = [code for code, _ in verdict_reasons.VERDICT_REASONS]
    assert len(codes) == len(set(codes))
    assert "floor_plan_differs" in codes and "other" in codes
    for code, label in verdict_reasons.VERDICT_REASONS:
        assert code.isascii() and code == code.lower() and " " not in code
        assert label and label[0].isupper()
    # Czech labels, with the diacritics: an ascii-folded label is a different word.
    labels = "".join(label for _, label in verdict_reasons.VERDICT_REASONS)
    assert not labels.isascii()
