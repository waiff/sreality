"""api.property_dedup image-annotation + phash-pair-note writes (migration 308) and
the training-example writes (migration 309): canonical image-pair ordering, note
cleaning, delete. Hermetic fake conn — no DB."""

from __future__ import annotations

from typing import Any

import pytest

import api.property_dedup as dedup


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._row: tuple[Any, ...] | None = None
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if s.startswith("INSERT INTO image_tag_annotations"):
            # RETURNING tag_flagged, render_flagged, note, updated_at
            self._row = (params[1], params[2], params[3], "2026-07-17T00:00:00Z")
        elif s.startswith("DELETE FROM image_tag_annotations"):
            self.rowcount = self._conn.delete_count
            self._row = None
        elif s.startswith("INSERT INTO phash_pair_notes"):
            # RETURNING note, updated_at
            self._row = (params[2], "2026-07-17T00:00:00Z")
        elif s.startswith("DELETE FROM phash_pair_notes"):
            self.rowcount = self._conn.delete_count
            self._row = None
        elif s.startswith("INSERT INTO image_training_examples"):
            if "unnest" in s:
                # The bulk path reads rowcount, not RETURNING — one row per id
                # (params = label, created_by, ids).
                self.rowcount = len(params[2])
                self._row = None
            else:
                # RETURNING label, updated_at
                self._row = (params[1], "2026-07-17T00:00:00Z")
        elif s.startswith("DELETE FROM image_training_examples"):
            self.rowcount = self._conn.delete_count
            self._row = None
        elif s.startswith("INSERT INTO image_border_cases"):
            # ON CONFLICT DO NOTHING RETURNING created_at — no row back means it
            # already existed (the caller falls back to a SELECT).
            self._row = None if self._conn.border_case_already_exists else ("2026-07-17T00:00:00Z",)
        elif s.startswith("SELECT created_at FROM image_border_cases"):
            self._row = ("2026-07-16T00:00:00Z",)  # the PRE-EXISTING row's timestamp
        elif s.startswith("DELETE FROM image_border_cases"):
            self.rowcount = self._conn.delete_count
            self._row = None

    def fetchone(self) -> Any:
        return self._row


class _FakeConn:
    def __init__(self, *, delete_count: int = 1, border_case_already_exists: bool = False) -> None:
        self.border_case_already_exists = border_case_already_exists
        self.executed: list[tuple[str, Any]] = []
        self.delete_count = delete_count

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_set_image_annotation_upserts_and_cleans_note() -> None:
    conn = _FakeConn()
    out = dedup.set_image_annotation(
        conn, image_id=42, tag_flagged=True, note="  wrong room  ",
    )
    _, params = conn.executed[0]
    assert params[0] == 42
    assert params[1] is True and params[2] is False
    assert params[3] == "wrong room"  # trimmed
    assert out["data"]["image_id"] == 42
    assert out["data"]["tag_flagged"] is True
    assert out["data"]["render_flagged"] is False


def test_set_image_annotation_blank_note_becomes_null() -> None:
    conn = _FakeConn()
    dedup.set_image_annotation(conn, image_id=7, note="   ")
    _, params = conn.executed[0]
    assert params[3] is None


def test_delete_image_annotation_reports_deleted() -> None:
    conn = _FakeConn(delete_count=1)
    out = dedup.delete_image_annotation(conn, image_id=42)
    _, params = conn.executed[0]
    assert params == (42,)
    assert out["data"]["deleted"] is True

    conn2 = _FakeConn(delete_count=0)
    out2 = dedup.delete_image_annotation(conn2, image_id=1)
    assert out2["data"]["deleted"] is False


def test_set_phash_note_canonicalises_pair_and_cleans_note() -> None:
    conn = _FakeConn()
    out = dedup.set_phash_note(conn, image_id_a=200, image_id_b=100, note="  same photo  ")
    _, params = conn.executed[0]
    assert params[0] == 100 and params[1] == 200  # low, high image id
    assert params[2] == "same photo"
    assert out["data"]["image_id_a"] == 100
    assert out["data"]["image_id_b"] == 200


def test_set_phash_note_rejects_identical_pair() -> None:
    with pytest.raises(ValueError):
        dedup.set_phash_note(_FakeConn(), image_id_a=7, image_id_b=7, note="x")


def test_delete_phash_note_canonicalises_and_reports() -> None:
    conn = _FakeConn(delete_count=1)
    out = dedup.delete_phash_note(conn, image_id_a=200, image_id_b=100)
    _, params = conn.executed[0]
    assert params == (100, 200)
    assert out["data"]["deleted"] is True


def test_set_training_example_upserts_trimmed_label() -> None:
    conn = _FakeConn()
    out = dedup.set_training_example(conn, image_id=42, label="  kitchen  ")
    _, params = conn.executed[0]
    assert params == (42, "kitchen", "operator")
    assert out["data"] == {"image_id": 42, "label": "kitchen", "updated_at": "2026-07-17T00:00:00Z"}


