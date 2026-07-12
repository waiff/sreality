"""Tests for /admin/plans + /admin/entitlements (billing tiers, migration 298).

require_admin is overridden with synthetic admin claims (test_admin_routes
fixture pattern); DB work runs against scripted fake connections
(test_pipeline conventions) so the SQL contracts are asserted, not mocked away.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        q = " ".join(sql.split())
        self._conn.executed.append((q, params))
        for predicate, rows in self._conn.script:
            if predicate(q):
                self._rows = list(rows)
                self.rowcount = len(rows)
                return
        self._rows = []
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _Tx:
    def __enter__(self) -> "_Tx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, script: list[tuple[Any, list[tuple[Any, ...]]]]) -> None:
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Tx:
        return _Tx()

    def cursor(self) -> _Cur:
        return _Cur(self)


_FREE_ROW = ("free", "Free", 0, {"browse": True}, True, "2026-07-01T00:00:00+00:00")
_PRO_ROW = ("pro", "Pro", 1, {"browse": True}, False, None)


@pytest.fixture()
def make_client():
    def _install(conn: _FakeConn) -> Any:
        api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
        api_main.app.dependency_overrides[deps.require_admin] = (
            lambda: {"is_admin": True, "legacy": True}
        )
        return TestClient(api_main.app)

    yield _install
    api_main.app.dependency_overrides.clear()


# --- plans CRUD --------------------------------------------------------------

def test_list_plans(make_client):
    conn = _FakeConn([
        (lambda q: "FROM plans ORDER BY position" in q, [_FREE_ROW, _PRO_ROW]),
    ])
    res = make_client(conn).get("/admin/plans")
    assert res.status_code == 200
    data = res.json()["data"]
    assert [p["key"] for p in data] == ["free", "pro"]
    assert data[0]["is_default"] is True
    assert data[0]["agendas"] == {"browse": True}


def test_create_plan(make_client):
    conn = _FakeConn([
        (lambda q: "INSERT INTO plans" in q, [("pro",)]),
        (lambda q: "FROM plans WHERE key" in q, [_PRO_ROW]),
    ])
    res = make_client(conn).post(
        "/admin/plans", json={"key": "pro", "name": "Pro", "position": 1}
    )
    assert res.status_code == 200, res.text
    assert res.json()["key"] == "pro"
    ins = next((q, p) for q, p in conn.executed if "INSERT INTO plans" in q)
    assert "ON CONFLICT (key) DO NOTHING RETURNING key" in ins[0]


def test_create_plan_duplicate_409(make_client):
    conn = _FakeConn([(lambda q: "INSERT INTO plans" in q, [])])
    res = make_client(conn).post("/admin/plans", json={"key": "free", "name": "Free"})
    assert res.status_code == 409


def test_create_plan_bad_key_422(make_client):
    res = make_client(_FakeConn([])).post(
        "/admin/plans", json={"key": "Pro Plan!", "name": "Pro"}
    )
    assert res.status_code == 422


def test_patch_plan_crowning_clears_old_default_first(make_client):
    conn = _FakeConn([
        (lambda q: "SELECT is_default FROM plans WHERE key" in q, [(False,)]),
        (lambda q: "FROM plans WHERE key = %s" in q and q.startswith("SELECT key"),
         [("pro", "Pro", 1, {"browse": True}, True, None)]),
    ])
    res = make_client(conn).patch("/admin/plans/pro", json={"is_default": True})
    assert res.status_code == 200, res.text
    assert res.json()["is_default"] is True
    sqls = [q for q, _ in conn.executed]
    clear_idx = next(
        i for i, q in enumerate(sqls)
        if "SET is_default = false" in q and "WHERE is_default AND key <> %s" in q
    )
    row_idx = next(
        i for i, q in enumerate(sqls)
        if q.startswith("UPDATE plans SET is_default = %s")
    )
    assert clear_idx < row_idx, "the old default must be cleared BEFORE crowning"


def test_patch_plan_uncrowning_default_422(make_client):
    conn = _FakeConn([
        (lambda q: "SELECT is_default FROM plans WHERE key" in q, [(True,)]),
    ])
    res = make_client(conn).patch("/admin/plans/free", json={"is_default": False})
    assert res.status_code == 422
    assert "crown another plan" in res.json()["detail"]


def test_patch_plan_unknown_404(make_client):
    conn = _FakeConn([])
    res = make_client(conn).patch("/admin/plans/nope", json={"name": "X"})
    assert res.status_code == 404


def test_delete_default_plan_409(make_client):
    conn = _FakeConn([
        (lambda q: "SELECT is_default FROM plans WHERE key" in q, [(True,)]),
    ])
    res = make_client(conn).delete("/admin/plans/free")
    assert res.status_code == 409


def test_delete_referenced_plan_409(make_client):
    conn = _FakeConn([
        (lambda q: "SELECT is_default FROM plans WHERE key" in q, [(False,)]),
        (lambda q: "SELECT 1 FROM entitlements WHERE plan" in q, [(1,)]),
    ])
    res = make_client(conn).delete("/admin/plans/pro")
    assert res.status_code == 409


def test_delete_plan_happy(make_client):
    conn = _FakeConn([
        (lambda q: "SELECT is_default FROM plans WHERE key" in q, [(False,)]),
        (lambda q: "SELECT 1 FROM entitlements WHERE plan" in q, []),
    ])
    res = make_client(conn).delete("/admin/plans/pro")
    assert res.status_code == 200
    assert res.json() == {"deleted": True, "key": "pro"}
    assert any("DELETE FROM plans WHERE key" in q for q, _ in conn.executed)


# --- entitlements --------------------------------------------------------------

def test_list_entitlements(make_client):
    acct = uuid.uuid4()
    conn = _FakeConn([
        (
            lambda q: "FROM accounts a" in q,
            [(acct, "op@example.com", "free", "active", None, False)],
        ),
    ])
    res = make_client(conn).get("/admin/entitlements")
    assert res.status_code == 200
    row = res.json()["data"][0]
    assert row == {
        "account_id": str(acct),
        "email": "op@example.com",
        "plan": "free",
        "status": "active",
        "current_period_end": None,
        "is_explicit": False,
    }


def test_put_entitlement_unknown_plan_422(make_client):
    conn = _FakeConn([(lambda q: "SELECT 1 FROM plans WHERE key" in q, [])])
    res = make_client(conn).put(
        f"/admin/entitlements/{uuid.uuid4()}", json={"plan": "nope"}
    )
    assert res.status_code == 422


def test_put_entitlement_unknown_account_404(make_client):
    conn = _FakeConn([
        (lambda q: "SELECT 1 FROM plans WHERE key" in q, [(1,)]),
        (lambda q: "SELECT 1 FROM accounts WHERE id" in q, []),
    ])
    res = make_client(conn).put(
        f"/admin/entitlements/{uuid.uuid4()}", json={"plan": "pro"}
    )
    assert res.status_code == 404


def test_put_entitlement_upserts(make_client):
    acct = uuid.uuid4()
    conn = _FakeConn([
        (lambda q: "SELECT 1 FROM plans WHERE key" in q, [(1,)]),
        (lambda q: "SELECT 1 FROM accounts WHERE id" in q, [(1,)]),
    ])
    res = make_client(conn).put(
        f"/admin/entitlements/{acct}", json={"plan": "pro", "status": "trialing"}
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "account_id": str(acct),
        "plan": "pro",
        "status": "trialing",
        "is_explicit": True,
    }
    upsert = next(
        (q, p) for q, p in conn.executed if "INSERT INTO entitlements" in q
    )
    assert "ON CONFLICT (account_id) DO UPDATE" in upsert[0]
    assert upsert[1] == (acct, "pro", "trialing")


def test_put_entitlement_bad_status_422(make_client):
    res = make_client(_FakeConn([])).put(
        f"/admin/entitlements/{uuid.uuid4()}",
        json={"plan": "pro", "status": "on_fire"},
    )
    assert res.status_code == 422
