"""Hermetic tests for the pure/DB-light logic in validate_vision_models.

No LLM, no R2. `_room_images` is the one DB touch point exercised here (via a
minimal fake cursor); the DB/LLM-integrated runners (run_lane_recall_ab etc.)
are exercised live by the harness itself (mirrors
tests/scripts/test_compare_condition_models.py's "pure math only" scope for a
sibling A/B-harness script).
"""

from __future__ import annotations

from typing import Any

from scripts.validate_vision_models import (
    _LANES,
    _candidate_rooms,
    _is_infra_error,
    _provider_for,
)
from toolkit import visual_match as vm
from toolkit.room_taxonomy import FULL_PRIORITY


# --- _provider_for -----------------------------------------------------------

def test_provider_for_gemini():
    assert _provider_for("gemini-3.1-flash-lite") == "gemini"
    assert _provider_for("gemini-2.5-flash-lite") == "gemini"


def test_provider_for_openai():
    assert _provider_for("gpt-5-mini") == "openai"
    assert _provider_for("o1-mini") == "openai"
    assert _provider_for("o3") == "openai"


def test_provider_for_qwen():
    assert _provider_for("qwen3-vl-235b-a22b-instruct") == "qwen"
    assert _provider_for("qwen3-vl-30b-a3b-instruct") == "qwen"


def test_provider_for_defaults_to_anthropic():
    assert _provider_for("claude-sonnet-4-5") == "anthropic"
    assert _provider_for("claude-haiku-4-5") == "anthropic"


# --- _is_infra_error -----------------------------------------------------------

def test_is_infra_error_matches_openai_style_http_errors():
    assert _is_infra_error(Exception("openai call failed: HTTP 429 insufficient_quota"))
    assert _is_infra_error(Exception("qwen call failed: HTTP 402 payment required"))


def test_is_infra_error_matches_existing_anthropic_gemini_patterns():
    assert _is_infra_error(Exception("Your credit balance is too low"))
    assert _is_infra_error(Exception("RESOURCE_EXHAUSTED: quota exceeded"))


def test_is_infra_error_does_not_match_a_real_verdict_miss():
    assert not _is_infra_error(Exception("record_visual_match returned bad verdict: 'Maybe'"))


# --- _LANES registry sanity ----------------------------------------------------

def test_every_lane_danger_verdict_is_a_valid_verdict_for_its_tool():
    valid_verdicts = {
        "compare": {"High", "Medium", "Low"},
        "floor_plan": set(vm._FLOOR_PLAN_VERDICTS),
        "site_plan": set(vm._SITE_PLAN_VERDICTS),
    }
    for lane, cfg in _LANES.items():
        assert cfg.danger_verdict in valid_verdicts[lane], lane


def test_compare_lane_extract_reads_visual_match_verdict():
    tool_calls = [{"name": "record_visual_match", "input": {"verdict": "High", "rationale": "x"}}]
    assert _LANES["compare"].extract(tool_calls) == "High"


def test_floor_plan_lane_extract_reads_floor_plan_verdict():
    tool_calls = [{"name": "record_floor_plan_match", "input": {"verdict": "different_layout", "rationale": "x"}}]
    assert _LANES["floor_plan"].extract(tool_calls) == "different_layout"


def test_site_plan_lane_extract_reads_site_plan_verdict():
    tool_calls = [{"name": "record_site_plan_match", "input": {"verdict": "same_unit", "rationale": "x"}}]
    assert _LANES["site_plan"].extract(tool_calls) == "same_unit"


def test_floor_plan_and_site_plan_lanes_use_the_fixed_plan_room_type():
    assert _LANES["floor_plan"].fixed_room_type == "floor_plan"
    assert _LANES["site_plan"].fixed_room_type == "site_plan"


def test_compare_lane_resolves_room_per_case():
    assert _LANES["compare"].fixed_room_type is None


# --- _candidate_rooms --------------------------------------------------------------

class _FakeCursor:
    def __init__(self, images: dict[tuple[int, str], list[str]]) -> None:
        self._images = images
        self._last: list[tuple[str]] = []

    def execute(self, sql: str, params: tuple) -> None:
        sreality_id, room_type, room_type2, limit = params
        assert room_type == room_type2  # the query's two EXISTS branches share one room_type
        paths = self._images.get((sreality_id, room_type), [])
        self._last = [(p,) for p in paths[:limit]]

    def fetchall(self) -> list[tuple[str]]:
        return self._last

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, images: dict[tuple[int, str], list[str]]) -> None:
        self._images = images

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._images)


def test_candidate_rooms_fixed_room_type_for_floor_plan():
    conn = _FakeConn({(1, "floor_plan"): ["a.jpg"], (2, "floor_plan"): ["b.jpg"]})
    assert _candidate_rooms(conn, "floor_plan", 1, 2) == [("floor_plan", ["a.jpg"], ["b.jpg"])]


def test_candidate_rooms_fixed_room_type_empty_when_one_side_empty():
    conn = _FakeConn({(1, "site_plan"): ["a.jpg"]})  # side 2 has nothing
    assert _candidate_rooms(conn, "site_plan", 1, 2) == []


def test_candidate_rooms_compare_lane_walks_full_priority_in_order():
    # Neither side has the FIRST-priority room (kitchen); both have the SECOND
    # (bathroom) and the SIXTH (living_room) — results must come back in
    # FULL_PRIORITY order (bathroom before living_room), not discovery order.
    assert FULL_PRIORITY[0] == "kitchen"
    assert FULL_PRIORITY[1] == "bathroom"
    conn = _FakeConn({
        (1, "bathroom"): ["a1.jpg"], (2, "bathroom"): ["b1.jpg"],
        (1, "living_room"): ["a2.jpg"], (2, "living_room"): ["b2.jpg"],
    })
    rooms = _candidate_rooms(conn, "compare", 1, 2)
    assert rooms == [
        ("bathroom", ["a1.jpg"], ["b1.jpg"]),
        ("living_room", ["a2.jpg"], ["b2.jpg"]),
    ]


def test_candidate_rooms_compare_lane_stops_at_max_attempts():
    all_rooms = FULL_PRIORITY[:5]
    conn = _FakeConn({(sid, room): [f"{sid}.jpg"] for sid in (1, 2) for room in all_rooms})
    rooms = _candidate_rooms(conn, "compare", 1, 2, max_attempts=2)
    assert [r[0] for r in rooms] == list(all_rooms[:2])


def test_candidate_rooms_compare_lane_empty_when_no_room_is_shared():
    conn = _FakeConn({(1, "kitchen"): ["a.jpg"], (2, "bathroom"): ["b.jpg"]})
    assert _candidate_rooms(conn, "compare", 1, 2) == []
