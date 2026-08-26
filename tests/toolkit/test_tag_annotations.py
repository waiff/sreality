"""Tag taxonomy CRUD and the tri-state (positive/negative/excluded) annotation
matrix — `tag_taxonomy` + `image_tag_labels` (migration 442). Hermetic fake conn
— no DB (the migration is verified separately, live)."""

from __future__ import annotations

import pytest

from tests.toolkit._labeling_fakes import _FakeConn, patch_unique_violation
from toolkit import tag_annotations as ta


@pytest.fixture(autouse=True)
def _patch_unique_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_unique_violation(monkeypatch)


@pytest.fixture()
def conn() -> _FakeConn:
    return _FakeConn()


# --- taxonomy ---------------------------------------------------------------


def test_add_tag(conn: _FakeConn) -> None:
    row = ta.add_tag(conn, label="interier - kuchyne")
    assert row["label"] == "interier - kuchyne"
    assert row["active"] is True
    assert row["id"] in conn.tag_taxonomy


def test_add_tag_normalizes_whitespace(conn: _FakeConn) -> None:
    row = ta.add_tag(conn, label="  interier   -   kuchyne  ")
    assert row["label"] == "interier - kuchyne"


def test_add_tag_rejects_empty(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        ta.add_tag(conn, label="   ")


def test_add_tag_rejects_duplicate(conn: _FakeConn) -> None:
    ta.add_tag(conn, label="garaz")
    with pytest.raises(ValueError, match="already exists"):
        ta.add_tag(conn, label="garaz")


def test_add_tag_caps_label_length_at_the_tables_check(conn: _FakeConn) -> None:
    # tag_taxonomy CHECKs char_length(label) BETWEEN 1 AND 100 — caught here so
    # the API returns a 422 instead of a driver error.
    with pytest.raises(ValueError):
        ta.add_tag(conn, label="x" * (ta.LABEL_MAX_CHARS + 1))
    row = ta.add_tag(conn, label="x" * ta.LABEL_MAX_CHARS)
    assert len(row["label"]) == ta.LABEL_MAX_CHARS


def test_add_tag_stores_family_and_blanks_become_null(conn: _FakeConn) -> None:
    assert ta.add_tag(conn, label="a", family="  interier ")["family"] == "interier"
    assert ta.add_tag(conn, label="b", family="   ")["family"] is None
    assert ta.add_tag(conn, label="c")["family"] is None


def test_rename_tag_leaves_every_dependent_row_untouched(conn: _FakeConn) -> None:
    """The point of the surrogate key (migration 442): image_tag_labels
    references tag_id, not label text, so a rename is ONE statement — no
    cascade rewrite of annotations, and none of the drift that invited."""
    tag = ta.add_tag(conn, label="old-name")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    conn.add_proposal(2, "m1", "old-name")
    conn.executed.clear()

    renamed = ta.rename_tag(conn, tag_id=tag["id"], new_label="new-name")

    assert renamed["label"] == "new-name"
    assert [sql for sql, _ in conn.executed] == [
        " ".join(
            "UPDATE tag_taxonomy SET label = %s WHERE id = %s "
            "RETURNING id, label, family, active, priority, ready_for_training, created_at".split()
        )
    ]
    assert conn.states_for(tag["id"]) == {1: "positive"}
    # label_proposals.label stays free text — a transient machine suggestion,
    # deliberately not a foreign key, so a rename does not touch it either.
    assert conn.proposals[(2, "m1")]["label"] == "old-name"


def test_rename_tag_unknown_id_raises(conn: _FakeConn) -> None:
    with pytest.raises(KeyError):
        ta.rename_tag(conn, tag_id=999, new_label="x")


def test_rename_tag_collision_raises(conn: _FakeConn) -> None:
    ta.add_tag(conn, label="a")
    b = ta.add_tag(conn, label="b")
    with pytest.raises(ValueError, match="already exists"):
        ta.rename_tag(conn, tag_id=b["id"], new_label="a")


def test_rename_tag_noop_when_unchanged(conn: _FakeConn) -> None:
    row = ta.add_tag(conn, label="same")
    assert ta.rename_tag(conn, tag_id=row["id"], new_label="same")["label"] == "same"


def test_rename_tag_normalizes_whitespace(conn: _FakeConn) -> None:
    row = ta.add_tag(conn, label="a")
    assert ta.rename_tag(conn, tag_id=row["id"], new_label=" b   c ")["label"] == "b c"


def test_rename_tag_rejects_empty(conn: _FakeConn) -> None:
    row = ta.add_tag(conn, label="a")
    with pytest.raises(ValueError):
        ta.rename_tag(conn, tag_id=row["id"], new_label="  ")


def test_remove_tag_cascades_its_annotations_only(conn: _FakeConn) -> None:
    doomed = ta.add_tag(conn, label="doomed")
    kept = ta.add_tag(conn, label="kept")
    ta.set_state(conn, image_id=1, tag_id=doomed["id"], state="positive")
    ta.set_state(conn, image_id=2, tag_id=doomed["id"], state="negative")
    ta.set_state(conn, image_id=1, tag_id=kept["id"], state="excluded")

    result = ta.remove_tag(conn, tag_id=doomed["id"])

    assert result == {"label": "doomed", "deleted_annotations": 2}
    assert doomed["id"] not in conn.tag_taxonomy
    assert conn.states_for(doomed["id"]) == {}
    # the images themselves — and every other tag's cells — are untouched.
    assert conn.states_for(kept["id"]) == {1: "excluded"}


def test_remove_tag_unknown_id_raises(conn: _FakeConn) -> None:
    with pytest.raises(KeyError):
        ta.remove_tag(conn, tag_id=999)


# --- set_tag_flags -----------------------------------------------------------


def test_add_tag_defaults_both_flags_false(conn: _FakeConn) -> None:
    row = ta.add_tag(conn, label="a")
    assert row["priority"] is False
    assert row["ready_for_training"] is False


def test_set_tag_flags_sets_priority_only(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    row = ta.set_tag_flags(conn, tag_id=tag["id"], priority=True)
    assert row["priority"] is True
    assert row["ready_for_training"] is False  # untouched, not clobbered


def test_set_tag_flags_sets_ready_for_training_only(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    row = ta.set_tag_flags(conn, tag_id=tag["id"], ready_for_training=True)
    assert row["ready_for_training"] is True
    assert row["priority"] is False


def test_set_tag_flags_sets_both_at_once(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    row = ta.set_tag_flags(conn, tag_id=tag["id"], priority=True, ready_for_training=True)
    assert row["priority"] is True
    assert row["ready_for_training"] is True


def test_set_tag_flags_can_clear_a_flag(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_tag_flags(conn, tag_id=tag["id"], priority=True)
    row = ta.set_tag_flags(conn, tag_id=tag["id"], priority=False)
    assert row["priority"] is False


def test_set_tag_flags_rejects_a_no_op_call(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    with pytest.raises(ValueError, match="nothing to update"):
        ta.set_tag_flags(conn, tag_id=tag["id"])


def test_set_tag_flags_unknown_id_raises(conn: _FakeConn) -> None:
    with pytest.raises(KeyError):
        ta.set_tag_flags(conn, tag_id=999, priority=True)


# --- get_or_create_tag_id ---------------------------------------------------


def test_get_or_create_tag_id_returns_an_existing_tag(conn: _FakeConn) -> None:
    existing = ta.add_tag(conn, label="existing")
    before = len(conn.tag_taxonomy)
    assert ta.get_or_create_tag_id(conn, label="existing") == existing["id"]
    assert len(conn.tag_taxonomy) == before


def test_get_or_create_tag_id_registers_a_freehand_label(conn: _FakeConn) -> None:
    # The coverage chart, the tag picker and the secondary-CLIP backfill all
    # read tag_taxonomy — a label that only reached image_tag_labels would be
    # invisible to every one of them, and the model could never propose it again.
    tag_id = ta.get_or_create_tag_id(conn, label="brand-new-tag", created_by="reviewer")
    assert conn.tag_taxonomy[tag_id]["label"] == "brand-new-tag"
    assert conn.tag_taxonomy[tag_id]["created_by"] == "reviewer"


def test_get_or_create_tag_id_normalizes_before_matching(conn: _FakeConn) -> None:
    # Without write-boundary normalization "site  plan\n" and "site plan" would
    # silently fragment one class into two tags.
    first = ta.get_or_create_tag_id(conn, label="site plan")
    assert ta.get_or_create_tag_id(conn, label="  site   plan\n") == first
    assert len(conn.tag_taxonomy) == 1


def test_get_or_create_tag_id_rejects_a_blank_label(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        ta.get_or_create_tag_id(conn, label="   ")


# --- set_state / bulk_set_state / clear_state -------------------------------


def test_set_state_writes_a_cell(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    out = ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")
    assert out["image_id"] == 7 and out["tag_id"] == tag["id"]
    assert out["state"] == "positive"
    assert out["updated_at"] is not None
    assert conn.states_for(tag["id"]) == {7: "positive"}


def test_set_state_is_idempotent_and_overwrites_in_place(conn: _FakeConn) -> None:
    # "No confirmation dialogs on individual toggles" — re-setting the same or a
    # different state must update the one cell, never accumulate rows.
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")
    ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")
    ta.set_state(conn, image_id=7, tag_id=tag["id"], state="excluded")
    assert conn.states_for(tag["id"]) == {7: "excluded"}
    assert len(conn.image_tag_labels) == 1


def test_set_state_preserves_created_by_on_a_later_decision(conn: _FakeConn) -> None:
    # Real Postgres' ON CONFLICT DO UPDATE SET state=..., updated_at=now() never
    # touches created_by — the first annotator stays on the record.
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive", created_by="operator")
    ta.set_state(conn, image_id=7, tag_id=tag["id"], state="negative", created_by="someone_else")
    assert conn.image_tag_labels[(7, tag["id"])]["created_by"] == "operator"


def test_set_state_rejects_an_unknown_state(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    with pytest.raises(ValueError):
        ta.set_state(conn, image_id=7, tag_id=tag["id"], state="maybe")
    assert conn.image_tag_labels == {}


def test_bulk_set_state_writes_every_id_under_one_tag(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    out = ta.bulk_set_state(conn, image_ids=[7, 8, 9], tag_id=tag["id"], state="negative")
    assert out == {"updated": 3, "tag_id": tag["id"], "state": "negative",
                   "image_ids": [7, 8, 9]}
    assert conn.states_for(tag["id"]) == {7: "negative", 8: "negative", 9: "negative"}


def test_bulk_set_state_dedupes_ids(conn: _FakeConn) -> None:
    # ON CONFLICT DO UPDATE cannot affect the same row twice in one statement —
    # a repeated id would abort the whole batch, so ids are deduped first.
    tag = ta.add_tag(conn, label="a")
    out = ta.bulk_set_state(conn, image_ids=[7, 8, 7, 8, 9], tag_id=tag["id"], state="positive")
    assert out["image_ids"] == [7, 8, 9]
    assert out["updated"] == 3


def test_bulk_set_state_rejects_an_empty_selection(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    with pytest.raises(ValueError):
        ta.bulk_set_state(conn, image_ids=[], tag_id=tag["id"], state="positive")


def test_bulk_set_state_caps_batch_size(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    with pytest.raises(ValueError):
        ta.bulk_set_state(
            conn, image_ids=list(range(ta.BULK_STATE_MAX + 1)), tag_id=tag["id"],
            state="positive",
        )


def test_bulk_set_state_rejects_an_unknown_state(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    with pytest.raises(ValueError):
        ta.bulk_set_state(conn, image_ids=[7], tag_id=tag["id"], state="maybe")
    assert conn.image_tag_labels == {}


def test_clear_state_reverts_a_cell_to_untouched(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")

    assert ta.clear_state(conn, image_id=7, tag_id=tag["id"]) == {
        "image_id": 7, "tag_id": tag["id"], "deleted": True,
    }
    assert conn.image_tag_labels == {}
    # No row IS the untouched state, so clearing twice is a reported no-op.
    assert ta.clear_state(conn, image_id=7, tag_id=tag["id"])["deleted"] is False


# --- list_images_for_tag ----------------------------------------------------


def test_list_images_for_tag_lists_the_whole_sample(conn: _FakeConn) -> None:
    """Driven FROM labeling_sample, not from the annotations — an image the
    secondary CLIP never proposed this tag for is still reachable."""
    tag = ta.add_tag(conn, label="a")
    conn.add_to_sample(1, added_at="t1")
    conn.add_to_sample(2, added_at="t2")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="excluded")

    rows = {r["image_id"]: r for r in ta.list_images_for_tag(conn, tag_id=tag["id"])}
    assert set(rows) == {1, 2}
    assert rows[1]["state"] == "excluded"
    assert rows[1]["storage_path"] == "img/1.jpg"
    assert rows[1]["created_by"] == "operator"
    # No row means untouched — surfaced as a state, not as a null the caller
    # has to interpret.
    assert rows[2]["state"] == "untouched"
    assert rows[2]["updated_at"] is None


def test_list_images_for_tag_ignores_images_outside_the_sample(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    conn.add_to_sample(1)
    conn.add_images(2)
    ta.set_state(conn, image_id=2, tag_id=tag["id"], state="positive")
    assert [r["image_id"] for r in ta.list_images_for_tag(conn, tag_id=tag["id"])] == [1]


def test_list_images_for_tag_filters_by_state(conn: _FakeConn) -> None:
    # The "kitchen = excluded" filter the tag-centric grid is built around.
    tag = ta.add_tag(conn, label="kuchyne")
    for image_id in (1, 2, 3, 4):
        conn.add_to_sample(image_id, added_at=f"t{image_id}")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    ta.set_state(conn, image_id=2, tag_id=tag["id"], state="excluded")
    ta.set_state(conn, image_id=3, tag_id=tag["id"], state="negative")

    def ids(state: str | None) -> set[int]:
        return {r["image_id"] for r in ta.list_images_for_tag(conn, tag_id=tag["id"], state=state)}

    assert ids("positive") == {1}
    assert ids("excluded") == {2}
    assert ids("negative") == {3}
    assert ids("untouched") == {4}
    assert ids(None) == {1, 2, 3, 4}


def test_list_images_for_tag_scopes_state_to_the_requested_tag(conn: _FakeConn) -> None:
    a = ta.add_tag(conn, label="a")
    b = ta.add_tag(conn, label="b")
    conn.add_to_sample(1)
    ta.set_state(conn, image_id=1, tag_id=b["id"], state="positive")
    rows = ta.list_images_for_tag(conn, tag_id=a["id"])
    assert [r["state"] for r in rows] == ["untouched"]


def test_list_images_for_tag_rejects_an_unknown_state(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        ta.list_images_for_tag(conn, tag_id=1, state="maybe")


def test_list_images_for_tag_clamps_the_limit(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    for image_id in range(1, 6):
        conn.add_to_sample(image_id, added_at=f"t{image_id}")
    assert len(ta.list_images_for_tag(conn, tag_id=tag["id"], limit=2)) == 2
    # 0/negative floors to 1 rather than returning an empty grid...
    assert len(ta.list_images_for_tag(conn, tag_id=tag["id"], limit=0)) == 1
    # ...and an over-large ask is capped, not honoured.
    ta.list_images_for_tag(conn, tag_id=tag["id"], limit=10_000)
    assert conn.executed[-1][1]["limit"] == ta.IMAGE_LIST_MAX


def test_list_images_for_tag_orders_newest_first_with_a_stable_tiebreaker(
    conn: _FakeConn,
) -> None:
    # A whole grow lands in one transaction sharing one added_at; without
    # image_id in the sort the grid reshuffles under the operator between
    # refetches (the ORDER BY timestamp-ties lesson).
    tag = ta.add_tag(conn, label="a")
    for image_id in (1, 2, 3):
        conn.add_to_sample(image_id, added_at="t1")
    conn.add_to_sample(4, added_at="t2")
    rows = ta.list_images_for_tag(conn, tag_id=tag["id"])
    assert [r["image_id"] for r in rows] == [4, 3, 2, 1]


# --- list_tags_for_image -----------------------------------------------------


def test_list_tags_for_image_lists_every_active_tag(conn: _FakeConn) -> None:
    # The mirror of list_images_for_tag: fixed image, varying tag.
    kitchen = ta.add_tag(conn, label="kuchyne", family="interier")
    living = ta.add_tag(conn, label="obyvak", family="interier")
    ta.set_state(conn, image_id=1, tag_id=kitchen["id"], state="positive")
    ta.set_state(conn, image_id=1, tag_id=living["id"], state="excluded")

    rows = {r["label"]: r for r in ta.list_tags_for_image(conn, image_id=1)}
    assert rows["kuchyne"]["state"] == "positive"
    assert rows["obyvak"]["state"] == "excluded"


def test_list_tags_for_image_shows_untouched_for_a_tag_with_no_row(conn: _FakeConn) -> None:
    ta.add_tag(conn, label="a")
    rows = ta.list_tags_for_image(conn, image_id=99)
    assert rows == [{"id": 1, "label": "a", "family": None, "state": "untouched", "updated_at": None}]


def test_list_tags_for_image_excludes_inactive_tags(conn: _FakeConn) -> None:
    a = ta.add_tag(conn, label="a")
    ta.add_tag(conn, label="b")
    conn.tag_taxonomy[a["id"]]["active"] = False
    labels = [r["label"] for r in ta.list_tags_for_image(conn, image_id=1)]
    assert labels == ["b"]


def test_list_tags_for_image_groups_by_family_nulls_last(conn: _FakeConn) -> None:
    ta.add_tag(conn, label="standalone")
    ta.add_tag(conn, label="obyvak", family="interier")
    ta.add_tag(conn, label="fasada", family="exterier")
    families = [r["family"] for r in ta.list_tags_for_image(conn, image_id=1)]
    # Families sort before the NULL/standalone tail; alphabetical within each.
    assert families == ["exterier", "interier", None]


def test_list_tags_for_image_is_scoped_to_the_requested_image(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    rows = ta.list_tags_for_image(conn, image_id=2)
    assert rows == [{"id": tag["id"], "label": "a", "family": None, "state": "untouched", "updated_at": None}]


# --- list_positive_tags_for_images -------------------------------------------


def test_list_positive_tags_for_images_batches_the_lookup(conn: _FakeConn) -> None:
    kitchen = ta.add_tag(conn, label="kuchyne")
    garage = ta.add_tag(conn, label="garaz")
    ta.set_state(conn, image_id=1, tag_id=kitchen["id"], state="positive")
    ta.set_state(conn, image_id=1, tag_id=garage["id"], state="positive")
    ta.set_state(conn, image_id=2, tag_id=kitchen["id"], state="positive")

    rows = ta.list_positive_tags_for_images(conn, image_ids=[1, 2])
    by_image: dict[int, list[str]] = {}
    for r in rows:
        by_image.setdefault(r["image_id"], []).append(r["label"])
    assert by_image[1] == ["garaz", "kuchyne"]  # ordered by label within an image
    assert by_image[2] == ["kuchyne"]


def test_list_positive_tags_for_images_excludes_non_positive_states(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="negative")
    ta.set_state(conn, image_id=2, tag_id=tag["id"], state="excluded")
    assert ta.list_positive_tags_for_images(conn, image_ids=[1, 2]) == []


def test_list_positive_tags_for_images_ignores_images_outside_the_batch(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    ta.set_state(conn, image_id=2, tag_id=tag["id"], state="positive")
    rows = ta.list_positive_tags_for_images(conn, image_ids=[1])
    assert [r["image_id"] for r in rows] == [1]


def test_list_positive_tags_for_images_dedupes_ids(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    rows = ta.list_positive_tags_for_images(conn, image_ids=[1, 1, 1])
    assert len(rows) == 1


def test_list_positive_tags_for_images_empty_selection_is_a_no_op(conn: _FakeConn) -> None:
    assert ta.list_positive_tags_for_images(conn, image_ids=[]) == []
    assert conn.executed == []


def test_list_positive_tags_for_images_caps_batch_size(conn: _FakeConn) -> None:
    with pytest.raises(ValueError, match="at most"):
        ta.list_positive_tags_for_images(conn, image_ids=list(range(ta.BATCH_IMAGE_MAX + 1)))


# --- tag_overview -----------------------------------------------------------


def test_tag_overview_shape(conn: _FakeConn) -> None:
    a = ta.add_tag(conn, label="a", family="interier")
    ta.add_tag(conn, label="b")
    ta.set_state(conn, image_id=1, tag_id=a["id"], state="positive")
    ta.set_state(conn, image_id=2, tag_id=a["id"], state="negative")
    ta.set_state(conn, image_id=3, tag_id=a["id"], state="excluded")
    conn.add_proposal(4, "m1", "a", status="pending")
    conn.add_proposal(5, "m1", "a", status="dismissed")
    conn.add_to_sample(1)
    conn.add_to_sample(2)

    overview = ta.tag_overview(conn)
    assert overview["sample_size"] == 2
    row_a = next(r for r in overview["tags"] if r["label"] == "a")
    assert row_a["id"] == a["id"]
    assert row_a["family"] == "interier"
    assert row_a["active"] is True
    assert row_a["positive_count"] == 1
    assert row_a["negative_count"] == 1
    assert row_a["excluded_count"] == 1
    assert row_a["gate_count"] == 1
    assert row_a["border_case_count"] == 0
    assert row_a["pending_count"] == 1
    assert row_a["dismissed_count"] == 1
    # A tag nobody has annotated yet still appears, with zeros — the coverage
    # strip has to show what is missing, not only what exists.
    row_b = next(r for r in overview["tags"] if r["label"] == "b")
    assert row_b["positive_count"] == 0
    assert row_b["pending_count"] == 0


def test_tag_overview_keeps_border_cases_out_of_the_gate_count(conn: _FakeConn) -> None:
    """A border case does not count toward Gate 1 (operator decision, 2026-08-21):
    an image nobody could classify is not evidence a tag is learnable. It stays a
    positive — `positive_count`, the honest inventory a tag REMOVE would delete,
    still includes it — but the gate reads `gate_count`."""
    tag = ta.add_tag(conn, label="a")
    for image_id in (1, 2, 3):
        ta.set_state(conn, image_id=image_id, tag_id=tag["id"], state="positive")
    conn.border_cases.add(2)
    # Flagged but never annotated: it belongs to no tag, so it lands in no count.
    conn.border_cases.add(99)

    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["positive_count"] == 3
    assert row["gate_count"] == 2
    assert row["border_case_count"] == 1
    assert row["gate_count"] + row["border_case_count"] == row["positive_count"]


def test_tag_overview_border_case_split_applies_to_positives_only(conn: _FakeConn) -> None:
    # gate_count/border_case_count partition the POSITIVES; a border-cased
    # negative or excluded cell must not be double-counted into either.
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="negative")
    ta.set_state(conn, image_id=2, tag_id=tag["id"], state="excluded")
    conn.border_cases.update({1, 2})

    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["positive_count"] == 0
    assert row["gate_count"] == 0
    assert row["border_case_count"] == 0
    assert row["negative_count"] == 1
    assert row["excluded_count"] == 1


def test_clearing_a_border_case_returns_the_image_to_the_gate_count(
    conn: _FakeConn,
) -> None:
    """"…unless removed from the border case group": the exclusion is a JOIN, not
    a stamp on the annotation, so unflagging restores the count with no
    re-annotation."""
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    conn.border_cases.add(1)
    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["gate_count"] == 0

    conn.border_cases.discard(1)
    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["gate_count"] == 1
    assert row["border_case_count"] == 0


def test_tag_overview_is_ordered_by_label(conn: _FakeConn) -> None:
    for label in ("c", "a", "b"):
        ta.add_tag(conn, label=label)
    assert [r["label"] for r in ta.tag_overview(conn)["tags"]] == ["a", "b", "c"]
