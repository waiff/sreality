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
from api import tenant_pool


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    # Pipeline routes run on the tenant pool since Phase 1; the route-level
    # verify_jwt is overridden to a legacy identity and account resolution is
    # stubbed so no SQL hits the fake connection object.
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: {
        "sub": None, "legacy": True,
    }
    monkeypatch.setattr(
        tenant_pool, "resolve_account_id", lambda conn, claims: None,
    )
    monkeypatch.setattr(
        pipeline_module, "list_stages",
        lambda conn, *, account_id=None: {"data": [{
            "id": 1, "key": "interested", "label": "Zájem", "position": 1,
            "color": "copper", "is_terminal": False, "is_entry": True,
        }]},
    )
    monkeypatch.setattr(
        pipeline_module, "add_card",
        lambda conn, body, *, account_id=None: {
            "property_id": body.property_id, "stage_key": "interested", "added": True,
        },
    )
    monkeypatch.setattr(
        pipeline_module, "remove_card",
        lambda conn, pid, *, account_id=None: {"removed": True},
    )
    monkeypatch.setattr(
        pipeline_module, "move_card",
        lambda conn, pid, body, *, account_id=None: {
            "property_id": pid, "stage_id": body.stage_id, "stage_key": "offer",
        },
    )
    monkeypatch.setattr(
        pipeline_module, "create_stage",
        lambda conn, body, *, account_id=None: {
            "id": 9, "key": "due_diligence", "label": body.label, "position": 6,
            "color": body.color, "is_terminal": body.is_terminal, "is_entry": False,
        },
    )
    monkeypatch.setattr(
        pipeline_module, "update_stage",
        lambda conn, sid, body, *, account_id=None: {
            "id": sid, "key": "viewing", "label": body.label or "Prohlídka",
            "position": 2, "color": body.color, "is_terminal": False,
            "is_entry": bool(body.is_entry),
        },
    )
    monkeypatch.setattr(
        pipeline_module, "reorder_stages",
        lambda conn, body, *, account_id=None: {"data": [{"id": i} for i in body.ordered_ids]},
    )
    monkeypatch.setattr(
        pipeline_module, "archive_stage",
        lambda conn, sid, *, account_id=None: {"archived": True, "stage_id": sid},
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

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


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
        (lambda q: "RECURSIVE chain" in q, [(42, 42)]),  # property active -> itself
        (lambda q: "WHERE is_entry" in q, [(1,)]),
        (lambda q: "max(board_position)" in q, [(5,)]),
        (lambda q: "INSERT INTO property_pipeline (" in q, [(42,)]),  # RETURNING -> inserted
        (lambda q: "FROM property_pipeline pp JOIN pipeline_stages" in q, [_CARD_ROW]),
    ])
    out = pipeline_module.add_card(conn, s.AddPipelineCardIn(property_id=42), account_id=None)
    assert out["added"] is True
    assert out["stage_key"] == "interested"
    assert any(
        "INSERT INTO property_pipeline_events" in e[0] for e in conn.executed
    )


def test_add_card_idempotent_returns_existing_stage_no_event():
    existing = (42, 3, "offer", "Nabídka", 2, None, None, None)
    conn = _FakeConn([
        (lambda q: "RECURSIVE chain" in q, [(42, 42)]),  # property active -> itself
        (lambda q: "WHERE is_entry" in q, [(1,)]),
        (lambda q: "max(board_position)" in q, [(5,)]),
        (lambda q: "INSERT INTO property_pipeline (" in q, []),  # ON CONFLICT -> no row
        (lambda q: "FROM property_pipeline pp JOIN pipeline_stages" in q, [existing]),
    ])
    out = pipeline_module.add_card(conn, s.AddPipelineCardIn(property_id=42), account_id=None)
    assert out["added"] is False
    assert out["stage_key"] == "offer"  # the existing card's stage, untouched
    assert not any(
        "INSERT INTO property_pipeline_events" in e[0] for e in conn.executed
    )


