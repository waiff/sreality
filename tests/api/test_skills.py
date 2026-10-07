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


# The live registry (api/dependencies.py:_build_providers); the seeded skills
# name only anthropic + gemini.
_REGISTERED_PROVIDERS = {"anthropic", "gemini", "openai", "qwen", "oss"}


def test_update_skill_accepts_preferred_model_naming_a_provider_subset():
    sk.PROVIDER_NAMES = set(_REGISTERED_PROVIDERS)
    try:
        assert sk._validate_preferred_model({
            "anthropic": "claude-sonnet-4-5",
            "gemini": "gemini-2.5-pro",
        }) == {"anthropic": "claude-sonnet-4-5", "gemini": "gemini-2.5-pro"}
    finally:
        sk.PROVIDER_NAMES = set()


def test_update_skill_rejects_unknown_provider():
    sk.PROVIDER_NAMES = set(_REGISTERED_PROVIDERS)
    try:
        with pytest.raises(sk.SkillValidationError, match="unknown provider"):
            sk._validate_preferred_model(
                {"anthropic": "x", "gemini": "y", "fake": "z"}
            )
    finally:
        sk.PROVIDER_NAMES = set()


def test_update_skill_rejects_empty_preferred_model():
    sk.PROVIDER_NAMES = set(_REGISTERED_PROVIDERS)
    try:
        with pytest.raises(sk.SkillValidationError, match="provider"):
            sk._validate_preferred_model({})
    finally:
        sk.PROVIDER_NAMES = set()


class _UpdateCapturingConn:
    """Records the UPDATE update_skill issues, then serves the row back."""

    def __init__(self, row: tuple) -> None:
        self.row = row
        self.updates: list[dict] = []
        self._last: tuple | None = None

    def cursor(self) -> "_UpdateCapturingConn":
        return self

    def transaction(self) -> "_UpdateCapturingConn":
        return self

    def __enter__(self) -> "_UpdateCapturingConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = ()) -> None:
        self._last = self.row
        if sql.lstrip().lower().startswith("update skills"):
            self.updates.append(dict(params))
            self._last = (self.row[0],)

    def fetchone(self) -> tuple | None:
        return self._last


def test_update_skill_saves_the_settings_payload_against_the_full_registry():
    """The Settings page resends the row's own two-provider map on every save."""
    sk.AGENT_TOOL_NAMES = {
        "find_comparables_relaxed", "analyze_distribution", "record_estimate",
    }
    sk.PROVIDER_NAMES = set(_REGISTERED_PROVIDERS)
    try:
        row = make_skill_row(name="rental_estimator_v1")
        current = sk.load_skill(
            _FakeConn(skills={"rental_estimator_v1": row}), "rental_estimator_v1",
        )
        conn = _UpdateCapturingConn(row)
        sk.update_skill(conn, "rental_estimator_v1", {
            "system_prompt": "you are a fox",
            "allowed_tools": current.allowed_tools,
            "preferred_model": current.preferred_model,
            "limits": {
                "max_iterations": current.limits.max_iterations,
                "max_cost_usd": current.limits.max_cost_usd,
                "wall_clock_timeout_s": current.limits.wall_clock_timeout_s,
            },
        })
        assert len(conn.updates) == 1
        assert json.loads(conn.updates[0]["preferred_model"]) == {
            "anthropic": "claude-sonnet-4-5",
            "gemini": "gemini-2.5-pro",
        }
    finally:
        sk.AGENT_TOOL_NAMES = set()
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
    sk.PROVIDER_NAMES = set(_REGISTERED_PROVIDERS)
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
