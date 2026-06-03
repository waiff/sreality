"""Tests for the /filter-presets endpoints (Browse saved filter presets).

Hermetic — overrides get_db_conn so no real DB is hit, and patches the
api.filter_presets.* persistence helpers with in-memory fakes. Pydantic
validation (blank name, missing filter_spec) runs unmodified through the
route layer.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import filter_presets as fp
from api import main as api_main


@pytest.fixture()
def client():
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


@pytest.fixture()
def store(monkeypatch):
    """In-memory replacement for the filter_presets.* persistence functions."""
    state: dict[str, Any] = {"presets": {}, "next_id": 1}

    def _to_dict(pid: str) -> dict[str, Any]:
        return dict(state["presets"][pid])

    def fake_create_preset(conn, *, name, filter_spec):
        pid = str(state["next_id"])
        state["next_id"] += 1
        state["presets"][pid] = {
            "id": pid,
            "name": name,
            "filter_spec": filter_spec,
            "created_at": f"2026-06-03T00:00:0{pid}+00:00",
            "updated_at": f"2026-06-03T00:00:0{pid}+00:00",
        }
        return _to_dict(pid)

    def fake_list_presets(conn):
        rows = [_to_dict(pid) for pid in state["presets"]]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows

    def fake_get_preset(conn, pid):
        return _to_dict(pid) if pid in state["presets"] else None

    def fake_update_preset(conn, pid, *, name=None, filter_spec=None):
        if pid not in state["presets"]:
            return None
        if name is not None:
            state["presets"][pid]["name"] = name
        if filter_spec is not None:
            state["presets"][pid]["filter_spec"] = filter_spec
        return _to_dict(pid)

    def fake_delete_preset(conn, pid):
        return state["presets"].pop(pid, None) is not None

    monkeypatch.setattr(fp, "create_preset", fake_create_preset)
    monkeypatch.setattr(fp, "list_presets", fake_list_presets)
    monkeypatch.setattr(fp, "get_preset", fake_get_preset)
    monkeypatch.setattr(fp, "update_preset", fake_update_preset)
    monkeypatch.setattr(fp, "delete_preset", fake_delete_preset)

    return state


SPEC = {"categoryMain": "byt", "categoryType": "prodej", "priceMax": 6_000_000}


def test_create_preset_returns_row(client, store):
    res = client.post("/filter-presets", json={"name": "Praha pod 6M", "filter_spec": SPEC})
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "1"
    assert body["name"] == "Praha pod 6M"
    assert body["filter_spec"] == SPEC


def test_create_preset_blank_name_422(client, store):
    res = client.post("/filter-presets", json={"name": "", "filter_spec": SPEC})
    assert res.status_code == 422


def test_create_preset_missing_spec_422(client, store):
    res = client.post("/filter-presets", json={"name": "x"})
    assert res.status_code == 422


def test_list_presets_newest_first(client, store):
    client.post("/filter-presets", json={"name": "a", "filter_spec": SPEC})
    client.post("/filter-presets", json={"name": "b", "filter_spec": SPEC})
    res = client.get("/filter-presets")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    assert [r["name"] for r in body["data"]] == ["b", "a"]


def test_get_preset_404_when_missing(client, store):
    assert client.get("/filter-presets/999").status_code == 404


def test_update_preset_renames_without_touching_spec(client, store):
    client.post("/filter-presets", json={"name": "old", "filter_spec": SPEC})
    res = client.put("/filter-presets/1", json={"name": "new"})
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "new"
    assert body["filter_spec"] == SPEC


def test_update_preset_replaces_spec(client, store):
    client.post("/filter-presets", json={"name": "p", "filter_spec": SPEC})
    new_spec = {"categoryMain": "dum", "categoryType": "prodej"}
    res = client.put("/filter-presets/1", json={"filter_spec": new_spec})
    assert res.status_code == 200
    assert res.json()["filter_spec"] == new_spec


def test_update_preset_404_when_missing(client, store):
    assert client.put("/filter-presets/999", json={"name": "x"}).status_code == 404


def test_delete_preset(client, store):
    client.post("/filter-presets", json={"name": "p", "filter_spec": SPEC})
    assert client.delete("/filter-presets/1").json() == {"deleted": True}
    assert client.delete("/filter-presets/1").status_code == 404
