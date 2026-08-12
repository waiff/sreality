"""Broker route tests — hermetic, like test_routes.py (no DB/HTTP).

Both fixtures override `verify_jwt` only: `require_admin` depends on it, so one
override drives both gates and the 403 below is the real dependency's decision.
The admin fixture is parametrized over BOTH claim shapes `require_admin` honours,
so dropping either arm of `_is_admin` fails here instead of quietly serving one
admin a masked envelope while `/contacts` still answers them in full.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import brokers as broker_routes
from toolkit import brokers as brokers_toolkit

_LEADER_ROW = {"broker_id": 1, "display_name": "RK Alfa",
               "primary_email": "a@b.cz", "primary_phone": "+420 111 222 333",
               "active_property_count": 9}


def _client(claims: dict):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: claims
    return TestClient(api_main.app)


@pytest.fixture()
def client():
    yield _client({"sub": "u-1", "email": "user@example.com"})
    api_main.app.dependency_overrides.clear()


@pytest.fixture(params=[{"is_admin": True}, {"app_metadata": {"is_admin": True}}],
                ids=["top-level-claim", "app-metadata-claim"])
def admin_client(request):
    yield _client({"sub": "u-0", **request.param})
    api_main.app.dependency_overrides.clear()


def test_leaderboard_passes_params(client, monkeypatch):
    captured = {}
    def fake(conn, **kw):
        captured.update(kw)
        return {"data": [{"broker_id": 1}], "metadata": {"tool": "broker_leaderboard"}}
    monkeypatch.setattr(broker_routes.brokers, "leaderboard", fake)

    res = client.get("/brokers/leaderboard",
                     params={"region_ids": [27, 116], "metric": "listing_count", "limit": 5})
    assert res.status_code == 200
    assert captured["region_ids"] == [27, 116]
    assert captured["metric"] == "listing_count"
    assert captured["limit"] == 5
    assert res.json()["data"] == [{"broker_id": 1}]  # no contact column, no flags


def test_leaderboard_masks_contacts_for_a_plain_user(client, monkeypatch):
    """The inversion this wave fixes: the leaderboard returned up to 2000 brokers'
    email + phone behind a token shipped inside the public SPA bundle."""
    monkeypatch.setattr(broker_routes.brokers, "leaderboard",
                        lambda conn, **kw: {"data": [dict(_LEADER_ROW)], "metadata": {}})
    body = client.get("/brokers/leaderboard").json()
    row = body["data"][0]
    assert row == {"broker_id": 1, "display_name": "RK Alfa",
                   "active_property_count": 9, "has_email": True, "has_phone": True}
    assert body["metadata"]["pii_masked"] is True


def test_leaderboard_is_unmasked_for_an_admin(admin_client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "leaderboard",
                        lambda conn, **kw: {"data": [dict(_LEADER_ROW)], "metadata": {}})
    body = admin_client.get("/brokers/leaderboard").json()
    assert body["data"][0]["primary_email"] == "a@b.cz"
    assert body["metadata"]["pii_masked"] is False


def test_leaderboard_flags_a_broker_with_no_contacts(client, monkeypatch):
    monkeypatch.setattr(
        broker_routes.brokers, "leaderboard",
        lambda conn, **kw: {"data": [{"broker_id": 2, "primary_email": None,
                                      "primary_phone": ""}], "metadata": {}})
    assert client.get("/brokers/leaderboard").json()["data"][0] == {
        "broker_id": 2, "has_email": False, "has_phone": False}


def test_get_broker_404_when_missing(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "get_broker", lambda conn, bid: None)
    assert client.get("/brokers/999").status_code == 404


def test_get_broker_returns_dossier(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "get_broker",
                        lambda conn, bid: {"data": {"broker": {"broker_id": bid}}, "metadata": {}})
    res = client.get("/brokers/527")
    assert res.status_code == 200
    assert res.json()["data"]["broker"]["broker_id"] == 527


def _dossier(conn, bid):
    return {"data": {"broker": {"broker_id": bid, "display_name": "RK Alfa",
                                "primary_email": "a@b.cz", "primary_phone": None},
                     "memberships": [{"firm_id": 3, "firm_domain": "alfa.cz"}],
                     "contacts": [{"kind": "email", "value": "a@b.cz"},
                                  {"kind": "phone", "value": "+420 111 222 333"}]},
            "metadata": {}}


def test_dossier_masks_the_broker_row_and_drops_the_contact_list(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "get_broker", _dossier)
    data = client.get("/brokers/527").json()["data"]
    assert "contacts" not in data
    assert data["broker"] == {"broker_id": 527, "display_name": "RK Alfa",
                              "has_email": True, "has_phone": False}
    assert data["memberships"] == [{"firm_id": 3, "firm_domain": "alfa.cz"}]


def test_dossier_is_whole_for_an_admin(admin_client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "get_broker", _dossier)
    data = admin_client.get("/brokers/527").json()["data"]
    assert data["broker"]["primary_email"] == "a@b.cz"
    assert data["contacts"][1]["value"] == "+420 111 222 333"


def test_by_listing_404_when_unattributed(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "listing_broker",
                        lambda conn, sid=None, *, listing_id=None: None)
    assert client.get("/brokers/by-listing/123").status_code == 404


def test_by_listing_surrogate_wins_over_the_path_sreality_id(client, monkeypatch):
    captured = {}
    def fake(conn, sreality_id=None, *, listing_id=None):
        captured.update(sreality_id=sreality_id, listing_id=listing_id)
        return {"data": {"broker_id": 4}, "metadata": {}}
    monkeypatch.setattr(broker_routes.brokers, "listing_broker", fake)
    assert client.get("/brokers/by-listing/123", params={"listing_id": 88}).status_code == 200
    assert captured == {"sreality_id": 123, "listing_id": 88}


def test_by_listing_accepts_a_surrogate_only_query(client, monkeypatch):
    captured = {}
    def fake(conn, sreality_id=None, *, listing_id=None):
        captured.update(sreality_id=sreality_id, listing_id=listing_id)
        return {"data": {"broker_id": 4}, "metadata": {}}
    monkeypatch.setattr(broker_routes.brokers, "listing_broker", fake)
    assert client.get("/brokers/by-listing", params={"listing_id": 88}).status_code == 200
    assert captured == {"sreality_id": None, "listing_id": 88}


def test_by_listing_422_without_any_id(client, monkeypatch):
    def boom(conn, sreality_id=None, *, listing_id=None):
        raise ValueError("a sreality_id or listing_id is required")
    monkeypatch.setattr(broker_routes.brokers, "listing_broker", boom)
    assert client.get("/brokers/by-listing").status_code == 422


def test_by_listings_batch(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(broker_routes.brokers, "listing_brokers",
                        lambda conn, ids: captured.update(ids=ids)
                        or {"data": [{"listing_id": 7, "broker_id": 1}], "metadata": {}})
    res = client.post("/brokers/by-listings", json={"listing_ids": [7, 9]})
    assert res.status_code == 200
    assert captured["ids"] == [7, 9]
    assert res.json()["data"][0]["listing_id"] == 7


def test_by_listings_rejects_an_unbounded_batch(client):
    res = client.post("/brokers/by-listings", json={"listing_ids": list(range(1001))})
    assert res.status_code == 422


def test_brokers_by_ids_repeated_query_params(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(broker_routes.brokers, "brokers_by_ids",
                        lambda conn, ids: captured.update(ids=ids)
                        or {"data": [dict(_LEADER_ROW)], "metadata": {}})
    res = client.get("/brokers", params=[("ids", 1), ("ids", 2)])
    assert res.status_code == 200
    assert captured["ids"] == [1, 2]
    assert "primary_email" not in res.json()["data"][0]


def test_brokers_by_ids_rejects_an_unbounded_batch(client):
    """The GET batch and its POST sibling must agree: an over-cap batch is a 422,
    never a silently truncated 200 that drops the highest ids as if they were
    brokers that don't exist."""
    over = [("ids", i) for i in range(brokers_toolkit.MAX_BATCH + 1)]
    assert client.get("/brokers", params=over).status_code == 422