def test_add_card_redirects_merged_away_property_to_survivor():
    # property 99 was merged into the active survivor 42; the card + event must
    # land on 42, never orphan onto the retired 99.
    conn = _FakeConn([
        (lambda q: "RECURSIVE chain" in q, [(99, 42)]),
        (lambda q: "WHERE is_entry" in q, [(1,)]),
        (lambda q: "max(board_position)" in q, [(5,)]),
        (lambda q: "INSERT INTO property_pipeline (" in q, [(42,)]),
        (lambda q: "FROM property_pipeline pp JOIN pipeline_stages" in q, [_CARD_ROW]),
    ])
    out = pipeline_module.add_card(conn, s.AddPipelineCardIn(property_id=99), account_id=None)
    assert out["added"] is True
    inserts = [p for q, p in conn.executed if "INSERT INTO property_pipeline (" in q]
    assert inserts and inserts[0][0] == 42
    events = [p for q, p in conn.executed if "property_pipeline_events" in q]
    assert events and events[0][0] == 42


def test_add_card_no_active_survivor_is_422():
    conn = _FakeConn([(lambda q: "RECURSIVE chain" in q, [])])  # missing / broken chain
    with pytest.raises(fastapi.HTTPException) as ei:
        pipeline_module.add_card(conn, s.AddPipelineCardIn(property_id=7), account_id=None)
    assert ei.value.status_code == 422


def test_move_card_to_new_stage_logs_event_and_stamps_entered():
    conn = _FakeConn([
        (lambda q: "SELECT stage_id FROM property_pipeline WHERE property_id" in q, [(1,)]),
        (lambda q: "FROM property_pipeline pp JOIN pipeline_stages" in q,
         [(42, 3, "offer", "Nabídka", 2, None, None, None)]),
    ])
    out = pipeline_module.move_card(conn, 42, s.MoveCardIn(stage_id=3), account_id=None)
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
    pipeline_module.move_card(conn, 42, s.MoveCardIn(stage_id=1, board_position=2.5), account_id=None)
    sqls = [q for q, _ in conn.executed]
    assert any("UPDATE property_pipeline SET" in q and "board_position" in q for q in sqls)
    assert not any("entered_stage_at = now()" in q for q in sqls)
    assert not any("INSERT INTO property_pipeline_events" in q for q in sqls)


# --- stage-management routes ----------------------------------------------

def test_create_stage_route(client):
    res = client.post("/pipeline/stages", json={"label": "Due diligence", "color": "plum"})
    assert res.status_code == 200
    body = res.json()
    assert body["label"] == "Due diligence"
    assert body["color"] == "plum"
    assert body["is_entry"] is False


def test_create_stage_rejects_bad_color_via_pydantic(client):
    # color is free-form in the schema; the persistence layer validates the palette.
    # An over-long label is rejected by pydantic before the handler runs.
    res = client.post("/pipeline/stages", json={"label": "x" * 81})
    assert res.status_code == 422


def test_update_stage_route(client):
    res = client.patch("/pipeline/stages/2", json={"label": "Viewing", "is_entry": True})
    assert res.status_code == 200
    assert res.json()["is_entry"] is True


def test_reorder_stages_route(client):
    res = client.post("/pipeline/stages/reorder", json={"ordered_ids": [3, 1, 2]})
    assert res.status_code == 200
    assert [d["id"] for d in res.json()["data"]] == [3, 1, 2]


def test_archive_stage_route(client):
    res = client.delete("/pipeline/stages/5")
    assert res.status_code == 200
    assert res.json() == {"archived": True, "stage_id": 5}


# --- stage-management logic against the scripted fake connection -----------

def test_slugify_strips_diacritics_and_punctuation():
    assert pipeline_module._slugify("Důležité — Nabídka!") == "dulezite_nabidka"
    assert pipeline_module._slugify("🏠🏠") == "stage"


