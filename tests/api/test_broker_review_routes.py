"""Broker merge-review route tests — hermetic (no DB)."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import broker_review as routes


@pytest.fixture()
def client():
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.require_admin] = (
        lambda: {"is_admin": True, "legacy": True}
    )
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_candidates_list(client, monkeypatch):
    monkeypatch.setattr(routes.review, "list_candidates",
                        lambda conn, **kw: {"candidates": [{"id": 1}], "count": 1})
    res = client.get("/broker-review/candidates")
    assert res.status_code == 200
    assert res.json()["count"] == 1


def test_merge_candidate_passes_subset(client, monkeypatch):
    captured = {}
    def fake(conn, cid, *, broker_ids=None, created_by=None):
        captured["cid"], captured["ids"] = cid, broker_ids
        return {"merge_group_id": "g", "survivor_broker_id": 1, "retired_broker_ids": [2]}
    monkeypatch.setattr(routes.review, "merge_candidate", fake)
    res = client.post("/broker-review/candidates/5/merge", json={"broker_ids": [1, 2]})
    assert res.status_code == 200
    assert captured == {"cid": 5, "ids": [1, 2]}


def test_merge_candidate_404(client, monkeypatch):
    monkeypatch.setattr(routes.review, "merge_candidate", lambda conn, cid, **kw: None)
    assert client.post("/broker-review/candidates/9/merge", json={}).status_code == 404


def test_merge_candidate_conflict(client, monkeypatch):
    def boom(conn, cid, **kw):
        raise routes.review.MergeError("fewer than two active")
    monkeypatch.setattr(routes.review, "merge_candidate", boom)
    assert client.post("/broker-review/candidates/9/merge", json={}).status_code == 409


def test_dismiss_candidate(client, monkeypatch):
    monkeypatch.setattr(routes.review, "dismiss_candidate",
                        lambda conn, cid, **kw: {"id": cid, "status": "dismissed"})
    res = client.post("/broker-review/candidates/3/dismiss")
    assert res.status_code == 200 and res.json()["status"] == "dismissed"


def test_unmerge_404(client, monkeypatch):
    monkeypatch.setattr(routes.review, "unmerge_group", lambda conn, g, **kw: None)
    assert client.post("/broker-review/merges/abc/unmerge").status_code == 404


def test_unmerge_ok(client, monkeypatch):
    monkeypatch.setattr(routes.review, "unmerge_group",
                        lambda conn, g, **kw: {"merge_group_id": g, "survivor_broker_id": 1,
                                               "restored_broker_ids": [2]})
    res = client.post("/broker-review/merges/abc/unmerge")
    assert res.status_code == 200 and res.json()["restored_broker_ids"] == [2]
