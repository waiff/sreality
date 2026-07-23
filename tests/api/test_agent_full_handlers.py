"""Hermetic tests for the slice-1.5 agent handlers.

Each test directly invokes one of the new handler functions in
`api.agent` with a stubbed toolkit module, asserting the dispatch
shape (argument routing, state mutation, error paths). The full
loop is already covered by `test_agent.py` against the slice-1
tool subset.
"""

from __future__ import annotations

from typing import Any

import pytest

from api import agent as agent_mod
from toolkit.comparables import ComparableFilters, TargetSpec
from tests.api._fakes import _FakeConn


def _state(
    *,
    last_cohort: list[dict[str, Any]] | None = None,
    base_filters: ComparableFilters | None = None,
) -> agent_mod._LoopState:
    return agent_mod._LoopState(
        conn=_FakeConn(app_settings={}),
        sreality_client=None,  # type: ignore[arg-type]
        llm_client=None,  # type: ignore[arg-type]
        target=TargetSpec(lat=50.08, lng=14.43, area_m2=60.0, disposition="2+kk"),
        base_filters=base_filters or ComparableFilters(radius_m=1000, max_age_days=14),
        last_cohort=list(last_cohort or []),
    )


# --- compute_market_velocity ---------------------------------------------

def test_market_velocity_forwards_radius_and_population(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, target, filters, *, lifecycle, trend_split_days):
        captured["radius_m"] = filters.radius_m
        captured["lifecycle"] = lifecycle
        captured["trend_split_days"] = trend_split_days
        return {"data": {"cohort_size": 12, "tom_stats": {"median_days": 42}}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "compute_market_velocity", fake)
    state = _state()
    out = agent_mod._handle_compute_market_velocity(
        {"radius_m": 1500, "lifecycle": "delisted", "trend_split_days": 14}, state,
    )
    assert captured == {"radius_m": 1500, "lifecycle": "delisted", "trend_split_days": 14}
    assert out["data"]["cohort_size"] == 12


def test_market_velocity_defaults(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, target, filters, *, lifecycle, trend_split_days):
        captured["radius_m"] = filters.radius_m
        captured["lifecycle"] = lifecycle
        captured["trend_split_days"] = trend_split_days
        return {"data": {}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "compute_market_velocity", fake)
    state = _state()
    agent_mod._handle_compute_market_velocity({}, state)
    assert captured["radius_m"] == 1000  # from base_filters
    assert captured["lifecycle"] == "all"
    assert captured["trend_split_days"] == 7


# --- compute_listing_velocity --------------------------------------------

def test_listing_velocity_forwards_args(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, sreality_id, *, radius_m, disposition_match, lifecycle):
        captured.update({
            "sreality_id": sreality_id,
            "radius_m": radius_m,
            "disposition_match": disposition_match,
            "lifecycle": lifecycle,
        })
        return {"data": {"classification": "stuck"}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "compute_listing_velocity", fake)
    state = _state()
    out = agent_mod._handle_compute_listing_velocity(
        {
            "sreality_id": 12345,
            "radius_m": 800,
            "disposition_match": "loose",
            "lifecycle": "active",
        },
        state,
    )
    assert captured == {
        "sreality_id": 12345,
        "radius_m": 800,
        "disposition_match": "loose",
        "lifecycle": "active",
    }
    assert out["data"]["classification"] == "stuck"


# --- compute_walkability + compute_amenity_supply ------------------------

def test_walkability_forwards_target_coords(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, *, lat, lng, radius_m):
        captured.update(lat=lat, lng=lng, radius_m=radius_m)
        return {
            "data": {"walkability_score": 72, "missing_categories": []},
            "metadata": {"result_count": 8},
        }

    monkeypatch.setattr(agent_mod, "compute_walkability", fake)
    state = _state()
    out = agent_mod._handle_compute_walkability({"radius_m": 800}, state)
    assert captured == {"lat": 50.08, "lng": 14.43, "radius_m": 800}
    assert out["data"]["walkability_score"] == 72


