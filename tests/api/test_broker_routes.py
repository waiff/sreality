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
    monkeypatch.setattr(broker_routes.brokers, "listing_broker", lambda conn, sid: None)
    assert client.get("/brokers/by-listing/123").status_code == 404


def test_contacts_route(client, monkeypatch):
    monkeypatch.setattr(broker_routes.brokers, "broker_contacts",
                        lambda conn, bid: {"data": [{"kind": "email", "value": "a@b.cz"}], "metadata": {}})
    res = client.get("/brokers/527/contacts")
    assert res.status_code == 200
    assert res.json()["data"][0]["value"] == "a@b.cz"
