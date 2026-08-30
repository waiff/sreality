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
    # 461 restates the CHECK again (adding suggest_exam_answer); the same rule
    # applies down the chain — anything 459 carried must survive.
    restated_461 = _check_values(
        (ROOT / "migrations" / "461_exam_letters_sets12_and_suggestions.sql").read_text())
    missing_461 = restated - restated_461
    assert not missing_461, f"461 drops inherited called_for values: {sorted(missing_461)}"


# --- the truncation trap ----------------------------------------------------


def test_the_token_budget_leaves_room_for_reasoning() -> None:
    """gpt-5-mini spends output tokens on reasoning BEFORE writing anything, so a
    budget sized for the answer is consumed by the thinking and the call returns an
    EMPTY STRING — billed in full, with nothing to parse.

    Measured twice in this repo: this lane's first calibration failed 5 of 10 at
    300 tokens, and the enrichment lane hit the same wall at 512 in July. The reply
    is ~30 tokens; the ceiling is headroom for reasoning, not for the answer."""
    import scripts.screen_exam_cohort as mod
    assert mod.MAX_TOKENS >= 4096


def test_the_probe_factor_matches_the_measured_yield() -> None:
    # ~1.7% of probed ids resolve to a usable image (6,000 probes -> 100 images),
    # because images.id spans far more values than there are rows. A factor of 12
    # offered 10 images when asked for 25.
    import scripts.screen_exam_cohort as mod
    assert mod.PROBE_FACTOR >= 60


def test_a_failed_screen_is_offered_again() -> None:
    # A failed screen is not a screen. Excluding errored images would strand them
    # AND leave their rows dragging the error rate above the stratify gate with no
    # way to clear it.
    import scripts.screen_exam_cohort as mod
    sql = " ".join(mod._UNSCREENED_PROBE_SQL.split())
    assert "s.error IS NULL" in sql


# --- parallelism ------------------------------------------------------------


def test_the_screener_runs_in_parallel_by_default() -> None:
    # MEASURED: 148s for 25 images = 5.9s each, nearly all of it waiting on R2 and
    # the model. Sequentially 1,500 images is 2.5 hours against a 25-minute lane.
    import scripts.screen_exam_cohort as mod
    assert mod.DEFAULT_WORKERS >= 4
    assert mod.WORKERS_MAX >= mod.DEFAULT_WORKERS


def test_each_worker_opens_its_own_connection() -> None:
    """psycopg connections are not thread-safe, and LLMClient writes an llm_calls
    row per call — sharing one connection across workers would interleave writes on
    it, which is the classic way a parallel lane corrupts its own cost ledger."""
    import inspect
    from toolkit import vision_batch
    src = inspect.getsource(vision_batch.run_vision_batch)
    assert "wconn = db.connect()" in src
    assert "wconn.close()" in src


def test_the_budget_is_checked_by_the_worker_under_a_lock() -> None:
    # Checking after the fact on a parallel lane means discovering the overspend
    # once every in-flight call has already been billed.
    import inspect
    from toolkit import vision_batch
    src = inspect.getsource(vision_batch.run_vision_batch)
    assert "with lock:" in src and "_stop()" in src


def test_the_lane_passes_a_worker_count(lane: dict[str, Any]) -> None:
    step = _step(lane)
    assert "--workers" in step["run"]
    assert "WORKERS" in step["env"]


# --- the function actually RUNS ---------------------------------------------


