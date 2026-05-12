"""Tests for /buildings endpoints (Phase B0).

Hermetic — overrides the DB-conn dependency and monkey-patches the
persistence helpers so no real DB is hit. Mirrors the in-memory
`_State` pattern from `tests/api/test_estimations.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import building_runs as br
from api import dependencies as deps
from api import main as api_main


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.require_token] = lambda: None
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


class _State:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.children: dict[int, list[dict[str, Any]]] = {}
        self.next_id = 1


def _patch_persistence(monkeypatch) -> _State:
    state = _State()

    def fake_insert(conn, **fields: Any) -> int:
        rid = state.next_id
        state.next_id += 1
        state.rows[rid] = dict(fields)
        return rid

    def fake_fetch(conn, building_id: int) -> dict[str, Any] | None:
        if building_id not in state.rows:
            return None
        fields = state.rows[building_id]
        return {
            "id": building_id,
            "created_at": "2026-05-12T10:00:00+00:00",
            **{
                k: fields.get(k)
                for k in (
                    "source", "status",
                    "input_url", "input_sreality_id", "input_spec",
                    "source_kind", "parse_confidence",
                    "parse_confidence_per_field", "source_html",
                    "subject_summary",
                    "units_proposal", "units",
                    "total_rent_p25_czk", "total_rent_p50_czk",
                    "total_rent_p75_czk",
                    "total_sale_p25_czk", "total_sale_p50_czk",
                    "total_sale_p75_czk",
                    "business_case",
                    "warnings", "error_message",
                )
            },
        }

    def fake_children(conn, building_id: int) -> list[dict[str, Any]]:
        return list(state.children.get(building_id, []))

    monkeypatch.setattr(br, "_insert_building", fake_insert)
    monkeypatch.setattr(br, "_fetch_building", fake_fetch)
    monkeypatch.setattr(br, "_fetch_children", fake_children)
    return state


# ----------------------------------------------------------------------

def test_post_buildings_creates_pending_shell(client, monkeypatch):
    state = _patch_persistence(monkeypatch)

    response = client.post(
        "/buildings",
        json={"source": "ui", "input_url": "https://example.cz/dum/123"},
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["id"] == 1
    assert body["status"] == "pending"
    assert body["source"] == "ui"
    assert body["input_url"] == "https://example.cz/dum/123"
    assert body["units"] is None
    assert body["units_proposal"] is None
    assert body["business_case"] is None
    assert state.rows[1]["status"] == "pending"


def test_post_buildings_input_url_optional(client, monkeypatch):
    _patch_persistence(monkeypatch)
    response = client.post("/buildings", json={"source": "api"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["input_url"] is None


def test_get_building_returns_row_with_children(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    client.post("/buildings", json={"source": "api"})
    state.children[1] = [
        {
            "id": 100, "created_at": "2026-05-12T11:00:00+00:00",
            "status": "success", "estimate_kind": "rent",
            "building_unit_id": "u1",
            "estimated_monthly_rent_czk": 18500,
            "rent_p25_czk": 17000, "rent_p75_czk": 20000,
            "estimated_sale_price_czk": None,
            "sale_p25_czk": None, "sale_p75_czk": None,
            "confidence": "high", "error_message": None,
        },
    ]

    response = client.get("/buildings/1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == 1
    assert len(body["children"]) == 1
    assert body["children"][0]["building_unit_id"] == "u1"
    assert body["children"][0]["estimated_monthly_rent_czk"] == 18500


def test_get_building_404_when_missing(client, monkeypatch):
    _patch_persistence(monkeypatch)
    response = client.get("/buildings/999")
    assert response.status_code == 404


def test_list_buildings_filters(client, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_list(conn, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "data": [
                {"id": 1, "source": "ui", "status": "pending"},
                {"id": 2, "source": "ui", "status": "pending"},
            ],
            "total": 2,
            "limit": kwargs.get("limit"),
            "offset": kwargs.get("offset"),
        }

    monkeypatch.setattr(api_main, "list_building_runs", fake_list)

    response = client.get(
        "/buildings",
        params={"source": "ui", "status": "pending", "limit": 10, "offset": 0},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert captured["source"] == "ui"
    assert captured["status"] == "pending"
    assert captured["limit"] == 10
    assert captured["offset"] == 0


def test_list_buildings_invalid_status_rejected(client, monkeypatch):
    _patch_persistence(monkeypatch)
    response = client.get("/buildings", params={"status": "bogus"})
    assert response.status_code == 422
