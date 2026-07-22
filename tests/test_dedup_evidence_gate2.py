"""Gate 2: api.property_dedup image-evidence readers must key on the surrogate
images.listing_id, never the post-Gate-2 possibly-NULL sreality_id. Covers
clip_coverage (fully internal, driven off the audit/listings surrogate) and
decision_evidence (frontend still addresses by sreality_id, resolved to the surrogate
once and carried through the image joins). Hermetic fake conn — no DB."""

from __future__ import annotations

from typing import Any

import api.property_dedup as dedup


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if "SELECT sreality_id, id FROM listings WHERE sreality_id IN" in s:
            self._rows = list(self._conn.resolve_rows)
        elif "count(*) FROM image_clip_tags" in s or "count(*) FROM image_clip_embeddings" in s:
            self._rows = [(0,)]
        elif "WITH tagged AS" in s:
            self._rows = [(0, 0, 0, 0, 0, 0)]
        else:
            self._rows = list(self._conn.image_rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(
        self, *, resolve_rows: list[tuple[Any, ...]] | None = None,
        image_rows: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.resolve_rows = resolve_rows or []
        self.image_rows = image_rows or []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_clip_coverage_counts_listings_on_the_surrogate_id() -> None:
    conn = _FakeConn()
    dedup.clip_coverage(conn, priority_region_id=27)
    big = next(s for s, _ in conn.executed if "WITH tagged AS" in s)
    # The tagged set + the per-listing membership test both key on the surrogate id.
    assert "SELECT DISTINCT i.listing_id" in big
    assert "WHERE i.listing_id IS NOT NULL" in big
    assert "l.id IN (SELECT listing_id FROM tagged)" in big
    # Never the post-Gate-2 possibly-NULL sreality columns.
    assert "i.sreality_id" not in big
    assert "l.sreality_id IN" not in big
    assert "SELECT sreality_id FROM tagged" not in big


def test_decision_evidence_resolves_sreality_to_surrogate_and_joins_images_on_it() -> None:
    # The frontend still passes the displayed sreality_id; decision_evidence resolves it
    # to the surrogate listing_id ONCE and feeds THAT to the image joins, which key on
    # images.listing_id. Pre-fix the helpers joined images.sreality_id directly.
    conn = _FakeConn(resolve_rows=[(5001, 901), (6002, 902)], image_rows=[])
    out = dedup.decision_evidence(
        conn, left_sreality_id=5001, right_sreality_id=6002, stage="phash",
    )
    # The resolution ran against the sreality contract the frontend speaks.
    resolve = next(
        (s, p) for s, p in conn.executed
        if "SELECT sreality_id, id FROM listings WHERE sreality_id IN" in s
    )
    assert resolve[1] == (5001, 6002)
    # pHash pair evidence joins images on the surrogate, bound to the RESOLVED lids.
    pair_sql, pair_params = next(
        (s, p) for s, p in conn.executed if "FROM images ia JOIN images ib" in s
    )
    assert "ia.listing_id = %(a)s AND ib.listing_id = %(b)s" in pair_sql
    assert "ia.sreality_id" not in pair_sql and "ib.sreality_id" not in pair_sql
    assert pair_params["a"] == 901 and pair_params["b"] == 902
    # Per-listing room images also key on images.listing_id, bound to the resolved lid
    # (901/902) — NOT the sreality id (5001/6002).
    room_calls = [
        (s, p) for s, p in conn.executed
        if "FROM images WHERE listing_id = %s" in s
    ]
    assert len(room_calls) == 2
    assert {p[0] for _, p in room_calls} == {901, 902}
    for s, _ in room_calls:
        assert "sreality_id" not in s
    # The output still exposes the sreality id the frontend rendered.
    assert out["data"]["left"]["sreality_id"] == 5001
    assert out["data"]["right"]["sreality_id"] == 6002


def test_decision_evidence_unresolved_sreality_id_yields_empty_not_a_crash() -> None:
    # An id that resolves to no listing -> None -> the image joins bind None and return
    # empty, exactly as an unknown id did before (no int(None) crash, no wrong-id read).
    conn = _FakeConn(resolve_rows=[], image_rows=[])
    out = dedup.decision_evidence(
        conn, left_sreality_id=5001, right_sreality_id=6002, stage="phash",
    )
    assert out["data"]["left"]["images"] == []
    assert out["data"]["right"]["images"] == []
    room_calls = [p for s, p in conn.executed if "FROM images WHERE listing_id = %s" in s]
    assert all(p[0] is None for p in room_calls)