def test_brokers_by_ids_rejects_a_non_numeric_id(client):
    assert client.get("/brokers", params=[("ids", "abc")]).status_code == 422


def test_batch_routes_accept_an_empty_selection(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "brokers_by_ids",
                        lambda conn, ids: {"data": [], "metadata": {}})
    monkeypatch.setattr(broker_routes.brokers, "listing_brokers",
                        lambda conn, ids: {"data": [], "metadata": {}})
    assert client.get("/brokers").json()["data"] == []
    assert client.post("/brokers/by-listings", json={"listing_ids": []}).json()["data"] == []


def test_search_masks_contacts_for_a_plain_user(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "search",
                        lambda conn, q, *, limit=12, include_pii=False: {
                            "data": [dict(_LEADER_ROW)], "metadata": {}})
    body = client.get("/brokers/search", params={"q": "alfa"}).json()
    assert body["data"][0] == {"broker_id": 1, "display_name": "RK Alfa",
                               "active_property_count": 9,
                               "has_email": True, "has_phone": True}
    assert body["metadata"]["pii_masked"] is True


def _search_include_pii(client, monkeypatch) -> bool:
    seen = {}
    monkeypatch.setattr(broker_routes.brokers, "search",
                        lambda conn, q, *, limit=12, include_pii=False:
                        seen.update(include_pii=include_pii)
                        or {"data": [], "metadata": {}})
    assert client.get("/brokers/search", params={"q": "alfa"}).status_code == 200
    return seen["include_pii"]


