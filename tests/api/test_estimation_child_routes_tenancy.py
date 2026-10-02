"""Trace payload + feedback routes are account-scoped, not bundle-token-scoped.

The 2026-10-01 review found `GET /estimations/{id}/trace/{n}/payload` and
`GET`/`POST /estimations/{id}/feedback` on the service-role connection behind
`require_token` alone. That token ships in the SPA bundle, so any holder could read
any account's trace payloads and spend refiner LLM credit on any run.

The fakes below model the two connections. `_TenantConn` stands in for the RLS-scoped
tenant transaction: it sees the caller's own run plus the shared SYSTEM run (migration
291's read arms), and OWNS only the first. `_ServiceConn` sees EVERY run, so a route
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
_SYSTEM_RUN = 9
_PLATFORM_PROMPT = "PLATFORM SKILL PROMPT"


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
    def __init__(self, visible_runs: set[int], owned_runs: set[int]) -> None:
        self.visible_runs = visible_runs
        self.owned_runs = owned_runs
        self.executed: list[str] = []
        self.opened = 0
        self.closed = False
        self.rolled_back = False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> contextlib.nullcontext:
        return contextlib.nullcontext()

    def answer(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        run_id = params[0] if params else None
        seen = run_id in self.visible_runs
        if "FROM estimation_runs" in sql and "current_account_ids()" in sql:
            return [(1,)] if run_id in self.owned_runs else []
        if "FROM estimation_runs WHERE id" in sql:
            return [(1,)] if seen else []
        if "FROM estimation_trace_payloads" in sql:
            return [(params[1], {"rows": 3}, None)] if seen else []
        if sql.startswith("SELECT") and "FROM estimation_feedback" in sql:
            return [(41, run_id, "earlier note", None, "submitted", None)] if seen else []
        if sql.startswith("INSERT INTO estimation_feedback"):
            return [(42, params[0], params[1], None, params[2], None)]
        return []

    def inserts(self) -> list[str]:
        return [q for q in self.executed if q.startswith("INSERT")]


class _TenantConn(_FakeConn):
    """The RLS-scoped transaction: reads the own + SYSTEM runs, owns only its own."""

    def __init__(self) -> None:
        super().__init__({_OWN_RUN, _SYSTEM_RUN}, {_OWN_RUN})


class _ServiceConn(_FakeConn):
    """Service role: RLS-exempt, sees every run (and has no caller to own one)."""

    def __init__(self) -> None:
        super().__init__({_OWN_RUN, _FOREIGN_RUN, _SYSTEM_RUN}, set())


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

    @contextlib.contextmanager
    def _fake_tenant_transaction(claims: dict):
        tenant.opened += 1
        try:
            yield tenant
        except BaseException:
            tenant.rolled_back = True
            raise
        finally:
            tenant.closed = True

    @contextlib.contextmanager
    def _fake_service_conn():
        service.opened += 1
        yield service

    llm_bound_to: list[Any] = []

    def _fake_llm_client(conn):
        llm_bound_to.append(conn)
        return llm

    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = _stub_tenant_conn
    monkeypatch.setattr(tenant_pool, "tenant_transaction", _fake_tenant_transaction)
    monkeypatch.setattr(deps, "open_background_conn", _fake_service_conn)
    monkeypatch.setattr(deps, "get_llm_client", _fake_llm_client)
    monkeypatch.setattr(
        api_main, "get_estimation_run",
        lambda conn, run_id: {"id": run_id, "via_tenant": conn is tenant},
    )
    refiner_calls: list[dict[str, Any]] = []

    def fake_run_refinement(conn, llm_client, *, feedback, run):
        refiner_calls.append({
            "conn": conn, "llm": llm_client, "run": run,
            "tenant_closed": tenant.closed,
        })
        refinement = {
            "id": 5, "status": "proposed",
            "original_prompt": _PLATFORM_PROMPT, "proposed_prompt": _PLATFORM_PROMPT,
        }
        return refinement, "proposed"

    monkeypatch.setattr(api_refiner, "run_refinement", fake_run_refinement)
    yield {
        "client": TestClient(api_main.app),
        "tenant": tenant, "service": service, "llm": llm,
        "refiner": refiner_calls, "llm_bound_to": llm_bound_to,
    }
    api_main.app.dependency_overrides.clear()


def _jwt(*, admin: bool = False) -> dict[str, str]:
    claims: dict[str, Any] = {
        "aud": "authenticated", "sub": "11111111-1111-1111-1111-111111111111",
    }
    if admin:
        claims["app_metadata"] = {"is_admin": True}
    tok = jwt.encode(claims, _JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def _call(client, method: str, path: str, headers: dict[str, str]):
    if method == "POST":
        return client.post(path, json={"feedback_text": "too broad"}, headers=headers)
    return client.get(path, headers=headers)


def _post(conns, run_id: int, *, admin: bool = False, **body: Any):
    return conns["client"].post(
        f"/estimations/{run_id}/feedback",
        json={"feedback_text": "too broad", **body},
        headers=_jwt(admin=admin),
    )


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
    assert conns["tenant"].executed == [] and conns["tenant"].opened == 0
    assert conns["service"].opened == 0
    assert conns["refiner"] == []


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_another_accounts_run_is_404_with_no_write_and_no_spend(
    conns, method: str, path: str, admin: bool,
) -> None:
    """The same 404 GET /estimations/{id} answers for a run RLS hides — for an admin
    too (RLS's admin arm covers only NULL-account runs). The POST defaults to
    kick_off_refinement=true, so this also proves no refiner spend."""
    res = _call(conns["client"], method, path.format(run=_FOREIGN_RUN), _jwt(admin=admin))
    assert res.status_code == 404
    assert conns["tenant"].inserts() == []
    assert conns["service"].opened == 0, "the service role must not open for a foreign run"
    assert conns["refiner"] == []


def test_own_trace_payload_reads_through_the_tenant_conn(conns) -> None:
    res = conns["client"].get(f"/estimations/{_OWN_RUN}/trace/2/payload", headers=_jwt())
    assert res.status_code == 200
    assert res.json() == {"step_n": 2, "full_output": {"rows": 3}, "captured_at": None}
    assert any("estimation_trace_payloads" in q for q in conns["tenant"].executed)
    assert conns["service"].opened == 0


def test_own_feedback_list_reads_through_the_tenant_conn(conns) -> None:
    res = conns["client"].get(f"/estimations/{_OWN_RUN}/feedback", headers=_jwt())
    assert res.status_code == 200
    assert [r["id"] for r in res.json()["data"]] == [41]
    assert conns["service"].opened == 0


def test_own_feedback_post_is_one_committed_tenant_write(conns) -> None:
    """Gate + insert share one short tenant transaction (migration 292's trigger and
    WITH CHECK back the gate); the service role never opens."""
    res = _post(conns, _OWN_RUN, kick_off_refinement=False)
    assert res.status_code == 200
    assert res.json()["feedback"]["status"] == "submitted"
    assert res.json()["refinement"] is None
    tenant = conns["tenant"]
    assert any("current_account_ids()" in q for q in tenant.executed), "non-admin gate is OWN"
    assert [q.split("(")[0].strip() for q in tenant.inserts()] == [
        "INSERT INTO estimation_feedback",
    ]
    assert tenant.opened == 1 and tenant.closed and not tenant.rolled_back
    assert conns["service"].opened == 0
    assert conns["refiner"] == []


def test_non_admin_default_body_stores_the_note_without_refining(conns) -> None:
    """kick_off_refinement defaults to true, and signup is open: a non-admin must not
    reach the unmetered refiner, nor read back the platform skill prompt it returns
    (that row is otherwise only served behind require_admin)."""
    res = _post(conns, _OWN_RUN)
    assert res.status_code == 200
    assert res.json()["feedback"]["status"] == "submitted"
    assert res.json()["refinement"] is None
    assert _PLATFORM_PROMPT not in res.text
    assert conns["refiner"] == [] and conns["llm_bound_to"] == []
    assert conns["service"].opened == 0


def test_non_admin_cannot_write_on_a_shared_system_run(conns) -> None:
    """A note on a SYSTEM run is stamped SYSTEM (migration 292) and so readable by
    every account: the read arm must not double as the write gate for a non-admin."""
    res = _post(conns, _SYSTEM_RUN)
    assert res.status_code == 404
    assert conns["tenant"].inserts() == [] and conns["tenant"].rolled_back
    assert conns["service"].opened == 0
    assert conns["refiner"] == []


def test_admin_may_write_and_refine_on_a_system_run(conns) -> None:
    res = _post(conns, _SYSTEM_RUN, admin=True)
    assert res.status_code == 200
    assert res.json()["feedback"]["status"] == "proposed"
    assert res.json()["refinement"]["id"] == 5
    assert not any("current_account_ids()" in q for q in conns["tenant"].executed)


def test_admin_refiner_runs_on_the_service_conn_after_the_tenant_commit(conns) -> None:
    """No tenant transaction may sit open across the refiner's LLM call (the W1-1
    boundary POST /estimations keeps), and the refiner's platform writes (skills,
    app_settings, skill_refinements) plus its llm_calls metering ride the service
    role, against the run the tenant transaction resolved."""
    res = _post(conns, _OWN_RUN, admin=True)
    assert res.status_code == 200
    body = res.json()
    assert body["feedback"]["status"] == "proposed"
    assert body["feedback"]["refinement_id"] == 5
    [call] = conns["refiner"]
    assert call["tenant_closed"], "the tenant transaction was still open during the LLM call"
    assert call["conn"] is conns["service"] and call["llm"] is conns["llm"]
    assert conns["llm_bound_to"] == [conns["service"]]
    assert call["run"] == {"id": _OWN_RUN, "via_tenant": True}
    assert conns["tenant"].inserts() and not conns["service"].inserts()
    assert any(q.startswith("UPDATE estimation_feedback") for q in conns["service"].executed)
    assert conns["service"].opened == 1
