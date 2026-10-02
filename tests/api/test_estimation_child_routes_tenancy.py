"""Trace payload + feedback routes are account-scoped, not bundle-token-scoped.

The 2026-10-01 review found `GET /estimations/{id}/trace/{n}/payload` and
`GET`/`POST /estimations/{id}/feedback` on the service-role connection behind
`require_token` alone. That token ships in the SPA bundle, so any holder could read
any account's trace payloads and spend refiner LLM credit on any run.

The fakes below model the two connections the routes now hold. `_TenantConn` stands
in for the RLS-scoped tenant transaction: it sees ONLY the caller's own run, the way
migrations 291/292 make Postgres answer. `_ServiceConn` sees EVERY run, so a route
that slid back to gating on the service-role connection would let the foreign run
through and red these tests. RLS itself is proven live in
tests/test_tenant_isolation_live.py (estimation_trace_payloads, estimation_feedback).
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient
jwt = pytest.importorskip("jwt")

from api import dependencies as deps  # noqa: E402
from api import main as api_main  # noqa: E402
from api import refiner as api_refiner  # noqa: E402
from api import tenant_pool  # noqa: E402

_JWT_SECRET = "test-hs256-secret"
_STATIC = "static-bundle-token"
_OWN_RUN = 7
_FOREIGN_RUN = 8


class _Cursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._conn.executed.append(sql)
        self._rows = self._conn.answer(sql, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, visible_runs: set[int]) -> None:
        self.visible_runs = visible_runs
        self.executed: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> contextlib.nullcontext:
        return contextlib.nullcontext()

    def answer(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        run_id = params[0] if params else None
        seen = run_id in self.visible_runs
        if "FROM estimation_runs WHERE id" in sql:
            return [(1,)] if seen else []
        if "FROM estimation_trace_payloads" in sql:
            return [(params[1], {"rows": 3}, None)] if seen else []
        if sql.startswith("SELECT") and "FROM estimation_feedback" in sql:
            return [(41, run_id, "earlier note", None, "submitted", None)] if seen else []
        if sql.startswith("INSERT INTO estimation_feedback"):
            return [(42, params[0], params[1], None, params[2], None)]
        return []


class _TenantConn(_FakeConn):
    """The RLS-scoped transaction: the caller's own run only."""

    def __init__(self) -> None:
        super().__init__({_OWN_RUN})


class _ServiceConn(_FakeConn):
    """Service role: RLS-exempt, sees every run."""

    def __init__(self) -> None:
        super().__init__({_OWN_RUN, _FOREIGN_RUN})


@pytest.fixture()
def conns(monkeypatch):
    monkeypatch.setenv("API_TOKEN", _STATIC)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)
    tenant, service, llm = _TenantConn(), _ServiceConn(), object()

    # The stub keeps verify_jwt as its own dependency, exactly like production's
    # tenant_conn, so the static-token refusal below is the real gate.
    def _stub_tenant_conn(claims: dict = fastapi.Depends(deps.verify_jwt)) -> _TenantConn:
        return tenant

    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = _stub_tenant_conn
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: service
    api_main.app.dependency_overrides[deps.get_llm_client] = lambda: llm
    refiner_calls: list[dict[str, Any]] = []

    def fake_run_refinement(conn, llm_client, *, feedback, run):
        refiner_calls.append({"conn": conn, "llm": llm_client, "run": run})
        return {"id": 5, "status": "proposed"}, "proposed"

    monkeypatch.setattr(api_refiner, "run_refinement", fake_run_refinement)
    yield {
        "client": TestClient(api_main.app),
        "tenant": tenant, "service": service, "llm": llm, "refiner": refiner_calls,
    }
    api_main.app.dependency_overrides.clear()


def _user_jwt() -> dict[str, str]:
    tok = jwt.encode(
        {"aud": "authenticated", "sub": "11111111-1111-1111-1111-111111111111"},
        _JWT_SECRET, algorithm="HS256",
    )
    return {"Authorization": f"Bearer {tok}"}


def _call(client, method: str, path: str, headers: dict[str, str]):
    if method == "POST":
        return client.post(path, json={"feedback_text": "too broad"}, headers=headers)
    return client.get(path, headers=headers)


_ROUTES = (
    ("GET", "/estimations/{run}/trace/2/payload"),
    ("GET", "/estimations/{run}/feedback"),
    ("POST", "/estimations/{run}/feedback"),
)


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_static_bundle_token_alone_is_refused(conns, method: str, path: str) -> None:
    res = _call(
        conns["client"], method, path.format(run=_OWN_RUN),
        {"Authorization": f"Bearer {_STATIC}"},
    )
    assert res.status_code == 401
    assert conns["tenant"].executed == [] and conns["service"].executed == []
    assert conns["refiner"] == []


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_another_accounts_run_is_404_with_no_write_and_no_spend(
    conns, method: str, path: str,
) -> None:
    """The same 404 GET /estimations/{id} answers for a run RLS hides. The POST
    defaults to kick_off_refinement=true, so this also proves no refiner spend."""
    res = _call(conns["client"], method, path.format(run=_FOREIGN_RUN), _user_jwt())
    assert res.status_code == 404
    assert conns["service"].executed == [], "nothing may touch the service role for a foreign run"
    assert conns["refiner"] == []


def test_own_trace_payload_reads_through_the_tenant_conn(conns) -> None:
    res = conns["client"].get(f"/estimations/{_OWN_RUN}/trace/2/payload", headers=_user_jwt())
    assert res.status_code == 200
    assert res.json() == {"step_n": 2, "full_output": {"rows": 3}, "captured_at": None}
    assert any("estimation_trace_payloads" in q for q in conns["tenant"].executed)
    assert conns["service"].executed == []


def test_own_feedback_list_reads_through_the_tenant_conn(conns) -> None:
    res = conns["client"].get(f"/estimations/{_OWN_RUN}/feedback", headers=_user_jwt())
    assert res.status_code == 200
    assert [r["id"] for r in res.json()["data"]] == [41]
    assert conns["service"].executed == []


def test_own_feedback_post_gates_on_tenant_and_writes_on_service(conns) -> None:
    res = conns["client"].post(
        f"/estimations/{_OWN_RUN}/feedback",
        json={"feedback_text": "too broad", "kick_off_refinement": False},
        headers=_user_jwt(),
    )
    assert res.status_code == 200
    assert res.json()["feedback"]["status"] == "submitted"
    assert res.json()["refinement"] is None
    assert any("FROM estimation_runs WHERE id" in q for q in conns["tenant"].executed)
    assert any(q.startswith("INSERT INTO estimation_feedback") for q in conns["service"].executed)
    assert conns["refiner"] == []


def test_own_feedback_post_refines_on_the_service_conn(conns, monkeypatch) -> None:
    """The refiner's platform writes (skills, app_settings, skill_refinements) and its
    llm_calls metering ride the service-role connection, against the run the tenant
    connection resolved."""
    monkeypatch.setattr(
        api_main, "get_estimation_run",
        lambda conn, run_id: {"id": run_id, "via_tenant": conn is conns["tenant"]},
    )
    res = conns["client"].post(
        f"/estimations/{_OWN_RUN}/feedback",
        json={"feedback_text": "too broad"},
        headers=_user_jwt(),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["feedback"]["status"] == "proposed"
    assert body["feedback"]["refinement_id"] == 5
    [call] = conns["refiner"]
    assert call["conn"] is conns["service"] and call["llm"] is conns["llm"]
    assert call["run"] == {"id": _OWN_RUN, "via_tenant": True}
    assert any(q.startswith("UPDATE estimation_feedback") for q in conns["service"].executed)