def test_amenity_supply_default_radius(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, *, lat, lng, radius_m):
        captured.update(lat=lat, lng=lng, radius_m=radius_m)
        return {"data": {"summary": {"scarce": [], "adequate": [], "abundant": []}}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "compute_amenity_supply", fake)
    state = _state()
    agent_mod._handle_compute_amenity_supply({}, state)
    assert captured == {"lat": 50.08, "lng": 14.43, "radius_m": 1000}


# --- find_comparables_along_axis (merge into cohort) ---------------------

def test_axis_merges_new_listings_into_cohort(monkeypatch):
    def fake(conn, target, filters, *, transport_types, anchor_radius_m, corridor_m, **kwargs):
        return {
            "data": {
                "listings": [
                    {"listing_id": 900100, "sreality_id": 100, "price_per_m2": 500},  # already in cohort
                    {"listing_id": 900200, "sreality_id": 200, "price_per_m2": 600},  # new
                    {"listing_id": 900300, "sreality_id": 300, "price_per_m2": 700},  # new
                ],
            },
            "metadata": {"result_count": 3, "lines_considered": 2},
        }

    monkeypatch.setattr(agent_mod, "find_comparables_along_axis", fake)
    state = _state(last_cohort=[
        {"listing_id": 900100, "sreality_id": 100, "price_per_m2": 500},
        {"listing_id": 900101, "sreality_id": 101, "price_per_m2": 510},
    ])
    out = agent_mod._handle_find_comparables_along_axis(
        {"transport_types": ["tram"], "anchor_radius_m": 600, "corridor_m": 250},
        state,
    )

    ids = sorted(int(l["sreality_id"]) for l in state.last_cohort)
    assert ids == [100, 101, 200, 300]
    assert out["data"]["cohort_added"] == 2
    assert out["data"]["cohort_size_after_merge"] == 4


def test_axis_handler_uses_defaults_when_args_missing(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, target, filters, *, transport_types, anchor_radius_m, corridor_m, **kwargs):
        captured.update(
            transport_types=transport_types,
            anchor_radius_m=anchor_radius_m,
            corridor_m=corridor_m,
        )
        return {"data": {"listings": []}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "find_comparables_along_axis", fake)
    state = _state()
    agent_mod._handle_find_comparables_along_axis({}, state)
    assert captured["transport_types"] is None
    assert captured["anchor_radius_m"] == 800
    assert captured["corridor_m"] == 300


# --- summarize_listing ---------------------------------------------------

def test_summarize_listing_forwards_id(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, llm_client, *, sreality_id):
        captured["sreality_id"] = sreality_id
        return {
            "data": {
                "sreality_id": sreality_id,
                "summary": {
                    "headline": "Bright 2+kk in Vinohrady",
                    "key_highlights": ["balcony", "lift"],
                    "concerns": [],
                    "condition_assessment": "good",
                },
                "cache_hit": True,
            },
            "metadata": {},
        }

    monkeypatch.setattr(agent_mod, "summarize_listing", fake)
    state = _state()
    out = agent_mod._handle_summarize_listing({"sreality_id": 999}, state)
    assert captured == {"sreality_id": 999}
    assert out["data"]["summary"]["headline"].startswith("Bright")


# --- compare_listing_images (cohort gate) --------------------------------

def test_compare_images_requires_both_ids_in_cohort(monkeypatch):
    monkeypatch.setattr(
        agent_mod, "compare_listing_images",
        lambda *a, **kw: pytest.fail("toolkit must not be called when gate fails"),
    )
    state = _state(last_cohort=[{"sreality_id": 100}, {"sreality_id": 101}])
    with pytest.raises(ValueError) as excinfo:
        agent_mod._handle_compare_listing_images(
            {"sreality_id_a": 100, "sreality_id_b": 999}, state,
        )
    assert "999" in str(excinfo.value)


