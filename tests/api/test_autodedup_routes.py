"""Tests for /autodedup/* — what the AUTODEDUP Progress page reads (PROGRAM.md §12, W1).

The routes hold no SQL of their own; `autodedup/progress_sql.py` does. So the connection is
faked and dispatches on the statement, and the assertions are about the CONTRACT: the
`{"data": …, "store_ready": …}` envelope, every `autodedup.iterations` column reaching the
page, keyset paging that reports `has_more` from a real extra row, Decimal/datetime coming
back as JSON, the header rollup summing the per-wave statement, and above all that an
un-migrated database renders instead of 500ing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import autodedup as routes
from autodedup import progress_sql as psql


def _iteration_row(iteration_id: int, wave: str = "W1") -> tuple[Any, ...]:
    """One row in `ITERATION_COLUMNS` order — the select list's contract."""
    return (
        iteration_id,
        wave,
        "blocking probe bake-off",
        "done",
        "ran K1 and K4 over the trial region and compared recall",
        ["psql", "autodedup.census"],
        {"cohort": "praha", "blocks": 412},
        {"recall": 0.91, "gate": 0.85},
        Decimal("12.3456"),
        {"report": "https://example.invalid/a.json"},
        998877,
        "nothing surprising",
        datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 16, 9, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 16, 7, 59, tzinfo=timezone.utc),
    )


class _Cursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._conn.calls.append((sql, params))
        if "to_regclass" in sql:
            self._rows = [(self._conn.ready,)]
        elif "per_wave" in sql:
            self._rows = list(self._conn.wave_rows)
        else:
            if not self._conn.ready:
                raise AssertionError("queried the ledger before the readiness probe")
            after = (params or {}).get("after_id")
            limit = int((params or {}).get("limit") or 0)
            rows = [r for r in self._conn.iteration_rows if after is None or r[0] < after]
            self._rows = rows[:limit]

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self) -> None:
        self.ready: bool = True
        self.iteration_rows: list[tuple[Any, ...]] = []
        self.wave_rows: list[tuple[Any, ...]] = []
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


@pytest.fixture()
def conn() -> _FakeConn:
    return _FakeConn()


@pytest.fixture()
def client(conn: _FakeConn):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


# --------------------------------------------------------------- the un-migrated database


def test_iterations_renders_when_the_store_does_not_exist(client, conn):
    conn.ready = False
    resp = client.get("/autodedup/iterations")
    assert resp.status_code == 200
    assert resp.json() == {"data": None, "store_ready": False}
    # The catalog probe went first and nothing else was asked.
    assert len(conn.calls) == 1
    assert "to_regclass('autodedup.iterations')" in conn.calls[0][0]


def test_stats_renders_when_the_store_does_not_exist(client, conn):
    conn.ready = False
    resp = client.get("/autodedup/stats")
    assert resp.status_code == 200
    assert resp.json() == {"data": None, "store_ready": False}
    assert len(conn.calls) == 1


# ------------------------------------------------------------------------ the ledger page


def test_the_select_list_is_the_column_tuple(client, conn):
    """`ITERATION_COLUMNS` is what rows are zipped onto, and the SELECT list is what fills
    them: a column added to one and not the other would silently mislabel every column after
    it, and `zip` never raises. So the two are compared literally."""
    body = psql.AUTODEDUP_ITERATIONS_SQL
    head = body[body.index("SELECT ") + len("SELECT "): body.index(" FROM ")]
    assert tuple(name.strip() for name in head.split(",")) == psql.ITERATION_COLUMNS


def test_the_keyset_predicate_is_strictly_less_than(client, conn):
    """The fake cursor re-implements paging, so only the statement text proves the operator:
    `<=` here would hand back the previous page's last row on every boundary."""
    assert "id < %(after_id)s::bigint" in psql.AUTODEDUP_ITERATIONS_SQL
    assert "ORDER BY id DESC" in psql.AUTODEDUP_ITERATIONS_SQL


def test_every_iteration_column_reaches_the_page(client, conn):
    conn.iteration_rows = [_iteration_row(7)]
    body = client.get("/autodedup/iterations").json()
    assert set(body) == {"data", "store_ready"}
    assert body["store_ready"] is True
    item = body["data"]["items"][0]
    assert set(item) == set(psql.ITERATION_COLUMNS)
    assert item["id"] == 7
    assert item["wave"] == "W1"
    assert item["status"] == "done"
    assert item["tools"] == ["psql", "autodedup.census"]
    assert item["sample_stats"] == {"cohort": "praha", "blocks": 412}
    assert item["metrics"] == {"recall": 0.91, "gate": 0.85}
    assert item["run_id"] == 998877
    # numeric(10,4) and timestamptz both have to survive the wire
    assert item["cost_usd"] == pytest.approx(12.3456)
    assert item["started_at"] == "2026-09-16T08:00:00+00:00"
    assert item["finished_at"] == "2026-09-16T09:30:00+00:00"
    assert item["created_at"] == "2026-09-16T07:59:00+00:00"


def test_an_empty_ledger_is_an_empty_page_not_a_missing_store(client, conn):
    body = client.get("/autodedup/iterations").json()
    assert body["store_ready"] is True
    assert body["data"] == {"items": [], "has_more": False, "next_after_id": None}


