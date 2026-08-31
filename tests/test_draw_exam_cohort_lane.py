"""The exam-draw lane, and the orderings it must refuse.

Sealing is irreversible and the exam is the one measurement everything downstream
is judged by, so the failure modes worth testing here are all about ORDER: sealing
before the stratified half exists, redrawing a sealed cohort, or a schedule that
would eventually do either on its own.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LANE = ROOT / ".github" / "workflows" / "draw_exam_cohort.yml"


@pytest.fixture(scope="module")
def lane() -> dict[str, Any]:
    return yaml.safe_load(LANE.read_text())


def _step(lane: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in lane["jobs"]["draw"]["steps"]
                if s.get("name") == "Draw exam cohort")


def test_the_lane_is_dispatch_only_and_stays_that_way(lane: dict[str, Any]) -> None:
    # A scheduled exam draw would eventually redraw the exam, and every grade taken
    # before that would silently stop being comparable. There is no cadence at
    # which this is safe, so there is no commented-out cron either.
    triggers = lane.get(True) or lane.get("on")
    assert "workflow_dispatch" in triggers
    assert "schedule" not in triggers
    assert "cron" not in LANE.read_text()


def test_runs_are_serialised_and_never_cancelled(lane: dict[str, Any]) -> None:
    assert lane["concurrency"]["group"] == "draw-exam-cohort"
    assert lane["concurrency"]["cancel-in-progress"] is False


def test_the_job_reaches_the_database_and_nothing_else(lane: dict[str, Any]) -> None:
    # Pure SQL: the screening lane is where the model and R2 are needed, and a lane
    # holding credentials it never uses widens the blast radius for nothing.
    assert lane["jobs"]["draw"]["env"]["SUPABASE_DB_URL"] == "${{ secrets.SUPABASE_DB_URL }}"
    text = LANE.read_text()
    for unused in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "R2_ACCESS_KEY_ID",
                   "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"):
        assert unused not in text


def test_inputs_reach_the_script_through_env_not_interpolation(
    lane: dict[str, Any],
) -> None:
    step = _step(lane)
    assert "${{" not in step["run"]
    assert set(step["env"]) == {"COHORT", "PURE_RANDOM", "PER_TAG", "ACTION", "DRY_RUN"}


def test_the_cohort_name_is_validated_as_an_identifier(lane: dict[str, Any]) -> None:
    # The one free-text input, and it lands in a command line.
    assert "*[!a-zA-Z0-9_-]*" in _step(lane)["run"]


def test_the_default_action_is_the_read_only_one(lane: dict[str, Any]) -> None:
    # A dispatch form whose default writes is a form that gets dispatched by
    # accident. `status` is the safe landing.
    triggers = lane.get(True) or lane.get("on")
    assert triggers["workflow_dispatch"]["inputs"]["action"]["default"] == "status"
    assert triggers["workflow_dispatch"]["inputs"]["pure_random"]["default"] == "0"


def test_sealing_is_named_as_irreversible_in_the_form(lane: dict[str, Any]) -> None:
    # The operator sees this string in the dispatch UI, at the moment it matters.
    triggers = lane.get(True) or lane.get("on")
    desc = triggers["workflow_dispatch"]["inputs"]["action"]["description"]
    assert "IRREVERSIBLE" in desc.upper()


# --- the script's own refusals ---------------------------------------------


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    import scripts.draw_exam_cohort as mod
    monkeypatch.setattr("sys.argv", ["draw_exam_cohort", *argv])
    return mod.main()


def test_drawing_and_sealing_in_one_run_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The one ordering mistake that cannot be undone: it would close the exam
    # before the stratified half could be added, leaving a 100-image core that can
    # never grade the four rare tags.
    assert _run(monkeypatch, ["--cohort", "exam_v1", "--pure-random", "100", "--seal"]) == 1


def test_a_run_with_no_action_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, ["--cohort", "exam_v1"]) == 1


def test_a_negative_draw_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, ["--cohort", "exam_v1", "--pure-random", "-5"]) == 1