def test_compare_images_dispatches_when_pair_in_cohort(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, llm_client, *, sreality_id_a, sreality_id_b, n_images):
        captured.update(a=sreality_id_a, b=sreality_id_b, n_images=n_images)
        return {
            "data": {
                "sreality_id_a": sreality_id_a,
                "sreality_id_b": sreality_id_b,
                "comparison": {"overall_similarity": "moderate"},
                "cache_hit": False,
            },
            "metadata": {},
        }

    monkeypatch.setattr(agent_mod, "compare_listing_images", fake)
    state = _state(last_cohort=[{"sreality_id": 100}, {"sreality_id": 101}])
    out = agent_mod._handle_compare_listing_images(
        {"sreality_id_a": 100, "sreality_id_b": 101, "n_images": 4},
        state,
    )
    assert captured == {"a": 100, "b": 101, "n_images": 4}
    assert out["data"]["comparison"]["overall_similarity"] == "moderate"


# --- _tool_summary cases for the new tools -------------------------------

def test_tool_summary_market_velocity():
    out = agent_mod._tool_summary(
        "compute_market_velocity",
        {
            "data": {
                "cohort_size": 12, "active_count": 7, "delisted_count": 5,
                "tom_stats": {"median_days": 42.0, "p75_days": 90.0},
            },
            "metadata": {},
        },
    )
    assert out["cohort_size"] == 12
    assert out["median_tom_days"] == 42.0
    assert out["p75_tom_days"] == 90.0


def test_tool_summary_walkability():
    out = agent_mod._tool_summary(
        "compute_walkability",
        {
            "data": {"walkability_score": 78, "missing_categories": ["metro_station"]},
            "metadata": {"result_count": 7},
        },
    )
    assert out["walkability_score"] == 78
    assert out["missing_categories"] == ["metro_station"]
    assert out["n_categories_with_data"] == 7


def test_tool_summary_axis():
    out = agent_mod._tool_summary(
        "find_comparables_along_axis",
        {
            "data": {"cohort_added": 4, "cohort_size_after_merge": 14},
            "metadata": {"result_count": 6, "lines_considered": 3},
        },
    )
    assert out == {
        "axis_listings": 6,
        "lines_considered": 3,
        "cohort_added": 4,
        "cohort_size_after_merge": 14,
    }


def test_tool_summary_compare_images():
    out = agent_mod._tool_summary(
        "compare_listing_images",
        {
            "data": {
                "sreality_id_a": 100, "sreality_id_b": 101,
                "comparison": {"overall_similarity": "low"},
                "cache_hit": True,
            },
            "metadata": {},
        },
    )
    assert out["overall_similarity"] == "low"
    assert out["cache_hit"] is True


# --- Gate-2: handlers accept the surrogate listing_id --------------------

def test_listing_velocity_handler_forwards_listing_id(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, sreality_id=None, *, listing_id=None, radius_m, disposition_match, lifecycle):
        captured.update(sreality_id=sreality_id, listing_id=listing_id, radius_m=radius_m)
        return {"data": {"classification": "fast"}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "compute_listing_velocity", fake)
    state = _state()
    agent_mod._handle_compute_listing_velocity({"listing_id": 555, "radius_m": 900}, state)
    assert captured["listing_id"] == 555
    assert captured["sreality_id"] is None
    assert captured["radius_m"] == 900


def test_summarize_handler_forwards_listing_id(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, llm_client, *, sreality_id=None, listing_id=None):
        captured.update(sreality_id=sreality_id, listing_id=listing_id)
        return {"data": {}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "summarize_listing", fake)
    agent_mod._handle_summarize_listing({"listing_id": 777}, _state())
    assert captured == {"sreality_id": None, "listing_id": 777}


def test_handler_listing_id_wins_when_both_supplied(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, llm_client, *, sreality_id=None, listing_id=None):
        captured.update(sreality_id=sreality_id, listing_id=listing_id)
        return {"data": {}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "summarize_listing", fake)
    # The id-spaces overlap numerically; the surrogate is authoritative and we
    # do NOT cross-resolve — the sreality_id must simply be dropped.
    agent_mod._handle_summarize_listing({"sreality_id": 1, "listing_id": 2}, _state())
    assert captured == {"sreality_id": None, "listing_id": 2}


def test_handler_neither_id_raises_value_error():
    with pytest.raises(ValueError):
        agent_mod._handle_summarize_listing({}, _state())


