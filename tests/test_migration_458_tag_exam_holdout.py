"""Offline shape guards on migration 458 — the sealed exam's storage.

The posture matters more than the columns here: this table names which images the
probes are graded on, so publishing it would hand out the answer key's index.
"""

from __future__ import annotations

import re
from pathlib import Path

MIG = (Path(__file__).resolve().parent.parent
       / "migrations" / "458_tag_exam_holdout.sql").read_text()
NORM = " ".join(MIG.split())


def test_both_tables_have_rls_enabled() -> None:
    for t in ("tag_exam_cohorts", "tag_exam_members"):
        assert f"alter table {t} enable row level security" in NORM


def test_both_tables_revoke_the_browser_roles() -> None:
    # This project's default privileges auto-GRANT to anon/authenticated on every
    # new table, so silence here is not safety.
    for t in ("tag_exam_cohorts", "tag_exam_members"):
        assert re.search(rf"revoke all on {t} from anon, authenticated", NORM)


def test_the_cohort_sequence_is_revoked_too() -> None:
    # A bigserial's sequence carries its own ACL; revoking the table alone leaves it.
    assert "revoke all on sequence tag_exam_cohorts_id_seq from anon, authenticated" in NORM


def test_there_is_no_public_view() -> None:
    # migration 310 (image_border_cases) ships one and is the wrong precedent to
    # copy: a public exam roster tells anyone which images the grade rests on.
    assert "_public" not in MIG.replace("no `_public` view", "").replace(
        "NO _public view", "")


def test_an_image_belongs_to_at_most_one_exam() -> None:
    # Two cohorts sharing an image makes "which grade may this image inform?"
    # unanswerable.
    assert "create unique index tag_exam_members_one_exam_per_image on tag_exam_members (image_id)" in NORM


def test_membership_is_indexed_by_image() -> None:
    # Every training read probes this by image_id; without the index the exclusion
    # scans the exam on every statement it is formatted into.
    assert "create index tag_exam_members_image_idx on tag_exam_members (image_id)" in NORM


def test_sealed_at_is_nullable() -> None:
    # The draw is two-phase — the stratified half cannot exist before the screener
    # runs — so a cohort accepts inserts while unsealed. "One-way door" means a
    # SEALED cohort is immutable, not that a cohort takes one write.
    assert "sealed_at   timestamptz," in MIG
    assert "sealed_at timestamptz not null" not in NORM


def test_inclusion_probability_is_bounded_and_never_zero() -> None:
    # Statistics weight by 1/p. A zero would divide by zero; a value above 1 is not
    # a probability. Both are arithmetic that silently poisons an estimate.
    assert "inclusion_probability > 0 and inclusion_probability <= 1" in NORM


def test_the_frame_vocabulary_is_constrained() -> None:
    assert "check (frame in ('pure_random', 'stratified'))" in NORM


def test_members_cascade_from_their_cohort_and_image() -> None:
    assert "references tag_exam_cohorts (id) on delete cascade" in NORM
    assert "references images (id) on delete cascade" in NORM


def test_the_header_records_why_answers_live_in_image_tag_labels() -> None:
    # The single-write-path decision is the load-bearing one; a later reader must
    # find the reasoning here rather than re-litigating it.
    assert "image_tag_labels" in MIG
    assert "HOLDOUT_EXCLUSION" in MIG
