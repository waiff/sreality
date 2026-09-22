"""Tests for the dismissal endpoints (migration 536).

Hermetic. Route tests stub the persistence helpers (their arity is part of the
assertion: undo is RLS-only and takes no account); the dismiss/undismiss logic
runs against a scripted fake connection.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import dismissals as dismissals_module
from api import main as api_main
from api import schemas as s
from api import tenant_pool

_SUB = "11111111-1111-1111-1111-111111111111"
_ACCT = "22222222-2222-2222-2222-222222222222"


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: {"sub": _SUB}
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda conn, claims: _ACCT)
    seen: dict[str, Any] = {}

    def _dismiss(conn, body, *, account_id):
        seen["account_id"] = account_id
        return {"property_id": body.property_id, "added": True}

    monkeypatch.setattr(dismissals_module, "dismiss", _dismiss)
    monkeypatch.setattr(
        dismissals_module, "undismiss",
        lambda conn, pid: {"property_id": pid, "removed": True},
    )
    yield TestClient(api_main.app), seen
    api_main.app.dependency_overrides.clear()


def test_dismiss_route_names_the_resolved_account(client):
    http, seen = client
    res = http.post("/dismissals", json={"property_id": 42})
    assert res.status_code == 200
    assert res.json() == {"property_id": 42, "added": True}
    assert str(seen["account_id"]) == _ACCT


def test_dismiss_route_requires_property_id(client):
    http, _ = client
    assert http.post("/dismissals", json={}).status_code == 422


def test_undismiss_route_is_rls_only(client):
    http, _ = client
    res = http.delete("/dismissals/42")
    assert res.status_code == 200
    assert res.json() == {"property_id": 42, "removed": True}


def test_dismiss_without_an_account_is_400_but_undo_is_not(client, monkeypatch):
    http, _ = client
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda conn, claims: None)
    res = http.post("/dismissals", json={"property_id": 42})
    assert res.status_code == 400
    assert res.json()["detail"] == "no account for caller"
    assert http.delete("/dismissals/42").status_code == 200


# --- logic against a scripted fake connection -------------------------------

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


def _inserts(conn: _FakeConn) -> list[tuple[str, Any]]:
    return [(q, p) for q, p in conn.executed if "INSERT INTO property_dismissals" in q]


def test_dismiss_inserts_for_the_account_on_the_resolved_survivor():
    conn = _FakeConn([
        (lambda q: "RECURSIVE chain" in q, [(99, 42)]),
        (lambda q: "INSERT INTO property_dismissals" in q, [(1,)]),
    ])
    out = dismissals_module.dismiss(conn, s.DismissPropertyIn(property_id=99), account_id=_ACCT)
    assert out == {"property_id": 42, "added": True}
    [(sql, params)] = _inserts(conn)
    assert params == (_ACCT, 42)
    assert "ON CONFLICT (property_id, account_id) WHERE lifted_at IS NULL DO NOTHING" in sql


def test_dismiss_is_idempotent():
    conn = _FakeConn([(lambda q: "RECURSIVE chain" in q, [(42, 42)])])
    out = dismissals_module.dismiss(conn, s.DismissPropertyIn(property_id=42), account_id=_ACCT)
    assert out == {"property_id": 42, "added": False}


def test_dismiss_refuses_a_live_deal():
    conn = _FakeConn([
        (lambda q: "RECURSIVE chain" in q, [(42, 42)]),
        (lambda q: "FROM property_pipeline pp" in q, [(1,)]),  # a LIVE card
    ])
    with pytest.raises(fastapi.HTTPException) as ei:
        dismissals_module.dismiss(conn, s.DismissPropertyIn(property_id=42), account_id=_ACCT)
    assert ei.value.status_code == 409
    assert _inserts(conn) == []
    # the check is RLS-scoped: it binds only the property
    check = next(p for q, p in conn.executed if "FROM property_pipeline" in q)
    assert check == (42,)


def test_dismiss_accepts_a_deal_closed_into_a_terminal_stage():
    """"Passed" is exactly "reviewed it, didn't like it": a closed card must not
    block the dismissal. Only a card at a NON-terminal stage counts as live."""
    conn = _FakeConn([
        (lambda q: "RECURSIVE chain" in q, [(42, 42)]),
        # the live-deal probe finds nothing: the property's card is closed
        (lambda q: "INSERT INTO property_dismissals" in q, [(1,)]),
    ])
    out = dismissals_module.dismiss(conn, s.DismissPropertyIn(property_id=42), account_id=_ACCT)
    assert out == {"property_id": 42, "added": True}
    probe = next(q for q, _ in conn.executed if "FROM property_pipeline" in q)
    assert "JOIN pipeline_stages ps ON ps.id = pp.stage_id" in probe
    assert "NOT ps.is_terminal" in probe


def test_dismiss_unknown_property_is_422():
    conn = _FakeConn([(lambda q: "RECURSIVE chain" in q, [])])
    with pytest.raises(fastapi.HTTPException) as ei:
        dismissals_module.dismiss(conn, s.DismissPropertyIn(property_id=7), account_id=_ACCT)
    assert ei.value.status_code == 422


def test_undismiss_lifts_never_deletes_on_the_resolved_survivor():
    conn = _FakeConn([
        (lambda q: "RECURSIVE chain" in q, [(99, 42)]),
        (lambda q: "UPDATE property_dismissals" in q, [(1,)]),
    ])
    out = dismissals_module.undismiss(conn, 99)
    assert out == {"property_id": 42, "removed": True}
    [(sql, params)] = [(q, p) for q, p in conn.executed if "property_dismissals" in q]
    assert sql.startswith("UPDATE property_dismissals SET lifted_at = now()")
    assert "lift_reason = 'operator'" in sql
    assert "account_id" not in sql
    assert params == (42,)


def test_live_deal_lift_is_decided_by_the_data_not_the_caller():
    """The one statement of the rule: a dismissal yields only to a card at a
    non-terminal stage, RLS-scoped (no account predicate), batch-shaped."""
    conn = _FakeConn([(lambda q: "UPDATE property_dismissals d" in q, [(1,), (1,)])])
    with conn.cursor() as cur:
        assert dismissals_module.lift_dismissals_of_live_deals(cur, [42, 43]) == 2
    [(sql, params)] = [(q, p) for q, p in conn.executed if "property_dismissals" in q]
    assert "lift_reason = 'pipeline'" in sql
    assert "d.property_id = ANY(%s)" in sql
    assert "NOT ps.is_terminal" in sql
    assert "account_id" not in sql
    assert params == ([42, 43],)


def test_undismiss_nothing_active_reports_not_removed():
    conn = _FakeConn([(lambda q: "RECURSIVE chain" in q, [(42, 42)])])
    assert dismissals_module.undismiss(conn, 42) == {"property_id": 42, "removed": False}


def test_undismiss_without_a_survivor_touches_nothing():
    conn = _FakeConn([(lambda q: "RECURSIVE chain" in q, [])])
    assert dismissals_module.undismiss(conn, 7) == {"property_id": 7, "removed": False}
    assert not any("property_dismissals" in q for q, _ in conn.executed)
