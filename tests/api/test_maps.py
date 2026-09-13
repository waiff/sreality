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


# ---------------- backup-key failover (MAPY2_CZ_API_KEY) ----------------


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status}", response=resp)


def _mock_http_sequence(monkeypatch, outcomes: list[Any]) -> dict[str, Any]:
    """Mock _http_get_json to yield each outcome (payload or Exception) per call,
    recording the apikey used each time."""
    state: dict[str, Any] = {"i": 0, "keys": []}

    def fake(url: str, params: dict[str, Any]) -> dict[str, Any]:
        state["keys"].append(params.get("apikey"))
        outcome = outcomes[state["i"]]
        state["i"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(maps, "_http_get_json", fake)
    return state


@pytest.mark.parametrize("bad_status", [401, 403, 429])
def test_suggest_fails_over_to_backup_key(client, monkeypatch, bad_status):
    monkeypatch.setenv("MAPY2_CZ_API_KEY", "backup-key")
    state = _mock_http_sequence(monkeypatch, [_http_error(bad_status), _MOCK_SUGGEST])
    res = client.get("/maps/suggest", params={"query": "Vinohrady"})
    assert res.status_code == 200
    assert len(res.json()["items"]) == 2
    assert state["keys"] == ["test-key", "backup-key"]


def test_suggest_no_failover_on_server_error(client, monkeypatch):
    # A 500 is a Mapy outage, not a key problem: 503 immediately, backup untouched.
    monkeypatch.setenv("MAPY2_CZ_API_KEY", "backup-key")
    state = _mock_http_sequence(monkeypatch, [_http_error(500), _MOCK_SUGGEST])
    res = client.get("/maps/suggest", params={"query": "Vinohrady"})
    assert res.status_code == 503
    assert state["keys"] == ["test-key"]


def test_suggest_both_keys_rejected_returns_503(client, monkeypatch):
    monkeypatch.setenv("MAPY2_CZ_API_KEY", "backup-key")
    state = _mock_http_sequence(monkeypatch, [_http_error(403), _http_error(403)])
    res = client.get("/maps/suggest", params={"query": "Vinohrady"})
    assert res.status_code == 503
    assert state["keys"] == ["test-key", "backup-key"]


def test_suggest_uses_backup_when_primary_unset(client, monkeypatch):
    monkeypatch.delenv("MAPY_CZ_API_KEY", raising=False)
    monkeypatch.setenv("MAPY2_CZ_API_KEY", "backup-key")
    state = _mock_http_sequence(monkeypatch, [_MOCK_SUGGEST])
    res = client.get("/maps/suggest", params={"query": "Vinohrady"})
    assert res.status_code == 200
    assert state["keys"] == ["backup-key"]


# ---------------- /maps/resolve ----------------


class _FakeCursor:
    """Minimal psycopg-like cursor for hermetic resolve tests.

    Each scripted entry answers ONE `execute`: a tuple for a `fetchone` query,
    a list for a `fetchall` one."""

    def __init__(self, scripted: list[Any]):
        self._scripted = scripted
        self._next: Any = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: Any = None) -> None:
        self._next = self._scripted.pop(0) if self._scripted else None

    def fetchone(self) -> Any:
        return self._next

    def fetchall(self) -> Any:
        return self._next if isinstance(self._next, list) else []


class _FakeConn:
    def __init__(self, scripted: list[Any]):
        self._scripted = scripted

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._scripted)


def _override_conn(scripted: list[Any]) -> None:
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: _FakeConn(scripted)


# W3 S3: resolve reads the RUIAN MIRROR, not admin_boundaries. The chip carries
# the RUIAN code, which is the same number `listing_location` answers with, so
# chip and listing come from one registry version.
#
# The scripted sequence for a resolvable point is always:
#   1. to_regclass('public.ruian_admin_units') -> (True,)
#   2. the current registry version                -> (7,)
#   3. the containing-obec PIP + its parent chain  -> [(unit_id, level, code, name)]
_MIRROR_PRESENT = [(True,), (7,)]
_CHAIN_JIHLAVA = [
    (9001, "obec", 586846, "Jihlava"),
    (9002, "okres", 3707, "Jihlava"),
    (9003, "kraj", 108, "Kraj Vysočina"),
    (9004, "stat", 1, "Česko"),
]