def test_screen_batch_really_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive _screen_batch end to end against fakes.

    WHY THIS EXISTS. Every other test in this file reads the function's SOURCE —
    `inspect.getsource`, YAML, constants. All of them passed while the shipped code
    raised `NameError: name 'threading' is not defined` on its first live run: a
    string-replace edit that added the import silently failed to match, `ast.parse`
    still succeeded because a missing name is a RUNTIME error, and no test ever
    called the function.

    A guard that reads code instead of running it proves the code was WRITTEN, not
    that it WORKS.
    """
    import scripts.screen_exam_cohort as mod

    written: list[tuple[int, list[int] | None, str | None]] = []

    class _FakeLLM:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        def call(self, **kw: Any) -> Any:
            class _R:
                cost_usd = 0.001
                text = '{"ids": [22]}'
            return _R()

    class _FakeConn:
        def close(self) -> None: ...

    monkeypatch.setattr("api.llm_client.LLMClient", _FakeLLM)
    monkeypatch.setattr("api.providers.openai.OpenAIProvider", lambda *a, **k: object())
    monkeypatch.setattr("scraper.db.connect", lambda *a, **k: _FakeConn())
    monkeypatch.setattr("toolkit.vision_images.image_block",
                        lambda *a, **k: {"type": "image"})
    monkeypatch.setattr(
        "toolkit.exam_screening.record_screen",
        lambda conn, **kw: written.append(
            (kw["image_id"], kw["guess_tag_ids"], kw["error"])),
    )

    rows = [(i, f"img/{i}.jpg") for i in range(1, 13)]
    stats = mod._screen_batch(
        None, object(), cohort_id=1, rows=rows,
        tags=[{"id": 22, "label": "koupelna"}], model="gpt-5-mini",
        max_usd=0, max_seconds=0, workers=4,
    )
    assert stats["ok"] == 12 and stats["errors"] == 0
    assert stats["hits"] == 12
    assert len(written) == 12
    assert all(g == [22] and e is None for _i, g, e in written)


def test_screen_batch_stops_at_the_cost_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The ceiling has to bind in the WORKER, before the call. A parallel lane that
    # checks afterwards discovers the overspend once every in-flight call is billed.
    import scripts.screen_exam_cohort as mod

    class _FakeLLM:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        def call(self, **kw: Any) -> Any:
            class _R:
                cost_usd = 1.0
                text = '{"ids": []}'
            return _R()

    class _FakeConn:
        def close(self) -> None: ...

    monkeypatch.setattr("api.llm_client.LLMClient", _FakeLLM)
    monkeypatch.setattr("api.providers.openai.OpenAIProvider", lambda *a, **k: object())
    monkeypatch.setattr("scraper.db.connect", lambda *a, **k: _FakeConn())
    monkeypatch.setattr("toolkit.vision_images.image_block", lambda *a, **k: {})
    monkeypatch.setattr("toolkit.exam_screening.record_screen", lambda conn, **kw: None)

    stats = mod._screen_batch(
        None, object(), cohort_id=1, rows=[(i, "p") for i in range(1, 40)],
        tags=[{"id": 22, "label": "k"}], model="m",
        max_usd=3.0, max_seconds=0, workers=2,
    )
    assert stats["aborted"] is True
    # Two workers can each be mid-call when the ceiling trips, so the bound is the
    # ceiling plus at most one in-flight call per worker — never the whole queue.
    assert stats["ok"] <= 3 + 2


def test_a_screener_failure_is_recorded_as_an_error_not_an_empty_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.screen_exam_cohort as mod

    written: list[tuple[int, list[int] | None, str | None]] = []

    class _FakeLLM:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        def call(self, **kw: Any) -> Any:
            class _R:
                cost_usd = 0.001
                text = ""          # the live truncation failure, exactly
            return _R()

    class _FakeConn:
        def close(self) -> None: ...

    monkeypatch.setattr("api.llm_client.LLMClient", _FakeLLM)
    monkeypatch.setattr("api.providers.openai.OpenAIProvider", lambda *a, **k: object())
    monkeypatch.setattr("scraper.db.connect", lambda *a, **k: _FakeConn())
    monkeypatch.setattr("toolkit.vision_images.image_block", lambda *a, **k: {})
    monkeypatch.setattr(
        "toolkit.exam_screening.record_screen",
        lambda conn, **kw: written.append(
            (kw["image_id"], kw["guess_tag_ids"], kw["error"])),
    )

    stats = mod._screen_batch(
        None, object(), cohort_id=1, rows=[(1, "p"), (2, "p")],
        tags=[{"id": 22, "label": "k"}], model="m",
        max_usd=0, max_seconds=0, workers=2,
    )
    assert stats["errors"] == 2 and stats["ok"] == 0
    # An empty guess and a failure mean opposite things downstream.
    assert all(g is None and e for _i, g, e in written)