def test_set_training_example_collapses_internal_whitespace() -> None:
    # Write-boundary normalization: "site  plan\n" and "site plan" must land as the
    # SAME stored label, or they'd silently fragment one training class into two.
    conn = _FakeConn()
    out = dedup.set_training_example(conn, image_id=42, label="site   plan\n")
    _, params = conn.executed[0]
    assert params == (42, "site plan", "operator")
    assert out["data"]["label"] == "site plan"


def test_set_training_example_rejects_blank_label() -> None:
    with pytest.raises(ValueError):
        dedup.set_training_example(_FakeConn(), image_id=42, label="   ")


def test_bulk_set_training_examples_upserts_every_id_under_one_label() -> None:
    conn = _FakeConn()
    out = dedup.bulk_set_training_examples(conn, image_ids=[7, 8, 9], label="  kitchen ")
    _, params = conn.executed[0]
    assert params == ("kitchen", "operator", [7, 8, 9])
    assert out["data"]["updated"] == 3
    assert out["data"]["label"] == "kitchen"


def test_bulk_set_training_examples_dedupes_ids() -> None:
    # ON CONFLICT DO UPDATE cannot affect the same row twice in one statement — a
    # repeated id would abort the ENTIRE batch, so ids are deduped before the write.
    conn = _FakeConn()
    out = dedup.bulk_set_training_examples(conn, image_ids=[7, 8, 7, 8, 9], label="kitchen")
    _, params = conn.executed[0]
    assert params[2] == [7, 8, 9]
    assert out["data"]["image_ids"] == [7, 8, 9]


def test_training_label_length_is_capped_at_the_tables_check() -> None:
    # The table CHECKs char_length(label) BETWEEN 1 AND 100 — caught here so it's a
    # 422, and so one over-long label can't abort a whole batch.
    long_label = "x" * (dedup.TRAINING_LABEL_MAX_CHARS + 1)
    with pytest.raises(ValueError):
        dedup.set_training_example(_FakeConn(), image_id=42, label=long_label)
    with pytest.raises(ValueError):
        dedup.bulk_set_training_examples(_FakeConn(), image_ids=[7], label=long_label)
    # Exactly at the cap still goes through.
    conn = _FakeConn()
    dedup.set_training_example(conn, image_id=42, label="x" * dedup.TRAINING_LABEL_MAX_CHARS)
    assert conn.executed


def test_bulk_set_training_examples_rejects_blank_label_and_empty_selection() -> None:
    with pytest.raises(ValueError):
        dedup.bulk_set_training_examples(_FakeConn(), image_ids=[7], label="  ")
    with pytest.raises(ValueError):
        dedup.bulk_set_training_examples(_FakeConn(), image_ids=[], label="kitchen")


def test_bulk_set_training_examples_caps_batch_size() -> None:
    over = list(range(dedup.BULK_TRAINING_LABEL_MAX + 1))
    with pytest.raises(ValueError):
        dedup.bulk_set_training_examples(_FakeConn(), image_ids=over, label="kitchen")


def test_delete_training_label_removes_every_row_under_the_normalized_label() -> None:
    conn = _FakeConn(delete_count=87)
    out = dedup.delete_training_label(conn, label="  půdorys ")
    _, params = conn.executed[0]
    assert params == ("půdorys",)
    assert out["data"] == {"deleted": 87, "label": "půdorys"}


def test_delete_training_label_rejects_blank() -> None:
    with pytest.raises(ValueError):
        dedup.delete_training_label(_FakeConn(), label="   ")


def test_delete_training_example_reports_deleted() -> None:
    conn = _FakeConn(delete_count=1)
    out = dedup.delete_training_example(conn, image_id=42)
    _, params = conn.executed[0]
    assert params == (42,)
    assert out["data"]["deleted"] is True

    conn2 = _FakeConn(delete_count=0)
    out2 = dedup.delete_training_example(conn2, image_id=1)
    assert out2["data"]["deleted"] is False


def test_set_border_case_inserts_and_returns_the_new_row() -> None:
    conn = _FakeConn()
    out = dedup.set_border_case(conn, image_id=42)
    sql, params = conn.executed[0]
    assert sql.startswith("INSERT INTO image_border_cases")
    assert params == (42, "operator")
    assert out["data"] == {"image_id": 42, "created_at": "2026-07-17T00:00:00Z"}


def test_set_border_case_is_idempotent_on_a_repeat_flag() -> None:
    # Clicking the button twice for the same image must not error or duplicate —
    # it falls back to reading back the pre-existing row's timestamp.
    conn = _FakeConn(border_case_already_exists=True)
    out = dedup.set_border_case(conn, image_id=42)
    assert len(conn.executed) == 2  # the no-op INSERT, then the fallback SELECT
    assert out["data"] == {"image_id": 42, "created_at": "2026-07-16T00:00:00Z"}


def test_delete_border_case_reports_deleted() -> None:
    conn = _FakeConn(delete_count=1)
    out = dedup.delete_border_case(conn, image_id=42)
    _, params = conn.executed[0]
    assert params == (42,)
    assert out["data"]["deleted"] is True

    conn2 = _FakeConn(delete_count=0)
    out2 = dedup.delete_border_case(conn2, image_id=1)
    assert out2["data"]["deleted"] is False
