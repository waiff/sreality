"""Shape gate for migration 540 — what a real-time decision RESTED ON (PROGRAM.md 11a, E92).

Offline, no DB. The generic RLS/grant rails see every statement; this file checks what a
generic rail cannot know: that the evidence columns and the two indexes the seventh feed reads
stay inside schema `autodedup` (ruling D8 forbids DDL on a shared hot table), that the
migration is additive, and that `first_decided_at` exists at all — the horizon E93 holds a
merge for is measured from it, and a column the lane could not read would make the hold
permanent.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "540_autodedup_evidence_arrival.sql"


def _code() -> str:
    body = "\n".join(line.split("--")[0]
                     for line in _MIGRATION.read_text(encoding="utf-8").lower().splitlines())
    return re.sub(r"\s+", " ", body)


def test_the_migration_exists_and_touches_only_schema_autodedup() -> None:
    assert _MIGRATION.is_file()
    code = _code()
    for table in re.findall(r"alter table (\S+)", code):
        assert table.startswith("autodedup."), table
    for table in re.findall(r"create table\S* (\S+)", code):
        assert table.startswith("autodedup."), table
    for index in re.findall(r"on (\S+) \(", code):
        assert index.startswith("autodedup."), index


def test_it_is_additive_and_idempotent() -> None:
    code = _code()
    assert "drop table" not in code and "drop column" not in code
    assert "drop index" not in code
    assert code.count("add column if not exists") == 6
    assert code.count("create index if not exists") == 3
    assert "set lock_timeout = '5s';" in code and "reset lock_timeout;" in code
    assert "set local" not in code


def test_the_fingerprint_row_records_what_the_decision_rested_on() -> None:
    """E92: four counts and a stamp. Without them a pass cannot tell that a listing's
    photographs have arrived since it decided — which is the whole defect."""
    code = _code()
    for column in ("ev_images", "ev_phash", "ev_clip", "ev_tags", "ev_complete",
                   "first_decided_at"):
        assert f"add column if not exists {column}" in code, column


def test_the_two_arms_of_the_evidence_sweep_are_index_served() -> None:
    code = _code()
    assert ("autodedup_rt_fp_evidence_idx on autodedup.rt_fp (generation, listing_id) "
            "where ev_complete = false") in code
    assert "autodedup_rt_fp_decided_idx on autodedup.rt_fp (generation, first_decided_at)" \
        in code
    # The held merges are found BY REASON: the horizon arm moves no fact at all, so no digest
    # and no feed could ever reach them again.
    assert "where decision = 'evidence_pending'" in code


def test_it_creates_no_table_and_therefore_owes_no_new_rls_statement() -> None:
    """`rt_fp` and `pairs` already enable RLS and revoke the browser roles (528, 539), and a
    column inherits its table's posture. A new TABLE here would owe both, so assert there
    isn't one rather than trusting the reading."""
    code = _code()
    assert "create table" not in code
    assert "create sequence" not in code and "bigserial" not in code
