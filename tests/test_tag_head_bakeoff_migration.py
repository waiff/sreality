"""Shape gate for migration 489 (the tagging bake-off store, schema dedup_sim).

Offline, no DB. Every relation is created inside a pgvector-conditional `DO`
block — `tag_head_bakeoff_vectors.embedding` is a `halfvec`, which cannot even be
PARSED where the extension is absent — so tests/test_migration_rls_grants.py's
statement scanner sees none of it. This file is what actually checks the security
posture and the column shape, in lieu of that scanner; it is the sibling of
tests/test_dinov3_embeddings_migration.py, which does the same job for 480.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "489_tag_head_bakeoff_store.sql"

_TABLES = (
    "tag_head_bakeoff_runs", "tag_head_bakeoff_arms", "tag_head_bakeoff_vectors",
    "tag_head_bakeoff_scores", "tag_head_bakeoff_metrics",
)

_ENCODER_COLUMNS = (
    "model", "revision", "library", "pooling", "resolution", "preprocessing", "dtype",
)


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _flat() -> str:
    return re.sub(r"\s+", " ", _sql().lower())


def _flat_code() -> str:
    """The migration with its `--` comments stripped — the prose above cites
    `halfvec(768)` as the shape this table deliberately does NOT use."""
    body = "\n".join(line.split("--")[0] for line in _sql().lower().splitlines())
    return re.sub(r"\s+", " ", body)


def test_migration_file_exists() -> None:
    assert _MIGRATION.is_file(), f"expected {_MIGRATION}"


def test_guarded_behind_pgvector_availability() -> None:
    sql = _sql().lower()
    assert "pg_available_extensions" in sql
    assert "raise notice" in sql, "must degrade gracefully when pgvector is absent"


def test_every_table_lives_in_dedup_sim() -> None:
    # dedup_sim is droppable wholesale (migration 372's whole point). A bake-off
    # table in `public` would make this experiment's evidence permanent schema.
    sql = _flat()
    for table in _TABLES:
        assert f"create table if not exists dedup_sim.{table} (" in sql, table
    assert re.search(r"create table (if not exists )?(?!dedup_sim\.)", sql) is None or \
        all(f"dedup_sim.{t}" in sql for t in _TABLES)


def test_vectors_column_has_no_fixed_dimension() -> None:
    # An arm may be 512, 768 or 1024 wide. `halfvec(768)` — production's shape —
    # would silently exclude every arm that is not DINOv3 ViT-B.
    sql = _flat_code()
    assert "embedding halfvec not null" in sql
    assert "halfvec(" not in sql, "the bake-off's vector column must be unmodified halfvec"


def test_arms_carry_the_seven_identity_facts_not_null() -> None:
    sql = _flat()
    arms = sql.split("create table if not exists dedup_sim.tag_head_bakeoff_arms (")[1]
    arms = arms.split("$sql$")[0]
    for col in _ENCODER_COLUMNS:
        assert re.search(rf"\b{col}\s+(?:text|integer)\s+not null", arms), col
    assert "unique (run_id, arm)" in arms


def test_scores_primary_key_is_the_full_cell_key() -> None:
    sql = _flat()
    scores = sql.split("create table if not exists dedup_sim.tag_head_bakeoff_scores (")[1]
    m = re.search(r"primary key \(([^)]+)\)", scores)
    assert m, "expected a primary key on the scores table"
    assert {c.strip() for c in m.group(1).split(",")} == {
        "arm_id", "mode", "tag_id", "image_id", "split"}


def test_scores_label_is_nullable_and_split_is_constrained() -> None:
    sql = _flat()
    scores = sql.split("create table if not exists dedup_sim.tag_head_bakeoff_scores (")[1]
    # NULL is how an exam abstention is stored: the ratified rule grades nothing
    # when either side left out, and dropping the row would lose the photo too.
    assert re.search(r"label\s+smallint check \(label in \(0, 1\)\)", scores)
    assert "check (split in ('cv', 'exam'))" in scores


def test_metrics_rates_are_nullable() -> None:
    sql = _flat()
    metrics = sql.split("create table if not exists dedup_sim.tag_head_bakeoff_metrics (")[1]
    for col in ("cv_precision", "cv_recall", "cv_f1",
                "exam_precision", "exam_recall", "exam_f1"):
        assert re.search(rf"{col}\s+double precision(?!\s+not null)", metrics), (
            f"{col} must be nullable: 'nothing was proposed' is not 'everything "
            "proposed was wrong'")
    # The counts behind every rate, so a reader can recompute a bound.
    for col in ("cv_graded_n", "exam_graded_n", "exam_abstained_n",
                "cv_tp", "cv_fp", "cv_tn", "cv_fn",
                "exam_tp", "exam_fp", "exam_tn", "exam_fn"):
        assert col in metrics, col


def test_bucket_index_orders_by_score_descending() -> None:
    sql = _flat()
    assert re.search(
        r"create index if not exists tag_head_bakeoff_scores_bucket_idx\s*"
        r"on dedup_sim\.tag_head_bakeoff_scores\s*"
        r"\(arm_id, mode, tag_id, split, score desc\)", sql)


def test_image_references_cascade() -> None:
    sql = _flat()
    assert sql.count("references images(id) on delete cascade") >= 2


def test_rls_enabled_and_default_grants_revoked_for_every_table() -> None:
    sql = _sql().lower()
    for table in _TABLES:
        assert f"alter table dedup_sim.{table} enable row level security" in sql, table
        assert f"revoke all on dedup_sim.{table} from anon, authenticated" in sql, table


def test_dynamic_ddl_is_annotated() -> None:
    assert re.search(r"--\s*ci-allow-dynamic:\s*tag_head_bakeoff", _sql())


def test_migration_number_is_not_grandfathered() -> None:
    from tests.test_migration_numbers import GRANDFATHER_MAX

    assert int(_MIGRATION.name.split("_", 1)[0]) > GRANDFATHER_MAX
