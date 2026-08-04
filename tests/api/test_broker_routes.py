"""Broker route tests — hermetic, like test_routes.py (no DB/HTTP)."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import brokers as broker_routes


@pytest.fixture()
def client():
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.require_admin] = (
        lambda: {"is_admin": True, "legacy": True}
    )
    yield TestClient(api_main.app)
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
    assert res.json()["data"] == [{"broker_id": 1}]


def test_get_broker_404_when_missing(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "get_broker", lambda conn, bid: None)
    assert client.get("/brokers/999").status_code == 404


def test_get_broker_returns_dossier(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "get_broker",
                        lambda conn, bid: {"data": {"broker": {"broker_id": bid}}, "metadata": {}})
    res = client.get("/brokers/527")
    assert res.status_code == 200
    assert res.json()["data"]["broker"]["broker_id"] == 527


def test_by_listing_404_when_unattributed(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "listing_broker", lambda conn, lid: None)
    assert client.get("/brokers/by-listing/123").status_code == 404


def test_by_listing_batch_passes_ids(client, monkeypatch):
    captured = {}
    def fake(conn, listing_ids):
        captured["listing_ids"] = listing_ids
        return {"data": [{"listing_id": 123, "broker_id": 1}], "metadata": {}}
    monkeypatch.setattr(broker_routes.brokers, "listing_brokers_by_ids", fake)

    res = client.get("/brokers/by-listing", params={"listing_ids": [123, 456]})
    assert res.status_code == 200
    assert captured["listing_ids"] == [123, 456]
    assert res.json()["data"][0]["listing_id"] == 123


def test_by_ids_batch_passes_ids(client, monkeypatch):
    captured = {}
    def fake(conn, broker_ids):
        captured["broker_ids"] = broker_ids
        return {"data": [{"broker_id": 7}], "metadata": {}}
    monkeypatch.setattr(broker_routes.brokers, "brokers_by_ids", fake)

    res = client.get("/brokers/by-ids", params={"broker_ids": [7, 8]})
    assert res.status_code == 200
    assert captured["broker_ids"] == [7, 8]
    assert res.json()["data"][0]["broker_id"] == 7


def test_contacts_route(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "broker_contacts",
                        lambda conn, bid: {"data": [{"kind": "email", "value": "a@b.cz"}], "metadata": {}})
    res = client.get("/brokers/527/contacts")
    assert res.status_code == 200
    assert res.json()["data"][0]["value"] == "a@b.cz"


def test_leaderboard_requires_admin():
    """Without the require_admin override (and no Authorization header), the
    request must be rejected — this is the actual A6 gate, not just a UX nicety
    (see api/routes/brokers.py). A stray require_token regression here would
    silently reopen the exposure migration 299 closed."""
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    try:
        res = TestClient(api_main.app).get("/brokers/leaderboard")
        assert res.status_code == 401
    finally:
        api_main.app.dependency_overrides.clear()