def test_create_stage_derives_key_and_appends_position():
    stage_row = (9, "due_diligence", "Due diligence", 6, "plum", False, False)
    conn = _FakeConn([
        (lambda q: "SELECT lower(key) FROM pipeline_stages" in q,
         [("interested",), ("viewing",)]),
        (lambda q: "coalesce(max(position), 0) + 1" in q, [(6,)]),
        (lambda q: "INSERT INTO pipeline_stages" in q, [stage_row]),
    ])
    out = pipeline_module.create_stage(
        conn, s.CreateStageIn(label="Due diligence", color="plum"), account_id=None,
    )
    assert out["key"] == "due_diligence"
    assert out["position"] == 6
    ins = next(p for q, p in conn.executed if "INSERT INTO pipeline_stages" in q)
    assert ins[0] == "due_diligence"  # the derived key is passed first


def test_create_stage_rejects_palette_violation():
    conn = _FakeConn([])
    with pytest.raises(fastapi.HTTPException) as ei:
        pipeline_module.create_stage(conn, s.CreateStageIn(label="X", color="neon"), account_id=None)
    assert ei.value.status_code == 422


def test_update_stage_crowning_entry_demotes_the_others():
    updated = (2, "viewing", "Prohlídka", 2, "ochre", False, True)
    conn = _FakeConn([
        (lambda q: "SELECT is_entry, is_terminal FROM pipeline_stages WHERE id" in q,
         [(False, False)]),
        (lambda q: "UPDATE pipeline_stages SET" in q and "RETURNING" in q, [updated]),
    ])
    out = pipeline_module.update_stage(conn, 2, s.UpdateStageIn(is_entry=True), account_id=None)
    assert out["is_entry"] is True
    sqls = [q for q, _ in conn.executed]
    assert any(
        "UPDATE pipeline_stages SET is_entry = false" in q and "WHERE is_entry AND id <> %s" in q
        for q in sqls
    )


def test_update_stage_rejects_uncrowning_entry():
    conn = _FakeConn([])
    with pytest.raises(fastapi.HTTPException) as ei:
        pipeline_module.update_stage(conn, 1, s.UpdateStageIn(is_entry=False), account_id=None)
    assert ei.value.status_code == 422


def test_update_stage_rejects_entry_that_is_terminal():
    conn = _FakeConn([
        (lambda q: "SELECT is_entry, is_terminal FROM pipeline_stages WHERE id" in q,
         [(False, True)]),  # already terminal
    ])
    with pytest.raises(fastapi.HTTPException) as ei:
        pipeline_module.update_stage(conn, 4, s.UpdateStageIn(is_entry=True), account_id=None)
    assert ei.value.status_code == 422


def test_reorder_rejects_set_mismatch():
    conn = _FakeConn([
        (lambda q: "SELECT id FROM pipeline_stages WHERE archived_at IS NULL" in q,
         [(1,), (2,), (3,)]),
    ])
    with pytest.raises(fastapi.HTTPException) as ei:
        pipeline_module.reorder_stages(conn, s.ReorderStagesIn(ordered_ids=[1, 2]), account_id=None)
    assert ei.value.status_code == 422


def test_archive_refuses_entry_stage():
    conn = _FakeConn([
        (lambda q: "SELECT is_entry, archived_at FROM pipeline_stages WHERE id" in q,
         [(True, None)]),
    ])
    with pytest.raises(fastapi.HTTPException) as ei:
        pipeline_module.archive_stage(conn, 1, account_id=None)
    assert ei.value.status_code == 409


def test_archive_refuses_stage_with_cards():
    conn = _FakeConn([
        (lambda q: "SELECT is_entry, archived_at FROM pipeline_stages WHERE id" in q,
         [(False, None)]),
        (lambda q: "SELECT 1 FROM property_pipeline WHERE stage_id" in q, [(1,)]),
    ])
    with pytest.raises(fastapi.HTTPException) as ei:
        pipeline_module.archive_stage(conn, 3, account_id=None)
    assert ei.value.status_code == 409


def test_archive_soft_retires_empty_stage():
    conn = _FakeConn([
        (lambda q: "SELECT is_entry, archived_at FROM pipeline_stages WHERE id" in q,
         [(False, None)]),
        (lambda q: "SELECT 1 FROM property_pipeline WHERE stage_id" in q, []),
    ])
    out = pipeline_module.archive_stage(conn, 3, account_id=None)
    assert out == {"archived": True, "stage_id": 3}
    assert any("SET archived_at = now()" in q for q, _ in conn.executed)
