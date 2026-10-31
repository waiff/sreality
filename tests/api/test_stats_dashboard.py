"""Tests for `GET`/`PUT /stats/dashboard` — the account's working board (migrations 626, 647).

Hermetic: the tenant connection is a scripted fake that records every statement, the JWT is
overridden, and the account resolver returns a distinctive sentinel (never None — tenancy.md's
#917 rule), so the tests can assert the sentinel ARRIVES at the upsert. Validation
runs unmodified through the route: the board passes `toolkit.stats_registry.validate_board` (the
`Charts` type), and a refusal is a 422 that names the chart and the registry's reason without
touching the table. Since 647 the working board is the account's ONE unnamed row, so the upsert
infers the partial unique index and the read, RLS alone (tenancy shape 1), finds that row.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps  # noqa: E402
from api import main as api_main  # noqa: E402
from api import tenant_pool  # noqa: E402
from api.routes import stats_dashboards as sd  # noqa: E402
from tests.api._routes import method_routes, reachable_calls  # noqa: E402
from tests.api._script_conn import ScriptConn  # noqa: E402
from toolkit import stats_registry as sr  # noqa: E402

_SUB = "11111111-1111-1111-1111-111111111111"
_ACCT = "44444444-4444-4444-4444-444444444444"
_BOARD = uuid.UUID("55555555-5555-5555-5555-555555555555")
_AT = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)

_CHART = {"id": "c1", "view": "box", "rows": ["disposition", "condition"], "cols": [],
          "measure": "ppm2"}


def _upsert(q: str) -> bool:
    return q.startswith("INSERT INTO stats_dashboards (account_id, charts, updated_at)")


def _read(q: str) -> bool:
    return q.startswith("SELECT id, charts, updated_at FROM stats_dashboards")


@pytest.fixture()
def conn() -> ScriptConn:
    return ScriptConn([(_upsert, [(_AT,)])])


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, conn: ScriptConn) -> Iterator[Any]:
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: {"sub": _SUB}
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda c, claims: _ACCT)
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_the_board_is_upserted_whole_for_the_callers_account(client, conn: ScriptConn) -> None:
    charts = [
        {**_CHART, "title": "derived, never stored"},
        {"id": "c2", "view": "line", "rows": ["area_band_10"], "cols": ["condition"],
         "measure": "price", "stats": ["p75", "p10"]},
    ]
    res = client.put("/stats/dashboard", json={"charts": charts})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {
        "charts": [
            {"id": "c1", "view": "box", "stats": ["median"], "rows": ["disposition", "condition"],
             "cols": [], "measure": "ppm2"},
            {"id": "c2", "view": "line", "stats": ["p75", "p10"], "rows": ["area_band_10"],
             "cols": ["condition"], "measure": "price"},
        ],
        "updated_at": _AT.isoformat(),
    }
    [(sql, params)] = conn.executed
    assert sql.startswith("INSERT INTO stats_dashboards (account_id, charts, updated_at)")
    # The working board is the account's one UNNAMED row: the upsert infers 647's partial index.
    assert ("ON CONFLICT (account_id) WHERE name IS NULL DO UPDATE "
            "SET charts = EXCLUDED.charts, updated_at = EXCLUDED.updated_at") in sql
    assert sql.endswith("RETURNING updated_at")
    assert str(params[0]) == _ACCT
    assert json.loads(params[1]) == body["charts"]


def test_an_empty_board_is_a_valid_board(client, conn: ScriptConn) -> None:
    res = client.put("/stats/dashboard", json={"charts": []})
    assert res.status_code == 200
    assert res.json()["charts"] == []
    [(_sql, params)] = conn.executed
    assert json.loads(params[1]) == []


@pytest.mark.parametrize(
    "charts, detail",
    [
        ([{**_CHART, "cols": ["kraj"]}], "chart 0: a box chart takes row dimensions only"),
        ([_CHART, {**_CHART, "id": "c2", "view": "pie"}], "chart 1: unknown view 'pie'"),
        ([{**_CHART, "rows": ["disposition"] * 2}], "chart 0: a dimension may appear once"),
        ([{**_CHART, "rows": ["kraj", "okres", "obec", "cast_obce", "ulice", "condition", "disposition"]}],
         "chart 0: at most 6 row dimensions"),
        ([{**_CHART, "measure": "yield"}], "chart 0: unknown measure 'yield'"),
        ([{**_CHART, "stats": ["n"]}], "chart 0: unknown stat 'n'"),
        ([{**_CHART, "stats": []}], "chart 0: stats must be a non-empty list"),
        ([_CHART, dict(_CHART)], "chart 1: duplicate id 'c1'"),
        ([{**_CHART, "id": ""}], "chart 0: chart id must be a short string"),
        ([_CHART, {**_CHART, "id": "c2", "view": "line", "rows": ["kraj", "obec"]}],
         "chart 1: a line chart takes exactly 1 row dimension"),
        ([{**_CHART, "view": "bar", "rows": []}], "chart 0: a bar chart takes exactly 1 row dimension"),
        ([{**_CHART, "rows": [["kraj"]]}], "chart 0: dimensions must be strings"),
        ([{**_CHART, "rows": [{"a": 1}]}], "chart 0: dimensions must be strings"),
        ([{**_CHART, "measure": ["ppm2"]}], "chart 0: unknown measure ['ppm2']"),
        ([{**_CHART, "id": f"c{i}"} for i in range(sr.MAX_CHARTS + 1)],
         f"at most {sr.MAX_CHARTS} charts on a board"),
    ],
)
def test_a_chart_the_registry_refuses_is_a_422_and_writes_nothing(
    client, conn: ScriptConn, charts: list, detail: str,
) -> None:
    res = client.put("/stats/dashboard", json={"charts": charts})
    assert res.status_code == 422
    assert res.json()["detail"] == detail
    assert conn.executed == []


def test_the_body_shape_is_pydantics(client, conn: ScriptConn) -> None:
    assert client.put("/stats/dashboard", json={}).status_code == 422
    assert client.put("/stats/dashboard", json={"charts": ["c1"]}).status_code == 422
    assert conn.executed == []


def test_a_full_board_is_accepted(client, conn: ScriptConn) -> None:
    full = [{**_CHART, "id": f"c{i}"} for i in range(sr.MAX_CHARTS)]
    res = client.put("/stats/dashboard", json={"charts": full})
    assert res.status_code == 200, res.text
    assert len(res.json()["charts"]) == sr.MAX_CHARTS


def test_a_caller_without_an_account_writes_nothing_and_reads_no_board(client, conn, monkeypatch) -> None:
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda c, claims: None)
    res = client.put("/stats/dashboard", json={"charts": [_CHART]})
    assert res.status_code == 400
    assert res.json()["detail"] == "no account for caller"
    assert conn.executed == []
    # The read resolves no account: RLS finds no row of an account-less caller.
    conn.script = []
    res = client.get("/stats/dashboard")
    assert res.status_code == 200 and res.json() is None
    assert [q for q, _ in conn.executed] == [sd._WORKING_BOARD_SQL]


def test_the_read_is_scoped_by_rls_alone(client, conn) -> None:
    conn.script = [(_read, [(_BOARD, [_CHART], _AT)])]
    res = client.get("/stats/dashboard")
    assert res.status_code == 200, res.text
    assert res.json() == {"id": str(_BOARD), "charts": [_CHART], "updated_at": _AT.isoformat()}
    [(sql, params)] = conn.executed
    # Tenancy shape 1: no account argument, no account predicate, and never a named dashboard.
    assert sql == ("SELECT id, charts, updated_at FROM stats_dashboards WHERE name IS NULL "
                   "ORDER BY updated_at DESC LIMIT 1")
    assert params is None


def test_no_working_board_yet_reads_null(client, conn) -> None:
    conn.script = []
    res = client.get("/stats/dashboard")
    assert res.status_code == 200
    assert res.json() is None


def test_the_working_board_write_never_acts_for_another_account(client, conn) -> None:
    acting = {"X-Act-As": "22222222-2222-2222-2222-222222222222"}
    res = client.put("/stats/dashboard", json={"charts": [_CHART]}, headers=acting)
    assert res.status_code == 403
    assert res.json()["detail"] == "this route does not act for another account"
    assert conn.executed == []


def test_the_working_board_read_under_x_act_as_is_the_callers_own(client, conn) -> None:
    """stats_dashboards is identity-scoped (pipeline-sharing §3.7): its policy reads current_account_ids(), so
    under the header the read answers the delegate's OWN board, never the owner's, and is no 403."""
    conn.script = [(_read, [(_BOARD, [_CHART], _AT)])]
    res = client.get("/stats/dashboard", headers={"X-Act-As": "22222222-2222-2222-2222-222222222222"})
    assert res.status_code == 200, res.text
    assert res.json() == {"id": str(_BOARD), "charts": [_CHART], "updated_at": _AT.isoformat()}
    [(sql, params)] = conn.executed
    assert sql == sd._WORKING_BOARD_SQL and params is None
    assert "acting_account_ids" not in sql


def test_both_routes_ride_the_tenant_connection_and_only_the_write_names_its_account() -> None:
    routes = {(m, p): r for m, p, r in method_routes() if p == "/stats/dashboard"}
    assert set(routes) == {("GET", "/stats/dashboard"), ("PUT", "/stats/dashboard")}
    for (method, _path), route in routes.items():
        calls = reachable_calls(route)
        assert tenant_pool.tenant_conn in calls
        assert (tenant_pool.require_account_id in calls) == (method == "PUT")
        assert deps.get_db_conn not in calls and deps.require_token not in calls


def test_the_write_helper_requires_its_account_and_the_read_takes_none() -> None:
    """No default on the write: a dropped account is a TypeError, not a NULL owner (#917)."""
    import inspect

    with pytest.raises(TypeError):
        sd.save_working_board(ScriptConn([]), [])  # type: ignore[call-arg]
    assert list(inspect.signature(sd.working_board).parameters) == ["conn"]
