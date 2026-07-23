"""Tests for toolkit.get_manual_rental_estimates.

Hermetic: a scripted fake cursor returns prepared rows. No DB
connection is opened.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit.manual_estimates import get_manual_rental_estimates


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.cur = _FakeCursor(rows)

    def cursor(self) -> _FakeCursor:
        return self.cur


def _row(
    id: int = 1,
    sreality_id: int = 12345,
    rent_czk: int = 30000,
    author: str = "petr",
    source_kind: str = "broker",
    notes: str | None = "from broker quote",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> tuple[Any, ...]:
    ts = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
    return (
        id, sreality_id, rent_czk, author, source_kind, notes,
        created_at or ts, updated_at or ts,
    )


def test_empty_result_returns_empty_envelope() -> None:
    conn = _FakeConn(rows=[])
    out = get_manual_rental_estimates(conn, sreality_id=999)  # type: ignore[arg-type]

    assert out["data"] == {"estimates": []}
    assert out["metadata"]["tool"] == "get_manual_rental_estimates"
    assert out["metadata"]["filters_used"] == {"sreality_id": 999}
    assert out["metadata"]["result_count"] == 0
    assert out["metadata"]["data_freshness"] is None
    assert "queried_at" in out["metadata"]


def test_single_estimate_envelope_shape() -> None:
    conn = _FakeConn(rows=[_row()])
    out = get_manual_rental_estimates(conn, sreality_id=12345)  # type: ignore[arg-type]

    assert out["metadata"]["result_count"] == 1
    estimates = out["data"]["estimates"]
    assert len(estimates) == 1
    e = estimates[0]
    assert e["id"] == 1
    assert e["sreality_id"] == 12345
    assert e["rent_czk"] == 30000
    assert e["author"] == "petr"
    assert e["source_kind"] == "broker"
    assert e["notes"] == "from broker quote"
    assert e["created_at"].startswith("2026-05-13T12:00:00")
    assert e["updated_at"].startswith("2026-05-13T12:00:00")
    assert out["metadata"]["data_freshness"] == e["updated_at"]


def test_multiple_estimates_data_freshness_picks_max_updated_at() -> None:
    older = _row(
        id=1,
        updated_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
    )
    newer = _row(
        id=2,
        updated_at=datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc),
    )
    conn = _FakeConn(rows=[newer, older])  # query orders DESC by created_at
    out = get_manual_rental_estimates(conn, sreality_id=12345)  # type: ignore[arg-type]

    assert out["metadata"]["result_count"] == 2
    assert out["metadata"]["data_freshness"].startswith("2026-05-13T18:00:00")


def test_sql_executed_with_sreality_id_param() -> None:
    conn = _FakeConn(rows=[])
    get_manual_rental_estimates(conn, sreality_id=42)  # type: ignore[arg-type]
    sql, params = conn.cur.executed[0]
    assert "manual_rental_estimates" in sql
    assert "where sreality_id = %s" in sql
    assert "order by created_at desc" in sql
    assert params == (42,)


# --- Gate-2: addressable by the surrogate listing_id ----------------------


def test_listing_id_arm_queries_by_id_column() -> None:
    conn = _FakeConn(rows=[])
    out = get_manual_rental_estimates(conn, listing_id=77)  # type: ignore[arg-type]
    sql, params = conn.cur.executed[0]
    # The surrogate arm keys on listing_id, never the legacy sreality_id.
    assert "where listing_id = %s" in sql
    assert "sreality_id" not in sql.split("where", 1)[1]
    assert params == (77,)
    assert out["metadata"]["filters_used"] == {"listing_id": 77}


def test_listing_id_wins_when_both_supplied() -> None:
    conn = _FakeConn(rows=[])
    get_manual_rental_estimates(conn, sreality_id=42, listing_id=77)  # type: ignore[arg-type]
    sql, params = conn.cur.executed[0]
    assert "where listing_id = %s" in sql
    assert params == (77,)


def test_neither_id_raises_clean_value_error() -> None:
    conn = _FakeConn(rows=[])
    with pytest.raises(ValueError):
        get_manual_rental_estimates(conn)  # type: ignore[arg-type]