def test_compare_images_handler_dispatches_by_listing_id(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, llm_client, *, listing_id_a, listing_id_b, n_images):
        captured.update(a=listing_id_a, b=listing_id_b, n_images=n_images)
        return {"data": {"cache_hit": False}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "compare_listing_images", fake)
    state = _state(last_cohort=[
        {"listing_id": 100, "sreality_id": 11},
        {"listing_id": 200, "sreality_id": 22},
    ])
    agent_mod._handle_compare_listing_images(
        {"listing_id_a": 100, "listing_id_b": 200, "n_images": 4}, state,
    )
    assert captured == {"a": 100, "b": 200, "n_images": 4}


def test_compare_images_listing_id_cohort_gate(monkeypatch):
    monkeypatch.setattr(
        agent_mod, "compare_listing_images",
        lambda *a, **k: pytest.fail("toolkit must not be called when gate fails"),
    )
    state = _state(last_cohort=[
        {"listing_id": 100, "sreality_id": 11},
        {"listing_id": 200, "sreality_id": None},  # NULL sreality still nameable by surrogate
    ])
    with pytest.raises(ValueError, match="999"):
        agent_mod._handle_compare_listing_images(
            {"listing_id_a": 100, "listing_id_b": 999}, state,
        )


def test_compare_images_handler_requires_both_listing_ids():
    with pytest.raises(ValueError, match="BOTH"):
        agent_mod._handle_compare_listing_images(
            {"listing_id_a": 100}, _state(last_cohort=[{"listing_id": 100}]),
        )


def test_per_listing_tool_schemas_accept_listing_id():
    """Every per-listing tool schema now takes listing_id and enforces
    at-least-one in the handler (required is empty)."""
    for name in (
        "verify_listing_freshness",
        "compute_listing_velocity",
        "summarize_listing",
        "get_manual_rental_estimates",
    ):
        schema = agent_mod.AGENT_TOOLS[name].input_schema
        assert "listing_id" in schema["properties"], name
        assert "sreality_id" in schema["properties"], name
        assert schema["required"] == [], name
    compare = agent_mod.AGENT_TOOLS["compare_listing_images"].input_schema
    assert {"sreality_id_a", "sreality_id_b", "listing_id_a", "listing_id_b"} <= set(
        compare["properties"].keys()
    )
    assert compare["required"] == []


# --- registry sanity check -----------------------------------------------

def test_all_new_tools_registered():
    """Catch regressions if _build_tool_registry drifts."""
    expected = {
        "compute_market_velocity",
        "compute_listing_velocity",
        "compute_walkability",
        "compute_amenity_supply",
        "find_comparables_along_axis",
        "summarize_listing",
        "compare_listing_images",
    }
    assert expected <= set(agent_mod.AGENT_TOOLS.keys())
    for name in expected:
        td = agent_mod.AGENT_TOOLS[name]
        assert td.handler is not None
        assert not td.is_terminator


# --- finalisation persists the SURROGATE, not the legacy handle ------------
#
# estimation_cohort_entries is keyed on listing_id, but the agent addresses
# comparables by sreality_id. The two id spaces are disjoint in production
# (0 of 566,812 listings have id = sreality_id) yet overlap numerically, so
# feeding the wrong one silently updates the wrong rows — or none at all —
# instead of raising. These pin the translation at the boundary.

class _Skill:
    name = "test-skill"


def _finalise_state(cohort, *, decisions=None, run_id=7):
    state = _state(last_cohort=cohort)
    state.estimation_run_id = run_id
    state.final_call = {
        "estimated_monthly_rent_czk": 20_000,
        "rent_p25_czk": 18_000,
        "rent_p75_czk": 22_000,
        "confidence": "high",
        "comparable_decisions": decisions or [],
    }
    return state


def _run_finalise(monkeypatch, cohort, *, decisions=None):
    from api.estimate_yield import _used_entry

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        agent_mod, "_persist_finalisation",
        lambda state, **kw: captured.update(kw),
    )
    result = agent_mod._finalise(
        _finalise_state(cohort, decisions=decisions),
        skill=_Skill(),
        provider="anthropic",
        stop_reason="record_estimate",
        purchase_price_czk=None,
        used_entry=_used_entry,
    )
    return captured, result


