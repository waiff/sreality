"""Tests for the B2 per-unit fan-out + rollup orchestrator.

Hermetic — patches the persistence helpers, the child-estimation creator
(api.estimation_runs.create_estimation_run), and app_settings lookups so
no real DB, no real LLM, no real HTTP. The endpoint-level test verifies
confirm_units schedules the orchestrator; the function-level tests drive
br._run_building_estimations directly (the inline path).
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import building_runs as br
from api import dependencies as deps
from api import estimation_runs as er
from api import main as api_main


# -- shared fakes ------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self.buildings: dict[int, dict[str, Any]] = {}
        self.children: dict[int, dict[str, Any]] = {}
        self.next_child_id = 100
        self.bodies: list[Any] = []
        self.links: list[tuple[int, int, str | None]] = []


_CHILD_KEYS = (
    "id", "created_at", "status", "estimate_kind", "building_unit_id",
    "estimated_monthly_rent_czk", "rent_p25_czk", "rent_p75_czk",
    "estimated_sale_price_czk", "sale_p25_czk", "sale_p75_czk",
    "confidence", "error_message",
)


def _patch(monkeypatch, *, child_status: str = "success",
           building: dict[str, Any] | None = None) -> _State:
    state = _State()
    state.buildings[1] = building if building is not None else {
        "id": 1,
        "source": "ui",
        "status": "estimating",
        "subject_summary": {"fields": {"lat": 50.08, "lng": 14.42}},
        "input_spec": {"lat": 50.08, "lng": 14.42},
        "units": [
            {"unit_id": "u1", "area_m2": 60.0, "disposition": "2+kk",
             "label": "flat 1", "floor": "1", "condition": "dobry",
             "is_potential": False, "source": "both", "notes": None},
            {"unit_id": "u2", "area_m2": 80.0, "disposition": "3+kk",
             "label": "flat 2", "floor": "2", "condition": "dobry",
             "is_potential": False, "source": "both", "notes": None},
        ],
    }

    def fake_fetch_building(conn, bid: int) -> dict[str, Any] | None:
        row = state.buildings.get(bid)
        return dict(row) if row is not None else None

    def fake_update_building(conn, bid: int, **fields: Any) -> None:
        state.buildings.setdefault(bid, {}).update(fields)

    def fake_fetch_children(conn, bid: int) -> list[dict[str, Any]]:
        return [
            {k: c.get(k) for k in _CHILD_KEYS}
            for c in state.children.values()
            if c.get("building_run_id") == bid
        ]

    def fake_link(conn, child_id: int, *, building_id: int,
                  unit_id: str | None) -> None:
        state.links.append((child_id, building_id, unit_id))
        if child_id in state.children:
            state.children[child_id]["building_run_id"] = building_id
            state.children[child_id]["building_unit_id"] = unit_id

    def fake_setting(conn, key: str, fallback: Any) -> Any:
        if key == "building_default_estimator_skill":
            return "rental_estimator_v1"
        if key == "building_sale_estimator_skill":
            return None
        return fallback

    def fake_create(conn, sreality_client, llm_client, body,
                    background_tasks=None) -> dict[str, Any]:
        state.bodies.append(body)
        cid = state.next_child_id
        state.next_child_id += 1
        is_rent = body.estimate_kind == "rent"
        child = {
            "id": cid,
            "created_at": "2026-05-12T10:00:00+00:00",
            "status": child_status,
            "estimate_kind": body.estimate_kind,
            "building_run_id": None,
            "building_unit_id": None,
            "confidence": "high" if child_status == "success" else None,
            "error_message": None if child_status == "success" else "boom",
            "estimated_monthly_rent_czk": 20_000 if (is_rent and child_status == "success") else None,
            "rent_p25_czk": 19_000 if (is_rent and child_status == "success") else None,
            "rent_p75_czk": 21_000 if (is_rent and child_status == "success") else None,
            "estimated_sale_price_czk": 6_000_000 if (not is_rent and child_status == "success") else None,
            "sale_p25_czk": 5_750_000 if (not is_rent and child_status == "success") else None,
            "sale_p75_czk": 6_250_000 if (not is_rent and child_status == "success") else None,
        }
        state.children[cid] = child
        return dict(child)

    monkeypatch.setattr(br, "_fetch_building", fake_fetch_building)
    monkeypatch.setattr(br, "_update_building_fields", fake_update_building)
    monkeypatch.setattr(br, "_fetch_children", fake_fetch_children)
    monkeypatch.setattr(br, "_link_child_to_building", fake_link)
    monkeypatch.setattr(er, "create_estimation_run", fake_create)
    monkeypatch.setattr(er, "_load_app_setting", fake_setting)
    return state


# -- function-level: _run_building_estimations -------------------------------


def test_fans_out_rent_and_sale_per_unit_and_links(monkeypatch):
    state = _patch(monkeypatch)
    br._run_building_estimations(object(), object(), object(), building_id=1)

    # 2 units × (rent + sale) = 4 children, each linked to its unit.
    assert len(state.children) == 4
    assert len(state.links) == 4
    by_unit: dict[str, set[str]] = {}
    for cid, bid, unit_id in state.links:
        assert bid == 1
        kind = state.children[cid]["estimate_kind"]
        by_unit.setdefault(unit_id, set()).add(kind)
    assert by_unit == {"u1": {"rent", "sale"}, "u2": {"rent", "sale"}}


def test_rent_uses_agent_skill_sale_uses_deterministic(monkeypatch):
    state = _patch(monkeypatch)
    br._run_building_estimations(object(), object(), object(), building_id=1)

    rent_bodies = [b for b in state.bodies if b.estimate_kind == "rent"]
    sale_bodies = [b for b in state.bodies if b.estimate_kind == "sale"]
    assert len(rent_bodies) == 2 and len(sale_bodies) == 2

    for b in rent_bodies:
        assert b.mode == "agent"
        assert b.skill == "rental_estimator_v1"
        assert b.category_main == "byt"
        assert b.category_type == "pronajem"
    for b in sale_bodies:
        assert b.mode == "deterministic"
        assert b.category_main == "byt"
        assert b.category_type == "prodej"

    # Unit area + disposition flow into the child target spec.
    areas = sorted(b.spec.area_m2 for b in rent_bodies)
    assert areas == [60.0, 80.0]


def test_rolls_up_totals_on_success(monkeypatch):
    state = _patch(monkeypatch)
    br._run_building_estimations(object(), object(), object(), building_id=1)

    b = state.buildings[1]
    assert b["status"] == "success"
    # 2 successful rent children at 19000/20000/21000.
    assert b["total_rent_p25_czk"] == 38_000
    assert b["total_rent_p50_czk"] == 40_000
    assert b["total_rent_p75_czk"] == 42_000
    # 2 successful sale children at 5.75M / 6M / 6.25M.
    assert b["total_sale_p25_czk"] == 11_500_000
    assert b["total_sale_p50_czk"] == 12_000_000
    assert b["total_sale_p75_czk"] == 12_500_000


def test_all_children_failed_marks_building_failed(monkeypatch):
    state = _patch(monkeypatch, child_status="failed")
    br._run_building_estimations(object(), object(), object(), building_id=1)

    b = state.buildings[1]
    assert b["status"] == "failed"
    assert b["error_message"] == "all per-unit estimations failed"
    assert b["total_rent_p50_czk"] is None
    assert b["total_sale_p50_czk"] is None


def test_missing_latlng_marks_failed_without_fanning_out(monkeypatch):
    state = _patch(monkeypatch, building={
        "id": 1, "source": "ui", "status": "estimating",
        "subject_summary": {"fields": {}},
        "input_spec": {},
        "units": [{"unit_id": "u1", "area_m2": 60.0, "disposition": "2+kk"}],
    })
    br._run_building_estimations(object(), object(), object(), building_id=1)

    assert state.children == {}
    assert state.buildings[1]["status"] == "failed"
    assert "lat/lng" in state.buildings[1]["error_message"]


def test_no_units_marks_failed(monkeypatch):
    state = _patch(monkeypatch, building={
        "id": 1, "source": "ui", "status": "estimating",
        "subject_summary": {"fields": {"lat": 50.0, "lng": 14.0}},
        "units": [],
    })
    br._run_building_estimations(object(), object(), object(), building_id=1)

    assert state.children == {}
    assert state.buildings[1]["status"] == "failed"
    assert "no confirmed units" in state.buildings[1]["error_message"]


def test_noop_when_not_estimating(monkeypatch):
    state = _patch(monkeypatch, building={
        "id": 1, "source": "ui", "status": "awaiting_input",
        "subject_summary": {"fields": {"lat": 50.0, "lng": 14.0}},
        "units": [{"unit_id": "u1", "area_m2": 60.0}],
    })
    br._run_building_estimations(object(), object(), object(), building_id=1)
    assert state.children == {}
    # Status untouched — orchestrator only acts on 'estimating' rows.
    assert state.buildings[1]["status"] == "awaiting_input"


def test_reentrancy_guard_finalises_without_double_fanout(monkeypatch):
    state = _patch(monkeypatch)
    # Pretend a prior partial run already created both children for u1+u2.
    for unit_id in ("u1", "u2"):
        for kind in ("rent", "sale"):
            cid = state.next_child_id
            state.next_child_id += 1
            state.children[cid] = {
                "id": cid, "created_at": "2026-05-12T10:00:00+00:00",
                "status": "success", "estimate_kind": kind,
                "building_run_id": 1, "building_unit_id": unit_id,
                "confidence": "high", "error_message": None,
                "estimated_monthly_rent_czk": 20_000 if kind == "rent" else None,
                "rent_p25_czk": 19_000 if kind == "rent" else None,
                "rent_p75_czk": 21_000 if kind == "rent" else None,
                "estimated_sale_price_czk": 6_000_000 if kind == "sale" else None,
                "sale_p25_czk": 5_750_000 if kind == "sale" else None,
                "sale_p75_czk": 6_250_000 if kind == "sale" else None,
            }
    before = len(state.children)
    br._run_building_estimations(object(), object(), object(), building_id=1)
    # No new children created — it jumped straight to finalisation.
    assert len(state.children) == before
    assert state.buildings[1]["status"] == "success"
    assert state.buildings[1]["total_rent_p50_czk"] == 40_000


# -- rollup math unit ---------------------------------------------------------


def test_rollup_skips_null_percentiles():
    children = [
        {"estimate_kind": "rent", "status": "success",
         "estimated_monthly_rent_czk": 20_000,
         "rent_p25_czk": None, "rent_p75_czk": 21_000},
        {"estimate_kind": "rent", "status": "success",
         "estimated_monthly_rent_czk": 30_000,
         "rent_p25_czk": 28_000, "rent_p75_czk": 31_000},
    ]
    totals = br._rollup_totals(children)
    assert totals["total_rent_p50_czk"] == 50_000
    # Only one child contributed a p25, so the sum reflects that one.
    assert totals["total_rent_p25_czk"] == 28_000
    assert totals["total_rent_p75_czk"] == 52_000
    assert totals["total_sale_p50_czk"] is None


def test_rollup_ignores_failed_children():
    children = [
        {"estimate_kind": "rent", "status": "failed",
         "estimated_monthly_rent_czk": None,
         "rent_p25_czk": None, "rent_p75_czk": None},
        {"estimate_kind": "rent", "status": "success",
         "estimated_monthly_rent_czk": 20_000,
         "rent_p25_czk": 19_000, "rent_p75_czk": 21_000},
    ]
    totals = br._rollup_totals(children)
    assert totals["total_rent_p50_czk"] == 20_000


# -- endpoint-level: confirm_units schedules the orchestrator -----------------


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.require_token] = lambda: None
    api_main.app.dependency_overrides[deps.get_sreality_client] = lambda: object()
    api_main.app.dependency_overrides[deps.get_llm_client] = lambda: object()
    from starlette.background import BackgroundTasks
    monkeypatch.setattr(
        BackgroundTasks, "add_task",
        lambda self, func, *args, **kwargs: func(*args, **kwargs),
    )
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_confirm_units_schedules_orchestrator(client, monkeypatch):
    state = _patch(monkeypatch, building={
        "id": 1, "source": "ui", "status": "awaiting_input",
    })
    scheduled: list[int] = []
    monkeypatch.setattr(
        br, "_orchestrate_building_estimations_background",
        lambda **kw: scheduled.append(kw["building_id"]),
    )

    res = client.post(
        "/buildings/1/confirm_units",
        json={"units": [{"unit_id": "u1", "is_potential": False}]},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "estimating"
    assert scheduled == [1]
    assert state.buildings[1]["status"] == "estimating"
