"""Tests for the Mapy.cz proxy endpoints. Hermetic — all HTTP mocked."""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api import maps


# Mirrors Mapy.cz's actual /v1/suggest shape: `label` is the result-class
# tag ("Ulice ", "Část obce "), NOT the human-readable place name. The
# specific name lives in `name`; the address with city/region context lives
# in `location`. See scraper/geocoding.py:13–38 for the schema notes.
_MOCK_SUGGEST = {
    "items": [
        {
            "name": "Vinohrady",
            "label": "Část obce ",
            "location": "Vinohrady, Praha 2, Hlavní město Praha",
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
            "label": "Ulice ",
            "location": "Vinohradská, Praha 2, Hlavní město Praha",
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


# ---------------- /maps/resolve ----------------


class _FakeCursor:
    """Minimal psycopg-like cursor for hermetic resolve tests."""

    def __init__(self, scripted: list[Any]):
        self._scripted = scripted
        self._next: Any = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._next = self._scripted.pop(0) if self._scripted else None

    def fetchone(self) -> Any:
        return self._next


class _FakeConn:
    def __init__(self, scripted: list[Any]):
        self._scripted = scripted

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._scripted)


def _override_conn(scripted: list[Any]) -> None:
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: _FakeConn(scripted)


_CITY_SUGGESTION = {
    "label": "Praha 2, Praha",
    "lat": 50.077,
    "lng": 14.441,
    "type": "regional.municipality",
    "regional_structure": [
        {"name": "Praha 2", "type": "regional.municipality"},
        {"name": "Hlavní město Praha", "type": "regional.region"},
    ],
}

_STREET_SUGGESTION = {
    "label": "Vinohradská 1234, Praha 2",
    "lat": 50.078,
    "lng": 14.444,
    "type": "regional.street",
    "regional_structure": [],
}


def test_resolve_city_falls_back_to_point_when_admin_boundaries_absent(client):
    # to_regclass returns None → table doesn't exist → fallback path
    _override_conn(scripted=[(None,)])
    res = client.post("/maps/resolve", json=_CITY_SUGGESTION)
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "point_with_radius"
    assert body["polygon"] is None
    assert body["default_radius_m"] == 5000  # municipality default
    assert body["lat"] == 50.077
    assert body["label"] == "Praha 2, Praha"


def test_resolve_city_returns_admin_polygon_when_table_populated(client):
    # to_regclass → True; EXISTS → True; first match for "Praha 2"/obec → (554782, "Praha 2")
    _override_conn(scripted=[(True,), (True,), (554782, "Praha 2")])
    res = client.post("/maps/resolve", json=_CITY_SUGGESTION)
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "admin_polygon"
    assert body["polygon"] == {"level": "obec", "id": 554782, "name": "Praha 2"}


def test_resolve_walks_to_region_when_obec_unmatched(client):
    # to_regclass → True; EXISTS → True; obec "Praha 2" miss → kraj "Hlavní město Praha" hit
    _override_conn(
        scripted=[(True,), (True,), None, (19, "Hlavní město Praha")]
    )
    res = client.post("/maps/resolve", json=_CITY_SUGGESTION)
    body = res.json()
    assert body["kind"] == "admin_polygon"
    assert body["polygon"] == {"level": "kraj", "id": 19, "name": "Hlavní město Praha"}


def test_resolve_street_always_point_with_radius(client):
    # Even with admin_boundaries populated, regional.street has no level mapping
    _override_conn(scripted=[(True,), (True,)])
    res = client.post("/maps/resolve", json=_STREET_SUGGESTION)
    body = res.json()
    assert body["kind"] == "point_with_radius"
    assert body["polygon"] is None
    assert body["default_radius_m"] == 500


def test_resolve_unresolved_when_no_coords(client):
    _override_conn(scripted=[])
    res = client.post(
        "/maps/resolve",
        json={"label": "nowhere", "lat": None, "lng": None},
    )
    body = res.json()
    assert body["kind"] == "unresolved"
    assert body["lat"] is None
    assert body["polygon"] is None


def test_resolve_table_empty_falls_back(client):
    # table exists but has no rows → fallback
    _override_conn(scripted=[(True,), (False,)])
    res = client.post("/maps/resolve", json=_CITY_SUGGESTION)
    body = res.json()
    assert body["kind"] == "point_with_radius"


def test_resolve_unknown_type_uses_default_radius(client):
    _override_conn(scripted=[(None,)])
    res = client.post(
        "/maps/resolve",
        json={
            "label": "Mystery place",
            "lat": 50.0,
            "lng": 14.0,
            "type": "regional.unknown_thing",
        },
    )
    body = res.json()
    assert body["kind"] == "point_with_radius"
    assert body["default_radius_m"] == 1500
