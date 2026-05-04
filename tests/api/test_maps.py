"""Tests for the Mapy.cz proxy endpoints. Hermetic — all HTTP mocked."""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api import maps


_MOCK_SUGGEST = {
    "items": [
        {
            "name": "Vinohrady",
            "label": "Vinohrady, Praha",
            "type": "regional.municipality_part",
            "position": {"lon": 14.441, "lat": 50.077},
            "regionalStructure": [
                {"name": "Vinohrady", "type": "regional.municipality_part"},
                {"name": "Praha 2", "type": "regional.municipality"},
                {"name": "Hlavní město Praha", "type": "regional.region"},
                {"name": "Česko", "type": "regional.country"},
            ],
        },
        {
            "name": "Vinohradská",
            "label": "Vinohradská, Praha",
            "type": "regional.street",
            "position": {"lon": 14.45, "lat": 50.08},
            "regionalStructure": [],
        },
    ],
}


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.get_sreality_client] = lambda: object()
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test-key")
    maps.clear_suggest_cache()
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()
    maps.clear_suggest_cache()


def _mock_http(monkeypatch, payload: dict[str, Any], counter: list[int] | None = None):
    def fake(url: str, params: dict[str, Any]) -> dict[str, Any]:
        if counter is not None:
            counter.append(1)
        return payload
    monkeypatch.setattr(maps, "_http_get_json", fake)


def test_suggest_returns_items(client, monkeypatch):
    _mock_http(monkeypatch, _MOCK_SUGGEST)
    res = client.get("/maps/suggest", params={"query": "Vinohrady"})
    assert res.status_code == 200
    body = res.json()
    assert "items" in body
    assert len(body["items"]) == 2
    assert body["items"][0]["name"] == "Vinohrady"


def test_suggest_503_when_key_unset(client, monkeypatch):
    monkeypatch.delenv("MAPY_CZ_API_KEY", raising=False)
    res = client.get("/maps/suggest", params={"query": "Vinohrady"})
    assert res.status_code == 503
    assert "geocoding not configured" in res.json()["detail"]


def test_suggest_caches_within_ttl(client, monkeypatch):
    counter: list[int] = []
    _mock_http(monkeypatch, _MOCK_SUGGEST, counter=counter)

    res1 = client.get("/maps/suggest", params={"query": "Vinohrady"})
    res2 = client.get("/maps/suggest", params={"query": "Vinohrady"})
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert len(counter) == 1, "second identical query should be served from cache"


def test_suggest_distinct_queries_not_cached_together(client, monkeypatch):
    counter: list[int] = []
    _mock_http(monkeypatch, _MOCK_SUGGEST, counter=counter)

    client.get("/maps/suggest", params={"query": "Vinohrady"})
    client.get("/maps/suggest", params={"query": "Smichov"})
    assert len(counter) == 2


def test_suggest_query_required(client):
    res = client.get("/maps/suggest")
    assert res.status_code == 422


def test_suggest_limit_bounds(client, monkeypatch):
    _mock_http(monkeypatch, _MOCK_SUGGEST)
    assert client.get("/maps/suggest", params={"query": "x", "limit": 0}).status_code == 422
    assert client.get("/maps/suggest", params={"query": "x", "limit": 21}).status_code == 422
