"""Sample growth and proposal review for the NEW DEDUP Labeling program —
`dedup_sim.labeling_sample` + `dedup_sim.label_proposals` (migration 373).
Taxonomy and the tri-state annotation matrix live in
tests/toolkit/test_tag_annotations.py; the shared fake conn carries those tables
too, so a proposal decision exercises the REAL tag_annotations write path
(get_or_create_tag_id + set_state) rather than a mock. Hermetic — no DB."""

from __future__ import annotations

import pytest

from tests.toolkit._labeling_fakes import _FakeConn, patch_unique_violation
from toolkit import dedup_sim_labeling as dsl
from toolkit import tag_annotations as ta


@pytest.fixture(autouse=True)
def _patch_unique_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_unique_violation(monkeypatch)


@pytest.fixture()
def conn() -> _FakeConn:
    return _FakeConn()


def _tag_state(conn: _FakeConn, label: str, image_id: int) -> str | None:
    tag = conn.tag_by_label(label)
    if tag is None:
        return None
    cell = conn.image_tag_labels.get((image_id, tag["id"]))
    return cell["state"] if cell else None


# --- sample ---------------------------------------------------------------


def test_grow_sample_adds_new_images(conn: _FakeConn) -> None:
    conn.add_images(1, 2, 3, 4, 5)
    result = dsl.grow_sample(conn, count=3)
    assert result["added"] == 3
    assert len(conn.sample) == 3


def test_grow_sample_skips_already_sampled(conn: _FakeConn) -> None:
    conn.add_images(1, 2, 3)
    conn.add_to_sample(3)
    result = dsl.grow_sample(conn, count=5)
    assert result["added"] == 2


