"""Every read of image_tag_labels either excludes the sealed exam or is censused.

WHY A CENSUS AND NOT A NARROWER RULE. The obvious guard is "a statement joining
image_tag_labels to image_clip_embeddings must anti-join the exam". Measured against
the live corpus on 2026-08-28 that rule covers **4 of the 19** statements reading
image_tag_labels — and, worse, it would miss the one reader the exam most needs
protecting from. No trainer exists yet (`docs/design/clip-linear-probe.md` is a
proposal); when one is written it will most naturally SELECT labels in one statement,
SELECT embeddings in another, and join them in numpy. Neither statement joins the two
tables. Both sail past the narrow rule. The exam leaks into training on day one and
nothing fails.

So the rule is broad and the exceptions are named: any statement naming
image_tag_labels must either carry `toolkit.tag_holdout.HOLDOUT_EXCLUSION` or appear
below with a reason. A new read is then a decision someone had to write down, not an
omission nobody noticed.

KNOWN LIMIT, stated rather than papered over: `tests/sql_corpus.py` scans
RUNTIME_DIRS = scraper/toolkit/api/scripts/location_data. SQL living inside a
migration — a view, a matview, a pg_cron function body — is invisible here. A future
view joining labels to embeddings would escape this guard entirely. The partial rail
is `tests/test_migration_rls_grants.py`, which fires on an ungated view over a
registered admin-only relation; it only helps if the relation is registered.
"""

from __future__ import annotations

import pytest

from tests.sql_corpus import discover, first_keyword
from toolkit import tag_holdout

# The real anti-join, not merely a mention of the table. `hx` is the alias the
# shared constant uses, so matching it proves toolkit.tag_holdout.exclusion_for was
# formatted in rather than a hand-rolled copy that can drift from it. A statement
# that JOINS the exam deliberately (the exam-sitting reads) mentions the table but
# does not carry this, and must therefore be censused by name — which is the honest
# outcome: "excludes the exam" and "reads the exam on purpose" are different facts.
# Since migration 464 the exclusion is PURPOSE-narrowed: only holdout cohorts
# protect. The marker pins the join — a bare member anti-join no longer counts
# as the holdout exclusion (it would also exclude curated training material).
_MARKER = ("NOT EXISTS ( SELECT 1 FROM tag_exam_members hx "
           "JOIN tag_exam_cohorts hc")


def _norm(sql: str) -> str:
    return " ".join(sql.split())

# Statements that read image_tag_labels and legitimately do NOT exclude the exam.
# Keyed by "<file>::<constant>" or "<file>:<line>" as sql_corpus reports it; the
# value is why. Adding an entry is a deliberate act — that is the whole point.
_EXEMPT: dict[str, str] = {
    # --- the write path -------------------------------------------------------
    "_UPSERT_STATE_RETURNING_SQL":
        "The write. The operator's exam answers go THROUGH it — excluding the exam "
        "here would make the exam unanswerable.",
    "_UPSERT_STATE_SQL":
        "Same write, bulk variant (no RETURNING).",
    "_READ_STATE_SQL":
        "Reads back ONE (image_id, tag_id) cell the caller just wrote, so the API "
        "can report whether the human-wins guard suppressed it. Single cell, never "
        "a population.",

    # --- per-image reads, never a training population -------------------------
    "_LIST_TAGS_FOR_IMAGE_SQL":
        "One image's tags for the detail panel. Scoped to an image the caller "
        "already holds; it selects no population to train or grade on.",
    "_LIST_POSITIVE_TAGS_FOR_IMAGES_SQL":
        "Tag badges for images already on screen. Same reasoning.",

    # --- candidate-queue bookkeeping ------------------------------------------
    "_EXISTING_POOL_SQL":
        "Near-duplicate and per-property history. A holdout image's phash BLOCKING "
        "a twin from being drawn is desirable — it stops the training set acquiring "
        "a near-copy of an exam image.",
    "_SUMMARY_BY_DRAW_SQL":
        "Yield of the candidate queue, joined to tag_candidates — which the pool "
        "exclusion already keeps holdout images out of.",
    "_SUMMARY_BY_CATEGORY_SQL":
        "Same, bucketed by property type.",
    "_TAG_QUEUE_SQL":
        "Counts a tag's OPEN candidates to decide whether to draw more. Reads "
        "tag_candidates, which cannot contain a holdout image.",

    # --- reads that are ABOUT the exam ----------------------------------------
    "_CURATED_SEED_SQL":
        "The curated draw's worklist: the operator's own draft-positive marks, "
        "selected for RE-labeling through the exam UI (migration 464). Drafts "
        "are not training labels — the careful answers written over them are — "
        "and the cohort-blind member anti-join stops any re-seating.",
    "_DRAFT_POSITIVE_COUNTS_SQL":
        "Sizes the curated draw's rarest-first order by counting DRAFT positives "
        "per tag. Drafts are outside every training and grading read by source.",
    "_NEXT_MEMBER_SQL":
        "Serves the next unanswered exam image. It reads the exam deliberately — "
        "excluding it would leave nothing to answer.",
    "_PROGRESS_SQL":
        "Counts how much of the exam has a verdict on every routing tag. Scoped to "
        "one cohort; it selects no training population.",
    "_ANSWERS_SQL":
        "The review subpage's read: a sitting's own answers, served back for "
        "correction. Scoped to one cohort's members; edits go back through "
        "record_answer, so it selects no training population and opens no second "
        "write path.",

    # --- drawing the exam itself ----------------------------------------------
    "_PREEXISTING_LABELS_SQL":
        "Freezes an image's PRIOR label state at the moment it is drawn INTO the "
        "exam. Excluding exam members here would defeat the point — these images "
        "are about to become members — and it selects no training population, only "
        "the state of ids the caller already holds.",

    # --- legacy ---------------------------------------------------------------
    "_LIST_PROPOSALS_SQL":
        "The retiring dedup_sim proposal lane. It feeds no probe and goes with the "
        "rest of dedup_sim.",

}


