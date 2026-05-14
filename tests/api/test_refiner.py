"""Hermetic tests for the slice C refiner.

We don't hit the LLM (no API key in CI) or Postgres. The
production refiner is two cleanly-separable functions:

  1. `_pick_skill_name_from_run` — pure trace parsing.
  2. `_build_refiner_user_message` — pure JSON serialization.

Both are covered here. The end-to-end POST /estimations/{id}/feedback
loop is tested separately in test_estimations.py via the existing
FastAPI test client + monkeypatched LLM client.
"""

from __future__ import annotations

import json

from api.refiner import (
    _build_refiner_user_message,
    _compact_steps,
    _pick_skill_name_from_run,
)


def test_pick_skill_name_from_run_reads_skill_choice_step():
    run = {
        "trace": {
            "summary": "agent anthropic/rental_estimator_v1 after 5 LLM turns",
            "steps": [
                {
                    "n": 1, "kind": "computation", "label": "skill_choice",
                    "output_summary": {
                        "skill_name": "rental_estimator_v1",
                        "provider": "anthropic",
                        "model": "claude-sonnet-4-5",
                    },
                },
                {
                    "n": 2, "kind": "reasoning",
                    "output_summary": {"text": "..."},
                },
            ],
        },
    }
    assert _pick_skill_name_from_run(run) == "rental_estimator_v1"


def test_pick_skill_name_from_run_none_when_no_skill_choice():
    """Deterministic / pre-slice-A.1 runs return None — those can't
    be refined."""
    run = {
        "trace": {
            "steps": [
                {"n": 1, "kind": "tool_call", "tool": "find_comparables"},
            ],
        },
    }
    assert _pick_skill_name_from_run(run) is None


def test_pick_skill_name_from_run_none_on_missing_trace():
    assert _pick_skill_name_from_run({}) is None
    assert _pick_skill_name_from_run({"trace": None}) is None


def test_compact_steps_drops_tool_input_keeps_summary():
    """The refiner prompt strips heavy tool inputs but keeps the
    bounded output_summary."""
    steps = [
        {
            "n": 1, "kind": "tool_call", "tool": "find_comparables",
            "input": {"radius_m": 1000, "many": "fields"},
            "duration_ms": 120,
            "output_summary": {"result_count": 12},
        },
        {
            "n": 2, "kind": "computation", "label": "scale per-m² by area",
            "duration_ms": 1,
            "output_summary": {"estimated": 30000},
        },
        {
            "n": 3, "kind": "reasoning",
            "duration_ms": 800,
            "output_summary": {"text": "Cohort is tight."},
        },
    ]
    out = _compact_steps(steps)
    assert len(out) == 3
    # tool_call: tool name preserved, input dropped
    assert out[0]["tool"] == "find_comparables"
    assert "input" not in out[0]
    assert out[0]["output_summary"] == {"result_count": 12}
    # computation: label preserved
    assert out[1]["label"] == "scale per-m² by area"
    # reasoning: just kind + summary
    assert out[2]["kind"] == "reasoning"


def test_build_refiner_user_message_packs_full_context():
    """User message includes the feedback, the run's outputs, and
    the original prompt — everything the refiner needs to write a
    grounded edit."""
    feedback = {
        "id": 7,
        "feedback_text": "The cohort was too broad.",
    }
    run = {
        "id": 17,
        "estimated_monthly_rent_czk": 25_100,
        "rent_p25_czk": 23_000,
        "rent_p75_czk": 27_500,
        "confidence": "medium",
        "warnings": ["spread > 0.4"],
        "input_spec": {"lat": 50.087, "lng": 14.42, "area_m2": 50},
        "comparables_used": [{"sreality_id": 1, "reason": "tight match"}],
        "comparables_excluded": [{"sreality_id": 2, "reason": "luxury outlier"}],
        "trace": {
            "summary": "agent anthropic/rental_estimator_v1 …",
            "steps": [
                {
                    "n": 1, "kind": "computation", "label": "skill_choice",
                    "output_summary": {
                        "skill_name": "rental_estimator_v1",
                        "provider": "anthropic",
                        "model": "claude-sonnet-4-5",
                    },
                },
            ],
        },
    }
    text = _build_refiner_user_message(
        feedback=feedback, run=run, skill_name="rental_estimator_v1",
        original_prompt="You are a Czech real estate rental analyst.",
    )
    # User message has a one-line intro followed by a JSON blob —
    # split at the first '{' to parse it back.
    intro, payload_text = text.split("{", 1)
    assert "record_skill_refinement" in intro
    payload = json.loads("{" + payload_text)
    assert payload["skill_name"] == "rental_estimator_v1"
    assert payload["feedback_text"] == "The cohort was too broad."
    assert payload["run"]["id"] == 17
    assert payload["run"]["confidence"] == "medium"
    assert payload["run"]["comparables_used"][0]["reason"] == "tight match"
    assert payload["run"]["comparables_excluded"][0]["reason"] == "luxury outlier"
    assert payload["original_prompt"].startswith("You are a Czech")
    assert payload["trace_steps"][0]["label"] == "skill_choice"