def test_finalise_persists_listing_ids_not_sreality_ids(monkeypatch):
    """The live regression: #879 moved _persist_finalisation's SQL onto
    listing_id but left every caller building the id set from sreality_id, so
    the UPDATE matched nothing and present_at_finalisation silently stopped
    being written."""
    cohort = [
        {"listing_id": 9001, "sreality_id": 111},
        {"listing_id": 9002, "sreality_id": 222},
    ]
    captured, _ = _run_finalise(monkeypatch, cohort)
    assert captured["included_listing_ids"] == {9001, 9002}
    # the legacy handles must NOT reach the persistence layer
    assert not ({111, 222} & captured["included_listing_ids"])


def test_finalise_keeps_null_sreality_row_in_the_persisted_set(monkeypatch):
    """Post-Gate-2 a new non-sreality listing has sreality_id NULL. It must
    neither raise int(None) nor silently drop out of present_at_finalisation —
    default-include is computed over the surrogate set for exactly this reason."""
    cohort = [
        {"listing_id": 9001, "sreality_id": 111},
        {"listing_id": 9002, "sreality_id": None},
    ]
    captured, result = _run_finalise(monkeypatch, cohort)
    assert captured["included_listing_ids"] == {9001, 9002}
    assert len(result.data["comparables_used"]) == 2


def test_finalise_translates_agent_exclusion_into_surrogate_space(monkeypatch):
    """The agent excludes by sreality_id; persistence must receive listing_id."""
    cohort = [
        {"listing_id": 9001, "sreality_id": 111},
        {"listing_id": 9002, "sreality_id": 222},
    ]
    decisions = [
        {"sreality_id": 222, "decision": "excluded", "reason": "different street"},
    ]
    captured, result = _run_finalise(monkeypatch, cohort, decisions=decisions)
    assert captured["excluded_by_listing_id"] == {9002: "different street"}
    assert captured["included_listing_ids"] == {9001}
    # the operator-facing payload still speaks the agent's id space
    assert result.data["comparables_excluded"] == [
        {"sreality_id": 222, "reason": "different street"},
    ]


def test_record_selection_summary_tolerates_null_sreality_id(monkeypatch):
    """Runs after the estimate is already paid for and outside the per-tool
    try/except, so an int(None) here would discard a finished run."""
    captured: dict[str, Any] = {}

    class _H:
        def set_summary(self, s): captured.update(s)

    class _Ctx:
        def __enter__(self): return _H()
        def __exit__(self, *a): return None

    class _Rec:
        def computation(self, _name): return _Ctx()

    result = agent_mod.AgentResult(
        data={"comparables_used": [
            {"listing_id": 9001, "sreality_id": 111},
            {"listing_id": 9002, "sreality_id": None},
        ]},
        metadata={},
    )
    agent_mod._record_selection_summary(
        _Rec(), _state(last_cohort=[]), result, "record_estimate",
    )
    # legacy field keeps its id space (readers expect sreality_ids); the
    # surrogate field beside it stays complete
    assert captured["final_comparable_ids"] == [111]
    assert captured["final_comparable_listing_ids"] == [9001, 9002]


def test_compare_images_cohort_gate_tolerates_null_sreality_id(monkeypatch):
    """A NULL-sreality cohort row must not crash the membership check; it is
    simply not nameable by a tool whose schema takes sreality_ids."""
    monkeypatch.setattr(
        agent_mod, "compare_listing_images",
        lambda *a, **k: {"data": {"verdict": "High"}},
    )
    state = _state(last_cohort=[
        {"sreality_id": 100}, {"sreality_id": None},
    ])
    out = agent_mod._handle_compare_listing_images(
        {"sreality_id_a": 100, "sreality_id_b": 100}, state,
    )
    assert out["data"]["verdict"] == "High"
    with pytest.raises(ValueError, match="not in the current"):
        agent_mod._handle_compare_listing_images(
            {"sreality_id_a": 100, "sreality_id_b": 999}, state,
        )
