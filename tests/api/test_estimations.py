"""Tests for /estimations endpoints (POST, GET-by-id, list, preview).

Hermetic — overrides the DB-conn, SrealityClient, and LLMClient
dependencies, and mocks the persistence helpers + dispatcher +
estimate_yield so no real DB / HTTP / LLM is hit.
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
from scraper import source_dispatcher as sd
from scraper import url_parser as scraper_url_parser


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.get_sreality_client] = (
        lambda: object()
    )
    api_main.app.dependency_overrides[deps.get_llm_client] = (
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
                    "source", "mode", "status", "estimate_kind",
                    "input_url", "input_sreality_id", "input_spec",
                    "input_purchase_price_czk",
                    "estimated_monthly_rent_czk", "rent_p25_czk",
                    "rent_p75_czk",
                    "estimated_sale_price_czk", "sale_p25_czk",
                    "sale_p75_czk",
                    "gross_yield_pct", "confidence",
                    "comparables_used", "trace", "warnings",
                    "error_message", "parent_run_id", "rerun_reason",
                    "source_kind", "parse_confidence",
                    "parse_confidence_per_field", "source_html",
                    "subject_summary",
                )
            },
        }

    monkeypatch.setattr(er, "_insert_run", fake_insert)
    monkeypatch.setattr(er, "_fetch_run", fake_fetch)
    monkeypatch.setattr(er, "_build_subject_summary", lambda *a, **kw: None)
    return state


def _patch_estimate(monkeypatch, exc: Exception | None = None,
                    data: dict[str, Any] | None = None) -> None:
    def fake(conn, target, filters, purchase_price_czk=None, *,
            estimate_kind="rent", expected_monthly_rent_czk=None,
            trace_recorder=None):
        if exc is not None:
            raise exc
        if trace_recorder is not None:
            with trace_recorder.tool_call(
                "find_comparables", input={}
            ) as h:
                h.set_summary({"result_count": 5})
        if data is not None:
            payload = data
        elif estimate_kind == "sale":
            payload = {
                "estimate_kind": "sale",
                "estimated_monthly_rent_czk": None,
                "rent_p25_czk": None,
                "rent_p75_czk": None,
                "estimated_sale_price_czk": 6_000_000,
                "sale_p25_czk": 5_750_000,
                "sale_p75_czk": 6_250_000,
                "gross_yield_pct": (
                    round((expected_monthly_rent_czk * 12) / 6_000_000 * 100, 2)
                    if expected_monthly_rent_czk else None
                ),
                "confidence": "high",
                "sample_size": 5,
                "comparables_used": [
                    {"sreality_id": 1, "snapshot_id": 11},
                ],
                "warnings": [],
            }
        else:
            payload = {
                "estimate_kind": "rent",
                "estimated_monthly_rent_czk": 20500,
                "rent_p25_czk": 19000,
                "rent_p75_czk": 22000,
                "estimated_sale_price_czk": None,
                "sale_p25_czk": None,
                "sale_p75_czk": None,
                "gross_yield_pct": 4.92,
                "confidence": "high",
                "sample_size": 5,
                "comparables_used": [
                    {"sreality_id": 1, "snapshot_id": 11},
                ],
                "warnings": [],
            }
        return {"data": payload, "metadata": {"tool": "estimate_yield"}}
    monkeypatch.setattr(ey, "estimate_yield", fake)


def _patch_url_parser(monkeypatch, sreality_id: int = 2836292428) -> None:
    """Patch the dispatcher's view of parse_sreality_url so a sreality URL
    flows through the new dispatcher with the same shape as before.
    """
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
    monkeypatch.setattr(sd.url_parser, "parse_sreality_url", fake)


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
    assert inserted["trace"]["version"] == 2
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


def test_post_sale_estimate_persists_sale_columns(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    _patch_estimate(monkeypatch)

    res = client.post(
        "/estimations",
        json={
            "estimate_kind": "sale",
            "spec": {
                "lat": 50.087, "lng": 14.42, "area_m2": 50.0,
                "disposition": "2+kk",
            },
            "expected_monthly_rent_czk": 25_000,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["estimate_kind"] == "sale"
    assert body["estimated_sale_price_czk"] == 6_000_000
    assert body["sale_p25_czk"] == 5_750_000
    assert body["sale_p75_czk"] == 6_250_000
    assert body["estimated_monthly_rent_czk"] is None
    assert body["gross_yield_pct"] == 5.0
    inserted = state.inserts[1]
    assert inserted["estimate_kind"] == "sale"
    assert inserted["estimated_sale_price_czk"] == 6_000_000


def test_post_default_estimate_kind_is_rent(client, monkeypatch):
    _patch_persistence(monkeypatch)
    _patch_estimate(monkeypatch)

    res = client.post(
        "/estimations",
        json={
            "spec": {
                "lat": 50.087, "lng": 14.42, "area_m2": 50.0,
                "disposition": "2+kk",
            },
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["estimate_kind"] == "rent"
    assert body["estimated_monthly_rent_czk"] == 20500
    assert body["estimated_sale_price_czk"] is None


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


def test_post_with_mode_agent_does_not_500(client, monkeypatch):
    """Regression: the agent-path _insert_run used to omit estimate_kind
    and the three sale columns, KeyError-ing inside psycopg and bubbling
    out as a generic 500. POST should land a real row instead."""
    state = _patch_persistence(monkeypatch)

    def fake_update(conn, run_id: int, **fields: Any) -> None:
        state.inserts[run_id].update(fields)

    monkeypatch.setattr(er, "_update_run_terminal", fake_update)

    from api import agent as agent_mod
    from api import skills as sk

    monkeypatch.setattr(
        agent_mod, "run_agent_estimation",
        lambda *a, **kw: agent_mod.AgentResult(
            data={
                "estimated_monthly_rent_czk": 25000,
                "rent_p25_czk": 23000,
                "rent_p75_czk": 27000,
                "gross_yield_pct": None,
                "confidence": "medium",
                "comparables_used": [],
                "warnings": [],
            },
            metadata={
                "stop_reason": "record_estimate",
                "iterations": 1,
                "total_cost_usd": 0.0,
                "provider": "anthropic",
                "skill": "rental_estimator_v1",
            },
        ),
    )
    monkeypatch.setattr(
        sk, "load_skill",
        lambda conn, name: sk.Skill(
            name=name, description="", system_prompt="",
            allowed_tools=["record_estimate"],
            preferred_model={"anthropic": "x", "gemini": "y"},
            limits=sk.SkillLimits(
                max_iterations=5, max_cost_usd=1.0,
                wall_clock_timeout_s=60.0,
            ),
        ),
    )

    res = client.post(
        "/estimations",
        json={
            "mode": "agent",
            "spec": {
                "lat": 50.0, "lng": 14.0, "area_m2": 50.0,
                "disposition": "2+kk",
            },
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["mode"] == "agent"
    assert body["estimate_kind"] == "rent"
    assert len(state.inserts) == 1
    inserted = state.inserts[1]
    assert inserted["estimate_kind"] == "rent"
    assert inserted["estimated_sale_price_czk"] is None


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
# GET /estimations/preview
# ----------------------------------------------------------------------

def test_preview_returns_normalised_spec(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    _patch_url_parser(monkeypatch, sreality_id=2836292428)

    res = client.get(
        "/estimations/preview"
        "?url=https://www.sreality.cz/detail/x/2836292428"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["sreality_id"] == 2836292428
    assert body["in_database"] is False
    assert body["url"].endswith("2836292428")
    assert body["spec"]["lat"] == 50.087
    assert body["spec"]["lng"] == 14.42  # normalised from parser's 'lon'
    assert "lon" not in body["spec"]
    assert body["spec"]["area_m2"] == 50.0
    assert body["spec"]["disposition"] == "2+kk"
    assert body["spec"]["floor"] == 3
    assert body["spec"]["exclude_ids"] == []
    # Preview must not persist anything.
    assert len(state.inserts) == 0


def test_preview_invalid_url_returns_400(client, monkeypatch):
    state = _patch_persistence(monkeypatch)

    res = client.get(
        "/estimations/preview?url=https://example.com/not-a-listing"
    )
    assert res.status_code == 400
    assert "sreality_id" in res.json()["detail"].lower()
    assert len(state.inserts) == 0


def test_preview_upstream_error_returns_502(client, monkeypatch):
    import requests
    state = _patch_persistence(monkeypatch)

    def fake(url: str, *, client, conn) -> dict[str, Any]:
        raise requests.HTTPError("502 Bad Gateway from sreality")

    monkeypatch.setattr(scraper_url_parser, "parse_sreality_url", fake)

    res = client.get(
        "/estimations/preview"
        "?url=https://www.sreality.cz/detail/x/2836292428"
    )
    assert res.status_code == 502
    assert "sreality" in res.json()["detail"].lower()
    assert len(state.inserts) == 0


def test_preview_exposes_listing_block(client, monkeypatch):
    def fake(url: str, *, client, conn) -> dict[str, Any]:
        return {
            "sreality_id": 2836292428,
            "spec": {
                "sreality_id": 2836292428,
                "lat": 50.087, "lon": 14.42,
                "area_m2": 50.0, "disposition": "2+kk", "floor": 3,
                "price_czk": 18500, "price_unit": "měsíc",
                "category_main": "byt", "category_type": "pronajem",
                "locality": "Praha 1, Nové Město",
                "district": "Praha 1",
                "locality_district_id": 5001,
                "locality_region_id": 10,
                "total_floors": 6,
                "has_balcony": True, "has_lift": True, "has_parking": False,
                "building_type": "cihlová",
                "condition": "po rekonstrukci",
                "energy_rating": "C",
            },
            "images": [
                {"url": "x", "sequence": 1},
                {"url": "y", "sequence": 2},
                {"url": "z", "sequence": 3},
            ],
            "fetched_at": "2026-05-04T10:00:00+00:00",
            "source_url": url,
            "in_database": True,
        }
    monkeypatch.setattr(scraper_url_parser, "parse_sreality_url", fake)

    res = client.get(
        "/estimations/preview"
        "?url=https://www.sreality.cz/detail/x/2836292428"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["in_database"] is True
    listing = body["listing"]
    assert listing["price_czk"] == 18500
    assert listing["district"] == "Praha 1"
    assert listing["total_floors"] == 6
    assert listing["has_balcony"] is True
    assert listing["has_parking"] is False
    assert listing["building_type"] == "cihlová"
    assert listing["energy_rating"] == "C"
    assert listing["image_count"] == 3


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
    # No outer WHERE clause — every filter predicate is gated behind
    # an "er.<col> = " prefix, none of which should appear here.
    for predicate in (
        "er.source =", "er.status =",
        "er.input_sreality_id =", "er.source_kind =",
    ):
        assert predicate not in list_sql
        assert predicate not in count_sql
    assert "ORDER BY er.created_at DESC" in list_sql
    assert "LIMIT %(limit)s OFFSET %(offset)s" in list_sql
    assert "cost_usd_total" in list_sql
    assert "FROM llm_calls WHERE estimation_run_id = er.id" in list_sql
    assert list_params == {"limit": 20, "offset": 5}
    assert count_params == {}


def test_list_with_filters_builds_where_clause():
    conn = _FakeConn(results=[[], (3,)])
    er.list_estimation_runs(
        conn, source="ui", status="success",
        sreality_id=12345, limit=50, offset=0,
    )
    list_sql, list_params = conn.executions[0]
    assert "er.source = %(source)s" in list_sql
    assert "er.status = %(status)s" in list_sql
    assert "er.input_sreality_id = %(sreality_id)s" in list_sql
    assert list_params["source"] == "ui"
    assert list_params["status"] == "success"
    assert list_params["sreality_id"] == 12345


def test_list_filters_by_source_kind():
    conn = _FakeConn(results=[[], (0,)])
    er.list_estimation_runs(conn, source_kind="bezrealitky")
    list_sql, list_params = conn.executions[0]
    assert "er.source_kind = %(source_kind)s" in list_sql
    assert list_params["source_kind"] == "bezrealitky"


def test_get_estimation_run_returns_none_when_missing():
    conn = _FakeConn(results=[None])
    res = er.get_estimation_run(conn, run_id=999)
    assert res is None
    assert "WHERE er.id = %s" in conn.executions[0][0]
    assert "cost_usd_total" in conn.executions[0][0]


# ----------------------------------------------------------------------
# POST /estimations/preview
# ----------------------------------------------------------------------

def _patch_dispatcher_returns(
    monkeypatch, parse_result: sd.ParseResult,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake(url, *, sreality_client, llm_client, conn,
             force_refresh: bool = False, **_kw):
        captured["url"] = url
        captured["force_refresh"] = force_refresh
        return parse_result

    monkeypatch.setattr(sd, "parse_listing_url", fake)
    monkeypatch.setattr(er.source_dispatcher, "parse_listing_url", fake)
    return captured


def _result(**overrides: Any) -> sd.ParseResult:
    base = dict(
        spec={
            "lat": 50.087, "lng": 14.42, "area_m2": 50.0,
            "disposition": "2+kk", "floor": 3, "exclude_ids": [],
        },
        source_kind="sreality",
        parse_confidence="high",
        parse_confidence_per_field=None,
        source_html=None,
        from_cache=False,
        cost_usd=None,
        warnings=[],
        sreality_id=2836292428,
        source_url="https://www.sreality.cz/detail/x/2836292428",
        full_extraction=None,
    )
    base.update(overrides)
    return sd.ParseResult(**base)


def test_preview_sreality_returns_parsed_spec(client, monkeypatch):
    captured = _patch_dispatcher_returns(monkeypatch, _result())
    res = client.post(
        "/estimations/preview",
        json={"url": "https://www.sreality.cz/detail/x/2836292428"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["source_kind"] == "sreality"
    assert body["parse_confidence"] == "high"
    assert body["from_cache"] is False
    assert body["cost_usd"] is None
    assert body["sreality_id"] == 2836292428
    assert body["spec"]["lat"] == 50.087
    assert body["spec"]["lng"] == 14.42
    assert captured["url"].endswith("2836292428")


def test_preview_bezrealitky_returns_source_kind_and_confidence(client, monkeypatch):
    _patch_dispatcher_returns(monkeypatch, _result(
        source_kind="bezrealitky",
        parse_confidence="medium",
        parse_confidence_per_field={
            "area_m2": "high", "disposition": "high", "lat": "medium",
        },
        source_html="<html>...</html>",
        from_cache=False,
        cost_usd=0.018,
        sreality_id=None,
        source_url="https://www.bezrealitky.cz/listing/abc",
    ))
    res = client.post(
        "/estimations/preview",
        json={"url": "https://www.bezrealitky.cz/listing/abc"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["source_kind"] == "bezrealitky"
    assert body["parse_confidence"] == "medium"
    assert body["parse_confidence_per_field"]["lat"] == "medium"
    assert body["cost_usd"] == 0.018


def test_preview_applies_spec_overrides(client, monkeypatch):
    _patch_dispatcher_returns(monkeypatch, _result())
    res = client.post(
        "/estimations/preview",
        json={
            "url": "https://www.sreality.cz/detail/x/2836292428",
            "spec_overrides": {"area_m2": 65.0, "floor": 5},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["spec"]["area_m2"] == 65.0
    assert body["spec"]["floor"] == 5
    assert body["spec"]["lat"] == 50.087  # unchanged


def test_preview_returns_502_on_parse_error(client, monkeypatch):
    def boom(url, **_kw):
        raise sd.ParseError("LLM did not invoke record_listing")

    monkeypatch.setattr(sd, "parse_listing_url", boom)
    monkeypatch.setattr(er.source_dispatcher, "parse_listing_url", boom)
    res = client.post(
        "/estimations/preview",
        json={"url": "https://www.bezrealitky.cz/listing/abc"},
    )
    assert res.status_code == 502
    assert "parse failed" in res.json()["detail"]


def test_preview_returns_502_on_upstream_fetch_failure(client, monkeypatch):
    def boom(url, **_kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(sd, "parse_listing_url", boom)
    monkeypatch.setattr(er.source_dispatcher, "parse_listing_url", boom)
    res = client.post(
        "/estimations/preview",
        json={"url": "https://www.bezrealitky.cz/listing/abc"},
    )
    assert res.status_code == 502


def test_preview_sreality_returns_listing_block(client, monkeypatch):
    _patch_dispatcher_returns(monkeypatch, _result(
        wide_spec={
            "price_czk": 18500, "price_unit": "měsíc",
            "category_main": "byt", "category_type": "pronajem",
            "locality": "Praha 1, Nové Město", "district": "Praha 1",
            "locality_district_id": 5001, "locality_region_id": 10,
            "total_floors": 6,
            "has_balcony": True, "has_lift": True, "has_parking": False,
            "building_type": "cihlová", "condition": "po rekonstrukci",
            "energy_rating": "C",
        },
        fetched_at="2026-05-04T10:00:00+00:00",
    ))
    res = client.post(
        "/estimations/preview",
        json={"url": "https://www.sreality.cz/detail/x/2836292428"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["fetched_at"] == "2026-05-04T10:00:00+00:00"
    listing = body["listing"]
    assert listing["price_czk"] == 18500
    assert listing["district"] == "Praha 1"
    assert listing["total_floors"] == 6
    assert listing["has_balcony"] is True
    assert listing["building_type"] == "cihlová"


def test_preview_llm_listing_block_extracts_values_from_full_extraction(
    client, monkeypatch,
):
    _patch_dispatcher_returns(monkeypatch, _result(
        source_kind="bezrealitky",
        parse_confidence="medium",
        full_extraction={
            "price_czk":     {"value": 24000, "confidence": "high"},
            "total_floors":  {"value": 4,     "confidence": "medium"},
            "has_balcony":   {"value": True,  "confidence": "high"},
            "building_type": {"value": "panel", "confidence": "low"},
            "energy_rating": {"value": None,  "confidence": "low"},
        },
        source_html="<html>...</html>",
        cost_usd=0.012,
        sreality_id=None,
        source_url="https://www.bezrealitky.cz/listing/abc",
    ))
    res = client.post(
        "/estimations/preview",
        json={"url": "https://www.bezrealitky.cz/listing/abc"},
    )
    body = res.json()
    listing = body["listing"]
    assert listing["price_czk"] == 24000
    assert listing["total_floors"] == 4
    assert listing["has_balcony"] is True
    assert listing["building_type"] == "panel"
    assert listing["energy_rating"] is None
    assert listing["condition"] is None


def test_preview_force_refresh_threads_through_to_dispatcher(
    client, monkeypatch,
):
    captured: dict[str, Any] = {}

    def fake(url, *, sreality_client, llm_client, conn, force_refresh=False):
        captured["force_refresh"] = force_refresh
        return _result(source_kind="bezrealitky", parse_confidence="medium",
                       sreality_id=None,
                       source_url="https://www.bezrealitky.cz/listing/abc")

    monkeypatch.setattr(sd, "parse_listing_url", fake)
    monkeypatch.setattr(er.source_dispatcher, "parse_listing_url", fake)

    res = client.post(
        "/estimations/preview",
        json={
            "url": "https://www.bezrealitky.cz/listing/abc",
            "force_refresh": True,
        },
    )
    assert res.status_code == 200
    assert captured["force_refresh"] is True


def test_preview_force_refresh_default_is_false(client, monkeypatch):
    captured: dict[str, Any] = {}

    def fake(url, *, sreality_client, llm_client, conn, force_refresh=False):
        captured["force_refresh"] = force_refresh
        return _result()

    monkeypatch.setattr(sd, "parse_listing_url", fake)
    monkeypatch.setattr(er.source_dispatcher, "parse_listing_url", fake)

    res = client.post(
        "/estimations/preview",
        json={"url": "https://www.sreality.cz/detail/x/2836292428"},
    )
    assert res.status_code == 200
    assert captured["force_refresh"] is False


# ----------------------------------------------------------------------
# POST /estimations now flows through the dispatcher and persists
# the four new audit columns.
# ----------------------------------------------------------------------

def test_post_with_non_sreality_url_persists_provenance(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    _patch_estimate(monkeypatch)
    _patch_dispatcher_returns(monkeypatch, _result(
        source_kind="bezrealitky",
        parse_confidence="medium",
        parse_confidence_per_field={
            "area_m2": "high", "disposition": "high", "lat": "medium",
        },
        source_html="<html>spec</html>",
        cost_usd=0.018,
        warnings=["geocoded with medium confidence"],
        sreality_id=None,
        source_url="https://www.bezrealitky.cz/listing/abc",
    ))
    res = client.post(
        "/estimations",
        json={"url": "https://www.bezrealitky.cz/listing/abc"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["source_kind"] == "bezrealitky"
    assert body["parse_confidence"] == "medium"
    assert body["parse_confidence_per_field"]["lat"] == "medium"
    assert body["source_html"] == "<html>spec</html>"
    assert body["input_sreality_id"] is None
    assert body["input_url"].endswith("/abc")
    # Parse-time warnings flow through to the persisted row.
    assert any("geocoded" in w for w in (body["warnings"] or []))
    inserted = state.inserts[1]
    assert inserted["source_kind"] == "bezrealitky"
    assert inserted["parse_confidence"] == "medium"


def test_post_when_dispatch_fails_persists_failed_row(client, monkeypatch):
    state = _patch_persistence(monkeypatch)

    def boom(url, **_kw):
        raise sd.ParseError("LLM did not invoke record_listing")

    monkeypatch.setattr(sd, "parse_listing_url", boom)
    monkeypatch.setattr(er.source_dispatcher, "parse_listing_url", boom)
    res = client.post(
        "/estimations",
        json={"url": "https://www.bezrealitky.cz/listing/abc"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "failed"
    assert "ParseError" in body["error_message"]
    assert "record_listing" in body["error_message"]
    assert body["input_url"].endswith("/abc")
    assert body["input_sreality_id"] is None
    assert body["estimated_monthly_rent_czk"] is None
    inserted = state.inserts[1]
    assert inserted["status"] == "failed"
    assert "record_listing" in inserted["error_message"]


def test_post_with_sreality_url_populates_sreality_source_kind(client, monkeypatch):
    state = _patch_persistence(monkeypatch)
    _patch_estimate(monkeypatch)
    _patch_dispatcher_returns(monkeypatch, _result())

    res = client.post(
        "/estimations",
        json={"url": "https://www.sreality.cz/detail/x/2836292428"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["source_kind"] == "sreality"
    assert body["parse_confidence"] == "high"
    assert body["parse_confidence_per_field"] is None
    assert body["source_html"] is None
    assert body["input_sreality_id"] == 2836292428
