"""Tests for /buildings/* B1 endpoints (from_url, confirm_units, re_extract).

Hermetic — overrides the FastAPI dependencies, monkey-patches the
persistence helpers + the dispatcher + the extractor so no real DB,
no real LLM, no real HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import building_runs as br
from api import dependencies as deps
from api import main as api_main
from scraper import source_dispatcher
from toolkit import building_extraction


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.require_token] = lambda: None
    api_main.app.dependency_overrides[deps.get_sreality_client] = lambda: object()
    api_main.app.dependency_overrides[deps.get_llm_client] = lambda: object()
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


class _State:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.next_id = 1


def _patch_persistence(monkeypatch) -> _State:
    state = _State()

    def fake_insert(conn, **fields: Any) -> int:
        rid = state.next_id
        state.next_id += 1
        state.rows[rid] = dict(fields)
        return rid

    def fake_update(conn, building_id: int, **fields: Any) -> None:
        if building_id in state.rows:
            state.rows[building_id].update(fields)

    def fake_fetch(conn, building_id: int) -> dict[str, Any] | None:
        if building_id not in state.rows:
            return None
        fields = state.rows[building_id]
        return {
            "id": building_id,
            "created_at": "2026-05-12T10:00:00+00:00",
            "source": fields.get("source"),
            "status": fields.get("status"),
            "input_url": fields.get("input_url"),
            "input_sreality_id": fields.get("input_sreality_id"),
            "input_spec": fields.get("input_spec"),
            "source_kind": fields.get("source_kind"),
            "parse_confidence": fields.get("parse_confidence"),
            "parse_confidence_per_field": fields.get("parse_confidence_per_field"),
            "source_html": fields.get("source_html"),
            "subject_summary": fields.get("subject_summary"),
            "units_proposal": fields.get("units_proposal"),
            "units": fields.get("units"),
            "total_rent_p25_czk": fields.get("total_rent_p25_czk"),
            "total_rent_p50_czk": fields.get("total_rent_p50_czk"),
            "total_rent_p75_czk": fields.get("total_rent_p75_czk"),
            "total_sale_p25_czk": fields.get("total_sale_p25_czk"),
            "total_sale_p50_czk": fields.get("total_sale_p50_czk"),
            "total_sale_p75_czk": fields.get("total_sale_p75_czk"),
            "business_case": fields.get("business_case"),
            "warnings": fields.get("warnings"),
            "error_message": fields.get("error_message"),
            "special_instructions": fields.get("special_instructions"),
            "contextual_text": fields.get("contextual_text"),
        }

    def fake_children(conn, building_id: int) -> list[dict[str, Any]]:
        return []

    def fake_attachments(conn, building_id: int) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(br, "_insert_building", fake_insert)
    monkeypatch.setattr(br, "_update_building_fields", fake_update)
    monkeypatch.setattr(br, "_fetch_building", fake_fetch)
    monkeypatch.setattr(br, "_fetch_children", fake_children)
    monkeypatch.setattr(br, "_fetch_attachments", fake_attachments)
    return state


@dataclass
class _FakeParseResult:
    spec: dict[str, Any]
    source_kind: str = "sreality"
    parse_confidence: str = "high"
    parse_confidence_per_field: dict[str, str] | None = None
    source_html: str | None = None
    from_cache: bool = False
    cost_usd: float | None = None
    warnings: list[str] = field(default_factory=list)
    sreality_id: int | None = 999
    source_url: str = "https://example.cz/dum/1"
    full_extraction: dict[str, Any] | None = None
    fetched_at: str | None = None
    wide_spec: dict[str, Any] | None = None


def _patch_dispatcher(monkeypatch, result: _FakeParseResult) -> None:
    monkeypatch.setattr(
        source_dispatcher, "parse_listing_url",
        lambda *a, **kw: result,
    )


def _patch_extractor(monkeypatch, payload: dict[str, Any] | Exception) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(conn, llm, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if isinstance(payload, Exception):
            raise payload
        return {"data": payload, "metadata": {}}

    monkeypatch.setattr(building_extraction, "extract_building_units", fake)
    return calls


def _example_extractor_payload() -> dict[str, Any]:
    return {
        "sreality_id": 999,
        "snapshot_id": 42,
        "units": [
            {
                "unit_id": "u1", "label": None, "floor": "ground",
                "area_m2": 70.0, "disposition": "2+kk",
                "condition": "dobry", "is_potential": False,
                "source": "both", "notes": None,
            },
        ],
        "building": {
            "floor_count": 2, "has_attic": True, "year_built": 1932,
            "construction_type": "cihla", "total_area_m2": 70.0,
            "condition": "dobry", "notes": None,
        },
        "confidence": "high",
        "warnings": [],
        "n_images": 0,
        "model": "claude-sonnet-4-5",
        "cost_usd": 0.01,
        "cache_hit": False,
    }


# -- POST /buildings/from_url ------------------------------------------------


def test_from_url_happy_path(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    _patch_dispatcher(monkeypatch, _FakeParseResult(
        spec={"category_main": "dum", "category_type": "prodej",
              "lat": 50.1, "lng": 14.4},
    ))
    extractor_calls = _patch_extractor(monkeypatch, _example_extractor_payload())

    response = client.post(
        "/buildings/from_url",
        json={"source": "ui", "url": "https://example.cz/dum/1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "awaiting_input"
    assert body["units_proposal"]["units"][0]["unit_id"] == "u1"
    assert body["units"] is None
    assert state.rows[1]["status"] == "awaiting_input"
    assert len(extractor_calls) == 1
    assert extractor_calls[0]["sreality_id"] == 999


def test_from_url_byt_rejected(client, monkeypatch):
    _patch_persistence(monkeypatch)
    _patch_dispatcher(monkeypatch, _FakeParseResult(
        spec={"category_main": "byt", "category_type": "pronajem"},
    ))
    _patch_extractor(monkeypatch, _example_extractor_payload())

    response = client.post(
        "/buildings/from_url",
        json={"source": "ui", "url": "https://example.cz/byt/1"},
    )
    assert response.status_code == 400, response.text
    assert "apartment" in response.json()["detail"].lower()


def test_from_url_unsupported_category_rejected(client, monkeypatch):
    _patch_persistence(monkeypatch)
    _patch_dispatcher(monkeypatch, _FakeParseResult(
        spec={"category_main": "pozemek", "category_type": "prodej"},
    ))

    response = client.post(
        "/buildings/from_url",
        json={"source": "ui", "url": "https://example.cz/pozemek/1"},
    )
    assert response.status_code == 400
    assert "unsupported category_main" in response.json()["detail"]


def test_from_url_parse_failure_returns_502(client, monkeypatch):
    _patch_persistence(monkeypatch)
    monkeypatch.setattr(
        source_dispatcher, "parse_listing_url",
        lambda *a, **kw: (_ for _ in ()).throw(
            source_dispatcher.ParseError("dead url"),
        ),
    )

    response = client.post(
        "/buildings/from_url",
        json={"source": "ui", "url": "https://example.cz/dum/dead"},
    )
    assert response.status_code == 502
    assert "dead url" in response.json()["detail"]


def test_from_url_extractor_failure_marks_failed(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    _patch_dispatcher(monkeypatch, _FakeParseResult(
        spec={"category_main": "dum", "category_type": "prodej"},
    ))
    _patch_extractor(
        monkeypatch,
        building_extraction.BuildingExtractionError("no snapshot"),
    )

    response = client.post(
        "/buildings/from_url",
        json={"source": "ui", "url": "https://example.cz/dum/1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert "no snapshot" in body["error_message"]
    assert state.rows[1]["status"] == "failed"


def test_from_url_missing_sreality_id_marks_failed(client, monkeypatch):
    """A URL that parses but yields no sreality_id can't reach a snapshot."""
    _patch_persistence(monkeypatch)
    _patch_dispatcher(monkeypatch, _FakeParseResult(
        spec={"category_main": "dum"},
        sreality_id=None,
    ))
    extractor_calls = _patch_extractor(monkeypatch, _example_extractor_payload())

    response = client.post(
        "/buildings/from_url",
        json={"source": "ui", "url": "https://example.cz/dum/1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "sreality_id" in body["error_message"]
    assert extractor_calls == [], "extractor should not run without sreality_id"


# -- POST /buildings/{id}/confirm_units --------------------------------------


def test_confirm_units_happy_path(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    state.rows[1] = {
        "source": "ui", "status": "awaiting_input",
        "units_proposal": {"units": [], "building": {}, "confidence": "high",
                            "warnings": [], "n_images": 0,
                            "model": "x", "cost_usd": 0.01, "snapshot_id": 1},
    }
    state.next_id = 2

    units = [
        {
            "unit_id": "u1", "label": "flat 1", "floor": "1",
            "area_m2": 60.0, "disposition": "2+kk",
            "condition": "dobry", "is_potential": False,
            "source": "user_added", "notes": None,
        },
    ]
    response = client.post(
        "/buildings/1/confirm_units", json={"units": units},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "estimating"
    assert body["units"] == units
    assert state.rows[1]["status"] == "estimating"


def test_confirm_units_rejects_wrong_status(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    state.rows[1] = {"source": "ui", "status": "estimating"}
    state.next_id = 2

    response = client.post(
        "/buildings/1/confirm_units",
        json={"units": [{"unit_id": "u1", "is_potential": False}]},
    )
    assert response.status_code == 409


def test_confirm_units_rejects_missing_building(client, monkeypatch):
    _patch_persistence(monkeypatch)
    response = client.post(
        "/buildings/999/confirm_units",
        json={"units": [{"unit_id": "u1", "is_potential": False}]},
    )
    assert response.status_code == 404


def test_confirm_units_rejects_duplicate_unit_ids(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    state.rows[1] = {"source": "ui", "status": "awaiting_input"}
    state.next_id = 2

    response = client.post(
        "/buildings/1/confirm_units",
        json={"units": [
            {"unit_id": "u1", "is_potential": False},
            {"unit_id": "u1", "is_potential": False},
        ]},
    )
    assert response.status_code == 400
    assert "duplicate unit_id" in response.json()["detail"]


# -- POST /buildings/{id}/re_extract -----------------------------------------


def test_re_extract_happy_path(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    state.rows[1] = {
        "source": "ui", "status": "awaiting_input",
        "input_sreality_id": 999,
        "subject_summary": {"source_url": "x", "fields": {}, "building": {}},
    }
    state.next_id = 2
    extractor_calls = _patch_extractor(monkeypatch, _example_extractor_payload())

    response = client.post("/buildings/1/re_extract")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "awaiting_input"
    assert body["units_proposal"]["units"][0]["unit_id"] == "u1"
    assert extractor_calls[0]["force_refresh"] is True


def test_re_extract_rejects_wrong_status(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    state.rows[1] = {
        "source": "ui", "status": "estimating",
        "input_sreality_id": 999,
    }
    state.next_id = 2

    response = client.post("/buildings/1/re_extract")
    assert response.status_code == 409
