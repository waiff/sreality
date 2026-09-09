"""Shape gate for migration 490 (the versioned tag model registry, PUBLIC schema).

Offline, no DB. The generic rails in tests/test_migration_rls_grants.py already see
these statements (unlike 480/489 there is no pgvector-conditional DO block to hide
them), so this file checks the things a generic rail cannot know: that the store
lives in `public` and holds no foreign key into the droppable `dedup_sim`, that at
most one model can be active, that the winner columns are NOT NULL while no
threshold or boolean is stored, and that the seven encoder identity facts travel as
columns under the same names the rest of the program uses.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "490_tag_model_registry.sql"

_TABLES = ("tag_head_models", "tag_head_model_heads", "image_tag_scores")

_ENCODER_COLUMNS = (
    "model", "revision", "library", "pooling", "resolution", "preprocessing", "dtype",
)


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _flat() -> str:
    return re.sub(r"\s+", " ", _sql().lower())


def _code() -> str:
    """The migration with its `--` comment prose stripped — the header discusses
    `dedup_sim` at length and must not be mistaken for a reference to it."""
    body = "\n".join(line.split("--")[0] for line in _sql().lower().splitlines())
    return re.sub(r"\s+", " ", body)


def _table(name: str) -> str:
    return _flat().split(f"create table if not exists {name} (")[1].split(");")[0]


def test_migration_file_exists() -> None:
    assert _MIGRATION.is_file(), f"expected {_MIGRATION}"


def test_every_table_is_in_the_public_schema() -> None:
    # The point of the migration: this is production state that must survive
    # Wave 8's `drop schema dedup_sim cascade`.
    sql = _flat()
    for table in _TABLES:
        assert f"create table if not exists {table} (" in sql, table
    assert "dedup_sim." not in _code(), (
        "the tag model store must not reference dedup_sim in any statement — that "
        "schema is dropped at Wave 8 and this store outlives it")


def test_no_foreign_key_into_the_evidence_schema() -> None:
    code = _code()
    assert "references dedup_sim" not in code
    # Provenance instead of a constraint: plain columns nobody can cascade from.
    models = _table("tag_head_models")
    assert "source_run_id bigint" in models
    assert "source_arm text" in models


def test_at_most_one_active_model() -> None:
    sql = _flat()
    assert re.search(
        r"create unique index if not exists tag_head_models_one_active_idx "
        r"on tag_head_models \(status\) where status = 'active'", sql), (
        "two active models is an ambiguous state, not a degraded one — the "
        "database has to refuse it")
    assert "check (status in ('candidate', 'active', 'retired'))" in sql


def test_models_carry_the_seven_identity_facts_not_null() -> None:
    models = _table("tag_head_models")
    for col in _ENCODER_COLUMNS:
        assert re.search(rf"\b{col}\s+(?:text|integer)\s+not null", models), col
    assert "version text not null unique" in models
    # The head set is frozen on the model: a winner is an argmax over exactly it.
    assert "heads bigint[] not null" in models


def test_heads_hold_the_artifact_and_a_copy_of_the_metrics() -> None:
    heads = _table("tag_head_model_heads")
    assert "artifact jsonb not null" in heads
    assert "metrics jsonb not null" in heads
    assert "primary key (model_id, tag_id)" in heads
    assert "references tag_head_models(id) on delete cascade" in heads
    # No FK to tag_taxonomy: a shipped model stays readable after a tag retires.
    assert "references tag_taxonomy" not in heads


def test_scores_are_one_row_per_image_per_model_with_a_winner() -> None:
    scores = _table("image_tag_scores")
    assert "primary key (image_id, model_id)" in scores
    assert "references images(id) on delete cascade" in scores
    assert "scores jsonb not null" in scores
    assert "winner_tag_id bigint not null" in scores
    assert "winner_score real not null" in scores


def test_no_threshold_and_no_per_head_boolean_is_stored() -> None:
    """The operator's 2026-09-09 ruling, enforced as schema: the product has no
    per-head yes/no, so there is nothing here to store one in. A consumer applies
    its own floor to `winner_score`."""
    scores = _table("image_tag_scores")
    assert "threshold" not in scores
    assert "boolean" not in scores
    assert "predicted" not in scores


def test_indexes_cover_the_two_reads_that_matter() -> None:
    sql = _flat()
    # "the most confident <tag> photos under this model"
    assert ("create index if not exists image_tag_scores_winner_idx on "
            "image_tag_scores (model_id, winner_tag_id, winner_score desc)") in sql
    # the scoring job's resume scan, which needs model_id leading
    assert ("create index if not exists image_tag_scores_model_image_idx on "
            "image_tag_scores (model_id, image_id)") in sql


def test_security_posture_at_creation() -> None:
    sql = _flat()
    for table in _TABLES:
        assert f"alter table {table} enable row level security" in sql, table
        assert re.search(
            rf"revoke all on {table}\s+from anon, authenticated", sql), table
    assert "create view" not in _code() and "create or replace view" not in _code(), (
        "no `_public` view exists or is planned — the SPA reads through the "
        "admin-gated API, never over supabase-js")


def test_every_table_is_commented() -> None:
    sql = _flat()
    for table in _TABLES:
        assert f"comment on table {table} is" in sql, table
    # The winner rule is written down where a DBA reading the catalog will meet it.
    assert "argmax" in sql
