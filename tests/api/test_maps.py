"""Tests for the Mapy.cz proxy endpoints. Hermetic — all HTTP mocked."""

from __future__ import annotations

from typing import Any

import pytest
import requests

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


def test_suggest_503_when_mapy_rejects_key(client, monkeypatch):
    # A suspended / invalid key makes Mapy reject the call (e.g. 403). The proxy
    # must return 503 (graceful frontend fallback), not a raw 500 (silent empty
    # dropdown).
    resp = requests.Response()
    resp.status_code = 403

    def _reject(url: str, params: dict[str, Any]) -> dict[str, Any]:
        raise requests.HTTPError("403 Forbidden", response=resp)

    monkeypatch.setattr(maps, "_http_get_json", _reject)
    res = client.get("/maps/suggest", params={"query": "Vinohrady"})
    assert res.status_code == 503
    assert "unavailable" in res.json()["detail"]


def test_suggest_503_when_mapy_unreachable(client, monkeypatch):
    def _down(url: str, params: dict[str, Any]) -> dict[str, Any]:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(maps, "_http_get_json", _down)
    res = client.get("/maps/suggest", params={"query": "Vinohrady"})
    assert res.status_code == 503


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


# Resolve now PIPs the picked point into admin_boundaries at the obec level and
# walks parent_id to okres/kraj — one row: (obec_id, obec_name, okres_id,
# okres_name, kraj_id, kraj_name). The id at the PICKED level is what matches.
_PIP_JIHLAVA = (586846, "Jihlava", 3707, "Jihlava", 108, "Kraj Vysočina")

_OBEC_SUGGESTION = {
    "label": "Jihlava, okres Jihlava",
    "lat": 49.3961,
    "lng": 15.5912,
    "type": "regional.municipality",
}

_OKRES_SUGGESTION = {
    "label": "Okres Jihlava",
    "lat": 49.40,
    "lng": 15.60,
    "type": "regional.region.district",
}

_STREET_SUGGESTION = {
    "label": "Vinohradská 1234, Praha 2",
    "lat": 50.078,
    "lng": 14.444,
    "type": "regional.street",
}


def test_resolve_obec_matches_obec_id(client):
    # to_regclass → True; EXISTS → True; obec-level PIP → the Jihlava row.
    _override_conn(scripted=[(True,), (True,), _PIP_JIHLAVA])
    res = client.post("/maps/resolve", json=_OBEC_SUGGESTION)
    assert res.status_code == 200
    body = res.json()
    # An obec pick resolves to the obec id — NOT the same-named okres.
    assert body["kind"] == "admin"
    assert body["level"] == "obec"
    assert body["id"] == 586846
    assert body["obec_id"] == 586846
    assert body["name"] == "Jihlava"


def test_resolve_okres_matches_okres_id(client):
    # Same point, but an okres-level pick resolves to the okres id (3707), so
    # picking "Okres Jihlava" and obec "Jihlava" are distinct admin units.
    _override_conn(scripted=[(True,), (True,), _PIP_JIHLAVA])
    res = client.post("/maps/resolve", json=_OKRES_SUGGESTION)
    body = res.json()
    assert body["kind"] == "admin"
    assert body["level"] == "okres"
    assert body["id"] == 3707
    assert body["obec_id"] == 586846


def test_resolve_street_is_locality_with_containing_obec(client):
    # A street resolves to its CONTAINING obec (for a locality-text narrow), not
    # a circle — so it's scoped to that municipality, no cross-city collisions.
    _override_conn(scripted=[(True,), (True,), _PIP_JIHLAVA])
    res = client.post("/maps/resolve", json=_STREET_SUGGESTION)
    body = res.json()
    assert body["kind"] == "locality"
    assert body["level"] == "locality"
    assert body["id"] is None
    assert body["obec_id"] == 586846


def test_resolve_falls_back_to_point_when_admin_boundaries_absent(client):
    # to_regclass returns None → table doesn't exist → point + radius fallback.
    _override_conn(scripted=[(None,)])
    res = client.post("/maps/resolve", json=_OBEC_SUGGESTION)
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "point_with_radius"
    assert body["level"] is None
    assert body["default_radius_m"] == 5000  # municipality default
    assert body["lat"] == 49.3961
    assert body["label"] == "Jihlava, okres Jihlava"


def test_resolve_foreign_point_falls_back_to_point(client):
    # In-bounds query but the point matches no obec polygon (foreign / gap).
    _override_conn(scripted=[(True,), (True,), None])
    res = client.post("/maps/resolve", json=_OBEC_SUGGESTION)
    body = res.json()
    assert body["kind"] == "point_with_radius"
    assert body["id"] is None


def test_resolve_unresolved_when_no_coords(client):
    _override_conn(scripted=[])
    res = client.post(
        "/maps/resolve",
        json={"label": "nowhere", "lat": None, "lng": None},
    )
    body = res.json()
    assert body["kind"] == "unresolved"
    assert body["lat"] is None
    assert body["id"] is None


def test_resolve_table_empty_falls_back(client):
    # table exists but has no rows → fallback
    _override_conn(scripted=[(True,), (False,)])
    res = client.post("/maps/resolve", json=_OBEC_SUGGESTION)
    body = res.json()
    assert body["kind"] == "point_with_radius"


def test_resolve_unknown_type_is_unresolved(client):
    # A type with no admin-level mapping (e.g. country / unknown) narrows
    # nothing — no PIP is attempted.
    _override_conn(scripted=[])
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
    assert body["kind"] == "unresolved"
    assert body["level"] is None
