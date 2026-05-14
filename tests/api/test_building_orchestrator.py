"""Hermetic tests for the Phase B2 per-unit fan-out + rollup.

Hits the orchestrator directly with monkeypatched persistence helpers
+ a stub `run_agent_estimation`. Verifies:

  - the orchestrator inserts one rent + one sale estimation_runs row
    per unit with building_run_id + building_unit_id populated;
  - operator inputs (special_instructions / contextual_text / attachments)
    flow into each child's agent call;
  - successful children's rent + sale percentiles roll up into the
    parent's total_rent_* / total_sale_* columns independently;
  - per-child failures don't break the fan-out; the parent transitions
    to 'success' as long as at least one child succeeded;
  - skipping a unit with no area_m2 marks both children 'failed';
  - missing sale skill falls back to rent-only fan-out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from api import building_orchestrator as bo
from api import building_runs as br
from api import estimation_runs as er


@dataclass
class _Skill:
    name: str = "rental_estimator_v1"
    preferred_model: dict[str, str] = field(
        default_factory=lambda: {"anthropic": "claude-sonnet-4-5"},
    )


def _agent_rent(*, rent: int = 30_000, p25: int = 25_000, p75: int = 35_000):
    from api.agent import AgentResult
    return AgentResult(
        data={
            "estimated_monthly_rent_czk": rent,
            "rent_p25_czk": p25,
            "rent_p75_czk": p75,
            "estimated_sale_price_czk": None,
            "sale_p25_czk": None,
            "sale_p75_czk": None,
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
            "estimate_kind": "rent",
        },
    )


def _agent_sale(*, sale: int = 8_000_000, p25: int = 7_200_000, p75: int = 9_000_000):
    from api.agent import AgentResult
    return AgentResult(
        data={
            "estimated_monthly_rent_czk": None,
            "rent_p25_czk": None,
            "rent_p75_czk": None,
            "estimated_sale_price_czk": sale,
            "sale_p25_czk": p25,
            "sale_p75_czk": p75,
            "confidence": "medium",
            "comparables_used": [{"sreality_id": 333, "snapshot_id": 444}],
            "warnings": [],
        },
        metadata={
            "stop_reason": "record_estimate",
            "iterations": 5,
            "provider": "anthropic",
            "skill": "sale_estimator_v1",
            "total_cost_usd": 0.09,
            "estimate_kind": "sale",
        },
    )


def _patch_persistence(monkeypatch, *, parent: dict[str, Any]):
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

    def fake_rollup(conn, building_run_id):
        rows = list(state["children"].values())

        def _sum_family(kind: str):
            p25 = median = p75 = 0
            n = 0
            for r in rows:
                if r.get("status") != "success" or r.get("estimate_kind") != kind:
                    continue
                if kind == "rent":
                    a, b, c = r.get("rent_p25_czk"), r.get("estimated_monthly_rent_czk"), r.get("rent_p75_czk")
                else:
                    a, b, c = r.get("sale_p25_czk"), r.get("estimated_sale_price_czk"), r.get("sale_p75_czk")
                if a is None or b is None or c is None:
                    continue
                p25 += int(a); median += int(b); p75 += int(c); n += 1
            return (p25, median, p75) if n else None

        rent = _sum_family("rent")
        sale = _sum_family("sale")
        fields: dict[str, Any] = {}
        if rent is not None:
            fields["total_rent_p25_czk"] = rent[0]
            fields["total_rent_p50_czk"] = rent[1]
            fields["total_rent_p75_czk"] = rent[2]
        if sale is not None:
            fields["total_sale_p25_czk"] = sale[0]
            fields["total_sale_p50_czk"] = sale[1]
            fields["total_sale_p75_czk"] = sale[2]
        if rent is None and sale is None:
            n_s = sum(1 for r in rows if r.get("status") == "success")
            n_f = sum(1 for r in rows if r.get("status") == "failed")
            fake_update_building(
                conn, parent["id"],
                status="failed",
                error_message=f"rollup: no successful child estimations with a numeric range (success={n_s}, failed={n_f})",
            )
            return
        fields["status"] = "success"
        fake_update_building(conn, parent["id"], **fields)

    monkeypatch.setattr(bo, "rollup_building_estimates", fake_rollup)
    return state


def _patch_external(
    monkeypatch,
    agent_outcomes: list[Any],
    *,
    sale_skill_available: bool = True,
):
    """Stub list_attachments, _resolve_default_skill, load_skill,
    run_agent_estimation."""
    calls: list[dict[str, Any]] = []

    def fake_list_attachments(conn, building_run_id):
        return [
            {"id": 901, "filename": "plan.png", "mime_type": "image/png",
             "storage_key": "k", "building_run_id": building_run_id},
        ]

    def fake_resolve_skill(conn, key, fallback):
        if key == bo._DEFAULT_SALE_SKILL_KEY:
            return "sale_estimator_v1" if sale_skill_available else "missing_sale"
        return "rental_estimator_v1"

    def fake_load_skill(conn, name):
        from api.skills import SkillNotFound
        if name == "missing_sale":
            raise SkillNotFound(f"skill {name!r} not found")
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


def test_fan_out_emits_rent_and_sale_per_unit(monkeypatch):
    units = [
        {"unit_id": "u1", "area_m2": 60.0, "disposition": "2+kk", "floor": "1"},
        {"unit_id": "u2", "area_m2": 75.0, "disposition": "3+kk", "floor": "2"},
    ]
    state = _patch_persistence(monkeypatch, parent=_parent(units))
    agent_calls = _patch_external(
        monkeypatch,
        [
            # Unit 1: rent then sale
            _agent_rent(rent=30_000, p25=25_000, p75=35_000),
            _agent_sale(sale=8_000_000, p25=7_200_000, p75=9_000_000),
            # Unit 2: rent then sale
            _agent_rent(rent=38_000, p25=33_000, p75=42_000),
            _agent_sale(sale=10_000_000, p25=9_000_000, p75=11_500_000),
        ],
    )

    bo.fan_out_unit_estimations(
        conn=object(),
        sreality_client=object(),
        llm_client=object(),
        building_run_id=7,
    )

    # Four child runs: 2 units × 2 kinds.
    assert len(state["children"]) == 4
    assert all(c["status"] == "success" for c in state["children"].values())

    # Children carry the right kind + FK linkage.
    by_unit_kind = {(c["building_unit_id"], c["estimate_kind"]): c for c in state["children"].values()}
    assert set(by_unit_kind) == {
        ("u1", "rent"), ("u1", "sale"),
        ("u2", "rent"), ("u2", "sale"),
    }
    for c in state["children"].values():
        assert c["building_run_id"] == 7
        assert c["mode"] == "agent"

    # Each agent call carried operator inputs + attachments and the right kind.
    assert len(agent_calls) == 4
    kinds_seen = [c["estimate_kind"] for c in agent_calls]
    assert kinds_seen.count("rent") == 2
    assert kinds_seen.count("sale") == 2
    for call in agent_calls:
        assert call["special_instructions"] == "Treat attic as habitable"
        assert call["contextual_text"] == "Owner says heating refurbished 2022"
        assert call["building_run_id"] == 7
        assert len(call["attachments"]) == 1
        assert call["attachments"][0]["id"] == 901

    # Rollup: rent + sale summed independently.
    assert state["building"]["status"] == "success"
    assert state["building"]["total_rent_p25_czk"] == 25_000 + 33_000
    assert state["building"]["total_rent_p50_czk"] == 30_000 + 38_000
    assert state["building"]["total_rent_p75_czk"] == 35_000 + 42_000
    assert state["building"]["total_sale_p25_czk"] == 7_200_000 + 9_000_000
    assert state["building"]["total_sale_p50_czk"] == 8_000_000 + 10_000_000
    assert state["building"]["total_sale_p75_czk"] == 9_000_000 + 11_500_000


def test_fan_out_tolerates_per_child_failure(monkeypatch):
    units = [
        {"unit_id": "u1", "area_m2": 60.0, "disposition": "2+kk"},
    ]
    state = _patch_persistence(monkeypatch, parent=_parent(units))
    _patch_external(
        monkeypatch,
        [
            RuntimeError("rent agent rate-limited"),
            _agent_sale(sale=7_500_000, p25=6_800_000, p75=8_300_000),
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
    # Parent rolls up to success with the surviving sale child only.
    assert state["building"]["status"] == "success"
    assert state["building"].get("total_rent_p50_czk") is None
    assert state["building"]["total_sale_p50_czk"] == 7_500_000


def test_fan_out_falls_back_to_rent_only_when_sale_skill_missing(monkeypatch):
    units = [
        {"unit_id": "u1", "area_m2": 60.0, "disposition": "2+kk"},
    ]
    state = _patch_persistence(monkeypatch, parent=_parent(units))
    agent_calls = _patch_external(
        monkeypatch,
        [_agent_rent()],
        sale_skill_available=False,
    )

    bo.fan_out_unit_estimations(
        conn=object(),
        sreality_client=object(),
        llm_client=object(),
        building_run_id=7,
    )

    # Only one agent call (rent); no sale child created.
    assert len(agent_calls) == 1
    assert agent_calls[0]["estimate_kind"] == "rent"
    assert len(state["children"]) == 1
    assert state["building"]["status"] == "success"
    assert state["building"].get("total_sale_p50_czk") is None


def test_fan_out_skips_units_without_area(monkeypatch):
    units = [
        {"unit_id": "u1", "area_m2": None, "disposition": "2+kk"},
        {"unit_id": "u2", "area_m2": 60.0, "disposition": "2+kk"},
    ]
    state = _patch_persistence(monkeypatch, parent=_parent(units))
    agent_calls = _patch_external(
        monkeypatch,
        [_agent_rent(), _agent_sale()],
    )

    bo.fan_out_unit_estimations(
        conn=object(),
        sreality_client=object(),
        llm_client=object(),
        building_run_id=7,
    )

    by_unit_kind = {(c["building_unit_id"], c["estimate_kind"]): c for c in state["children"].values()}
    # u1's two children are both 'failed' (no area_m2) and the agent
    # was never called for them.
    assert by_unit_kind[("u1", "rent")]["status"] == "failed"
    assert by_unit_kind[("u1", "sale")]["status"] == "failed"
    # u2's two children both went through the agent and succeeded.
    assert by_unit_kind[("u2", "rent")]["status"] == "success"
    assert by_unit_kind[("u2", "sale")]["status"] == "success"
    assert len(agent_calls) == 2
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
        parent=_parent(units, input_spec={"area_m2": 250.0}),
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
