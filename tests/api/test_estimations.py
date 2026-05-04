"""Tests for /estimations endpoints (POST, GET-by-id, list).

Hermetic — overrides the DB-conn and SrealityClient dependencies, and
mocks the persistence helpers + url_parser + estimate_yield so no real
DB or HTTP is hit.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import estimate_yield as ey
from api import estimation_runs as er
from api import main as api_main
from scraper import url_parser as scraper_url_parser


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.get_sreality_client] = (
        lambda: object()
    )
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


# ----------------------------------------------------------------------
# Helpers: in-memory persistence patch (one fake _insert_run that mints
# IDs, one fake _fetch_run that reads what was inserted).
# ----------------------------------------------------------------------

class _State:
    def __init__(self) -> None:
        self.inserts: dict[int, dict[str, Any]] = {}
        self.next_id = 1


def _patch_persistence(monkeypatch) -> _State:
    state = _State()

    def fake_insert(conn, **fields: Any) -> int:
        rid = state.next_id
        state.next_id += 1
        state.inserts[rid] = dict(fields)
        return rid

    def fake_fetch(conn, run_id: int) -> dict[str, Any] | None:
        if run_id not in state.inserts:
            return None
        fields = state.inserts[run_id]
        return {
            "id": run_id,
            "created_at": "2026-05-04T10:00:00+00:00",
            **{
                k: fields.get(k)
                for k in (
                    "source", "mode", "status",
                    "input_url", "input_sreality_id", "input_spec",
                    "input_purchase_price_czk",
                    "estimated_monthly_rent_czk", "rent_p25_czk",
                    "rent_p75_czk", "gross_yield_pct", "confidence",
                    "comparables_used", "trace", "warnings",
                    "error_message", "parent_run_id", "rerun_reason",
                )
            },
        }

    monkeypatch.setattr(er, "_insert_run", fake_insert)
    monkeypatch.setattr(er, "_fetch_run", fake_fetch)
    return state


def _patch_estimate(monkeypatch, exc: Exception | None = None,
                    data: dict[str, Any] | None = None) -> None:
    def fake(conn, target, filters, purchase_price_czk=None, *,
            trace_recorder=None):
        if exc is not None:
            raise exc
        if trace_recorder is not None:
            with trace_recorder.tool_call(
                "find_comparables", input={}
            ) as h:
                h.set_summary({"result_count": 5})
        return {
            "data": data or {
                "estimated_monthly_rent_czk": 20500,
                "rent_p25_czk": 19000,
                "rent_p75_czk": 22000,
                "gross_yield_pct": 4.92,
                "confidence": "high",
                "sample_size": 5,
                "comparables_used": [
                    {"sreality_id": 1, "snapshot_id": 11},
                ],
                "warnings": [],
            },
            "metadata": {"tool": "estimate_yield"},
        }
    monkeypatch.setattr(ey, "estimate_yield", fake)


def _patch_url_parser(monkeypatch, sreality_id: int = 2836292428) -> None:
    def fake(url: str, *, client, conn) -> dict[str, Any]:
        return {
            "sreality_id": sreality_id,
            "spec": {
                "sreality_id": sreality_id,
                "lat": 50.087,
                "lon": 14.42,
                "area_m2": 50.0,
                "disposition": "2+kk",
                "floor": 3,
            },
            "images": [],
            "fetched_at": "2026-05-04T10:00:00+00:00",
            "source_url": url,
            "in_database": False,
        }
    monkeypatch.setattr(
        scraper_url_parser, "parse_sreality_url", fake
    )


# ----------------------------------------------------------------------
# POST /estimations
# ----------------------------------------------------------------------

def test_post_with_spec_creates_success_row(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    _patch_estimate(monkeypatch)

    res = client.post(
        "/estimations",
        json={
            "spec": {
                "lat": 50.087, "lng": 14.42, "area_m2": 50.0,
                "disposition": "2+kk",
            },
            "purchase_price_czk": 5_000_000,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["estimated_monthly_rent_czk"] == 20500
    assert body["confidence"] == "high"
    assert body["input_url"] is None
    assert body["input_sreality_id"] is None
    assert body["input_spec"]["lat"] == 50.087
    assert body["input_spec"]["lng"] == 14.42
    assert body["input_purchase_price_czk"] == 5_000_000
    assert len(state.inserts) == 1
    inserted = state.inserts[1]
    assert inserted["status"] == "success"
    assert inserted["source"] == "api"
    assert inserted["mode"] == "deterministic"
    assert inserted["trace"]["version"] == 1
    assert inserted["trace"]["steps"][0]["tool"] == "find_comparables"


def test_post_with_url_calls_url_parser(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    _patch_estimate(monkeypatch)
    _patch_url_parser(monkeypatch, sreality_id=2836292428)

    res = client.post(
        "/estimations",
        json={
            "url": "https://www.sreality.cz/detail/.../2836292428",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["input_url"].endswith("2836292428")
    assert body["input_sreality_id"] == 2836292428
    assert body["input_spec"]["lat"] == 50.087
    assert body["input_spec"]["lng"] == 14.42  # normalised from parser's 'lon'
    assert "lon" not in body["input_spec"]


def test_post_with_url_and_spec_overrides_merges(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    _patch_estimate(monkeypatch)
    _patch_url_parser(monkeypatch)

    res = client.post(
        "/estimations",
        json={
            "url": "https://www.sreality.cz/detail/x/2836292428",
            "spec_overrides": {"area_m2": 60.0, "floor": 5},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["input_spec"]["area_m2"] == 60.0
    assert body["input_spec"]["floor"] == 5
    assert body["input_spec"]["lat"] == 50.087


def test_post_with_both_url_and_spec_returns_422(client):
    res = client.post(
        "/estimations",
        json={
            "url": "https://www.sreality.cz/detail/x/2836292428",
            "spec": {"lat": 50.0, "lng": 14.0},
        },
    )
    assert res.status_code == 422


def test_post_with_neither_url_nor_spec_returns_422(client):
    res = client.post(
        "/estimations",
        json={"purchase_price_czk": 5_000_000},
    )
    assert res.status_code == 422


def test_post_when_estimate_yield_raises_persists_failed(
    client, monkeypatch
):
    state = _patch_persistence(monkeypatch)
    _patch_estimate(monkeypatch, exc=RuntimeError("DB connection lost"))

    res = client.post(
        "/estimations",
        json={"spec": {"lat": 50.0, "lng": 14.0, "area_m2": 50.0}},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "failed"
    assert "DB connection lost" in body["error_message"]
    assert body["estimated_monthly_rent_czk"] is None
    assert body["confidence"] is None
    inserted = state.inserts[1]
    assert inserted["status"] == "failed"
    assert "DB connection lost" in inserted["error_message"]


def test_post_with_parent_run_id_populates_fk(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    _patch_estimate(monkeypatch)

    res = client.post(
        "/estimations",
        json={
            "spec": {"lat": 50.0, "lng": 14.0, "area_m2": 50.0},
            "parent_run_id": 7,
            "rerun_reason": "force refetch",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["parent_run_id"] == 7
    assert body["rerun_reason"] == "force refetch"


def test_post_invalid_source_returns_422(client):
    res = client.post(
        "/estimations",
        json={
            "source": "wibble",
            "spec": {"lat": 50.0, "lng": 14.0},
        },
    )
    assert res.status_code == 422


# ----------------------------------------------------------------------
# GET /estimations/{id}
# ----------------------------------------------------------------------

def test_get_returns_row(client, monkeypatch):
    fake_row = {"id": 42, "status": "success"}
    monkeypatch.setattr(
        api_main, "get_estimation_run", lambda conn, rid: fake_row
    )
    res = client.get("/estimations/42")
    assert res.status_code == 200
    assert res.json()["id"] == 42


def test_get_404_when_missing(client, monkeypatch):
    monkeypatch.setattr(
        api_main, "get_estimation_run", lambda conn, rid: None
    )
    res = client.get("/estimations/999")
    assert res.status_code == 404


# ----------------------------------------------------------------------
# GET /estimations (list)
# ----------------------------------------------------------------------

def test_list_passes_filters_and_pagination(client, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_list(conn, **kw):
        captured.update(kw)
        return {"data": [], "total": 0, "limit": kw["limit"],
                "offset": kw["offset"]}

    monkeypatch.setattr(api_main, "list_estimation_runs", fake_list)

    res = client.get(
        "/estimations?source=ui&status=success"
        "&sreality_id=12345&limit=10&offset=20"
    )
    assert res.status_code == 200
    assert captured["source"] == "ui"
    assert captured["status"] == "success"
    assert captured["sreality_id"] == 12345
    assert captured["limit"] == 10
    assert captured["offset"] == 20


def test_list_default_limit_50_offset_0(client, monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        api_main, "list_estimation_runs",
        lambda conn, **kw: captured.update(kw)
        or {"data": [], "total": 0, "limit": kw["limit"],
            "offset": kw["offset"]},
    )
    res = client.get("/estimations")
    assert res.status_code == 200
    assert captured["limit"] == 50
    assert captured["offset"] == 0
    assert captured["source"] is None
    assert captured["status"] is None
    assert captured["sreality_id"] is None


def test_list_limit_over_200_rejected(client):
    res = client.get("/estimations?limit=500")
    assert res.status_code == 422


def test_list_negative_offset_rejected(client):
    res = client.get("/estimations?offset=-1")
    assert res.status_code == 422


def test_list_invalid_status_rejected(client):
    res = client.get("/estimations?status=wibble")
    assert res.status_code == 422


# ----------------------------------------------------------------------
# list_estimation_runs SQL construction (fake conn)
# ----------------------------------------------------------------------

class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = ()) -> None:
        self._conn.executions.append((sql, params))

    def fetchone(self) -> Any:
        return self._conn._results.pop(0) if self._conn._results else None

    def fetchall(self) -> Any:
        return self._conn._results.pop(0) if self._conn._results else []


class _FakeConn:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.executions: list[tuple[str, Any]] = []
        self._results: list[Any] = list(results or [])

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_list_no_filters_builds_naked_sql():
    conn = _FakeConn(results=[[], (0,)])
    res = er.list_estimation_runs(conn, limit=20, offset=5)
    assert res == {"data": [], "total": 0, "limit": 20, "offset": 5}
    list_sql, list_params = conn.executions[0]
    count_sql, count_params = conn.executions[1]
    assert "WHERE" not in list_sql
    assert "ORDER BY created_at DESC" in list_sql
    assert "LIMIT %(limit)s OFFSET %(offset)s" in list_sql
    assert list_params == {"limit": 20, "offset": 5}
    assert "WHERE" not in count_sql
    assert count_params == {}


def test_list_with_filters_builds_where_clause():
    conn = _FakeConn(results=[[], (3,)])
    er.list_estimation_runs(
        conn, source="ui", status="success",
        sreality_id=12345, limit=50, offset=0,
    )
    list_sql, list_params = conn.executions[0]
    assert "source = %(source)s" in list_sql
    assert "status = %(status)s" in list_sql
    assert "input_sreality_id = %(sreality_id)s" in list_sql
    assert list_params["source"] == "ui"
    assert list_params["status"] == "success"
    assert list_params["sreality_id"] == 12345


def test_get_estimation_run_returns_none_when_missing():
    conn = _FakeConn(results=[None])
    res = er.get_estimation_run(conn, run_id=999)
    assert res is None
    assert "WHERE id = %s" in conn.executions[0][0]
