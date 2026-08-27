"""Offline guard over migration 450's TEXT — the candidate store's shape, its
backend-only posture, and the one property the whole design rests on: a row in
`tag_candidates` is NOT a label.

WHY A TEXT TEST. Grants, RLS and column shape are invisible to `_FakeConn`, and
the migration's executing gate is CI's replay job. What can be checked on every
push is that the file still DECLARES what the design assumes, and that the `draw`
vocabulary in the SQL still matches `toolkit.tag_candidates.DRAWS`. Same idiom as
tests/test_migration_446_tag_provenance.py and tests/test_migration_rls_grants.py:
offline, fast, structural.
"""

from __future__ import annotations

import re
from pathlib import Path

from toolkit import tag_candidates as tc

MIGRATION = (
    Path(__file__).resolve().parent.parent / "migrations" / "450_tag_candidates.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _norm() -> str:
    return " ".join(_sql().split()).lower()


def _statements() -> list[str]:
    body = re.sub(r"^\s*--.*$", "", _sql(), flags=re.MULTILINE)
    return [" ".join(s.split()).lower() for s in body.split(";") if s.strip()]


def _create_table() -> str:
    return next(s for s in _statements() if s.startswith("create table tag_candidates"))


# --- this PR deletes nothing --------------------------------------------------


def test_the_migration_is_purely_additive() -> None:
    """Migrations run autonomously once merged; a destructive statement sneaking
    into this file would run with them. The 72,000 backfill rows and the whole of
    dedup_sim are somebody else's gated PR."""
    sql = _norm()
    for forbidden in (
        "drop table", "drop column", "drop index", "drop view", "drop function",
        "drop schema", "delete from", "truncate", "alter column", "rename to",
    ):
        assert forbidden not in sql, f"450 must not contain {forbidden!r}"


def test_the_migration_leaves_dedup_sim_alone() -> None:
    # Repointing the two readers is necessary but NOT sufficient to drop the
    # schema: the secondary-CLIP proposal lane still writes labeling_sample and
    # still reads label_proposals. The prose explains that; no STATEMENT touches it.
    for statement in _statements():
        assert "dedup_sim" not in statement, f"450 must not touch dedup_sim: {statement[:80]}"


# --- the anti-label guard -----------------------------------------------------


def test_the_table_declares_no_column_a_reader_could_mistake_for_a_label() -> None:
    """The load-bearing property. Queue membership means "look at this", never
    "this is a negative" — an image never reviewed for a tag must stay
    distinguishable from one reviewed and judged not-that-tag (operator ruling,
    2026-08-27). Whether a candidate has been DECIDED is derived by joining
    image_tag_labels, so there is deliberately nothing here to misread."""
    create = _create_table()
    columns = [c.strip() for c in create.split("(", 1)[1].split(",")]
    names = {c.split()[0] for c in columns if c.split()}
    for forbidden in ("state", "status", "label", "reviewed", "decided"):
        assert forbidden not in names, f"tag_candidates must not declare {forbidden!r}"


def test_the_migration_restates_the_standing_negative_semantics() -> None:
    # Migration 442's table comment still asserts the rule the operator overturned,
    # and migrations are append-only, so 450 re-comments the table instead. A
    # catalog comment that contradicts the standing rule is a trap.
    sql = _norm()
    assert "comment on table image_tag_labels is" in sql
    assert "untouched never trains as negative" in sql
    assert "confers no label of any kind" in sql


# --- shape --------------------------------------------------------------------


def test_a_candidate_is_keyed_on_the_tag_and_the_image() -> None:
    # The same image can legitimately be a candidate for several tags; tag_id
    # leads because every read is tag-scoped.
    assert "primary key (tag_id, image_id)" in _create_table()


def test_the_draw_vocabulary_matches_the_toolkits() -> None:
    m = re.search(r"check \(draw in \(([^)]*)\)\)", _norm())
    assert m, "the draw CHECK is missing"
    assert {v.strip().strip("'") for v in m.group(1).split(",")} == set(tc.DRAWS)


def test_a_rank_is_stored_with_the_pool_it_is_a_rank_of() -> None:
    # pool_rank without pool_size means nothing, and pool_size is the pool AFTER
    # exclusions and exact-hash collapse — not the corpus.
    create = _create_table()
    assert "pool_rank integer not null check (pool_rank >= 1)" in create
    assert "pool_size integer not null" in create
    assert "check (pool_size >= pool_rank)" in create


def test_the_cap_keys_are_snapshots_not_live_join_keys() -> None:
    """A later merge re-points listings.property_id; that must not invalidate why
    this row was capped, so listing_id/property_id/phash are bare bigints with no
    FK to chase."""
    create = _create_table()
    assert "listing_id bigint not null," in create
    assert "property_id bigint," in create
    assert "phash bigint," in create
    assert re.search(r"listing_id bigint not null references", create) is None


def test_the_definition_reference_can_never_block_a_tag_deletion() -> None:
    # tag_definitions cascades off tag_taxonomy, so RESTRICT here would make the
    # live remove_tag route start failing. SET NULL cannot.
    assert re.search(
        r"definition_id bigint references tag_definitions \(id\) on delete set null",
        _norm(),
    )


def test_distance_is_documented_as_non_transferable_between_tags() -> None:
    # Inter-tag centroid cosines span 0.58-0.99: a global threshold on this column
    # is never valid, and the column comment is where a future reader learns it.
    sql = _norm()
    assert "comment on column tag_candidates.distance is" in sql
    assert "only within one tag" in sql


# --- posture ------------------------------------------------------------------


def test_the_table_is_backend_only() -> None:
    """This project's default privileges auto-GRANT on new relations — migrations
    442 and 445 both got bitten and 447 had to clean it up. The revoke is
    explicit, not inherited."""
    sql = _norm()
    assert "alter table tag_candidates enable row level security" in sql
    assert "revoke all on tag_candidates from anon, authenticated" in sql
    assert "tag_candidates_public" not in sql
    assert "create policy" not in sql
