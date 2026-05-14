"""Tests for the manual rental estimates API endpoints.

Hermetic — overrides get_db_conn so no real DB is hit, and patches the
persistence helpers in api.manual_estimates with in-memory dicts.
Pydantic validation tests run through the route layer; the underlying
CHECK constraints are covered by an integration test against a real
Postgres (out of scope here).
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api import manual_estimates as me


@pytest.fixture()
def client():
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.require_token] = lambda: None
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


@pytest.fixture()
def store(monkeypatch):
    state: dict[str, Any] = {
        "rows":      {},
        "next_id":   1,
        "by_listing_ts": 0,
    }

    def _to_row(r: dict[str, Any]) -> dict[str, Any]:
        return dict(r)

    def fake_list(conn, sid):
        items = [_to_row(r) for r in state["rows"].values() if r["sreality_id"] == sid]
        items.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return {"data": items}

    def fake_create(conn, sid, body):
        rid = state["next_id"]
        state["next_id"] += 1
        state["by_listing_ts"] += 1
        ts = f"2026-05-13T12:00:{state['by_listing_ts']:02d}+00:00"
        row = {
            "id":          rid,
            "sreality_id": sid,
            "rent_czk":    body.rent_czk,
            "author":      body.author,
            "source_kind": body.source_kind,
            "notes":       body.notes,
            "created_at":  ts,
            "updated_at":  ts,
        }
        state["rows"][rid] = row
        return _to_row(row)

    def fake_update(conn, rid, body):
        if rid not in state["rows"]:
            from fastapi import HTTPException
            raise HTTPException(404, "manual estimate not found")
        row = state["rows"][rid]
        if body.rent_czk is not None:
            row["rent_czk"] = body.rent_czk
        if body.author is not None:
            row["author"] = body.author
        if body.source_kind is not None:
            row["source_kind"] = body.source_kind
        if body.notes is not None:
            row["notes"] = body.notes
        state["by_listing_ts"] += 1
        row["updated_at"] = f"2026-05-13T13:00:{state['by_listing_ts']:02d}+00:00"
        return _to_row(row)

    def fake_delete(conn, rid):
        if rid not in state["rows"]:
            from fastapi import HTTPException
            raise HTTPException(404, "manual estimate not found")
        del state["rows"][rid]
        return {"deleted": True}

    monkeypatch.setattr(me, "list_manual_estimates",   fake_list)
    monkeypatch.setattr(me, "create_manual_estimate",  fake_create)
    monkeypatch.setattr(me, "update_manual_estimate",  fake_update)
    monkeypatch.setattr(me, "delete_manual_estimate",  fake_delete)
    return state


# --- create -----------------------------------------------------------------


def test_create_returns_row(client, store) -> None:
    r = client.post(
        "/listings/12345/manual_estimates",
        json={
            "rent_czk":    30000,
            "author":      "petr",
            "source_kind": "broker",
            "notes":       "from broker quote",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == 1
    assert body["sreality_id"] == 12345
    assert body["rent_czk"] == 30000
    assert body["author"] == "petr"
    assert body["source_kind"] == "broker"
    assert body["notes"] == "from broker quote"


def test_create_minimal_fields(client, store) -> None:
    r = client.post(
        "/listings/9/manual_estimates",
        json={"rent_czk": 25000, "author": "p", "source_kind": "gut"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["notes"] is None


def test_create_rejects_low_rent(client, store) -> None:
    r = client.post(
        "/listings/9/manual_estimates",
        json={"rent_czk": 500, "author": "p", "source_kind": "broker"},
    )
    assert r.status_code == 422


def test_create_rejects_high_rent(client, store) -> None:
    r = client.post(
        "/listings/9/manual_estimates",
        json={"rent_czk": 5_000_000, "author": "p", "source_kind": "broker"},
    )
    assert r.status_code == 422


def test_create_rejects_unknown_source_kind(client, store) -> None:
    r = client.post(
        "/listings/9/manual_estimates",
        json={"rent_czk": 25000, "author": "p", "source_kind": "fancy"},
    )
    assert r.status_code == 422


def test_create_rejects_empty_author(client, store) -> None:
    r = client.post(
        "/listings/9/manual_estimates",
        json={"rent_czk": 25000, "author": "", "source_kind": "broker"},
    )
    assert r.status_code == 422


# --- list -------------------------------------------------------------------


def test_list_returns_rows_for_listing(client, store) -> None:
    client.post(
        "/listings/12345/manual_estimates",
        json={"rent_czk": 30000, "author": "p", "source_kind": "broker"},
    )
    client.post(
        "/listings/12345/manual_estimates",
        json={"rent_czk": 31000, "author": "p2", "source_kind": "gut"},
    )
    client.post(
        "/listings/99/manual_estimates",
        json={"rent_czk": 99000, "author": "x", "source_kind": "other"},
    )

    r = client.get("/listings/12345/manual_estimates")
    assert r.status_code == 200
    items = r.json()["data"]
    assert len(items) == 2
    assert {it["rent_czk"] for it in items} == {30000, 31000}


def test_list_empty_when_no_estimates(client, store) -> None:
    r = client.get("/listings/0/manual_estimates")
    assert r.status_code == 200
    assert r.json() == {"data": []}


# --- patch ------------------------------------------------------------------


def test_patch_updates_fields(client, store) -> None:
    created = client.post(
        "/listings/12/manual_estimates",
        json={"rent_czk": 30000, "author": "p", "source_kind": "broker"},
    ).json()
    r = client.patch(
        f"/manual_estimates/{created['id']}",
        json={"rent_czk": 32000, "notes": "bumped"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rent_czk"] == 32000
    assert body["notes"] == "bumped"
    assert body["author"] == "p"


def test_patch_404_when_missing(client, store) -> None:
    r = client.patch("/manual_estimates/9999", json={"rent_czk": 50000})
    assert r.status_code == 404


# --- delete -----------------------------------------------------------------


def test_delete_round_trip(client, store) -> None:
    created = client.post(
        "/listings/12/manual_estimates",
        json={"rent_czk": 30000, "author": "p", "source_kind": "broker"},
    ).json()
    r = client.delete(f"/manual_estimates/{created['id']}")
    assert r.status_code == 200
    assert r.json() == {"deleted": True}

    r2 = client.get("/listings/12/manual_estimates")
    assert r2.json()["data"] == []


def test_delete_404_when_missing(client, store) -> None:
    r = client.delete("/manual_estimates/9999")
    assert r.status_code == 404


# --- tools endpoint ---------------------------------------------------------


def test_tools_endpoint_delegates_to_toolkit(client, store, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_tool(conn, sreality_id):  # noqa: ANN001
        captured["sid"] = sreality_id
        return {
            "data":     {"estimates": []},
            "metadata": {
                "tool":           "get_manual_rental_estimates",
                "filters_used":   {"sreality_id": sreality_id},
                "result_count":   0,
                "queried_at":     "2026-05-13T00:00:00+00:00",
                "data_freshness": None,
            },
        }

    import toolkit.manual_estimates as toolkit_me

    monkeypatch.setattr(toolkit_me, "get_manual_rental_estimates", fake_tool)

    r = client.post(
        "/tools/get_manual_rental_estimates",
        json={"sreality_id": 12345},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"] == {"estimates": []}
    assert body["metadata"]["tool"] == "get_manual_rental_estimates"
    assert captured["sid"] == 12345
