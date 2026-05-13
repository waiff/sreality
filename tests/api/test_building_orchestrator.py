"""Hermetic tests for the Phase B2 per-unit fan-out + rollup.

Hits the orchestrator directly with monkeypatched persistence helpers
+ a stub `run_agent_estimation`. Verifies:

  - the orchestrator inserts one estimation_runs row per unit with
    building_run_id + building_unit_id populated;
  - operator inputs (special_instructions / contextual_text / attachments)
    flow into each child's agent call;
  - successful children's rent percentiles roll up into the parent's
    total_rent_* columns;
  - per-child failures don't break the fan-out; the parent transitions
    to 'success' as long as at least one child succeeded;
  - skipping a unit with no area_m2 marks that child 'failed' but
    other children still run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from api import building_orchestrator as bo
from api import building_runs as br
from api import estimation_runs as er


@dataclass
class _Skill:
    name: str = "rental_estimator_v1"
    preferred_model: dict[str, str] = field(
        default_factory=lambda: {"anthropic": "claude-sonnet-4-5"},
    )


def _agent_result(*, rent: int = 30_000, p25: int = 25_000, p75: int = 35_000):
    from api.agent import AgentResult
    return AgentResult(
        data={
            "estimated_monthly_rent_czk": rent,
            "rent_p25_czk": p25,
            "rent_p75_czk": p75,
            "confidence": "medium",
            "comparables_used": [{"sreality_id": 111, "snapshot_id": 222}],
            "warnings": [],
        },
        metadata={
            "stop_reason": "record_estimate",
            "iterations": 4,
            "provider": "anthropic",
            "skill": "rental_estimator_v1",
            "total_cost_usd": 0.07,
        },
    )


def _patch_persistence(monkeypatch, *, parent: dict[str, Any]):
    """Replace _fetch_building / _update_building_fields / _insert_run /
    _update_run_terminal with in-memory equivalents."""
    state = {
        "building": dict(parent),
        "children": {},
        "next_child_id": 100,
    }

    def fake_fetch(conn, building_id):
        if building_id != parent["id"]:
            return None
        return dict(state["building"])

    def fake_update_building(conn, building_id, **fields):
        state["building"].update(fields)

    def fake_insert_run(conn, **fields):
        cid = state["next_child_id"]
        state["next_child_id"] += 1
        state["children"][cid] = dict(fields) | {"id": cid, "status": fields.get("status")}
        return cid

    def fake_update_terminal(conn, run_id, **fields):
        state["children"][run_id].update(fields)

    monkeypatch.setattr(br, "_fetch_building", fake_fetch)
    monkeypatch.setattr(br, "_update_building_fields", fake_update_building)
    monkeypatch.setattr(er, "_insert_run", fake_insert_run)
    monkeypatch.setattr(er, "_update_run_terminal", fake_update_terminal)

    # The orchestrator's rollup runs a raw SELECT; replace it too.
    def fake_rollup(conn, building_run_id):
        rows = state["children"].values()
        total_p25 = sum(int(r.get("rent_p25_czk") or 0) for r in rows if r.get("status") == "success")
        total_p50 = sum(int(r.get("estimated_monthly_rent_czk") or 0) for r in rows if r.get("status") == "success")
        total_p75 = sum(int(r.get("rent_p75_czk") or 0) for r in rows if r.get("status") == "success")
        succ_with_rent = [
            r for r in rows
            if r.get("status") == "success" and r.get("estimated_monthly_rent_czk") is not None
        ]
        if not succ_with_rent:
            n_succ = sum(1 for r in rows if r.get("status") == "success")
            n_fail = sum(1 for r in rows if r.get("status") == "failed")
            fake_update_building(
                conn, parent["id"],
                status="failed",
                error_message=f"rollup: no successful child estimations with a rent range (success={n_succ}, failed={n_fail})",
            )
            return
        fake_update_building(
            conn, parent["id"],
            status="success",
            total_rent_p25_czk=total_p25,
            total_rent_p50_czk=total_p50,
            total_rent_p75_czk=total_p75,
        )

    monkeypatch.setattr(bo, "rollup_building_estimates", fake_rollup)
    return state


def _patch_external(monkeypatch, agent_outcomes):
    """Stub list_attachments, _resolve_default_skill, load_skill,
    run_agent_estimation. agent_outcomes is a list iterated per call."""
    calls = []

    def fake_list_attachments(conn, building_run_id):
        return [
            {"id": 901, "filename": "plan.png", "mime_type": "image/png",
             "storage_key": "k", "building_run_id": building_run_id},
        ]

    def fake_resolve_skill(conn):
        return "rental_estimator_v1"

    def fake_load_skill(conn, name):
        return _Skill(name=name)

    outcomes = list(agent_outcomes)

    def fake_run_agent(*args, **kwargs):
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    import api.agent as agent_mod
    import api.attachments as attachments_mod
    import api.skills as skills_mod
    monkeypatch.setattr(attachments_mod, "list_attachments", fake_list_attachments)
    monkeypatch.setattr(bo, "_resolve_default_skill", fake_resolve_skill)
    monkeypatch.setattr(skills_mod, "load_skill", fake_load_skill)
    monkeypatch.setattr(agent_mod, "run_agent_estimation", fake_run_agent)
    return calls


def _parent(units, **overrides):
    base = {
        "id": 7,
        "status": "estimating",
        "source": "ui",
        "input_url": "https://example.cz/dum/1",
        "input_sreality_id": 999,
        "input_spec": {"lat": 50.08, "lng": 14.42, "area_m2": 250.0},
        "units": units,
        "special_instructions": "Treat attic as habitable",
        "contextual_text": "Owner says heating refurbished 2022",
    }
    base.update(overrides)
    return base


def test_fan_out_happy_path_two_units(monkeypatch):
    units = [
        {"unit_id": "u1", "area_m2": 60.0, "disposition": "2+kk", "floor": "1"},
        {"unit_id": "u2", "area_m2": 75.0, "disposition": "3+kk", "floor": "2"},
    ]
    state = _patch_persistence(monkeypatch, parent=_parent(units))
    agent_calls = _patch_external(
        monkeypatch,
        [
            _agent_result(rent=30_000, p25=25_000, p75=35_000),
            _agent_result(rent=38_000, p25=33_000, p75=42_000),
        ],
    )

    bo.fan_out_unit_estimations(
        conn=object(),
        sreality_client=object(),
        llm_client=object(),
        building_run_id=7,
    )

    # Two child runs inserted, both successful.
    assert len(state["children"]) == 2
    assert all(c["status"] == "success" for c in state["children"].values())

    # Each child carries the building FKs + per-unit identifiers.
    by_unit = {c["building_unit_id"]: c for c in state["children"].values()}
    assert set(by_unit) == {"u1", "u2"}
    for c in by_unit.values():
        assert c["building_run_id"] == 7
        assert c["estimate_kind"] == "rent"
        assert c["mode"] == "agent"

    # Operator inputs + attachments flowed into every agent call.
    assert len(agent_calls) == 2
    for call in agent_calls:
        assert call["special_instructions"] == "Treat attic as habitable"
        assert call["contextual_text"] == "Owner says heating refurbished 2022"
        assert call["building_run_id"] == 7
        attachments = call["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["id"] == 901

    # Rollup landed on the parent.
    assert state["building"]["status"] == "success"
    assert state["building"]["total_rent_p25_czk"] == 25_000 + 33_000
    assert state["building"]["total_rent_p50_czk"] == 30_000 + 38_000
    assert state["building"]["total_rent_p75_czk"] == 35_000 + 42_000


def test_fan_out_tolerates_per_child_failure(monkeypatch):
    units = [
        {"unit_id": "u1", "area_m2": 60.0, "disposition": "2+kk"},
        {"unit_id": "u2", "area_m2": 80.0, "disposition": "3+kk"},
    ]
    state = _patch_persistence(monkeypatch, parent=_parent(units))
    _patch_external(
        monkeypatch,
        [
            RuntimeError("agent rate-limited"),
            _agent_result(rent=40_000, p25=36_000, p75=44_000),
        ],
    )

    bo.fan_out_unit_estimations(
        conn=object(),
        sreality_client=object(),
        llm_client=object(),
        building_run_id=7,
    )

    statuses = sorted(c["status"] for c in state["children"].values())
    assert statuses == ["failed", "success"]
    # Parent rolls up to success with just the surviving child.
    assert state["building"]["status"] == "success"
    assert state["building"]["total_rent_p50_czk"] == 40_000


def test_fan_out_skips_units_without_area(monkeypatch):
    units = [
        {"unit_id": "u1", "area_m2": None, "disposition": "2+kk"},
        {"unit_id": "u2", "area_m2": 60.0, "disposition": "2+kk"},
    ]
    state = _patch_persistence(monkeypatch, parent=_parent(units))
    agent_calls = _patch_external(
        monkeypatch,
        [_agent_result()],  # only one call expected (u2)
    )

    bo.fan_out_unit_estimations(
        conn=object(),
        sreality_client=object(),
        llm_client=object(),
        building_run_id=7,
    )

    # u1 created a 'failed' child without an agent call; u2 went through.
    by_unit = {c["building_unit_id"]: c for c in state["children"].values()}
    assert by_unit["u1"]["status"] == "failed"
    assert "area_m2" in (by_unit["u1"].get("error_message") or "").lower()
    assert by_unit["u2"]["status"] == "success"
    assert len(agent_calls) == 1
    # Parent still rolls up to success with the one surviving child.
    assert state["building"]["status"] == "success"


def test_fan_out_marks_parent_failed_when_no_units(monkeypatch):
    state = _patch_persistence(monkeypatch, parent=_parent([]))
    _patch_external(monkeypatch, [])

    bo.fan_out_unit_estimations(
        conn=object(),
        sreality_client=object(),
        llm_client=object(),
        building_run_id=7,
    )

    assert state["building"]["status"] == "failed"
    assert "no confirmed units" in (state["building"]["error_message"] or "")


def test_fan_out_marks_parent_failed_when_missing_latlng(monkeypatch):
    units = [{"unit_id": "u1", "area_m2": 60.0, "disposition": "2+kk"}]
    state = _patch_persistence(
        monkeypatch,
        parent=_parent(units, input_spec={"area_m2": 250.0}),  # no lat/lng
    )
    _patch_external(monkeypatch, [])

    bo.fan_out_unit_estimations(
        conn=object(),
        sreality_client=object(),
        llm_client=object(),
        building_run_id=7,
    )

    assert state["building"]["status"] == "failed"
    assert "lat" in (state["building"]["error_message"] or "").lower()
