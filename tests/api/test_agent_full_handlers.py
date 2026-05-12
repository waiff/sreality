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

    def fake(conn, target, filters, *, population, trend_split_days):
        captured["radius_m"] = filters.radius_m
        captured["population"] = population
        captured["trend_split_days"] = trend_split_days
        return {"data": {"cohort_size": 12, "tom_stats": {"median_days": 42}}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "compute_market_velocity", fake)
    state = _state()
    out = agent_mod._handle_compute_market_velocity(
        {"radius_m": 1500, "population": "delisted", "trend_split_days": 14}, state,
    )
    assert captured == {"radius_m": 1500, "population": "delisted", "trend_split_days": 14}
    assert out["data"]["cohort_size"] == 12


def test_market_velocity_defaults(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, target, filters, *, population, trend_split_days):
        captured["radius_m"] = filters.radius_m
        captured["population"] = population
        captured["trend_split_days"] = trend_split_days
        return {"data": {}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "compute_market_velocity", fake)
    state = _state()
    agent_mod._handle_compute_market_velocity({}, state)
    assert captured["radius_m"] == 1000  # from base_filters
    assert captured["population"] == "all"
    assert captured["trend_split_days"] == 7


# --- compute_listing_velocity --------------------------------------------

def test_listing_velocity_forwards_args(monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, sreality_id, *, radius_m, disposition_match, population):
        captured.update({
            "sreality_id": sreality_id,
            "radius_m": radius_m,
            "disposition_match": disposition_match,
            "population": population,
        })
        return {"data": {"classification": "stuck"}, "metadata": {}}

    monkeypatch.setattr(agent_mod, "compute_listing_velocity", fake)
    state = _state()
    out = agent_mod._handle_compute_listing_velocity(
        {
            "sreality_id": 12345,
            "radius_m": 800,
            "disposition_match": "loose",
            "population": "active",
        },
        state,
    )
    assert captured == {
        "sreality_id": 12345,
        "radius_m": 800,
        "disposition_match": "loose",
        "population": "active",
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
                    {"sreality_id": 100, "price_per_m2": 500},  # already in cohort
                    {"sreality_id": 200, "price_per_m2": 600},  # new
                    {"sreality_id": 300, "price_per_m2": 700},  # new
                ],
            },
            "metadata": {"result_count": 3, "lines_considered": 2},
        }

    monkeypatch.setattr(agent_mod, "find_comparables_along_axis", fake)
    state = _state(last_cohort=[
        {"sreality_id": 100, "price_per_m2": 500},
        {"sreality_id": 101, "price_per_m2": 510},
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
