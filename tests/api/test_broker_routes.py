"""Broker route tests — hermetic, like test_routes.py (no DB/HTTP).

Both fixtures override `verify_jwt` only: `require_admin` depends on it, so one
override drives both gates and the 403 below is the real dependency's decision.
The admin fixture is parametrized over BOTH claim shapes `require_admin` honours,
so dropping either arm of `deps.is_admin` fails here instead of quietly serving one
admin a masked envelope while `/contacts` still answers them in full.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api import tenant_pool
from api.routes import brokers as broker_routes

_SEARCH_ROW = {"broker_id": 1, "display_name": "RK Alfa",
               "primary_email": "a@b.cz", "primary_phone": "+420 111 222 333",
               "active_properties": 9}


def _client(claims: dict):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[tenant_pool.read_conn] = lambda: object()
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


_BOARD_ROW = {"broker_id": 1, "display_name": "RK Alfa", "firm_id": None, "firm_name": None, "firm_domain": None,
              "primary_email": "a@b.cz", "primary_phone": "+420 111 222 333", "properties": 9, "n": 6, "exited": 3,
              "unknown": 0, "share": 0.5, "rank": 0.19}


def _stub_board(monkeypatch) -> list:
    calls = []
    monkeypatch.setattr(broker_routes, "board", lambda conn, args, *, contacts: calls.append((args, contacts)) or {
        "data": [dict(_BOARD_ROW)], "metadata": {"tool": "broker_board"}})
    return calls


def test_the_board_takes_browses_filters_and_its_own_options(client, monkeypatch):
    calls = _stub_board(monkeypatch)
    res = client.post("/brokers/board", json={
        "category_type": "prodej", "dispositions": ["2+kk"], "districts": [{"name": "Praha", "level": "kraj",
        "id": 19}], "bounds": {"west": 14.2, "south": 49.9, "east": 14.7, "north": 50.2}, "horizon_days": 60,
        "sort": "speed", "limit": 50, "min_properties": 3, "firm_ids": [8], "broker_id": 4})
    assert res.status_code == 200, res.text
    ((args, contacts),) = calls
    assert contacts is True
    assert (args.dispositions, args.districts[0].id, args.bounds.west, args.horizon_days, args.sort, args.limit,
            args.min_properties, args.firm_ids, args.broker_id) == (["2+kk"], 19, 14.2, 60, "speed", 50, 3, [8], 4)


@pytest.mark.parametrize("body", [
    {},
    {"category_type": "prodej", "tags": {"set": "in"}},
    {"category_type": "prodej", "show_dismissed": True},
    {"category_type": "prodej", "location": {"west": 14.2}},
    {"category_type": "prodej", "horizon_days": 45},
    {"category_type": "prodej", "limit": 201},
    {"category_type": "prodej", "min_properties": 0},
    {"category_type": "prodej", "metric": "listing_count"},
], ids=["no deal type", "a lens", "dismissals", "the place composite", "a horizon off the menu", "over the cap",
        "no minimum", "the leaderboard's metric"])
def test_the_board_refuses_what_it_cannot_judge_before_it_reads(client, monkeypatch, body):
    calls = _stub_board(monkeypatch)
    assert client.post("/brokers/board", json=body).status_code == 422 and calls == []


def test_the_board_is_unmasked_for_an_admin(admin_client, monkeypatch):
    """A plain user's board is masked: the PII gate below."""
    _stub_board(monkeypatch)
    body = admin_client.post("/brokers/board", json={"category_type": None}).json()
    assert body["data"][0]["primary_email"] == "a@b.cz"
    assert body["metadata"]["pii_masked"] is False


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


_ATTRIBUTION_WITH_CONTACT = {
    "listing_id": 7, "broker_id": 1, "sreality_id": None,
    "broker_display_name": "RK Alfa", "broker_firm_label": "RE/MAX",
    "primary_email": "a@b.cz", "primary_phone": "+420 111 222 333",
}


def _by_listings_with_contact(c, monkeypatch) -> dict:
    monkeypatch.setattr(broker_routes.brokers, "listing_brokers",
                        lambda conn, ids: {"data": [dict(_ATTRIBUTION_WITH_CONTACT)],
                                           "metadata": {}})
    return c.post("/brokers/by-listings", json={"listing_ids": [7]}).json()