_OBEC_SUGGESTION = {
    "label": "Jihlava, okres Jihlava",
    "name": "Jihlava",
    "lat": 49.3961,
    "lng": 15.5912,
    "type": "regional.municipality",
}

_OKRES_SUGGESTION = {
    "label": "Okres Jihlava",
    "name": "okres Jihlava",
    "lat": 49.40,
    "lng": 15.60,
    "type": "regional.region.district",
}

_STREET_SUGGESTION = {
    "label": "Vinohradská 1234, Praha 2",
    "name": "Vinohradská",
    "lat": 50.078,
    "lng": 14.444,
    "type": "regional.street",
}

_PART_SUGGESTION = {
    "label": "Vinohrady, Praha 2",
    "name": "Vinohrady",
    "lat": 50.077,
    "lng": 14.441,
    "type": "regional.municipality_part",
}


def test_resolve_obec_matches_the_obec_code(client):
    _override_conn(scripted=[*_MIRROR_PRESENT, _CHAIN_JIHLAVA])
    res = client.post("/maps/resolve", json=_OBEC_SUGGESTION)
    assert res.status_code == 200
    body = res.json()
    # An obec pick resolves to the obec code — NOT the same-named okres.
    assert body["kind"] == "admin"
    assert body["level"] == "obec"
    assert body["id"] == 586846
    assert body["obec_id"] == 586846
    assert body["name"] == "Jihlava"


def test_resolve_okres_matches_the_okres_code(client):
    # Same point, but an okres-level pick resolves to the okres code (3707), so
    # picking "Okres Jihlava" and obec "Jihlava" are distinct units.
    _override_conn(scripted=[*_MIRROR_PRESENT, _CHAIN_JIHLAVA])
    res = client.post("/maps/resolve", json=_OKRES_SUGGESTION)
    body = res.json()
    assert body["kind"] == "admin"
    assert body["level"] == "okres"
    assert body["id"] == 3707
    assert body["obec_id"] == 586846


def test_resolve_cast_obce_is_placed_by_name_inside_the_pipped_obec(client):
    """RUIAN publishes no part-of-municipality polygon (ruian_boundaries.LAYERS
    loads ten levels and neither cast_obce nor momc is one), so a quarter can
    never be point-in-polygon'd: the POINT places the obec and the NAME places
    the part inside it. That is the whole reason the pick's `name` is sent."""
    _override_conn(scripted=[
        *_MIRROR_PRESENT,
        _CHAIN_JIHLAVA,
        [("cast_obce", 490067, "Vinohrady")],
    ])
    res = client.post("/maps/resolve", json=_PART_SUGGESTION)
    body = res.json()
    assert body["kind"] == "admin"
    assert body["level"] == "cast_obce"
    assert body["id"] == 490067
    assert body["name"] == "Vinohrady"
    assert body["obec_id"] == 586846


def test_resolve_composite_quarter_name_retries_on_its_last_segment(client):
    """Mapy names a quarter the way a person says it — "Hradec Králové - Třebeš",
    "Ostrava-Poruba" — while RÚIAN stores the bare part ("Třebeš", "Poruba").

    RED by: dropping the retry, which turns every composite pick into a chip
    that matches nothing."""
    _override_conn(scripted=[
        *_MIRROR_PRESENT,
        _CHAIN_JIHLAVA,
        [],                                  # the full name is not a part
        [("cast_obce", 647055, "Třebeš")],   # its last segment is
    ])
    res = client.post("/maps/resolve", json={
        **_PART_SUGGESTION, "name": "Hradec Králové - Třebeš",
    })
    body = res.json()
    assert body["kind"] == "admin"
    assert body["level"] == "cast_obce"
    assert body["id"] == 647055


