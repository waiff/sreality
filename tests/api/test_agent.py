"""End-to-end agent loop tests.

Hermetic. Drives `run_agent_estimation` with a `_ScriptedProvider`
(see tests/api/_fakes.py). The toolkit functions invoked by the
loop are patched per-test via monkeypatch so we never touch the
real DB or sreality.cz.

Three required cases, each parameterised over the two providers:

1. Happy path — find -> analyze -> record. Asserts trace shape,
   metadata.stop_reason, iteration count, and llm_calls attribution.
2. Iteration cap — provider loops on the same tool call; asserts
   termination at `max_iterations` and null estimate.
3. Cost cap — provider returns one expensive turn; asserts
   termination after turn 1 and that no second complete() call was
   made.

Plus one unit test for `TraceRecorder.reasoning(...)` step shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from api import agent as agent_mod
from api.estimation_runs import TraceRecorder
from api.llm_client import LLMClient
from api.providers import (
    Completion,
    ModelPrice,
    TextBlock,
    ToolCall,
    Usage,
)
from api.skills import Skill, SkillLimits
from toolkit.comparables import ComparableFilters, TargetSpec
from toolkit.measures import MeasureBasisError
from tests.api._fakes import _FakeConn, _ScriptedProvider


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_skill(
    *,
    max_iterations: int = 12,
    max_cost_usd: float = 1.0,
    wall_clock_timeout_s: float = 120.0,
) -> Skill:
    return Skill(
        name="rental_estimator_v1",
        description="test",
        system_prompt="be terse",
        allowed_tools=[
            "find_comparables_relaxed",
            "analyze_distribution",
            "record_estimate",
        ],
        preferred_model={"anthropic": "claude-sonnet-4-5", "gemini": "gemini-2.5-pro"},
        limits=SkillLimits(
            max_iterations=max_iterations,
            max_cost_usd=max_cost_usd,
            wall_clock_timeout_s=wall_clock_timeout_s,
        ),
    )


def _target() -> TargetSpec:
    return TargetSpec(lat=50.08, lng=14.43, area_m2=60.0, disposition="2+kk")


def _filters() -> ComparableFilters:
    return ComparableFilters(radius_m=1000, max_age_days=14)


def _completion_with_text(text: str) -> Completion:
    return Completion(
        text_blocks=[text],
        tool_calls=[],
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5),
        model="m",
    )


def _completion_with_tool(
    name: str, args: dict[str, Any], *, text: str = "",
    input_tokens: int = 10, output_tokens: int = 5,
) -> Completion:
    return Completion(
        text_blocks=[text] if text else [],
        tool_calls=[ToolCall(id=name + "_1", name=name, input=args)],
        stop_reason="tool_use",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        model="m",
    )


def _cohort_envelope(
    basis: str | None = "rent_monthly_czk_m2",
) -> dict[str, Any]:
    """A rental cohort, carrying the LABEL beside every number.

    `price_per_m2_basis` is not decoration: find_comparables projects the
    measure and its label together, so a fixture that drops the label is a
    cohort that cannot be scaled into anything (`_finalise` refuses it, exactly
    as it would refuse a real number nobody can name).
    """
    return {
        "data": {
            "listings": [
                {
                    "listing_id": 900100, "sreality_id": 100, "price_czk": 30000, "area_m2": 60,
                    "price_per_m2": 500, "price_per_m2_basis": basis,
                    "latest_snapshot_id": 1,
                },
                {
                    "listing_id": 900101, "sreality_id": 101, "price_czk": 32000, "area_m2": 60,
                    "price_per_m2": 533, "price_per_m2_basis": basis,
                    "latest_snapshot_id": 2,
                },
                {
                    "listing_id": 900102, "sreality_id": 102, "price_czk": 28000, "area_m2": 60,
                    "price_per_m2": 467, "price_per_m2_basis": basis,
                    "latest_snapshot_id": 3,
                },
            ],
            "relaxation_trace": [],
        },
        "metadata": {"result_count": 3},
    }


def _distribution_envelope() -> dict[str, Any]:
    return {
        "data": {
            "n": 3, "median": 500.0, "p25": 467.0, "p75": 533.0,
            "mean": 500.0, "stdev": 33.0,
        },
        "metadata": {"filters_used": {"field": "price_per_m2"}},
    }


def _patch_toolkit(monkeypatch):
    """Patch the toolkit calls the agent loop dispatches into."""
    monkeypatch.setattr(
        agent_mod, "find_comparables_relaxed",
        lambda conn, target, filters, **kw: _cohort_envelope(),
    )
    monkeypatch.setattr(
        agent_mod, "analyze_distribution",
        lambda listings, field="price_per_m2": _distribution_envelope(),
    )
    monkeypatch.setattr(
        agent_mod, "find_distribution_outliers",
        lambda conn, listings, **kw: {"data": {"n": 3, "outliers": []}, "metadata": {}},
    )
    monkeypatch.setattr(
        agent_mod, "describe_neighborhood",
        # The keys the tool ACTUALLY publishes (`active_listing_count`, a
        # per-disposition block). The old spelling was the one `_tool_summary`
        # read for months and always got None from.
        lambda conn, **kw: {
            "data": {
                "active_listing_count": 50,
                "price_stats_by_disposition": {
                    "2+kk": {
                        "n": 12, "median_price_per_m2": 510,
                        "price_per_m2_basis": "rent_monthly_czk_m2",
                    },
                },
            },
            "metadata": {},
        },
    )
    monkeypatch.setattr(
        agent_mod, "verify_listing_freshness",
        lambda conn, client, sreality_id, max_age_hours=24: {
            "data": {"is_live": True, "from_cache": False}, "metadata": {},
        },
    )


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider_name", ["anthropic", "gemini"])
def test_happy_path_records_estimate(monkeypatch, provider_name):
    _patch_toolkit(monkeypatch)
    conn = _FakeConn(app_settings={})

    completions = [
        _completion_with_tool(
            "find_comparables_relaxed",
            {"radius_m": 1000, "min_results": 5},
            text="Going to start broad with a 1km radius.",
        ),
        _completion_with_tool(
            "analyze_distribution",
            {"field": "price_per_m2"},
            text="Three comparables; let's see the distribution.",
        ),
        _completion_with_tool(
            "record_estimate",
            {
                "estimated_monthly_rent_czk": 30000,
                "rent_p25_czk": 28000,
                "rent_p75_czk": 32000,
                "confidence": "medium",
                "comparables_used": [100, 101, 102],
                "warnings": [],
            },
            text="Cohort is tight; committing the estimate.",
        ),
    ]
    prov = _ScriptedProvider(
        provider_name, completions,
        prices={"claude-sonnet-4-5": ModelPrice(3.0, 15.0), "gemini-2.5-pro": ModelPrice(1.25, 10.0)},
    )
    client = LLMClient(conn, providers={provider_name: prov})
    recorder = TraceRecorder()

    result = agent_mod.run_agent_estimation(
        conn, sreality_client=None, llm_client=client,
        target=_target(), filters=_filters(),
        purchase_price_czk=6_000_000,
        skill=_make_skill(), provider=provider_name,
        recorder=recorder, estimation_run_id=42,
    )

    assert result.metadata["stop_reason"] == "record_estimate"
    assert result.metadata["iterations"] == 3
    assert result.metadata["provider"] == provider_name
    assert result.data["estimated_monthly_rent_czk"] == 30000
    assert result.data["rent_p25_czk"] == 28000
    assert result.data["rent_p75_czk"] == 32000
    assert result.data["confidence"] == "medium"
    assert result.data["gross_yield_pct"] == pytest.approx(6.0)
    assert len(result.data["comparables_used"]) == 3

    trace = recorder.to_dict("ok")
    kinds = [s["kind"] for s in trace["steps"]]
    # First step is the `skill_choice` computation emitted before
    # the loop runs (audit: which skill/provider/model was used).
    # Then reasoning + tool_call per turn × 3 turns; the terminator
    # has its own tool_call step. The final `computation` step is
    # the v2-trace comparable_selection_summary emitted after the
    # loop.
    assert kinds == [
        "computation",
        "reasoning", "tool_call",
        "reasoning", "tool_call",
        "reasoning", "tool_call",
        "computation",
    ]
    assert trace["steps"][0]["label"] == "skill_choice"
    assert trace["steps"][0]["output_summary"]["provider"] == provider_name
    summary_step = trace["steps"][-1]
    assert summary_step["label"] == "comparable_selection_summary"
    assert summary_step["output_summary"]["n_rounds"] == 1
    assert summary_step["output_summary"]["final_comparable_ids"] == [100, 101, 102]
    assert summary_step["output_summary"]["rounds"][0]["filters"]["radius_m"] == 1000
    # provider attribution on every llm_calls row
    assert all(
        row["params"][1] == provider_name for row in conn.llm_calls_rows
    )
    assert all(
        row["params"][9] == 42 for row in conn.llm_calls_rows
    )


# ---------------------------------------------------------------------------
# the per-m² basis gate (agent mode)
# ---------------------------------------------------------------------------

def _run_to_terminator(monkeypatch, *, cohort_basis_value: str | None):
    """Drive one full loop whose cohort carries `cohort_basis_value`."""
    monkeypatch.setattr(
        agent_mod, "find_comparables_relaxed",
        lambda conn, target, filters, **kw: _cohort_envelope(cohort_basis_value),
    )
    monkeypatch.setattr(
        agent_mod, "analyze_distribution",
        lambda listings, field="price_per_m2": _distribution_envelope(),
    )
    conn = _FakeConn(app_settings={})
    completions = [
        _completion_with_tool(
            "find_comparables_relaxed", {"radius_m": 1000, "min_results": 5},
        ),
        _completion_with_tool(
            "record_estimate",
            {
                "estimated_monthly_rent_czk": 30000,
                "rent_p25_czk": 28000,
                "rent_p75_czk": 32000,
                "confidence": "medium",
                "comparables_used": [100, 101, 102],
                "warnings": [],
            },
        ),
    ]
    prov = _ScriptedProvider(
        "anthropic", completions,
        prices={"claude-sonnet-4-5": ModelPrice(3.0, 15.0)},
    )
    return agent_mod.run_agent_estimation(
        conn, sreality_client=None, llm_client=LLMClient(conn, providers={"anthropic": prov}),
        target=_target(), filters=_filters(),
        purchase_price_czk=None,
        skill=_make_skill(), provider="anthropic",
        recorder=TraceRecorder(), estimation_run_id=42,
    )


def test_a_sale_cohort_cannot_be_recorded_as_a_monthly_rent(monkeypatch):
    """The defect this gate exists for.

    `category_type` is in `_FCR_OVERRIDE_FIELDS`, so an agent widening a thin
    rental cohort may pin `prodej` mid-run. The MODEL then multiplies a purchase
    Kč/m² by the area and reports it through `record_estimate` — the agent never
    calls `estimate_yield._scale`, so nothing else refuses it, and the run would
    land `status='success'` with a sale price labelled as a monthly rent.
    """
    with pytest.raises(MeasureBasisError, match="cannot be scaled"):
        _run_to_terminator(monkeypatch, cohort_basis_value="sale_capital_czk_m2")


def test_a_mixed_cohort_cannot_be_recorded_at_all(monkeypatch):
    monkeypatch.setattr(
        agent_mod, "find_comparables_relaxed",
        lambda conn, target, filters, **kw: {
            "data": {
                "listings": [
                    {
                        "listing_id": 900100, "sreality_id": 100,
                        "price_czk": 30000, "area_m2": 60, "price_per_m2": 500,
                        "price_per_m2_basis": "rent_monthly_czk_m2",
                        "latest_snapshot_id": 1,
                    },
                    {
                        "listing_id": 900101, "sreality_id": 101,
                        "price_czk": 4_000_000, "area_m2": 60,
                        "price_per_m2": 66_667,
                        "price_per_m2_basis": "sale_capital_czk_m2",
                        "latest_snapshot_id": 2,
                    },
                ],
                "relaxation_trace": [],
            },
            "metadata": {"result_count": 2},
        },
    )
    conn = _FakeConn(app_settings={})
    completions = [
        _completion_with_tool("find_comparables_relaxed", {"radius_m": 1000}),
        _completion_with_tool(
            "record_estimate",
            {
                "estimated_monthly_rent_czk": 30000, "confidence": "low",
                "comparables_used": [100, 101], "warnings": [],
            },
        ),
    ]
    prov = _ScriptedProvider(
        "anthropic", completions, prices={"claude-sonnet-4-5": ModelPrice(3.0, 15.0)},
    )
    with pytest.raises(MeasureBasisError, match="no single per-m"):
        agent_mod.run_agent_estimation(
            conn, sreality_client=None,
            llm_client=LLMClient(conn, providers={"anthropic": prov}),
            target=_target(), filters=_filters(), purchase_price_czk=None,
            skill=_make_skill(), provider="anthropic",
            recorder=TraceRecorder(), estimation_run_id=42,
        )


def test_a_rental_cohort_records_normally(monkeypatch):
    """The gate must not fire on the path it is meant to protect."""
    result = _run_to_terminator(
        monkeypatch, cohort_basis_value="rent_monthly_czk_m2",
    )
    assert result.metadata["stop_reason"] == "record_estimate"
    assert result.data["estimated_monthly_rent_czk"] == 30000


def test_a_number_with_no_label_is_refused_rather_than_assumed_to_be_rent(
    monkeypatch,
):
    with pytest.raises(MeasureBasisError, match="no single per-m"):
        _run_to_terminator(monkeypatch, cohort_basis_value=None)


def test_an_empty_cohort_is_not_turned_into_a_basis_error(monkeypatch):
    """"No comparables" must stay "no comparables".

    Nothing produced a per-m² number, so there is none to mislabel — the same
    carve-out `estimate_yield._scale` makes for an empty distribution.
    """
    monkeypatch.setattr(
        agent_mod, "find_comparables_relaxed",
        lambda conn, target, filters, **kw: {
            "data": {"listings": [], "relaxation_trace": []},
            "metadata": {"result_count": 0},
        },
    )
    conn = _FakeConn(app_settings={})
    completions = [
        _completion_with_tool("find_comparables_relaxed", {"radius_m": 1000}),
        _completion_with_tool(
            "record_estimate",
            {
                "estimated_monthly_rent_czk": 30000, "confidence": "low",
                "comparables_used": [], "warnings": ["no comparables"],
            },
        ),
    ]
    prov = _ScriptedProvider(
        "anthropic", completions, prices={"claude-sonnet-4-5": ModelPrice(3.0, 15.0)},
    )
    result = agent_mod.run_agent_estimation(
        conn, sreality_client=None,
        llm_client=LLMClient(conn, providers={"anthropic": prov}),
        target=_target(), filters=_filters(), purchase_price_czk=None,
        skill=_make_skill(), provider="anthropic",
        recorder=TraceRecorder(), estimation_run_id=42,
    )
    assert result.metadata["stop_reason"] == "record_estimate"


def test_the_opening_message_never_names_a_unit_it_cannot_decide():
    """`category_main=None` on a capital deal admits plots beside flats."""
    import json

    body = agent_mod._initial_user_message(
        _target(),
        ComparableFilters(category_main=None, category_type="prodej"),
        purchase_price_czk=None,
    )
    payload = json.loads(body.split("\n\n", 1)[1])
    assert payload["price_per_m2_basis"] is None
    assert payload["price_per_m2_unit"] is None

    body = agent_mod._initial_user_message(
        _target(),
        ComparableFilters(category_main="byt", category_type="pronajem"),
        purchase_price_czk=None,
    )
    payload = json.loads(body.split("\n\n", 1)[1])
    assert payload["price_per_m2_basis"] == "rent_monthly_czk_m2"
    assert payload["price_per_m2_unit"] == "Kč/m²/měs"


# ---------------------------------------------------------------------------
# iteration cap
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider_name", ["anthropic", "gemini"])
def test_iteration_cap_stops_loop(monkeypatch, provider_name):
    _patch_toolkit(monkeypatch)
    conn = _FakeConn(app_settings={})

    # Same tool over and over; never calls record_estimate.
    completions = [
        _completion_with_tool("find_comparables_relaxed", {"radius_m": 1000})
        for _ in range(10)
    ]
    prov = _ScriptedProvider(
        provider_name, completions,
        prices={"claude-sonnet-4-5": ModelPrice(3.0, 15.0), "gemini-2.5-pro": ModelPrice(1.25, 10.0)},
    )
    client = LLMClient(conn, providers={provider_name: prov})
    recorder = TraceRecorder()

    result = agent_mod.run_agent_estimation(
        conn, sreality_client=None, llm_client=client,
        target=_target(), filters=_filters(),
        purchase_price_czk=None,
        skill=_make_skill(max_iterations=3),
        provider=provider_name,
        recorder=recorder, estimation_run_id=7,
    )

    assert result.metadata["stop_reason"] == "max_iterations"
    assert result.data["estimated_monthly_rent_czk"] is None
    # max_iterations=3 means we executed turns 1, 2, 3 and the loop
    # exited at the start of turn 4.
    assert result.metadata["iterations"] == 4


# ---------------------------------------------------------------------------
# cost cap
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider_name", ["anthropic", "gemini"])
def test_cost_cap_stops_loop(monkeypatch, provider_name):
    _patch_toolkit(monkeypatch)
    conn = _FakeConn(app_settings={})

    expensive_completion = _completion_with_tool(
        "find_comparables_relaxed", {"radius_m": 1000},
        # Big enough usage that one turn blows past $1.
        # claude pricing: 200K*3/1e6 + 100K*15/1e6 = 0.6 + 1.5 = $2.10
        input_tokens=200_000, output_tokens=100_000,
    )
    completions = [expensive_completion, expensive_completion]
    prov = _ScriptedProvider(
        provider_name, completions,
        prices={
            "claude-sonnet-4-5": ModelPrice(3.0, 15.0),
            "gemini-2.5-pro": ModelPrice(1.25, 10.0),  # 200K*1.25 + 100K*10 = $1.25
        },
    )
    client = LLMClient(conn, providers={provider_name: prov})
    recorder = TraceRecorder()

    result = agent_mod.run_agent_estimation(
        conn, sreality_client=None, llm_client=client,
        target=_target(), filters=_filters(),
        purchase_price_czk=None,
        skill=_make_skill(max_cost_usd=1.0),
        provider=provider_name,
        recorder=recorder, estimation_run_id=9,
    )

    assert result.metadata["stop_reason"] == "max_cost"
    # The cap fires AT THE START of the next iteration after the
    # cost crossed the threshold, so exactly one complete() call.
    assert len(prov.calls) == 1


# ---------------------------------------------------------------------------
# TraceRecorder.reasoning() step shape
# ---------------------------------------------------------------------------

def test_reasoning_step_shape():
    recorder = TraceRecorder()
    with recorder.reasoning() as h:
        h.set_summary({
            "text": "thinking...",
            "tool_calls_queued": ["find_comparables_relaxed"],
            "provider": "anthropic",
        })
    trace = recorder.to_dict("done")
    step = trace["steps"][0]
    assert step["kind"] == "reasoning"
    assert step["n"] == 1
    assert "tool" not in step
    assert "label" not in step
    assert step["output_summary"]["tool_calls_queued"] == ["find_comparables_relaxed"]
    assert step["output_summary"]["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# TraceRecorder.set_full_output() / iter_payloads()
# ---------------------------------------------------------------------------

def test_recorder_captures_full_output_only_when_set():
    """Only steps that explicitly call set_full_output produce a payload row.

    Architectural rule #9: the trace JSONB always stores output_summary
    (bounded) per step. The side-table only gets entries the caller
    opts into via set_full_output. Computations and reasoning steps
    aren't expected to populate the side-table.
    """
    recorder = TraceRecorder()
    with recorder.tool_call("find_comparables", {"radius_m": 1000}) as h:
        h.set_summary({"result_count": 12})
        h.set_full_output({"data": {"listings": [{"sreality_id": 1}]}})
    with recorder.computation("scale") as h:
        h.set_summary({"estimated": 30000})
    with recorder.reasoning() as h:
        h.set_summary({"text": "tight cohort", "tool_calls_queued": []})

    trace = recorder.to_dict("ok")
    # All three steps are still in the trace with bounded summaries.
    assert [s["kind"] for s in trace["steps"]] == [
        "tool_call", "computation", "reasoning",
    ]
    # Only the one step that set_full_output is in the payloads list.
    payloads = recorder.iter_payloads()
    assert len(payloads) == 1
    step_n, payload = payloads[0]
    assert step_n == 1
    assert payload["data"]["listings"][0]["sreality_id"] == 1


def test_recorder_payload_step_numbers_match_trace_steps():
    """The (step_n, payload) pairs line up with the trace's step `n`."""
    recorder = TraceRecorder()
    with recorder.tool_call("a", {}) as h:
        h.set_full_output({"a": 1})
    with recorder.tool_call("b", {}) as h:
        # No full_output → no payload row.
        h.set_summary({"x": 2})
    with recorder.tool_call("c", {}) as h:
        h.set_full_output({"c": 3})

    payloads = recorder.iter_payloads()
    assert [p[0] for p in payloads] == [1, 3]
    assert payloads[0][1] == {"a": 1}
    assert payloads[1][1] == {"c": 3}


