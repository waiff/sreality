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
                   "source": ta.SOURCE_HUMAN, "excluded_reason": None,
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


def test_bulk_set_state_for_image_writes_every_tag_on_one_image(conn: _FakeConn) -> None:
    a = ta.add_tag(conn, label="a")
    b = ta.add_tag(conn, label="b")
    c = ta.add_tag(conn, label="c")
    out = ta.bulk_set_state_for_image(
        conn, image_id=7, tag_ids=[a["id"], b["id"], c["id"]], state="negative",
    )
    assert out == {
        "updated": 3, "image_id": 7, "state": "negative",
        "source": ta.SOURCE_HUMAN, "excluded_reason": None,
        "tag_ids": [a["id"], b["id"], c["id"]],
    }
    assert conn.image_tag_labels[(7, a["id"])]["state"] == "negative"
    assert conn.image_tag_labels[(7, b["id"])]["state"] == "negative"
    assert conn.image_tag_labels[(7, c["id"])]["state"] == "negative"


def test_bulk_set_state_for_image_never_touches_other_images(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=8, tag_id=tag["id"], state="positive")
    ta.bulk_set_state_for_image(conn, image_id=7, tag_ids=[tag["id"]], state="negative")
    assert conn.image_tag_labels[(8, tag["id"])]["state"] == "positive"