def test_by_listings_gives_a_non_admin_the_values(client, monkeypatch):
    """The property page's and board card's broker list shows the contact to every
    signed-in user (operator ruling 2026-10-18) — the one /brokers route that does
    not mask for a non-admin. The board, search and dossier still do."""
    body = _by_listings_with_contact(client, monkeypatch)
    assert body["data"] == [_ATTRIBUTION_WITH_CONTACT]
    assert body["metadata"]["pii_masked"] is False


def test_by_listings_gives_an_admin_the_values(admin_client, monkeypatch):
    body = _by_listings_with_contact(admin_client, monkeypatch)
    assert body["data"] == [_ATTRIBUTION_WITH_CONTACT]


def test_by_listings_sends_null_for_a_broker_with_neither_channel(client, monkeypatch):
    """NULL is a real answer ("this broker is unreachable") — the property page's
    broker list draws an em-dash from it."""
    monkeypatch.setattr(
        broker_routes.brokers, "listing_brokers",
        lambda conn, ids: {
            "data": [{**_ATTRIBUTION_WITH_CONTACT,
                      "primary_email": None, "primary_phone": None}],
            "metadata": {}})
    row = client.post("/brokers/by-listings", json={"listing_ids": [7]}).json()["data"][0]
    assert row["primary_email"] is None and row["primary_phone"] is None


def test_get_broker_property_ids_passes_the_portal_rule(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        broker_routes.brokers, "broker_property_ids",
        lambda conn, bid, **kw: captured.update(broker_id=bid, **kw)
        or {"data": [10, 11, 12], "metadata": {"capped": False}})
    res = client.get("/brokers/1/property-ids",
                     params=[("status", "inactive"), ("portal", "idnes"), ("portal", "remax")])
    assert res.status_code == 200
    assert captured == {"broker_id": 1, "status": "inactive", "portals": ["idnes", "remax"]}
    body = res.json()
    assert body["data"] == [10, 11, 12]  # a bare id list is a no-op under the mask
    assert body["metadata"]["capped"] is False
    assert body["metadata"]["pii_masked"] is True  # the policy runs uniformly regardless


def test_get_broker_property_ids_defaults_to_every_ad(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        broker_routes.brokers, "broker_property_ids",
        lambda conn, bid, **kw: captured.update(kw) or {"data": [], "metadata": {}})
    assert client.get("/brokers/1/property-ids").status_code == 200
    assert captured == {"status": "any", "portals": []}


@pytest.mark.parametrize("params", [
    [("portal", "nonsense")],
    [("portal", "idnes"), ("portal", "sreality.cz")],
    [("status", "delisted")],
])
def test_get_broker_property_ids_refuses_what_the_rule_does_not_know(client, monkeypatch, params):
    """An unknown portal must never reach the rule: `all_sources && '{nonsense}'`
    matches nothing, which would read as "this broker has no ads there"."""
    monkeypatch.setattr(broker_routes.brokers, "broker_property_ids",
                        lambda *a, **kw: pytest.fail("reached the toolkit"))
    assert client.get("/brokers/1/property-ids", params=params).status_code == 422


def test_the_broker_listing_id_route_is_gone(client):
    assert client.get("/brokers/1/listing-ids").status_code == 404


def test_the_batch_route_accepts_an_empty_selection(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "listing_brokers",
                        lambda conn, ids: {"data": [], "metadata": {}})
    assert client.post("/brokers/by-listings", json={"listing_ids": []}).json()["data"] == []


def test_the_broker_by_id_batch_route_is_gone(client):
    """No reader since W6 (migration 419) and none in 7 days of Railway traffic (#2063): a by-broker contact read
    would exceed what the page shows a user, which is the brokers of the ads they look at."""
    assert client.get("/brokers", params=[("ids", 1)]).status_code == 404