# ---------------------------------------------------------------------------
# Per-comparable decision normalisation
# ---------------------------------------------------------------------------

def test_normalise_decisions_drops_malformed_entries():
    """The agent's comparable_decisions output is run through a
    forgiving normaliser so one bad row from the model doesn't fail
    the run."""
    from api.agent import _normalise_decisions

    raw = [
        {"sreality_id": 1, "decision": "included", "reason": "tight match"},
        {"sreality_id": "2", "decision": "excluded", "reason": "luxury outlier"},
        # Missing sreality_id — drop.
        {"decision": "included", "reason": "x"},
        # Bogus decision value — drop.
        {"sreality_id": 3, "decision": "maybe", "reason": "?"},
        # Empty reason — drop.
        {"sreality_id": 4, "decision": "included", "reason": "  "},
        # Non-dict — drop.
        "garbage",
    ]
    out = _normalise_decisions(raw)
    assert [d["sreality_id"] for d in out] == [1, 2]
    assert out[0] == {"sreality_id": 1, "decision": "included", "reason": "tight match"}
    assert out[1]["decision"] == "excluded"


def test_normalise_decisions_returns_empty_when_field_absent():
    from api.agent import _normalise_decisions
    assert _normalise_decisions(None) == []
    assert _normalise_decisions("not a list") == []
    assert _normalise_decisions([]) == []


