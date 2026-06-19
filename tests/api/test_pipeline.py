"""Tests for the deal-pipeline endpoints (Phase 0: bookmark at entry stage).

Hermetic — overrides get_db_conn so no real DB is hit. Route tests patch the
persistence helpers; the add_card logic (entry stage, idempotency, event log)
is exercised against a scripted fake connection.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api import pipeline as pipeline_module
from api import schemas as s


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    monkeypatch.setattr(
        pipeline_module, "list_stages",
        lambda conn: {"data": [{
            "id": 1, "key": "interested", "label": "Zájem", "position": 1,
            "color": "copper", "is_terminal": False, "is_entry": True,
        }]},
    )
    monkeypatch.setattr(
        pipeline_module, "add_card",
        lambda conn, body: {
            "property_id": body.property_id, "stage_key": "interested", "added": True,
        },
    )
    monkeypatch.setattr(
        pipeline_module, "remove_card",
        lambda conn, pid: {"removed": True},
    )
    monkeypatch.setattr(
        pipeline_module, "move_card",
        lambda conn, pid, body: {
            "property_id": pid, "stage_id": body.stage_id, "stage_key": "offer",
        },
    )
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_list_stages(client):
    res = client.get("/pipeline/stages")
    assert res.status_code == 200
    assert res.json()["data"][0]["is_entry"] is True


def test_bookmark_property(client):
    res = client.post("/pipeline/cards", json={"property_id": 42})
    assert res.status_code == 200
    body = res.json()
    assert body["property_id"] == 42
    assert body["added"] is True
    assert body["stage_key"] == "interested"


def test_bookmark_requires_property_id(client):
    res = client.post("/pipeline/cards", json={})
    assert res.status_code == 422


def test_remove_card(client):
    res = client.delete("/pipeline/cards/42")
    assert res.status_code == 200
    assert res.json() == {"removed": True}


def test_move_card_route(client):
    res = client.patch("/pipeline/cards/42", json={"stage_id": 3})
    assert res.status_code == 200
    assert res.json()["stage_id"] == 3


# --- add_card logic against a scripted fake connection ---------------------

class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        q = " ".join(sql.split())
        self._conn.executed.append((q, params))
        for predicate, rows in self._conn.script:
            if predicate(q):
                self._rows = list(rows)
                self.rowcount = len(rows)
                return
        self._rows = []
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _Tx:
    def __enter__(self) -> "_Tx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, script: list[tuple[Any, list[tuple[Any, ...]]]]) -> None:
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Tx:
        return _Tx()

    def cursor(self) -> _Cur:
        return _Cur(self)


_CARD_ROW = (42, 1, "interested", "Zájem", 5, None, None, None)


def test_add_card_inserts_at_entry_stage_and_logs_event():
    conn = _FakeConn([
        (lambda q: "WHERE is_entry" in q, [(1,)]),
        (lambda q: "max(board_position)" in q, [(5,)]),
        (lambda q: "INSERT INTO property_pipeline (" in q, [(42,)]),  # RETURNING -> inserted
        (lambda q: "FROM property_pipeline pp JOIN pipeline_stages" in q, [_CARD_ROW]),
    ])
    out = pipeline_module.add_card(conn, s.AddPipelineCardIn(property_id=42))
    assert out["added"] is True
    assert out["stage_key"] == "interested"
    assert any(
        "INSERT INTO property_pipeline_events" in e[0] for e in conn.executed
    )


def test_add_card_idempotent_returns_existing_stage_no_event():
    existing = (42, 3, "offer", "Nabídka", 2, None, None, None)
    conn = _FakeConn([
        (lambda q: "WHERE is_entry" in q, [(1,)]),
        (lambda q: "max(board_position)" in q, [(5,)]),
        (lambda q: "INSERT INTO property_pipeline (" in q, []),  # ON CONFLICT -> no row
        (lambda q: "FROM property_pipeline pp JOIN pipeline_stages" in q, [existing]),
    ])
    out = pipeline_module.add_card(conn, s.AddPipelineCardIn(property_id=42))
    assert out["added"] is False
    assert out["stage_key"] == "offer"  # the existing card's stage, untouched
    assert not any(
        "INSERT INTO property_pipeline_events" in e[0] for e in conn.executed
    )


def test_move_card_to_new_stage_logs_event_and_stamps_entered():
    conn = _FakeConn([
        (lambda q: "SELECT stage_id FROM property_pipeline WHERE property_id" in q, [(1,)]),
        (lambda q: "FROM property_pipeline pp JOIN pipeline_stages" in q,
         [(42, 3, "offer", "Nabídka", 2, None, None, None)]),
    ])
    out = pipeline_module.move_card(conn, 42, s.MoveCardIn(stage_id=3))
    sqls = [q for q, _ in conn.executed]
    assert any(
        "UPDATE property_pipeline SET" in q and "entered_stage_at = now()" in q
        for q in sqls
    )
    assert any("INSERT INTO property_pipeline_events" in q for q in sqls)
    assert out["stage_key"] == "offer"


def test_move_card_reorder_only_logs_no_event():
    conn = _FakeConn([
        (lambda q: "SELECT stage_id FROM property_pipeline WHERE property_id" in q, [(1,)]),
        (lambda q: "FROM property_pipeline pp JOIN pipeline_stages" in q,
         [(42, 1, "interested", "Zájem", 3, None, None, None)]),
    ])
    pipeline_module.move_card(conn, 42, s.MoveCardIn(stage_id=1, board_position=2.5))
    sqls = [q for q, _ in conn.executed]
    assert any("UPDATE property_pipeline SET" in q and "board_position" in q for q in sqls)
    assert not any("entered_stage_at = now()" in q for q in sqls)
    assert not any("INSERT INTO property_pipeline_events" in q for q in sqls)