def test_bulk_set_state_for_image_dedupes_tag_ids(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    out = ta.bulk_set_state_for_image(
        conn, image_id=7, tag_ids=[tag["id"], tag["id"]], state="positive",
    )
    assert out["updated"] == 1
    assert out["tag_ids"] == [tag["id"]]


def test_bulk_set_state_for_image_rejects_an_empty_selection(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        ta.bulk_set_state_for_image(conn, image_id=7, tag_ids=[], state="positive")


def test_bulk_set_state_for_image_caps_batch_size(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        ta.bulk_set_state_for_image(
            conn, image_id=7, tag_ids=list(range(ta.BULK_STATE_MAX + 1)), state="positive",
        )


def test_bulk_set_state_for_image_rejects_an_unknown_state(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        ta.bulk_set_state_for_image(conn, image_id=7, tag_ids=[1], state="maybe")
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


def test_list_images_for_tag_lists_this_tags_candidate_queue(conn: _FakeConn) -> None:
    """Driven FROM tag_candidates (migration 449), not from the annotations — an
    image the secondary CLIP never proposed this tag for is still reachable, and
    the draw provenance rides along."""
    tag = ta.add_tag(conn, label="a")
    conn.add_candidate(tag["id"], 1, draw="centroid_head", category_main="byt",
                       pool_rank=1, drawn_at="t1")
    conn.add_candidate(tag["id"], 2, draw="random", category_main="pozemek",
                       pool_rank=812, drawn_at="t2")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="excluded")

    rows = {r["image_id"]: r for r in ta.list_images_for_tag(conn, tag_id=tag["id"])}
    assert set(rows) == {1, 2}
    assert rows[1]["state"] == "excluded"
    assert rows[1]["storage_path"] == "img/1.jpg"
    assert rows[1]["created_by"] == "operator"
    assert (rows[1]["draw"], rows[1]["category_main"], rows[1]["pool_rank"]) == (
        "centroid_head", "byt", 1,
    )
    assert rows[2]["draw"] == "random"
    # Queue membership is not a label: a candidate nobody has decided reads
    # untouched, never negative.
    assert rows[2]["state"] == "untouched"
    assert rows[2]["updated_at"] is None


def test_list_images_for_tag_is_scoped_to_this_tags_own_queue(conn: _FakeConn) -> None:
    # The pool is per tag now — another tag's candidate is not in this browse.
    a = ta.add_tag(conn, label="a")
    b = ta.add_tag(conn, label="b")
    conn.add_candidate(a["id"], 1)
    conn.add_candidate(b["id"], 2)
    assert [r["image_id"] for r in ta.list_images_for_tag(conn, tag_id=a["id"])] == [1]


def test_list_images_for_tag_keeps_a_decided_image_that_was_never_drawn(
    conn: _FakeConn,
) -> None:
    """The UNION's second arm. Without it the 1,440 legacy positives — decided
    long before candidate retrieval existed — would vanish from the browse and
    read as deleted labels."""
    tag = ta.add_tag(conn, label="a")
    conn.add_images(7)
    ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")
    rows = ta.list_images_for_tag(conn, tag_id=tag["id"], state="positive")
    assert [r["image_id"] for r in rows] == [7]
    # Never drawn, so it carries no draw provenance — not a fabricated one.
    assert (rows[0]["draw"], rows[0]["category_main"], rows[0]["pool_rank"]) == (
        None, None, None,
    )


def test_list_images_for_tag_filters_by_state(conn: _FakeConn) -> None:
    # The "kitchen = excluded" filter the tag-centric grid is built around.
    tag = ta.add_tag(conn, label="kuchyne")
    for image_id in (1, 2, 3, 4):
        conn.add_candidate(tag["id"], image_id, pool_rank=image_id, drawn_at="t1")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    ta.set_state(conn, image_id=2, tag_id=tag["id"], state="excluded")
    ta.set_state(conn, image_id=3, tag_id=tag["id"], state="negative")

    def ids(state: str | None) -> set[int]:
        return {r["image_id"] for r in ta.list_images_for_tag(conn, tag_id=tag["id"], state=state)}

    assert ids("positive") == {1}
    assert ids("excluded") == {2}
    assert ids("negative") == {3}
    # 'untouched' is now the tag's OPEN queue: candidates nobody has decided.
    assert ids("untouched") == {4}
    assert ids(None) == {1, 2, 3, 4}


def test_list_images_for_tag_scopes_state_to_the_requested_tag(conn: _FakeConn) -> None:
    a = ta.add_tag(conn, label="a")
    b = ta.add_tag(conn, label="b")
    conn.add_candidate(a["id"], 1)
    ta.set_state(conn, image_id=1, tag_id=b["id"], state="positive")
    rows = ta.list_images_for_tag(conn, tag_id=a["id"])
    assert [r["state"] for r in rows] == ["untouched"]


def test_list_images_for_tag_rejects_an_unknown_state(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        ta.list_images_for_tag(conn, tag_id=1, state="maybe")


def test_list_images_for_tag_clamps_the_limit(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    for image_id in range(1, 6):
        conn.add_candidate(tag["id"], image_id, pool_rank=image_id, drawn_at="t1")
    assert len(ta.list_images_for_tag(conn, tag_id=tag["id"], limit=2)) == 2
    # 0/negative floors to 1 rather than returning an empty grid...
    assert len(ta.list_images_for_tag(conn, tag_id=tag["id"], limit=0)) == 1
    # ...and an over-large ask is capped, not honoured.
    ta.list_images_for_tag(conn, tag_id=tag["id"], limit=10_000)
    assert conn.executed[-1][1]["limit"] == ta.IMAGE_LIST_MAX


def test_list_images_for_tag_orders_newest_draw_first_with_a_total_order(
    conn: _FakeConn,
) -> None:
    # A whole draw lands in one transaction sharing one drawn_at, so drawn_at
    # alone is not an order: pool_rank breaks the tie inside a draw and image_id
    # breaks THAT tie, or the grid reshuffles under the operator between refetches
    # (the ORDER BY timestamp-ties lesson).
    tag = ta.add_tag(conn, label="a")
    conn.add_candidate(tag["id"], 1, pool_rank=2, drawn_at="t1")
    conn.add_candidate(tag["id"], 2, pool_rank=1, drawn_at="t1")
    conn.add_candidate(tag["id"], 3, pool_rank=1, drawn_at="t1")
    conn.add_candidate(tag["id"], 4, pool_rank=9, drawn_at="t2")
    # Decided but never drawn: no drawn_at, so it sorts last (NULLS LAST).
    conn.add_images(5)
    ta.set_state(conn, image_id=5, tag_id=tag["id"], state="positive")

    rows = ta.list_images_for_tag(conn, tag_id=tag["id"])
    assert [r["image_id"] for r in rows] == [4, 3, 2, 1, 5]


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
    assert rows == [{
        "id": 1, "label": "a", "family": None, "state": "untouched",
        "updated_at": None, "source": None, "excluded_reason": None,
    }]


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
    assert rows == [{
        "id": tag["id"], "label": "a", "family": None, "state": "untouched",
        "updated_at": None, "source": None, "excluded_reason": None,
    }]


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
    conn.add_candidate(a["id"], 1, drawn_at="t1")
    conn.add_candidate(a["id"], 2, drawn_at="t2")

    overview = ta.tag_overview(conn)
    # Distinct images queued for at least ONE tag — a different quantity from the
    # retired `sample_size` (one pool shared by every tag), which is why the name
    # changed rather than the meaning.
    assert overview["candidate_image_count"] == 2
    assert "sample_size" not in overview
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
    assert (row_b["candidate_count"], row_b["candidate_open_count"]) == (0, 0)
    assert row_b["last_drawn_at"] is None


def test_tag_overview_reports_the_open_half_of_each_tags_queue(conn: _FakeConn) -> None:
    """`candidate_open_count` is the work LEFT — a candidate with any decision on
    this tag (positive, negative or excluded) is done. It is derived by joining
    image_tag_labels; the queue itself stores no state."""
    tag = ta.add_tag(conn, label="a")
    for image_id in (1, 2, 3):
        conn.add_candidate(tag["id"], image_id, drawn_at=f"t{image_id}")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    ta.set_state(conn, image_id=2, tag_id=tag["id"], state="negative")

    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["candidate_count"] == 3
    assert row["candidate_open_count"] == 1
    assert row["last_drawn_at"] == "t3"


def test_tag_overview_candidate_counts_are_per_tag(conn: _FakeConn) -> None:
    # One image can legitimately be queued for several tags; each tag counts its
    # own queue, and the header count is DISTINCT images across all of them.
    a = ta.add_tag(conn, label="a")
    b = ta.add_tag(conn, label="b")
    conn.add_candidate(a["id"], 1)
    conn.add_candidate(b["id"], 1)
    conn.add_candidate(b["id"], 2)

    overview = ta.tag_overview(conn)
    counts = {r["label"]: r["candidate_count"] for r in overview["tags"]}
    assert counts == {"a": 1, "b": 2}
    assert overview["candidate_image_count"] == 2


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


# --- provenance (migration 446) ----------------------------------------------
#
# WHAT THE FAKE CANNOT PROVE, stated once so no test below implies otherwise:
# `_FakeConn` runs no triggers and raises no CHECK/UNIQUE/FK violation, so
# `image_tag_label_events` and the `excluded_reason`-only-when-excluded CHECK are
# NOT exercised here — their gate is CI's migration-replay job, plus the
# migration-text assertions at the bottom of this file. What IS proved here is
# that the toolkit cannot PRODUCE a combination those constraints would reject,
# and the shapes/SQL it emits.


def test_set_state_defaults_to_a_human_decision_and_stamps_verified_at(
    conn: _FakeConn,
) -> None:
    tag = ta.add_tag(conn, label="a")
    out = ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")
    assert out["source"] == ta.SOURCE_HUMAN
    assert out["verified_at"] is not None
    assert out["applied"] is True
    assert out["excluded_reason"] is None


def test_set_state_leaves_verified_at_unstamped_for_a_machine_write(conn: _FakeConn) -> None:
    # 'machine' means NOBODY has checked. A verification timestamp on it would be
    # the exact lie the source vocabulary exists to prevent.
    tag = ta.add_tag(conn, label="a")
    out = ta.set_state(
        conn, image_id=7, tag_id=tag["id"], state="positive",
        source=ta.SOURCE_MACHINE, model="clip-vit-l",
    )
    assert out["source"] == ta.SOURCE_MACHINE
    assert out["verified_at"] is None
    assert conn.image_tag_labels[(7, tag["id"])]["model"] == "clip-vit-l"


def test_set_state_resolves_definition_id_from_the_tags_own_active_definition(
    conn: _FakeConn,
) -> None:
    """definition_id is never a parameter — it is resolved inside the INSERT by a
    subquery on the annotation's OWN tag_id, which is what makes citing another
    tag's definition (or a superseded one) structurally impossible."""
    tag = ta.add_tag(conn, label="a")
    other = ta.add_tag(conn, label="b")
    conn.set_active_definition(tag["id"], 91)
    conn.set_active_definition(other["id"], 92)

    out = ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")
    assert out["definition_id"] == 91

    sql = conn.executed[-1][0]
    assert "(SELECT id FROM tag_definitions WHERE tag_id = %(tag_id)s AND status = 'active')" in sql


def test_set_state_definition_id_is_null_when_the_tag_has_no_definition(
    conn: _FakeConn,
) -> None:
    # Every row predating migration 445 is in exactly this state. NULL is the
    # honest answer, not a gap to be filled in.
    tag = ta.add_tag(conn, label="a")
    assert ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")[
        "definition_id"
    ] is None


def test_no_write_path_accepts_a_definition_id_or_verified_at_parameter() -> None:
    """Both are DERIVED, never supplied: a caller-chosen definition_id could cite
    another tag's wording and a caller-chosen verified_at could claim a human
    looked at a cell nobody opened."""
    import inspect

    for fn in (ta.set_state, ta.bulk_set_state, ta.bulk_set_state_for_image):
        params = set(inspect.signature(fn).parameters)
        assert "definition_id" not in params, fn.__name__
        assert "verified_at" not in params, fn.__name__


def test_set_state_rejects_a_source_outside_the_writable_vocabulary(conn: _FakeConn) -> None:
    # backfill_442 is historical fact about 72,058 manufactured rows — never
    # something a caller may claim about a new one.
    tag = ta.add_tag(conn, label="a")
    with pytest.raises(ValueError, match="source must be one of"):
        ta.set_state(
            conn, image_id=7, tag_id=tag["id"], state="positive",
            source=ta.SOURCE_BACKFILL_442,
        )
    with pytest.raises(ValueError, match="source must be one of"):
        ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive", source="robot")
    assert conn.image_tag_labels == {}


def test_set_state_rejects_an_unknown_excluded_reason(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    with pytest.raises(ValueError, match="excluded_reason must be one of"):
        ta.set_state(
            conn, image_id=7, tag_id=tag["id"], state="excluded", excluded_reason="dunno",
        )
    assert conn.image_tag_labels == {}


def test_set_state_stores_both_excluded_reasons(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    amb = ta.set_state(
        conn, image_id=7, tag_id=tag["id"], state="excluded",
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    pruned = ta.set_state(
        conn, image_id=8, tag_id=tag["id"], state="excluded",
        excluded_reason=ta.EXCLUDED_PRUNED,
    )
    assert amb["excluded_reason"] == "ambiguous"
    assert pruned["excluded_reason"] == "pruned"


def test_set_state_drops_an_excluded_reason_on_a_non_excluded_state(conn: _FakeConn) -> None:
    """The DB CHECK forbids the combination outright; the toolkit normalises it to
    NULL so the CHECK can never be the thing that fires. A reason left on a
    positive row would silently poison the ambiguity rate."""
    tag = ta.add_tag(conn, label="a")
    out = ta.set_state(
        conn, image_id=7, tag_id=tag["id"], state="positive",
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    assert out["excluded_reason"] is None
    assert conn.executed[-1][1]["excluded_reason"] is None


def test_set_state_drops_a_model_on_a_purely_human_decision(conn: _FakeConn) -> None:
    # image_tag_labels_model_check: a model name only means something when a
    # machine had a hand in the decision.
    tag = ta.add_tag(conn, label="a")
    ta.set_state(
        conn, image_id=7, tag_id=tag["id"], state="positive",
        source=ta.SOURCE_HUMAN, model="clip-vit-l",
    )
    assert conn.image_tag_labels[(7, tag["id"])]["model"] is None


def test_set_state_keeps_the_model_on_a_human_confirmed_decision(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_state(
        conn, image_id=7, tag_id=tag["id"], state="positive",
        source=ta.SOURCE_HUMAN_CONFIRMED, model="clip-vit-l",
    )
    assert conn.image_tag_labels[(7, tag["id"])]["model"] == "clip-vit-l"


def test_a_machine_write_is_refused_on_a_cell_a_human_already_decided(
    conn: _FakeConn,
) -> None:
    """The human-wins rail lives in the upsert's DO UPDATE ... WHERE, not in a
    convention every future writer has to re-remember. Machine proposes, human
    disposes — enforced in SQL."""
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")

    out = ta.set_state(
        conn, image_id=7, tag_id=tag["id"], state="negative",
        source=ta.SOURCE_MACHINE, model="clip-vit-l",
    )
    assert out["applied"] is False
    # The standing human decision is what comes back, untouched.
    assert out["state"] == "positive"
    assert out["source"] == ta.SOURCE_HUMAN
    assert conn.states_for(tag["id"]) == {7: "positive"}


def test_a_machine_write_lands_on_untouched_machine_and_backfill_cells(
    conn: _FakeConn,
) -> None:
    tag = ta.add_tag(conn, label="a")
    # untouched
    assert ta.set_state(
        conn, image_id=1, tag_id=tag["id"], state="positive", source=ta.SOURCE_MACHINE,
    )["applied"] is True
    # already machine-decided
    assert ta.set_state(
        conn, image_id=1, tag_id=tag["id"], state="negative", source=ta.SOURCE_MACHINE,
    )["applied"] is True
    # migration 442's manufactured fiction — a machine may overwrite it freely
    conn.seed_cell(2, tag["id"], "negative", source=ta.SOURCE_BACKFILL_442,
                   created_by="backfill:image_training_examples")
    assert ta.set_state(
        conn, image_id=2, tag_id=tag["id"], state="positive", source=ta.SOURCE_MACHINE,
    )["applied"] is True


def test_a_human_write_always_lands_including_over_a_machine_decision(
    conn: _FakeConn,
) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_state(
        conn, image_id=7, tag_id=tag["id"], state="positive",
        source=ta.SOURCE_MACHINE, model="clip-vit-l",
    )
    out = ta.set_state(conn, image_id=7, tag_id=tag["id"], state="negative")
    assert out["applied"] is True
    # A human OVERRIDING a machine is a plain human decision — there is no fifth
    # 'human_corrected' source; the disagreement lives in the event history.
    assert out["source"] == ta.SOURCE_HUMAN
    assert out["state"] == "negative"


def test_the_upsert_never_erases_an_existing_verification(conn: _FakeConn) -> None:
    """Asserted on the SQL, not on the fake: `verified_at` is carried forward with
    coalesce so a later machine touch cannot blank a human's verification. The
    fake would happily accept either form — Postgres is the enforcer."""
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")
    sql = conn.executed[-1][0]
    assert "verified_at = coalesce(excluded.verified_at, image_tag_labels.verified_at)" in sql
    assert (
        "WHERE excluded.source <> 'machine' OR image_tag_labels.source IN "
        "('machine', 'backfill_442')" in sql
    )


def test_bulk_set_state_threads_provenance_onto_every_row(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    out = ta.bulk_set_state(
        conn, image_ids=[7, 8], tag_id=tag["id"], state="excluded",
        source=ta.SOURCE_HUMAN_CONFIRMED, model="clip-vit-l",
        excluded_reason=ta.EXCLUDED_PRUNED,
    )
    assert out["source"] == ta.SOURCE_HUMAN_CONFIRMED
    assert out["excluded_reason"] == "pruned"
    for image_id in (7, 8):
        cell = conn.image_tag_labels[(image_id, tag["id"])]
        assert cell["source"] == ta.SOURCE_HUMAN_CONFIRMED
        assert cell["excluded_reason"] == "pruned"
        assert cell["model"] == "clip-vit-l"


def test_bulk_set_state_for_image_threads_provenance_onto_every_row(conn: _FakeConn) -> None:
    a = ta.add_tag(conn, label="a")
    b = ta.add_tag(conn, label="b")
    out = ta.bulk_set_state_for_image(
        conn, image_id=7, tag_ids=[a["id"], b["id"]], state="excluded",
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    assert out["excluded_reason"] == "ambiguous"
    assert out["source"] == ta.SOURCE_HUMAN
    for tag_id in (a["id"], b["id"]):
        assert conn.image_tag_labels[(7, tag_id)]["excluded_reason"] == "ambiguous"


def test_bulk_paths_report_cells_submitted_not_cells_changed(conn: _FakeConn) -> None:
    """KNOWN LIMIT, pre-existing and unchanged: executemany has no RETURNING, so a
    machine batch cannot report which rows the human-wins rail refused. The
    machine loop must SELECT its candidates rather than trust this count."""
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=7, tag_id=tag["id"], state="positive")
    out = ta.bulk_set_state(
        conn, image_ids=[7, 8], tag_id=tag["id"], state="negative",
        source=ta.SOURCE_MACHINE,
    )
    assert out["updated"] == 2  # submitted
    assert conn.states_for(tag["id"]) == {7: "positive", 8: "negative"}  # one refused


def test_list_images_for_tag_carries_provenance(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    conn.add_candidate(tag["id"], 1)
    conn.add_candidate(tag["id"], 2)
    ta.set_state(
        conn, image_id=1, tag_id=tag["id"], state="excluded",
        excluded_reason=ta.EXCLUDED_PRUNED,
    )
    rows = {r["image_id"]: r for r in ta.list_images_for_tag(conn, tag_id=tag["id"])}
    assert rows[1]["source"] == ta.SOURCE_HUMAN
    assert rows[1]["excluded_reason"] == "pruned"
    # An untouched cell has no provenance at all — absence is not a negative.
    assert rows[2]["source"] is None
    assert rows[2]["excluded_reason"] is None


def test_list_tags_for_image_carries_provenance(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_state(
        conn, image_id=1, tag_id=tag["id"], state="excluded",
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    row = ta.list_tags_for_image(conn, image_id=1)[0]
    assert row["source"] == ta.SOURCE_HUMAN
    assert row["excluded_reason"] == "ambiguous"


# --- the ambiguity signal -----------------------------------------------------


def test_tag_overview_reports_the_provenance_inventory(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    ta.set_state(
        conn, image_id=2, tag_id=tag["id"], state="positive",
        source=ta.SOURCE_HUMAN_CONFIRMED, model="clip-vit-l",
    )
    ta.set_state(
        conn, image_id=3, tag_id=tag["id"], state="negative", source=ta.SOURCE_MACHINE,
    )
    conn.seed_cell(4, tag["id"], "negative", source=ta.SOURCE_BACKFILL_442,
                   created_by="backfill:image_training_examples")

    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["human_count"] == 2  # human + human_confirmed
    assert row["machine_count"] == 1
    assert row["backfill_count"] == 1
    # The tri-state counts deliberately STILL include the backfill rows; narrowing
    # them belongs to the separate gated deletion PR. backfill_count is what makes
    # that inventory legible.
    assert row["negative_count"] == 2


def test_tag_overview_echoes_the_threshold_and_floor_it_computed_with(
    conn: _FakeConn,
) -> None:
    # One definition of 0.15, computed server-side and echoed — so no surface
    # keeps a second hardcoded copy of the number it renders.
    overview = ta.tag_overview(conn)
    assert overview["ambiguity_threshold"] == ta.AMBIGUITY_RATE_THRESHOLD
    assert overview["ambiguity_min_decisions"] == ta.AMBIGUITY_MIN_DECISIONS
    assert conn.executed[-1][1] == {
        "threshold": ta.AMBIGUITY_RATE_THRESHOLD,
        "min_decisions": ta.AMBIGUITY_MIN_DECISIONS,
    }


def test_ambiguity_rate_is_ambiguous_exclusions_over_decisions(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    for image_id in range(1, 4):
        ta.set_state(conn, image_id=image_id, tag_id=tag["id"], state="positive")
    ta.set_state(
        conn, image_id=4, tag_id=tag["id"], state="excluded",
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["decided_count"] == 4
    assert row["ambiguous_count"] == 1
    assert row["ambiguity_rate"] == 0.25


def test_pruned_exclusions_are_outside_the_rates_numerator_AND_denominator(
    conn: _FakeConn,
) -> None:
    """The entire point of splitting the two reasons. Leaving pruned rows in the
    DENOMINATOR would let pruning DILUTE the signal — prune a hundred images and a
    genuinely broken tag reads healthy."""
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    ta.set_state(
        conn, image_id=2, tag_id=tag["id"], state="excluded",
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    before = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert before["decided_count"] == 2
    assert before["ambiguity_rate"] == 0.5

    for image_id in range(10, 110):
        ta.set_state(
            conn, image_id=image_id, tag_id=tag["id"], state="excluded",
            excluded_reason=ta.EXCLUDED_PRUNED,
        )
    after = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert after["pruned_count"] == 100
    assert after["decided_count"] == 2       # unmoved
    assert after["ambiguity_rate"] == 0.5    # undiluted


def test_backfill_442_rows_are_outside_the_rates_denominator(conn: _FakeConn) -> None:
    """72,058 manufactured negatives would drive every tag's rate to ~0 and the
    signal would never fire once — the concrete reason this ships before any more
    labeling."""
    tag = ta.add_tag(conn, label="a")
    ta.set_state(
        conn, image_id=1, tag_id=tag["id"], state="excluded",
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    for image_id in range(10, 60):
        conn.seed_cell(image_id, tag["id"], "negative", source=ta.SOURCE_BACKFILL_442,
                       created_by="backfill:image_training_examples")

    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["backfill_count"] == 50
    assert row["decided_count"] == 1
    assert row["ambiguity_rate"] == 1.0


def test_unverified_machine_rows_are_outside_the_rates_denominator_too(
    conn: _FakeConn,
) -> None:
    """Exactly the backfill_442 argument, applied to the loop this PR builds the
    substrate for: 10,000 machine negatives nobody has checked would bury 10
    ambiguous human calls out of 20 as thoroughly as 72,058 manufactured ones. The
    rate measures HUMAN indecision — "go fix the DEFINITION" is a human signal."""
    tag = ta.add_tag(conn, label="a")
    ta.set_state(
        conn, image_id=1, tag_id=tag["id"], state="excluded",
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    for image_id in range(10, 60):
        ta.set_state(
            conn, image_id=image_id, tag_id=tag["id"], state="negative",
            source=ta.SOURCE_MACHINE, model="clip-vit-l",
        )
    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["machine_count"] == 50
    assert row["decided_count"] == 1
    assert row["ambiguity_rate"] == 1.0


def test_an_excluded_cell_with_no_reason_counts_as_ambiguous(conn: _FakeConn) -> None:
    """The reason column is nullable (the one legacy excluded row predates it, and
    a non-SPA caller can omit it). Counting such a cell as neither ambiguous NOR
    pruned NOR decided would give it exactly the treatment reserved for a prune —
    an unnamed third bucket, invisible to the very diagnostic the two-reason split
    protects — while the grid renders it as ambiguous. A deliberate prune always
    names itself; an unexplained exclusion is "nobody could decide"."""
    tag = ta.add_tag(conn, label="a")
    ta.set_state(conn, image_id=1, tag_id=tag["id"], state="positive")
    ta.set_state(conn, image_id=2, tag_id=tag["id"], state="excluded")  # no reason

    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["ambiguous_count"] == 1
    assert row["pruned_count"] == 0
    assert row["decided_count"] == 2
    assert row["ambiguity_rate"] == 0.5


def test_the_rates_numerator_is_published_from_its_own_population(
    conn: _FakeConn,
) -> None:
    """ambiguous_count is the whole inventory; ambiguous_decided_count is what the
    rate actually divided. A surface pairing the first with decided_count would
    state a fraction nobody computed."""
    tag = ta.add_tag(conn, label="a")
    ta.set_state(
        conn, image_id=1, tag_id=tag["id"], state="excluded",
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    ta.set_state(conn, image_id=2, tag_id=tag["id"], state="positive")
    ta.set_state(
        conn, image_id=3, tag_id=tag["id"], state="excluded", source=ta.SOURCE_MACHINE,
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["ambiguous_count"] == 2          # inventory, machine row included
    assert row["ambiguous_decided_count"] == 1  # the rate's numerator
    assert row["decided_count"] == 2
    assert row["ambiguity_rate"] == 0.5


def test_a_tag_with_no_decisions_has_a_null_rate_not_zero(conn: _FakeConn) -> None:
    # "LLM health checks false-green": a recency gate plus $0 spend read as
    # maximally healthy for eleven days. No decisions is UNKNOWN, not healthy.
    ta.add_tag(conn, label="a")
    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["ambiguity_rate"] is None
    assert row["ambiguity_alert"] is False


def test_the_ambiguity_alert_needs_both_the_threshold_and_the_floor(
    conn: _FakeConn,
) -> None:
    tag = ta.add_tag(conn, label="a")
    # 3 of 5 is 60 percent and means nothing — over threshold, under the floor.
    for image_id in range(1, 4):
        ta.set_state(
            conn, image_id=image_id, tag_id=tag["id"], state="excluded",
            excluded_reason=ta.EXCLUDED_AMBIGUOUS,
        )
    for image_id in (4, 5):
        ta.set_state(conn, image_id=image_id, tag_id=tag["id"], state="positive")
    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["ambiguity_rate"] == 0.6
    assert row["ambiguity_alert"] is False

    # Past the floor, still over threshold: now it fires.
    for image_id in range(100, 100 + ta.AMBIGUITY_MIN_DECISIONS):
        ta.set_state(
            conn, image_id=image_id, tag_id=tag["id"], state="excluded",
            excluded_reason=ta.EXCLUDED_AMBIGUOUS,
        )
    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["decided_count"] >= ta.AMBIGUITY_MIN_DECISIONS
    assert row["ambiguity_alert"] is True


def test_a_healthy_tag_does_not_alert(conn: _FakeConn) -> None:
    tag = ta.add_tag(conn, label="a")
    for image_id in range(1, 41):
        ta.set_state(conn, image_id=image_id, tag_id=tag["id"], state="positive")
    ta.set_state(
        conn, image_id=41, tag_id=tag["id"], state="excluded",
        excluded_reason=ta.EXCLUDED_AMBIGUOUS,
    )
    row = next(r for r in ta.tag_overview(conn)["tags"] if r["label"] == "a")
    assert row["ambiguity_rate"] < ta.AMBIGUITY_RATE_THRESHOLD
    assert row["ambiguity_alert"] is False