def test_used_entry_carries_both_ids():
    """comparables_used must be a strict SUPERSET of the old shape.

    estimation_runs rows are immutable (rule 12), so the frozen entries carry
    sreality_id only. Emitting both gives every reader ONE rule — prefer
    listing_id, else resolve sreality_id — with no version switch. The
    sreality_id assertion is the load-bearing one: dropping it would silently
    break the SPA, which drives three batch fetches off that key and would
    render empty cells rather than raise."""
    from api.estimate_yield import _used_entry

    entry = _used_entry({
        "listing_id": 900_123, "sreality_id": 123,
        "latest_snapshot_id": 7, "latest_snapshot_at": None,
        "data_age_days": 2, "last_freshness_check_at": None,
    })
    assert entry["listing_id"] == 900_123
    assert entry["sreality_id"] == 123


def test_used_entry_tolerates_a_listing_without_a_legacy_id():
    """A post-Gate-2 comparable has no sreality_id. It must project as None,
    not raise — _used_entry runs inside the estimation write path."""
    from api.estimate_yield import _used_entry

    entry = _used_entry({
        "listing_id": 900_124, "sreality_id": None,
        "latest_snapshot_id": 8, "latest_snapshot_at": None,
        "data_age_days": 1, "last_freshness_check_at": None,
    })
    assert entry["listing_id"] == 900_124
    assert entry["sreality_id"] is None
    assert entry["snapshot_id"] == 8