def test_grow_sample_rejects_zero_or_negative(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        dsl.grow_sample(conn, count=0)


def test_grow_sample_rejects_over_max(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        dsl.grow_sample(conn, count=dsl.GROW_SAMPLE_MAX + 1)


def test_grow_sample_filters_by_category(conn: _FakeConn) -> None:
    conn.add_images(1, 2)
    conn.image_category = {1: "byt", 2: "dum"}
    result = dsl.grow_sample(conn, count=5, category_main="byt")
    assert result["added"] == 1
    assert 1 in conn.sample
    assert 2 not in conn.sample


def test_grow_sample_includes_images_with_no_property_yet(conn: _FakeConn) -> None:
    # A new listing whose property_maintenance attach hasn't run yet has no
    # properties row (rule #19: new rows land property_id NULL) — an
    # unfiltered grow must still pick it up (LEFT JOIN, not INNER JOIN).
    conn.add_images(1)
    result = dsl.grow_sample(conn, count=5)
    assert result["added"] == 1
    assert 1 in conn.sample


# --- list_proposals ---------------------------------------------------------


def test_list_proposals_filters(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a", proposed_at="t2")
    conn.add_proposal(2, "m1", "b", status="confirmed", proposed_at="t1")

    pending = dsl.list_proposals(conn, status="pending")
    assert len(pending) == 1
    assert pending[0]["image_id"] == 1

    by_label = dsl.list_proposals(conn, label="b")
    assert len(by_label) == 1
    assert by_label[0]["image_id"] == 2


def test_list_proposals_all_is_the_union_of_the_three_tabs(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a", proposed_at="t3")
    conn.add_proposal(2, "m1", "b", status="confirmed", proposed_at="t2")
    conn.add_proposal(3, "m1", "c", status="dismissed", proposed_at="t1")

    rows = dsl.list_proposals(conn, status="all")
    assert {r["image_id"]: r["status"] for r in rows} == {
        1: "pending", 2: "confirmed", 3: "dismissed",
    }
    # ...and so is an omitted status.
    assert len(dsl.list_proposals(conn)) == 3


def test_list_proposals_carries_the_current_state(conn: _FakeConn) -> None:
    """`current_state` is how the page greys an already-decided tile WITHOUT a
    second query — the tri-state successor to the old `trained_label`."""
    conn.add_proposal(1, "m1", "a", proposed_at="t2")
    conn.add_proposal(2, "m1", "a", proposed_at="t1")
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="excluded")

    rows = {r["image_id"]: r for r in dsl.list_proposals(conn, status="pending")}
    assert rows[1]["current_state"] == "excluded"
    # No row means untouched — the caller's cue that the tile is undecided.
    assert rows[2]["current_state"] is None


def test_list_proposals_current_state_is_scoped_to_the_proposals_own_tag(
    conn: _FakeConn,
) -> None:
    # A decision on some OTHER tag for the same image must not read as a
    # decision on this proposal (the join is on the proposal's own label).
    conn.add_proposal(1, "m1", "a")
    other = ta.add_tag(conn, label="b")
    ta.set_state(conn, image_id=1, tag_id=other["id"], state="positive")
    assert dsl.list_proposals(conn)[0]["current_state"] is None


def test_list_proposals_current_state_is_none_for_an_unregistered_tag(
    conn: _FakeConn,
) -> None:
    # A pending proposal's label may not exist in tag_taxonomy at all yet —
    # get_or_create_tag_id only registers it at review time, so the LEFT JOIN
    # must degrade to "untouched", not error.
    conn.add_proposal(1, "m1", "never-seen")
    assert dsl.list_proposals(conn)[0]["current_state"] is None


def test_list_proposals_orders_newest_first_with_a_stable_tiebreaker(
    conn: _FakeConn,
) -> None:
    # The backfill inserts a whole batch in one transaction, so every row shares
    # one proposed_at; without image_id the grid reshuffles between refetches.
    for image_id in (1, 2, 3):
        conn.add_proposal(image_id, "m1", "a", proposed_at="t1")
    conn.add_proposal(4, "m1", "a", proposed_at="t2")
    assert [r["image_id"] for r in dsl.list_proposals(conn)] == [4, 3, 2, 1]


def test_list_proposals_clamps_the_limit(conn: _FakeConn) -> None:
    for image_id in (1, 2, 3):
        conn.add_proposal(image_id, "m1", "a", proposed_at=f"t{image_id}")
    assert len(dsl.list_proposals(conn, limit=2)) == 2
    assert len(dsl.list_proposals(conn, limit=0)) == 1
    dsl.list_proposals(conn, limit=10_000)
    assert conn.executed[-1][1]["limit"] == dsl.PROPOSAL_LIST_MAX


# --- set_proposal_state -----------------------------------------------------


def test_set_proposal_state_positive_confirms_and_writes_a_positive_cell(
    conn: _FakeConn,
) -> None:
    conn.add_proposal(1, "m1", "a")
    result = dsl.set_proposal_state(conn, image_id=1, model="m1", state="positive")
    assert result["status"] == "confirmed"
    assert result["state"] == "positive"
    assert result["corrected"] is False
    assert conn.proposals[(1, "m1")]["status"] == "confirmed"
    assert _tag_state(conn, "a", 1) == "positive"


@pytest.mark.parametrize("state", ["negative", "excluded"])
def test_set_proposal_state_negative_and_excluded_dismiss_the_proposal(
    conn: _FakeConn, state: str,
) -> None:
    # The proposal row is review-queue bookkeeping with only two terminal
    # values; the real verdict is the tri-state cell it writes.
    conn.add_proposal(1, "m1", "a")
    result = dsl.set_proposal_state(conn, image_id=1, model="m1", state=state)
    assert result["status"] == "dismissed"
    assert conn.proposals[(1, "m1")]["status"] == "dismissed"
    assert _tag_state(conn, "a", 1) == state


def test_set_proposal_state_with_a_corrected_label(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a")
    result = dsl.set_proposal_state(
        conn, image_id=1, model="m1", state="positive", label="  b  ",
    )
    # The operator's correction is what the decision lands on...
    assert _tag_state(conn, "b", 1) == "positive"
    assert _tag_state(conn, "a", 1) is None
    assert result["label"] == "b"
    assert result["corrected"] is True
    # ...while the proposal keeps the model's own prediction, so "model said a,
    # operator said b" stays derivable without an extra column.
    assert result["proposed_label"] == "a"
    assert conn.proposals[(1, "m1")]["label"] == "a"


def test_set_proposal_state_registers_a_freehand_correction_in_the_taxonomy(
    conn: _FakeConn,
) -> None:
    # The coverage chart, the tag picker and the secondary-CLIP backfill all
    # read tag_taxonomy — a correction that only reached image_tag_labels would
    # be invisible to every one of them.
    conn.add_proposal(1, "m1", "a")
    assert conn.tag_by_label("brand-new-tag") is None

    dsl.set_proposal_state(conn, image_id=1, model="m1", state="positive", label="brand-new-tag")

    assert conn.tag_by_label("brand-new-tag") is not None
    assert _tag_state(conn, "brand-new-tag", 1) == "positive"


def test_set_proposal_state_does_not_duplicate_an_existing_tag(conn: _FakeConn) -> None:
    ta.add_tag(conn, label="existing")
    before = len(conn.tag_taxonomy)
    conn.add_proposal(1, "m1", "a")
    dsl.set_proposal_state(conn, image_id=1, model="m1", state="positive", label="existing")
    assert len(conn.tag_taxonomy) == before


def test_set_proposal_state_registers_the_proposed_label_too(conn: _FakeConn) -> None:
    # Unlike the old confirm_proposal (which only self-registered a CORRECTION),
    # every decision resolves its final label through get_or_create_tag_id — a
    # model-proposed tag that predates the taxonomy still gets a row, because
    # image_tag_labels.tag_id is a real foreign key now.
    conn.add_proposal(1, "m1", "off-taxonomy")
    dsl.set_proposal_state(conn, image_id=1, model="m1", state="negative")
    assert conn.tag_by_label("off-taxonomy") is not None
    assert _tag_state(conn, "off-taxonomy", 1) == "negative"


def test_set_proposal_state_blank_label_falls_back_to_the_proposal(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a")
    result = dsl.set_proposal_state(conn, image_id=1, model="m1", state="positive", label="   ")
    assert _tag_state(conn, "a", 1) == "positive"
    assert result["corrected"] is False


def test_set_proposal_state_same_label_is_not_flagged_as_corrected(conn: _FakeConn) -> None:
    # The UI always sends the picker's value, which is seeded from the
    # proposal — an untouched decision must not read as a correction.
    conn.add_proposal(1, "m1", "a")
    result = dsl.set_proposal_state(conn, image_id=1, model="m1", state="positive", label="a")
    assert result["corrected"] is False


def test_set_proposal_state_rejects_an_overlong_correction(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a")
    with pytest.raises(ValueError):
        dsl.set_proposal_state(
            conn, image_id=1, model="m1", state="positive",
            label="x" * (ta.LABEL_MAX_CHARS + 1),
        )
    # Rejected at the boundary — nothing was written, and the proposal is still
    # pending for a retry with a valid label.
    assert conn.image_tag_labels == {}
    assert conn.proposals[(1, "m1")]["status"] == "pending"


def test_set_proposal_state_rejects_an_unknown_state(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a")
    with pytest.raises(ValueError):
        dsl.set_proposal_state(conn, image_id=1, model="m1", state="maybe")
    assert conn.proposals[(1, "m1")]["status"] == "pending"
    assert conn.image_tag_labels == {}


def test_set_proposal_state_unknown_raises(conn: _FakeConn) -> None:
    with pytest.raises(KeyError):
        dsl.set_proposal_state(conn, image_id=1, model="m1", state="positive")


def test_set_proposal_state_already_reviewed_can_be_redecided(conn: _FakeConn) -> None:
    # There is only ONE write path into image_tag_labels here (unlike the old
    # confirm/dismiss split with ClipAudit's parallel Train CTA), so a repeat
    # call can never diverge the two stores — re-deciding just overwrites both
    # together.
    conn.add_proposal(1, "m1", "a", status="dismissed")
    result = dsl.set_proposal_state(conn, image_id=1, model="m1", state="positive")
    assert result["status"] == "confirmed"
    assert conn.proposals[(1, "m1")]["status"] == "confirmed"
    assert _tag_state(conn, "a", 1) == "positive"


def test_a_second_decision_on_a_decided_proposal_overwrites_the_first(
    conn: _FakeConn,
) -> None:
    conn.add_proposal(1, "m1", "a")
    dsl.set_proposal_state(conn, image_id=1, model="m1", state="positive")
    assert _tag_state(conn, "a", 1) == "positive"

    # Changing your mind is a normal tri-state action, not a stale retry —
    # the operator's LAST decision wins.
    dsl.set_proposal_state(conn, image_id=1, model="m1", state="negative")
    assert conn.proposals[(1, "m1")]["status"] == "dismissed"
    assert _tag_state(conn, "a", 1) == "negative"


def test_set_proposal_state_preserves_created_by_on_re_review(conn: _FakeConn) -> None:
    # Simulates deciding a second proposal for the same (image, tag) — real
    # Postgres' ON CONFLICT (image_id, tag_id) DO UPDATE never touches
    # created_by once set.
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="negative", created_by="operator")
    conn.add_proposal(1, "m2", "a")

    dsl.set_proposal_state(
        conn, image_id=1, model="m2", state="positive", reviewed_by="someone_else",
    )
    cell = conn.image_tag_labels[(1, tag["id"])]
    assert cell["state"] == "positive"
    assert cell["created_by"] == "operator"


def test_set_proposal_state_stamps_the_reviewer_on_the_proposal(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a")
    dsl.set_proposal_state(
        conn, image_id=1, model="m1", state="positive", reviewed_by="someone_else",
    )
    assert conn.proposals[(1, "m1")]["reviewed_by"] == "someone_else"
    assert conn.proposals[(1, "m1")]["reviewed_at"] is not None


# --- bulk_set_proposal_state ------------------------------------------------


def test_bulk_set_proposal_state_writes_a_cell_per_proposed_label(conn: _FakeConn) -> None:
    # Each row keeps its OWN proposed label — a batch can span more than one tag.
    conn.add_proposal(1, "m1", "a")
    conn.add_proposal(2, "m1", "b")
    result = dsl.bulk_set_proposal_state(conn, model="m1", image_ids=[1, 2], state="positive")
    assert result["updated"] == 2
    assert result["image_ids"] == [1, 2]
    assert conn.proposals[(1, "m1")]["status"] == "confirmed"
    assert conn.proposals[(2, "m1")]["status"] == "confirmed"
    assert _tag_state(conn, "a", 1) == "positive"
    assert _tag_state(conn, "b", 2) == "positive"


def test_bulk_set_proposal_state_negative_dismisses_and_still_annotates(
    conn: _FakeConn,
) -> None:
    # A dismissal is a real negative decision now, not a discard: it is exactly
    # the training signal a per-tag head needs.
    conn.add_proposal(1, "m1", "a")
    conn.add_proposal(2, "m1", "a")
    result = dsl.bulk_set_proposal_state(conn, model="m1", image_ids=[1, 2], state="negative")
    assert result["updated"] == 2
    assert conn.proposals[(1, "m1")]["status"] == "dismissed"
    assert _tag_state(conn, "a", 1) == "negative"
    assert _tag_state(conn, "a", 2) == "negative"


def test_bulk_set_proposal_state_can_redecide_already_reviewed(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a", status="dismissed")
    result = dsl.bulk_set_proposal_state(conn, model="m1", image_ids=[1], state="positive")
    assert result["updated"] == 1
    assert result["image_ids"] == [1]
    assert conn.proposals[(1, "m1")]["status"] == "confirmed"
    assert _tag_state(conn, "a", 1) == "positive"


def test_bulk_set_proposal_state_is_scoped_to_one_model(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a")
    conn.add_proposal(1, "m2", "a")
    dsl.bulk_set_proposal_state(conn, model="m1", image_ids=[1], state="positive")
    assert conn.proposals[(1, "m2")]["status"] == "pending"


def test_bulk_set_proposal_state_dedupes_ids(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a")
    conn.add_proposal(2, "m1", "b")
    result = dsl.bulk_set_proposal_state(
        conn, model="m1", image_ids=[1, 2, 1, 2], state="positive",
    )
    assert result["updated"] == 2
    assert result["image_ids"] == [1, 2]


def test_bulk_set_proposal_state_rejects_empty(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        dsl.bulk_set_proposal_state(conn, model="m1", image_ids=[], state="positive")


def test_bulk_set_proposal_state_rejects_over_max(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        dsl.bulk_set_proposal_state(
            conn, model="m1", image_ids=list(range(dsl.BULK_PROPOSAL_MAX + 1)),
            state="positive",
        )


def test_bulk_set_proposal_state_rejects_an_unknown_state(conn: _FakeConn) -> None:
    conn.add_proposal(1, "m1", "a")
    with pytest.raises(ValueError):
        dsl.bulk_set_proposal_state(conn, model="m1", image_ids=[1], state="maybe")
    assert conn.proposals[(1, "m1")]["status"] == "pending"
    assert conn.image_tag_labels == {}