def _reading_labels() -> list:
    """Statements that READ image_tag_labels.

    DELETEs are out of scope by construction: a DELETE's WHERE clause selects rows
    to remove, never a population to train or grade on, so it cannot leak the exam
    into training. Excluding them by statement type rather than by name also keeps
    the census off line numbers — the first version keyed the two inline deletes by
    line and broke the moment an import was added above them."""
    return [i for i in discover(include_inline=True, resolve_imports=True)
            if "image_tag_labels" in i.sql and first_keyword(i.sql) != "DELETE"]


def _key(item) -> str:
    """Constant NAME where there is one, origin otherwise.

    Deliberately not the line number: a constant that shifts three lines down
    because something above it grew is not a change worth breaking the build over,
    while a RENAMED constant is worth re-reviewing."""
    return item.name or item.origin


def test_every_label_read_excludes_the_exam_or_is_censused() -> None:
    offenders = []
    for item in _reading_labels():
        if _MARKER in _norm(item.sql) or _key(item) in _EXEMPT:
            continue
        offenders.append(f"{_key(item)}  ({item.origin})")
    assert not offenders, (
        "these read image_tag_labels without excluding the sealed exam:\n  "
        + "\n  ".join(offenders)
        + "\n\nEither format toolkit.tag_holdout.exclusion_for(<alias>) into the "
          "statement, or add it to _EXEMPT in this file WITH THE REASON. A training "
          "read that sees the exam makes every grade meaningless."
    )


def test_the_census_is_not_vacuous() -> None:
    # If discovery breaks, the loop above iterates nothing and passes. The corpus
    # held 19 label-reading statements when this was written; a large drop means
    # discovery is broken, not that the code got tidier.
    found = _reading_labels()
    assert len(found) >= 15, (
        f"only {len(found)} statements read image_tag_labels; sql_corpus discovery "
        "is probably broken, which would make this guard pass while protecting nothing"
    )


def test_the_guard_actually_rejects_an_unprotected_read() -> None:
    # The code this protects does not exist yet, so the predicate is exercised
    # against a statement written to fail it. Without this, a typo in the marker
    # would leave the whole census green and inert.
    trainer_shaped = (
        "SELECT image_id, state FROM image_tag_labels WHERE tag_id = %(tag_id)s"
    )
    assert _MARKER not in trainer_shaped
    assert "image_tag_labels" in trainer_shaped


def test_every_censused_entry_still_exists() -> None:
    # An exemption for a statement that has been renamed or deleted is a hole that
    # looks like a rule.
    keys = {_key(i) for i in _reading_labels()}
    stale = sorted(set(_EXEMPT) - keys)
    assert not stale, (
        "these _EXEMPT entries no longer match any statement — the code moved and "
        "the exemption is now protecting nothing:\n  " + "\n  ".join(stale)
    )


def test_the_exclusion_names_the_membership_table() -> None:
    assert _MARKER in _norm(tag_holdout.HOLDOUT_EXCLUSION)
    # Membership alone must exclude. A `sealed_at IS NOT NULL` qualifier here would
    # leave the entire drawing window unprotected — precisely when the images are
    # chosen but not yet answered.
    assert "sealed_at" not in tag_holdout.HOLDOUT_EXCLUSION


def test_the_sanctioned_training_door_excludes_the_exam() -> None:
    from toolkit.tag_holdout import _TRAINING_LABEL_ROWS_SQL
    assert _MARKER in _norm(_TRAINING_LABEL_ROWS_SQL)


def test_merely_naming_the_exam_table_does_not_satisfy_the_guard() -> None:
    # The weaker "mentions tag_exam_members" check passed a statement that JOINS
    # the exam as readily as one that excludes it, which is not what the docstring
    # promises. This pins the stronger reading.
    mentions_only = ("SELECT l.image_id FROM image_tag_labels l "
                     "JOIN tag_exam_members m ON m.image_id = l.image_id")
    assert "tag_exam_members" in mentions_only
    assert _MARKER not in _norm(mentions_only)


def test_the_exclusion_constant_is_not_named_like_a_statement() -> None:
    # sql_corpus hands any module-level *_SQL / *_QUERY constant to the PREPARE
    # sweep, which cannot parse a bare `AND NOT EXISTS (…)` fragment.
    from toolkit import tag_holdout as th
    for name in dir(th):
        if name.endswith(("_SQL", "_QUERY")) and isinstance(getattr(th, name), str):
            first = getattr(th, name).strip().split(None, 1)[0].upper()
            assert first in {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"}, (
                f"{name} is named like an executable statement but starts with {first!r}"
            )


@pytest.mark.parametrize("alias", ["itl", "p", "lab"])
def test_the_exclusion_binds_to_any_alias(alias: str) -> None:
    out = tag_holdout.exclusion_for(alias)
    assert f"hx.image_id = {alias}.image_id" in out
    assert "{alias}" not in out
