"""api.property_dedup image-annotation + phash-pair-note writes (migration 308):
canonical image-pair ordering, note cleaning, delete. Hermetic fake conn — no DB."""

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

    def fetchone(self) -> Any:
        return self._row


class _FakeConn:
    def __init__(self, *, delete_count: int = 1) -> None:
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