def test_search_masks_contacts_for_a_plain_user(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "search",
                        lambda conn, q, *, limit=12, include_pii=False: {
                            "data": [dict(_SEARCH_ROW)], "metadata": {}})
    body = client.get("/brokers/search", params={"q": "alfa"}).json()
    assert body["data"][0] == {"broker_id": 1, "display_name": "RK Alfa",
                               "active_properties": 9,
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


def test_firm_options(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(broker_routes.brokers, "firm_options",
                        lambda conn, *, q=None, limit=20: captured.update(q=q, limit=limit)
                        or {"data": [{"firm_id": 3, "canonical_domain": "mmreality.cz",
                                      "display_name": None, "broker_count": 1021}],
                            "metadata": {}})
    res = client.get("/brokers/firm-options", params={"q": "mmreality", "limit": 5})
    assert res.status_code == 200
    assert captured == {"q": "mmreality", "limit": 5}
    assert res.json()["data"][0]["canonical_domain"] == "mmreality.cz"


def test_firm_options_defaults_to_no_query(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "firm_options",
                        lambda conn, *, q=None, limit=20: {"data": [], "metadata": {}})
    assert client.get("/brokers/firm-options").status_code == 200


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
    ("POST", "/brokers/board", "/brokers/board", {"category_type": None}),
    ("GET", "/brokers/search", "/brokers/search?q=a", None),
    ("GET", "/brokers/firm-options", "/brokers/firm-options", None),
    ("GET", "/brokers/{broker_id}", "/brokers/1", None),
    ("GET", "/brokers/{broker_id}/listings", "/brokers/1/listings", None),
    ("GET", "/brokers/{broker_id}/property-ids", "/brokers/1/property-ids", None),
]

# The deliberate exceptions: every signed-in caller gets the contact values here
# (the property page's broker list, operator ruling 2026-10-18).
_UNMASKED_CALLS = [
    ("POST", "/brokers/by-listings", "/brokers/by-listings", {"listing_ids": [1]}),
]

_TOOLKIT_READS = ("search", "firm_options", "listing_brokers", "get_broker", "broker_listings",
                  "broker_property_ids")


def _stub_every_toolkit_read(monkeypatch) -> None:
    def sentinel(*a, **kw):
        return {"data": [dict(_SENTINEL["data"][0])], "metadata": {}}
    for name in _TOOLKIT_READS:
        monkeypatch.setattr(broker_routes.brokers, name, sentinel)
    monkeypatch.setattr(broker_routes, "board", sentinel)


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


@pytest.mark.parametrize("method,_template,url,body", _UNMASKED_CALLS,
                         ids=[f"{m} {t}" for m, t, _, _ in _UNMASKED_CALLS])
def test_the_unmasked_routes_still_stamp_the_policy(
        client, monkeypatch, method, _template, url, body):
    _stub_every_toolkit_read(monkeypatch)
    res = client.request(method, url, json=body)
    assert res.status_code == 200, res.text
    assert res.json()["metadata"]["pii_masked"] is False


@pytest.mark.parametrize("path", ["/brokers/search", "/brokers/firm-options", "/brokers/{broker_id}",
                                  "/broker-review/candidates"])
def test_a_read_counting_the_broker_book_is_bounded(path: str) -> None:
    """READ ONLY at 8 s, as the board's own read (tenant_pool.read_conn; tests/test_broker_board_live.py)."""
    from tests.api._routes import method_routes, reachable_calls

    (route,) = [r for m, p, r in method_routes() if (m, p) == ("GET", path)]
    assert tenant_pool.read_conn in reachable_calls(route)


def test_the_pii_gate_covers_every_non_admin_broker_route() -> None:
    from tests.api._routes import method_routes, reachable_calls

    live = {
        (method, path)
        for method, path, route in method_routes()
        if path.startswith("/brokers")
        and deps.require_admin not in reachable_calls(route)
    }
    covered = {(m, t) for m, t, _, _ in _NON_ADMIN_CALLS + _UNMASKED_CALLS}
    assert live == covered, (
        "the PII gate table and the mounted non-admin /brokers routes disagree; "
        f"uncovered={sorted(live - covered)} stale={sorted(covered - live)}")


def test_no_broker_route_rides_the_static_token() -> None:
    """Standing gate on the whole point of this wave: require_token discards the
    caller's claims, so `apply_pii_policy` could not see admin status — a broker
    read re-gated on it would be a silent regression (until 2026-10-13 that gate
    also admitted the bundle-public static key: unauthenticated PII access)."""
    from tests.api._routes import method_routes, reachable_calls

    offenders = sorted(
        f"  {method} {path}"
        for method, path, route in method_routes()
        if path.startswith("/brokers")
        and deps.require_token in reachable_calls(route)
    )
    assert not offenders, "broker route(s) back on the shared secret:\n" + "\n".join(offenders)
