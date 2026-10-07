"""Tests for the manual rental estimates API endpoints.

Hermetic — overrides get_db_conn so no real DB is hit, and patches the
persistence helpers in api.manual_estimates with in-memory dicts.
Pydantic validation tests run through the route layer; the underlying
CHECK constraints are covered by an integration test against a real
Postgres (out of scope here). The writes require an admin JWT (shared
reference data, migration 290); the gate itself is exercised un-overridden
in the auth tests at the bottom.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient
jwt = pytest.importorskip("jwt")  # PyJWT (api extra)

from api import dependencies as deps
from api import main as api_main
from api import manual_estimates as me
from api import schemas as s
from api import tenant_pool

_JWT_SECRET = "test-hs256-secret"
_ADMIN_CLAIMS = {"sub": "op-uuid", "email": "op@example.com",
                 "app_metadata": {"is_admin": True}}


def _jwt(is_admin: bool) -> str:
    return jwt.encode(
        {"aud": "authenticated", "sub": "u", "app_metadata": {"is_admin": is_admin}},
        _JWT_SECRET, algorithm="HS256",
    )


@pytest.fixture()
def client():
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.require_token] = lambda: None
    api_main.app.dependency_overrides[deps.require_admin] = lambda: _ADMIN_CLAIMS
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

    def fake_create(conn, sid, body, claims):
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

    def fake_update(conn, rid, body, claims):
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


# --- admin gate --------------------------------------------------------------


@pytest.fixture()
def gated_client(client, store, monkeypatch):
    """The real require_admin gate (no override), HS256 verification, a set API_TOKEN."""
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    monkeypatch.setenv("API_TOKEN", "secret-xyz")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)
    store["rows"][1] = {
        "id": 1, "sreality_id": 12345, "rent_czk": 30000, "author": "op",
        "source_kind": "broker", "notes": None,
        "created_at": "2026-05-13T12:00:00+00:00",
        "updated_at": "2026-05-13T12:00:00+00:00",
    }
    store["next_id"] = 2
    return client


_CREATE = {"rent_czk": 30000, "author": "petr", "source_kind": "broker"}


def _writes(c, headers):
    return [
        c.post("/listings/12345/manual_estimates", json=_CREATE, headers=headers),
        c.patch("/manual_estimates/1", json={"rent_czk": 31000}, headers=headers),
        c.delete("/manual_estimates/1", headers=headers),
    ]


def test_writes_refuse_static_token(gated_client, store) -> None:
    for r in _writes(gated_client, {"Authorization": "Bearer secret-xyz"}):
        assert r.status_code == 401, r.text
    assert set(store["rows"]) == {1}


def test_writes_refuse_no_credential(gated_client, store) -> None:
    for r in _writes(gated_client, {}):
        assert r.status_code == 401, r.text


def test_writes_refuse_non_admin_jwt(gated_client, store) -> None:
    for r in _writes(gated_client, {"Authorization": f"Bearer {_jwt(False)}"}):
        assert r.status_code == 403, r.text
    assert store["rows"][1]["rent_czk"] == 30000


def test_writes_accept_admin_jwt(gated_client, store) -> None:
    rs = _writes(gated_client, {"Authorization": f"Bearer {_jwt(True)}"})
    assert [r.status_code for r in rs] == [200, 200, 200], [r.text for r in rs]
    assert 1 not in store["rows"] and 2 in store["rows"]


def test_read_stays_on_static_token(gated_client, store) -> None:
    api_main.app.dependency_overrides.pop(deps.require_token, None)
    r = gated_client.get(
        "/listings/12345/manual_estimates",
        headers={"Authorization": "Bearer secret-xyz"},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["data"]) == 1


# --- identity stamping --------------------------------------------------------


class _Cur:
    def __init__(self, log: list) -> None:
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None

    def execute(self, sql, params=None) -> None:
        self.log.append((sql, params))

    def fetchone(self):
        return (1, 12345, 7, 30000, "petr", "broker", None,
                "2026-05-13T12:00:00+00:00", "2026-05-13T12:00:00+00:00")


class _Conn:
    def __init__(self) -> None:
        self.log: list = []

    def cursor(self):
        return _Cur(self.log)

    def transaction(self):
        return _Cur(self.log)


def test_create_stamps_admin_identity_and_account(monkeypatch) -> None:
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda conn, claims: "acct-1")
    conn = _Conn()
    me.create_manual_estimate(
        conn, 12345, s.CreateManualEstimateIn(**_CREATE), _ADMIN_CLAIMS,
    )
    sql, params = conn.log[-1]
    assert "updated_by, account_id" in sql
    assert params[-2:] == ("op@example.com", "acct-1")


def test_update_stamps_admin_identity_not_body(monkeypatch) -> None:
    conn = _Conn()
    body = s.UpdateManualEstimateIn.model_validate(
        {"rent_czk": 31000, "updated_by": "someone-else"},
    )
    me.update_manual_estimate(conn, 1, body, {"sub": "op-uuid"})
    _sql, params = conn.log[-1]
    assert "someone-else" not in params
    assert params[-2:] == ["op-uuid", 1]