def test_the_default_page_size_is_fifty_and_the_order_is_newest_first(client, conn):
    conn.iteration_rows = [_iteration_row(n) for n in range(200, 0, -1)]
    body = client.get("/autodedup/iterations").json()["data"]
    assert len(body["items"]) == 50
    assert [i["id"] for i in body["items"][:3]] == [200, 199, 198]
    # the extra probe row is asked for, never shown
    assert conn.calls[-1][1] == {"after_id": None, "limit": 51}


def test_keyset_paging_walks_the_ledger_without_repeating_a_row(client, conn):
    conn.iteration_rows = [_iteration_row(n) for n in range(5, 0, -1)]

    first = client.get("/autodedup/iterations", params={"limit": 2}).json()["data"]
    assert [i["id"] for i in first["items"]] == [5, 4]
    assert first["has_more"] is True
    assert first["next_after_id"] == 4

    second = client.get(
        "/autodedup/iterations", params={"limit": 2, "after": first["next_after_id"]}
    ).json()["data"]
    assert [i["id"] for i in second["items"]] == [3, 2]
    assert second["has_more"] is True

    last = client.get(
        "/autodedup/iterations", params={"limit": 2, "after": second["next_after_id"]}
    ).json()["data"]
    assert [i["id"] for i in last["items"]] == [1]
    assert last["has_more"] is False
    assert last["next_after_id"] is None


def test_a_page_that_exactly_empties_the_ledger_is_the_end(client, conn):
    """The honest `has_more`: a full page is not by itself another page."""
    conn.iteration_rows = [_iteration_row(n) for n in (2, 1)]
    body = client.get("/autodedup/iterations", params={"limit": 2}).json()["data"]
    assert len(body["items"]) == 2
    assert body["has_more"] is False
    assert body["next_after_id"] is None


@pytest.mark.parametrize("limit", [0, 201, -1])
def test_the_page_size_is_bounded(client, conn, limit):
    assert client.get("/autodedup/iterations", params={"limit": limit}).status_code == 422


# ------------------------------------------------------------------------ the header strip


def test_stats_sums_the_per_wave_statement(client, conn):
    conn.wave_rows = [
        ("W1", 3, Decimal("12.5000"), datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc), "running"),
        ("W0", 2, Decimal("7.2500"), datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc), "done"),
    ]
    body = client.get("/autodedup/stats").json()
    assert body["store_ready"] is True
    data = body["data"]
    assert set(data) == {
        "n_iterations",
        "total_cost_usd",
        "run_cap_usd",
        "program_cap_usd",
        "last_iteration_at",
        "waves",
    }
    assert data["n_iterations"] == 5
    assert data["total_cost_usd"] == pytest.approx(19.75)
    # D2's spend gate travels with the spend, so the page never retypes the caps.
    assert (data["run_cap_usd"], data["program_cap_usd"]) == (25.0, 200.0)
    assert data["last_iteration_at"] == "2026-09-16T09:00:00+00:00"
    assert data["waves"] == [
        {"wave": "W1", "n": 3, "last_status": "running", "cost_usd": 12.5},
        {"wave": "W0", "n": 2, "last_status": "done", "cost_usd": 7.25},
    ]


def test_the_headline_total_is_the_wave_column_added_up(client, conn):
    """The header must equal the wave rows added by hand — the route accumulates the
    numeric(10,4) as Decimal, though at 4-dp rounding no fixture can tell that from float
    addition; what this catches is a wave dropped from one of the two numbers."""
    conn.wave_rows = [
        ("W2", 1, Decimal("0.1000"), datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc), "done"),
        ("W1", 1, Decimal("0.2000"), datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc), "done"),
        ("W0", 1, Decimal("0.4000"), datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc), "done"),
    ]
    data = client.get("/autodedup/stats").json()["data"]
    assert data["total_cost_usd"] == 0.7


def test_stats_on_an_empty_ledger(client, conn):
    data = client.get("/autodedup/stats").json()["data"]
    assert data == {
        "n_iterations": 0,
        "total_cost_usd": 0.0,
        "run_cap_usd": 25.0,
        "program_cap_usd": 200.0,
        "last_iteration_at": None,
        "waves": [],
    }


# ------------------------------------------------------------------------- the deployment


def test_the_image_ships_the_package_the_router_imports():
    """`api/routes/autodedup.py` imports `autodedup.progress_sql` at module scope, so the
    Dockerfile must COPY that tree: setuptools' `packages.find` matches an absent directory
    silently, so a missing COPY builds green and crash-loops the WHOLE API at boot."""
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    assert "COPY autodedup/ ./autodedup/" in dockerfile.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- the gate


def test_autodedup_routes_require_admin(client, conn):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    assert client.get("/autodedup/iterations").status_code == 401
    assert client.get("/autodedup/stats").status_code == 401


def test_a_non_admin_jwt_is_refused(client, conn):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: {"app_metadata": {}}
    assert client.get("/autodedup/iterations").status_code == 403
    assert client.get("/autodedup/stats").status_code == 403


# ------------------------------------------------------------------- the missing relation


def test_a_relation_that_vanishes_between_probe_and_read_still_renders(client, conn, monkeypatch):
    """The probe can be beaten by a `drop schema autodedup cascade` — teardown is one
    statement in this program — so the read catches the SQLSTATE too."""
    pytest.importorskip("psycopg")
    from psycopg import errors as pg_errors

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise pg_errors.UndefinedTable("relation does not exist")

    monkeypatch.setattr(routes, "_fetch", _boom)
    assert client.get("/autodedup/iterations").json() == {"data": None, "store_ready": False}
    assert client.get("/autodedup/stats").json() == {"data": None, "store_ready": False}