def test_an_unplaceable_quarter_gets_NO_CODE_never_the_whole_town(client):
    """THE defect this branch exists to not ship. A městská část ("Praha 2",
    "Brno-střed" — both `momc`, which the answer table has no column for) and a
    colloquial Mapy neighbourhood cannot be placed as a `cast_obce`. Falling
    back to the containing obec would silently turn a saved "this quarter"
    filter into "this whole city" — most expensively in a watchdog, which would
    then mail the operator about every listing in Prague.

    No code is the honest answer: the chip renders `Nerozpoznáno` and matches
    nothing, exactly like every other unresolvable chip."""
    _override_conn(scripted=[
        *_MIRROR_PRESENT,
        _CHAIN_JIHLAVA,
        [],   # "Praha 2" is not a část obce
        [],   # neither is its last segment, "2"
    ])
    res = client.post("/maps/resolve", json={**_PART_SUGGESTION, "name": "Praha 2"})
    body = res.json()
    assert body["kind"] == "point_with_radius"
    assert body["id"] is None
    assert body["level"] is None
    # The obec was found by the PIP and is deliberately NOT published as the
    # chip's code — that is the whole point.
    assert body["obec_id"] is None


def test_an_ambiguous_quarter_name_is_refused_rather_than_guessed(client):
    """Two parts of one obec sharing a name is a cohort the operator did not
    choose. Fail closed rather than take matches[0]."""
    _override_conn(scripted=[
        *_MIRROR_PRESENT,
        _CHAIN_JIHLAVA,
        [("cast_obce", 490067, "Vinohrady"), ("cast_obce", 490068, "Vinohrady")],
    ])
    res = client.post("/maps/resolve", json=_PART_SUGGESTION)
    body = res.json()
    assert body["kind"] == "point_with_radius"
    assert body["id"] is None


def test_resolve_street_is_locality_with_containing_obec(client):
    # A street has no code of its own: it resolves to its CONTAINING obec, so
    # the chip filters at the obec level.
    _override_conn(scripted=[*_MIRROR_PRESENT, _CHAIN_JIHLAVA])
    res = client.post("/maps/resolve", json=_STREET_SUGGESTION)
    body = res.json()
    assert body["kind"] == "locality"
    assert body["level"] == "locality"
    assert body["id"] is None
    assert body["obec_id"] == 586846


def test_resolve_falls_back_to_point_when_the_mirror_is_absent(client):
    # to_regclass returns None → the mirror isn't loaded → point + radius.
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
    _override_conn(scripted=[*_MIRROR_PRESENT, []])
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


def test_resolve_no_current_registry_version_falls_back(client):
    # The mirror table exists but no registry_versions row is current.
    _override_conn(scripted=[(True,), None])
    res = client.post("/maps/resolve", json=_OBEC_SUGGESTION)
    body = res.json()
    assert body["kind"] == "point_with_radius"


def test_resolve_unknown_type_is_unresolved(client):
    # A type with no level mapping (e.g. country / unknown) narrows nothing —
    # no PIP is attempted.
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


# ---------------- /maps/resolve-names (the stored-chip reader) ----------------


def test_resolve_names_answers_every_level_a_name_means(client):
    """"Jihlava" is an obec AND an okres. The ILIKE this replaces matched both,
    so the compatibility reader keeps both codes and the chip becomes the union
    — never a silent narrowing to one of them."""
    _override_conn(scripted=[
        *_MIRROR_PRESENT,
        [("obec", 586846, "Jihlava"), ("okres", 3707, "Jihlava")],
    ])
    res = client.post("/maps/resolve-names", json={"chips": [{"name": "Jihlava"}]})
    assert res.status_code == 200
    assert res.json() == {
        "chips": [
            {
                "name": "Jihlava",
                "context": None,
                "matches": [
                    {"level": "obec", "id": 586846},
                    {"level": "okres", "id": 3707},
                ],
            }
        ]
    }


def test_resolve_names_unknown_name_gets_no_matches(client):
    _override_conn(scripted=[*_MIRROR_PRESENT, []])
    res = client.post("/maps/resolve-names", json={"chips": [{"name": "U Kulaťáku"}]})
    assert res.json()["chips"][0]["matches"] == []


def test_resolve_names_without_the_mirror_answers_empty(client):
    _override_conn(scripted=[(None,)])
    res = client.post("/maps/resolve-names", json={"chips": [{"name": "Brno"}]})
    assert res.status_code == 200
    assert res.json()["chips"][0]["matches"] == []


def test_resolve_names_empty_request_is_a_no_op(client):
    _override_conn(scripted=[])
    res = client.post("/maps/resolve-names", json={"chips": []})
    assert res.status_code == 200
    assert res.json() == {"chips": []}
