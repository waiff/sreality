"""Offline guard over migration 446's TEXT — the provenance columns, the
append-only `image_tag_label_events` table, and the trigger that fills it.

WHY A TEXT TEST AND NOT A BEHAVIOURAL ONE. The history table is written by a
database trigger, and `tests/toolkit/_labeling_fakes.py` runs no triggers (a fake
that modelled one would be a second, drifting implementation of it). The trigger's
only executing gate is CI's migration-replay job. What can be checked cheaply, on
every push, is that the migration still DECLARES the properties the whole design
rests on — and that the vocabulary in the SQL still matches the vocabulary in
`toolkit/tag_annotations.py`. Same idiom as tests/test_migration_rls_grants.py and
tests/test_sql_placeholders.py: offline, fast, structural.

Nothing here proves the trigger fires. It proves the migration would create a
trigger that fires on the three operations that matter, and that this PR deletes
nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from toolkit import tag_annotations as ta

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "446_tag_annotation_provenance.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _norm() -> str:
    return " ".join(_sql().split()).lower()


def _statements() -> list[str]:
    """Statement split that keeps the $$ … $$ function body in ONE piece."""
    body = re.sub(r"^\s*--.*$", "", _sql(), flags=re.MULTILINE)
    parts: list[str] = []
    for chunk in re.split(r"\$\$", body):
        parts.append(chunk)
    out: list[str] = []
    for i, chunk in enumerate(parts):
        if i % 2 == 1:  # inside a dollar-quoted body — never split it
            out[-1] = out[-1] + " $$ " + " ".join(chunk.split()) + " $$ "
            continue
        pieces = chunk.split(";")
        for j, piece in enumerate(pieces):
            text = " ".join(piece.split())
            if j == 0 and out and not out[-1].endswith("$$ "):
                out[-1] = (out[-1] + " " + text).strip()
            elif j == 0 and out:
                out[-1] = (out[-1] + " " + text).strip()
            elif text:
                out.append(text)
    return [s.lower() for s in out if s.strip()]


# --- this PR deletes nothing --------------------------------------------------


def test_the_migration_is_purely_additive() -> None:
    """The 72,058 manufactured rows are made IDENTIFIABLE here, not removed. The
    deletion is a separate, gated, backed-up PR — a destructive statement sneaking
    into this file would run autonomously."""
    sql = _norm()
    for forbidden in (
        "drop table", "drop column", "drop index", "drop view", "drop function",
        "delete from", "truncate", "alter column state", "alter column created_by",
    ):
        assert forbidden not in sql, f"445 must not contain {forbidden!r}"


def test_the_migration_touches_no_earlier_numbered_file() -> None:
    # Architecture rule 1: migrations are append-only. 445 is the only new file;
    # 442/443/444 are never edited, so their content is not this test's business —
    # but 445 must not try to un-do them either.
    assert "alter table image_tag_labels drop" not in _norm()


# --- the provenance columns ---------------------------------------------------


def test_source_is_not_null_with_no_default() -> None:
    """A row inserted without naming its source would silently claim to be a human
    decision — the exact lie this migration exists to make impossible. A forgetful
    writer must get a NOT NULL violation, not a free 'human'."""
    sql = _norm()
    assert "alter table image_tag_labels add column source text;" in sql
    assert "add column source text default" not in sql
    assert "alter table image_tag_labels alter column source set not null" in sql


def test_the_source_vocabulary_matches_the_toolkits() -> None:
    # 446 stated the vocabulary of its day; 464 restated it (drop-and-add) with
    # 'human_draft'. History must stay a SUBSET of today's tuple, and the LATEST
    # restatement must match it exactly — a value in only one place is either a
    # write that will start failing or a vocabulary nobody can insert.
    m = re.search(
        r"add constraint image_tag_labels_source_check check \(source in \(([^)]*)\)\)",
        _norm(),
    )
    assert m, "the source CHECK is missing"
    historical = {v.strip().strip("'") for v in m.group(1).split(",")}
    assert historical <= set(ta.SOURCES)
    import pathlib as _pl
    latest = (_pl.Path(__file__).resolve().parents[1] / "migrations" /
              "464_exam_cohort_purpose_and_draft_labels.sql").read_text()
    m2 = re.search(
        r"add constraint image_tag_labels_source_check\s+check \(source in \(([^)]*)\)\)",
        " ".join(latest.split()),
    )
    assert m2, "464 must restate the source CHECK"
    assert {v.strip().strip("'") for v in m2.group(1).split(",")} == set(ta.SOURCES)


def test_the_backfill_never_condemns_a_hand_labelled_positive() -> None:
    """created_by ALONE cannot tell the fiction from the ground truth: migration
    442 stamped BOTH arms of its backfill `backfill:image_training_examples`
    (442:82 writes the positives, 442:88 the one-hot negatives), and `created_by`
    is INSERT-only, so no later operator write ever rewrote it. Only the negative
    arm is manufactured; the positive arm transcribed the operator's own
    hand-labels from image_training_examples (migration 309). Calling those ~1,440
    positives backfill_442 would hand the deletion PR's
    `WHERE source = 'backfill_442'` the entire positive ground truth."""
    stmt = next(
        s for s in _statements()
        if s.startswith("update image_tag_labels set source = 'backfill_442'")
    )
    assert "starts_with(created_by, 'backfill:')" in stmt
    assert "state = 'negative'" in stmt
    # ...and only a cell nobody has re-decided since: 442 wrote created_at and
    # updated_at off one transaction clock, so this is exactly "never touched".
    # A re-decided cell IS a decision, and decisions are never deleted.
    assert "updated_at = created_at" in stmt


def test_every_other_existing_row_is_a_human_decision_with_its_verification() -> None:
    """The complement of the fiction, in one statement, so nothing can fall between
    the two: everything else is a decision a person made, and gets verified_at from
    updated_at so "human-verified" counts are right on day one."""
    stmt = next(
        s for s in _statements()
        if s.startswith("update image_tag_labels set source = 'human'")
    )
    assert "verified_at = updated_at" in stmt
    assert stmt.rstrip().endswith("where source is null")
    # Backfill rows keep verified_at NULL: nobody verified them.
    fiction = next(
        s for s in _statements()
        if s.startswith("update image_tag_labels set source = 'backfill_442'")
    )
    assert "verified_at" not in fiction


def test_the_deletion_prs_predicate_is_a_single_unambiguous_value() -> None:
    # The later gated PR's predicate is literally `WHERE source = 'backfill_442'`.
    # Naming the migration in the value is what keeps that unambiguous forever.
    assert ta.SOURCE_BACKFILL_442 == "backfill_442"
    assert "'backfill_442'" in _norm()


def test_excluded_reason_is_impossible_outside_an_excluded_cell() -> None:
    """A CHECK, not a convention: 'ambiguous' and 'pruned' have opposite diagnostic
    meanings, and a reason left on a positive or negative row would silently poison
    the ambiguity rate. The fake conn cannot raise a CHECK violation, so this
    declaration is the only offline evidence the rail exists."""
    m = re.search(
        r"add constraint image_tag_labels_excluded_reason_check check \( ?"
        r"excluded_reason is null or \(state = 'excluded' and excluded_reason in \(([^)]*)\)\)",
        _norm(),
    )
    assert m, "the excluded_reason CHECK is missing or has changed shape"
    assert {v.strip().strip("'") for v in m.group(1).split(",")} == set(ta.EXCLUDED_REASONS)


def test_a_model_can_only_be_named_when_a_machine_was_involved() -> None:
    assert re.search(
        r"add constraint image_tag_labels_model_check check \("
        r"model is null or source in \('machine', 'human_confirmed'\)\)",
        _norm(),
    )


def test_definition_id_can_never_block_a_tag_deletion() -> None:
    """tag_definitions cascades off tag_taxonomy, so a RESTRICT/NO ACTION here would
    make `remove_tag` — a shipped, live route — start failing. SET NULL cannot."""
    assert re.search(
        r"add column definition_id bigint references tag_definitions \(id\) "
        r"on delete set null",
        _norm(),
    )


# --- the append-only history --------------------------------------------------


def test_the_events_table_carries_no_foreign_keys() -> None:
    """An audit log with ON DELETE CASCADE destroys the record it exists to keep:
    remove_tag would evaporate a tag's entire decision history, including the
    deletion event that just fired. RESTRICT is worse — remove_tag would fail
    forever once any event existed."""
    create = next(s for s in _statements() if s.startswith("create table image_tag_label_events"))
    assert "references" not in create
    # ...and a denormalized label snapshot keeps the log readable once the tag is gone.
    assert "tag_label text" in create


def test_a_cleared_cell_is_recorded_as_a_null_state_not_as_a_missing_row() -> None:
    # Reverting a cell to untouched IS a decision. Absence is not a negative, in
    # the history exactly as in the matrix.
    create = next(s for s in _statements() if s.startswith("create table image_tag_label_events"))
    assert re.search(r"state text check \(state in \('positive', 'negative', 'excluded'\)\)", create)
    assert "state text not null" not in create
    assert "image_tag_label_events_something_happened check (state is not null or prior_state is not null)" in create


def test_the_events_table_is_backend_only() -> None:
    sql = _norm()
    assert "alter table image_tag_label_events enable row level security" in sql
    assert "revoke all on image_tag_label_events from anon, authenticated" in sql
    assert "image_tag_label_events_public" not in sql


def test_every_existing_annotation_gets_a_genesis_event() -> None:
    """So no cell's history starts mid-story. Seeded rows are distinguishable from
    captured ones by event_at (the row's updated_at) differing from created_at."""
    seed = next(
        s for s in _statements()
        if s.startswith("insert into image_tag_label_events") and "from image_tag_labels" in s
    )
    assert "left join tag_taxonomy" in seed  # label snapshot, tolerant of a missing tag
    assert "itl.updated_at" in seed


def test_the_seed_runs_before_the_trigger_exists() -> None:
    """Otherwise the schema backfill UPDATE above it would manufacture 73,499
    phantom "events" — polluting the very log it creates. A backfill is not a
    decision."""
    sql = _norm()
    assert sql.index("insert into image_tag_label_events") < sql.index(
        "create function log_image_tag_label_event"
    )
    assert sql.index("update image_tag_labels set source") < sql.index(
        "create function log_image_tag_label_event"
    )


# --- the trigger --------------------------------------------------------------


def test_the_trigger_fires_on_insert_update_AND_delete() -> None:
    """All three are decisions: a first verdict, a changed verdict, and a clear back
    to untouched. Missing DELETE would lose exactly the case application-level event
    writes are worst at."""
    trg = next(s for s in _statements() if s.startswith("create trigger image_tag_labels_log_event"))
    assert "after insert or delete or update of" in trg
    assert "on image_tag_labels" in trg


def test_the_trigger_is_per_row_so_a_bulk_statement_logs_every_row() -> None:
    """bulk_set_state / bulk_set_state_for_image use executemany, and the eventual
    gated deletion of 72,058 rows is one statement. FOR EACH ROW is what makes the
    log complete by construction instead of by a second executemany kept in
    lockstep with the first."""
    trg = next(s for s in _statements() if s.startswith("create trigger image_tag_labels_log_event"))
    assert "for each row" in trg
    assert "for each statement" not in trg


def test_the_triggers_update_scope_covers_every_provenance_column() -> None:
    # `update of <cols>` narrows the trigger to statements that touch something
    # meaningful; a column left out of this list would silently lose its history.
    trg = next(s for s in _statements() if s.startswith("create trigger image_tag_labels_log_event"))
    scope = trg.split("update of", 1)[1].split("on image_tag_labels", 1)[0]
    for column in ("state", "source", "definition_id", "model", "excluded_reason", "verified_at"):
        assert column in scope, f"{column} is outside the trigger's UPDATE scope"


def test_a_bare_updated_at_touch_is_not_logged_as_a_decision() -> None:
    fn = next(s for s in _statements() if s.startswith("create function log_image_tag_label_event"))
    for column in ("state", "source", "definition_id", "model", "excluded_reason", "verified_at"):
        assert f"old.{column} is not distinct from new.{column}" in fn


def test_the_trigger_function_is_not_security_definer_and_is_revoked() -> None:
    fn = next(s for s in _statements() if s.startswith("create function log_image_tag_label_event"))
    assert "security definer" not in fn
    assert "revoke execute on function log_image_tag_label_event() from public" in _norm()
