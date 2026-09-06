"""Shape gate for migration 480 (image_dinov3_embeddings).

Offline, no DB: the table is created inside a pgvector-conditional `DO` block
(tests/test_migration_rls_grants.py's dynamic-DDL scanner cannot see any
statement inside it), so this file is the thing that actually checks the
security posture and column shape hold, in lieu of the repo-wide scanner.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "480_dinov3_image_embeddings.sql"

_IDENTITY_COLUMNS = (
    "model", "revision", "library", "pooling", "resolution", "preprocessing", "dtype",
)


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def test_migration_file_exists() -> None:
    assert _MIGRATION.is_file(), f"expected {_MIGRATION}"


def test_guarded_behind_pgvector_availability() -> None:
    sql = _sql().lower()
    assert "pg_available_extensions" in sql
    assert "raise notice" in sql, "must degrade gracefully when pgvector is absent (CI replay)"


def test_creates_the_table_with_halfvec_768() -> None:
    sql = _sql().lower()
    assert "create table if not exists image_dinov3_embeddings" in sql
    assert "halfvec(768)" in sql


def test_all_six_identity_facts_are_not_null_columns() -> None:
    sql = _sql().lower()
    for col in _IDENTITY_COLUMNS:
        pattern = rf"\b{col}\s+(?:text|integer)\s+not null"
        assert re.search(pattern, sql), f"expected `{col} ... not null` in the CREATE TABLE"


def test_primary_key_covers_image_and_every_identity_fact() -> None:
    sql = re.sub(r"\s+", " ", _sql().lower())
    m = re.search(r"primary key \(([^)]+)\)", sql)
    assert m, "expected a primary key clause"
    pk_cols = {c.strip() for c in m.group(1).split(",")}
    assert pk_cols == {"image_id", *_IDENTITY_COLUMNS}, (
        "the primary key must be exactly image_id + the six identity facts, so a "
        "config change adds a row instead of silently overwriting a differently-"
        f"configured vector: got {sorted(pk_cols)}"
    )


def test_references_images_on_delete_cascade() -> None:
    sql = _sql().lower()
    assert "references images(id) on delete cascade" in sql


def test_rls_enabled_and_default_grants_revoked() -> None:
    sql = _sql().lower()
    assert "alter table image_dinov3_embeddings enable row level security" in sql
    assert "revoke all on image_dinov3_embeddings from anon, authenticated" in sql


def test_dynamic_ddl_is_annotated() -> None:
    # Mirrors tests/test_migration_rls_grants.py's own escape hatch: a migration
    # that builds DDL through EXECUTE must say so, or the RLS/grant scanner's
    # blindness to this file would be silent rather than reviewer-visible.
    assert re.search(r"--\s*ci-allow-dynamic:\s*image_dinov3_embeddings", _sql())


def test_encoder_identity_index_supports_checkpoint_resume() -> None:
    # The production job resumes by asking "which image ids has THE CURRENT
    # config already embedded" — that query wants the six facts leading, image_id
    # trailing, so this index (not just the PK, whose leading column is image_id)
    # is what makes it an index-only scan rather than a sequential one.
    sql = re.sub(r"\s+", " ", _sql().lower())
    assert re.search(
        r"create index if not exists image_dinov3_embeddings_encoder_idx\s*"
        r"on image_dinov3_embeddings\s*"
        r"\(model, revision, library, pooling, resolution, preprocessing, dtype, image_id\)",
        sql,
    )


def test_migration_number_is_not_grandfathered() -> None:
    from tests.test_migration_numbers import GRANDFATHER_MAX

    number = int(_MIGRATION.name.split("_", 1)[0])
    assert number > GRANDFATHER_MAX