def test_search_pushes_the_non_admin_identity_into_the_query(client, monkeypatch):
    """Search is the one read whose PREDICATE is caller-supplied, so masking the
    projection alone left the ILIKE matching text the response redacted — a
    guess-confirming oracle over exactly what _redact_shaped hides. The policy has
    to reach the query, not just the envelope."""
    assert _search_include_pii(client, monkeypatch) is False


def test_search_still_matches_the_raw_name_for_an_admin(admin_client, monkeypatch):
    assert _search_include_pii(admin_client, monkeypatch) is True


def test_geo_options(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(broker_routes.brokers, "geo_options",
                        lambda conn, *, geo_level=None: captured.update(level=geo_level)
                        or {"data": [{"geo_level": "region", "geo_id": 27, "name": "Praha",
                                      "broker_count": 12}], "metadata": {}})
    res = client.get("/brokers/geo-options", params={"geo_level": "region"})
    assert res.status_code == 200
    assert captured["level"] == "region"
    assert res.json()["data"][0]["broker_count"] == 12


@pytest.mark.parametrize("level", brokers_toolkit.GEO_LEVELS)
def test_geo_options_accepts_every_level_the_toolkit_knows(client, monkeypatch, level):
    monkeypatch.setattr(broker_routes.brokers, "geo_options",
                        lambda conn, *, geo_level=None: {"data": [], "metadata": {}})
    assert client.get("/brokers/geo-options", params={"geo_level": level}).status_code == 200


def test_geo_options_rejects_a_level_it_cannot_answer(client):
    """`obec` is in the SPA's geo vocabulary but never in broker_geo_options, and
    an unvalidated level fell through to "no filter" — i.e. every region AND okres
    returned as if they were obce."""
    assert client.get("/brokers/geo-options",
                      params={"geo_level": "obec"}).status_code == 422


def test_contacts_is_admin_only(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "broker_contacts",
                        lambda conn, bid: {"data": [{"kind": "email", "value": "a@b.cz"}],
                                           "metadata": {}})
    assert client.get("/brokers/527/contacts").status_code == 403


def test_contacts_route(admin_client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "broker_contacts",
                        lambda conn, bid: {"data": [{"kind": "email", "value": "a@b.cz"}], "metadata": {}})
    res = admin_client.get("/brokers/527/contacts")
    assert res.status_code == 200
    assert res.json()["data"][0]["value"] == "a@b.cz"


_SENTINEL = {"data": [{"broker_id": 1, "display_name": "leak@rk.cz",
                       "primary_email": "leak@rk.cz",
                       "primary_phone": "+420 111 222 333"}], "metadata": {}}

# (method, route template, concrete URL, JSON body) for every non-admin /brokers
# route. `test_the_pii_gate_covers_every_non_admin_broker_route` keeps it honest.
_NON_ADMIN_CALLS = [
    ("GET", "/brokers", "/brokers?ids=1", None),
    ("GET", "/brokers/leaderboard", "/brokers/leaderboard", None),
    ("GET", "/brokers/search", "/brokers/search?q=a", None),
    ("GET", "/brokers/geo-options", "/brokers/geo-options", None),
    ("GET", "/brokers/by-listing", "/brokers/by-listing?listing_id=1", None),
    ("GET", "/brokers/by-listing/{sreality_id}", "/brokers/by-listing/1", None),
    ("POST", "/brokers/by-listings", "/brokers/by-listings", {"listing_ids": [1]}),
    ("GET", "/brokers/{broker_id}", "/brokers/1", None),
    ("GET", "/brokers/{broker_id}/listings", "/brokers/1/listings", None),
]

_TOOLKIT_READS = ("brokers_by_ids", "leaderboard", "search", "geo_options",
                  "listing_broker", "listing_brokers", "get_broker", "broker_listings")


def _stub_every_toolkit_read(monkeypatch) -> None:
    for name in _TOOLKIT_READS:
        monkeypatch.setattr(broker_routes.brokers, name,
                            lambda *a, **kw: {"data": [dict(_SENTINEL["data"][0])],
                                              "metadata": {}})


@pytest.mark.parametrize("method,_template,url,body", _NON_ADMIN_CALLS,
                         ids=[f"{m} {t}" for m, t, _, _ in _NON_ADMIN_CALLS])
def test_every_non_admin_broker_route_applies_the_pii_policy(
        client, monkeypatch, method, _template, url, body):
    """`pii_masked` is stamped only by apply_pii_policy, so asserting it — plus the
    absence of the sentinel address anywhere in the body — is a complete gate that
    a route can't be added (or a `_policy(...)` wrapper dropped) without noticing."""
    _stub_every_toolkit_read(monkeypatch)
    res = client.request(method, url, json=body)
    assert res.status_code == 200, res.text
    assert "leak@rk.cz" not in res.text
    assert res.json()["metadata"]["pii_masked"] is True


def test_the_pii_gate_covers_every_non_admin_broker_route() -> None:
    from tests.api.test_admin_route_coverage import _api_routes, _reachable_calls

    live = {
        (method, path)
        for method, path, route in _api_routes()
        if path.startswith("/brokers")
        and deps.require_admin not in _reachable_calls(route.dependant)
    }
    covered = {(m, t) for m, t, _, _ in _NON_ADMIN_CALLS}
    assert live == covered, (
        "the PII gate table and the mounted non-admin /brokers routes disagree; "
        f"uncovered={sorted(live - covered)} stale={sorted(covered - live)}")


def test_no_broker_route_rides_the_static_token() -> None:
    """Standing gate on the whole point of this wave: VITE_API_TOKEN is extractable
    from the shipped bundle, so a broker read re-gated on require_token would be a
    silent regression to unauthenticated PII access."""
    from tests.api.test_admin_route_coverage import _api_routes, _reachable_calls

    offenders = sorted(
        f"  {method} {path}"
        for method, path, route in _api_routes()
        if path.startswith("/brokers")
        and deps.require_token in _reachable_calls(route.dependant)
    )
    assert not offenders, "broker route(s) back on the shared secret:\n" + "\n".join(offenders)
