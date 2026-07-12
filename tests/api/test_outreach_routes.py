"""Outreach CRM route tests — hermetic (no DB/HTTP/LLM)."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import outreach as outreach_routes


@pytest.fixture()
def client():
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.get_llm_client] = lambda: object()
    api_main.app.dependency_overrides[deps.require_admin] = (
        lambda: {"is_admin": True, "legacy": True}
    )
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_create_campaign(client, monkeypatch):
    captured = {}
    def fake(conn, **kw):
        captured.update(kw)
        return {"id": 1, "name": kw["name"], "status": "draft"}
    monkeypatch.setattr(outreach_routes.outreach, "create_campaign", fake)

    res = client.post("/outreach/campaigns", json={
        "name": "Praha byty", "goal": "off-market", "target": {"region_ids": [27]}})
    assert res.status_code == 200
    assert captured["name"] == "Praha byty"
    assert captured["target"] == {"region_ids": [27]}
    assert res.json()["id"] == 1


def test_get_campaign_404(client, monkeypatch):
    monkeypatch.setattr(outreach_routes.outreach, "get_campaign", lambda conn, cid: None)
    assert client.get("/outreach/campaigns/9").status_code == 404


def test_generate_drafts(client, monkeypatch):
    captured = {}
    def fake(conn, llm, cid, *, limit):
        captured["cid"], captured["limit"] = cid, limit
        return {"generated": 3, "targets": 5}
    monkeypatch.setattr(outreach_routes.outreach, "generate_drafts", fake)

    res = client.post("/outreach/campaigns/2/generate", params={"limit": 10})
    assert res.status_code == 200
    assert captured == {"cid": 2, "limit": 10}
    assert res.json() == {"generated": 3, "targets": 5}


def test_update_message_404(client, monkeypatch):
    monkeypatch.setattr(outreach_routes.outreach, "update_message", lambda conn, mid, **kw: None)
    assert client.patch("/outreach/messages/5", json={"status": "approved"}).status_code == 404


def test_update_message_ok(client, monkeypatch):
    captured = {}
    def fake(conn, mid, **kw):
        captured.update(kw); captured["mid"] = mid
        return {"id": mid, "status": kw.get("status")}
    monkeypatch.setattr(outreach_routes.outreach, "update_message", fake)
    res = client.patch("/outreach/messages/5", json={"status": "sent"})
    assert res.status_code == 200
    assert captured["mid"] == 5 and captured["status"] == "sent"


def test_suppression_add_and_remove(client, monkeypatch):
    monkeypatch.setattr(outreach_routes.outreach, "suppress_broker",
                        lambda conn, bid, **kw: {"broker_id": bid, "reason": kw.get("reason")})
    res = client.post("/outreach/suppressions", json={"broker_id": 7, "reason": "asked to stop"})
    assert res.status_code == 200 and res.json()["broker_id"] == 7

    monkeypatch.setattr(outreach_routes.outreach, "unsuppress_broker", lambda conn, bid: True)
    assert client.delete("/outreach/suppressions/7").status_code == 200

    monkeypatch.setattr(outreach_routes.outreach, "unsuppress_broker", lambda conn, bid: False)
    assert client.delete("/outreach/suppressions/7").status_code == 404
