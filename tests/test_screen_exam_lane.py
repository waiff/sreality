"""The screening lane — the only one in the programme that spends money.

Everything asserted here is a spending rail. The cost cap is the point of this
file: `api.llm_client`'s daily-cost check only LOGS, and on a long batch it
notices after the money is gone.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LANE = ROOT / ".github" / "workflows" / "screen_exam_cohort.yml"


@pytest.fixture(scope="module")
def lane() -> dict[str, Any]:
    return yaml.safe_load(LANE.read_text())


def _step(lane: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in lane["jobs"]["screen"]["steps"] if s.get("name") == "Screen")


def test_the_lane_never_runs_on_a_schedule(lane: dict[str, Any]) -> None:
    # A scheduled lane that spends money is a standing order nobody re-reads.
    triggers = lane.get(True) or lane.get("on")
    assert "schedule" not in triggers
    assert "cron" not in LANE.read_text()


def test_the_default_action_measures_before_it_spends(lane: dict[str, Any]) -> None:
    # calibrate on 25 images, not screen on 4000. A dispatch form's default is what
    # gets run by someone in a hurry.
    triggers = lane.get(True) or lane.get("on")
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["action"]["default"] == "calibrate"
    assert inputs["count"]["default"] == "25"


def test_the_lane_carries_a_spending_ceiling(lane: dict[str, Any]) -> None:
    triggers = lane.get(True) or lane.get("on")
    assert "max_usd" in triggers["workflow_dispatch"]["inputs"]
    assert "--max-usd" in _step(lane)["run"]


def test_the_lane_holds_both_credential_sets(lane: dict[str, Any]) -> None:
    # First lane in the repo to need a model AND image bytes: the LLM lanes have
    # never needed R2, and the R2 lanes have never called a model.
    env = lane["jobs"]["screen"]["env"]
    assert env["OPENAI_API_KEY"] == "${{ secrets.OPENAI_API_KEY }}"
    for r2 in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
               "R2_BUCKET_NAME"):
        assert r2 in env


def test_the_run_stops_before_the_runner_is_killed(lane: dict[str, Any]) -> None:
    # Being killed mid-flight would leave the bill known only to OpenAI. Stopping
    # between images means the spend is reported.
    step = _step(lane)
    budget = int(step["run"].split("--max-seconds ")[1].split()[0].strip('"'))
    assert budget < lane["jobs"]["screen"]["timeout-minutes"] * 60


def test_every_free_text_input_is_validated(lane: dict[str, Any]) -> None:
    run = _step(lane)["run"]
    assert "${{" not in run
    assert "*[!a-zA-Z0-9_-]*" in run   # cohort
    assert "*[!0-9]*" in run           # count
    assert "*[!0-9.]*" in run          # max_usd


# --- the script's spending refusals ----------------------------------------


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    import scripts.screen_exam_cohort as mod
    monkeypatch.setattr("sys.argv", ["screen_exam_cohort", *argv])
    return mod.main()


def test_exactly_one_phase_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, ["--cohort", "e", "--calibrate", "5", "--screen", "5"]) == 1
    assert _run(monkeypatch, ["--cohort", "e"]) == 1


def test_the_called_for_value_is_in_both_the_literal_and_the_check() -> None:
    # The DB CHECK is a drop-and-add: a value missing from the newest migration is
    # silently removed from the vocabulary, and the insert fails at write time.
    from api.llm_client import CalledFor
    import typing
    assert "screen_exam_image" in typing.get_args(CalledFor)
    mig = (ROOT / "migrations" / "459_exam_screening.sql").read_text()
    assert "'screen_exam_image'" in mig


def test_the_check_restates_every_value_it_inherited() -> None:
    # migrations/234 is the previous statement of this CHECK and carries five values
    # the Python literal never had. Dropping one here would break a live caller.
    import re
    def _check_values(text: str) -> set[str]:
        # Scope to the CHECK body only. migration 234 also seeds app_settings rows
        # full of quoted words that are not called_for values at all — the first
        # version of this test read those and reported four phantom regressions.
        after = text.split("llm_calls_called_for_check")[-1]
        body = after[after.index("[") if "[" in after.split(")")[0] else after.index("("):]
        return set(re.findall(r"'([a-z_]+)'", body.split("]")[0].split(");")[0]))

    inherited = _check_values(
        (ROOT / "migrations" / "234_dedup_floor_plan_match.sql").read_text())
    restated = _check_values(
        (ROOT / "migrations" / "459_exam_screening.sql").read_text())
    missing = inherited - restated
    assert not missing, f"459 drops inherited called_for values: {sorted(missing)}"
