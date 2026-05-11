"""Tests for api.skills — DB-backed skill loader + updater + validation."""

from __future__ import annotations

import json

import pytest

from api import skills as sk
from tests.api._fakes import _FakeConn, make_skill_row


def test_load_skill_returns_typed_dataclass():
    row = make_skill_row(name="rental_estimator_v1")
    conn = _FakeConn(skills={"rental_estimator_v1": row})
    s = sk.load_skill(conn, "rental_estimator_v1")
    assert s.name == "rental_estimator_v1"
    assert s.allowed_tools == [
        "find_comparables_relaxed", "analyze_distribution", "record_estimate",
    ]
    assert s.preferred_model["anthropic"] == "claude-sonnet-4-5"
    assert s.preferred_model["gemini"] == "gemini-2.5-pro"
    assert s.limits.max_iterations == 12
    assert s.limits.max_cost_usd == pytest.approx(1.0)


def test_load_skill_raises_when_missing():
    conn = _FakeConn(skills={})
    with pytest.raises(sk.SkillNotFound):
        sk.load_skill(conn, "nope")


def test_update_skill_rejects_unknown_tool():
    sk.AGENT_TOOL_NAMES = {"find_comparables_relaxed", "record_estimate"}
    with pytest.raises(sk.SkillValidationError, match="unknown tool"):
        sk._validate_allowed_tools(["find_comparables_relaxed", "boguscallout"])


def test_update_skill_rejects_partial_preferred_model():
    sk.AGENT_TOOL_NAMES = set()
    sk.PROVIDER_NAMES = {"anthropic", "gemini"}
    with pytest.raises(sk.SkillValidationError, match="missing entries"):
        sk._validate_preferred_model({"anthropic": "claude-sonnet-4-5"})
    sk.PROVIDER_NAMES = set()


def test_update_skill_rejects_unknown_provider():
    sk.PROVIDER_NAMES = {"anthropic", "gemini"}
    with pytest.raises(sk.SkillValidationError, match="unknown provider"):
        sk._validate_preferred_model({"anthropic": "x", "gemini": "y", "fake": "z"})
    sk.PROVIDER_NAMES = set()


def test_update_skill_rejects_limit_out_of_range():
    with pytest.raises(sk.SkillValidationError):
        sk._validate_limits({
            "max_iterations": 100,
            "max_cost_usd": 1.0,
            "wall_clock_timeout_s": 60.0,
        })
    with pytest.raises(sk.SkillValidationError):
        sk._validate_limits({
            "max_iterations": 10,
            "max_cost_usd": -1.0,
            "wall_clock_timeout_s": 60.0,
        })


def test_update_skill_accepts_well_formed_payload():
    sk.AGENT_TOOL_NAMES = {"find_comparables_relaxed", "record_estimate"}
    sk.PROVIDER_NAMES = {"anthropic", "gemini"}
    assert sk._validate_allowed_tools(["find_comparables_relaxed"]) == [
        "find_comparables_relaxed"
    ]
    assert sk._validate_preferred_model({
        "anthropic": "claude-sonnet-4-5",
        "gemini": "gemini-2.5-pro",
    })["anthropic"] == "claude-sonnet-4-5"
    assert sk._validate_limits({
        "max_iterations": 8,
        "max_cost_usd": 0.5,
        "wall_clock_timeout_s": 90.0,
    })["max_iterations"] == 8
    sk.AGENT_TOOL_NAMES = set()
    sk.PROVIDER_NAMES = set()


def test_validate_str_rejects_empty():
    with pytest.raises(sk.SkillValidationError):
        sk._validate_str("", "system_prompt")
    with pytest.raises(sk.SkillValidationError):
        sk._validate_str("  ", "system_prompt")
    assert sk._validate_str("ok", "system_prompt") == "ok"


def test_jsonb_dumps_round_trips_via_json():
    out = sk._jsonb_dumps([1, 2, 3])
    assert json.loads(out) == [1, 2, 3]
