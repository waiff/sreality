"""The candidate-draw lane, and the exit code that is its only failure signal.

WHY A LANE AT ALL. `draw_candidates` used to be reachable only from a synchronous
admin request, whose whole-call budget is 45 seconds. The pool query for `byt` alone
measures ~14s (EXPLAIN ANALYZE, 2026-08-28: a bitmap heap scan of 320,909 listings
sorted by random()), so the largest and most important category was routinely the one
the budget dropped. A background lane removes that ceiling — and it is the only way
the draw can run without the operator holding a browser open.

WHY THE EXIT CODE IS TESTED. `main()` deliberately catches per tag so one bad tag
cannot kill an --all-ready run. It then counted the errors and returned 0 regardless,
which on a dispatched lane means a run where EVERY tag raised still reports green.
A warning in the log is read by nobody.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LANE = ROOT / ".github" / "workflows" / "draw_tag_candidates.yml"


@pytest.fixture(scope="module")
def lane() -> dict[str, Any]:
    return yaml.safe_load(LANE.read_text())


def test_the_lane_exists_and_is_dispatchable(lane: dict[str, Any]) -> None:
    # PyYAML parses the bare `on:` key as the boolean True — YAML 1.1 — so the
    # trigger block is reached under that key, not the string "on".
    triggers = lane.get(True) or lane.get("on")
    assert "workflow_dispatch" in triggers


def test_the_schedule_is_not_live_yet(lane: dict[str, Any]) -> None:
    # A scheduled top-up that quietly fills queues with the wrong property mix is
    # worse than none — which is the failure this lane was built after. Enabling the
    # cron should be a deliberate edit, visible in a diff.
    triggers = lane.get(True) or lane.get("on")
    assert "schedule" not in triggers


def test_runs_are_serialised_but_never_cancelled(lane: dict[str, Any]) -> None:
    # Concurrent draws are idempotent (ON CONFLICT DO NOTHING), so the group exists
    # to stop two runs burning the same minutes on the same pool scans. Cancelling a
    # run mid-flight would throw away categories it had already committed.
    assert lane["concurrency"]["group"] == "draw-tag-candidates"
    assert lane["concurrency"]["cancel-in-progress"] is False


def test_the_job_can_reach_the_database(lane: dict[str, Any]) -> None:
    assert lane["jobs"]["draw"]["env"]["SUPABASE_DB_URL"] == "${{ secrets.SUPABASE_DB_URL }}"


def test_the_lane_asks_for_no_credential_it_does_not_need() -> None:
    # Pure SQL against the embedding store: no R2 fetch, no model download, no LLM
    # call. A lane that requests secrets it never uses widens the blast radius of a
    # compromised runner for nothing.
    text = LANE.read_text()
    for unused in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "OPENAI_API_KEY",
                   "ANTHROPIC_API_KEY", "R2_BUCKET_NAME"):
        assert unused not in text, f"lane requests {unused} but never uses it"


def test_the_run_finalises_before_the_runner_is_killed(lane: dict[str, Any]) -> None:
    # --max-seconds stops BETWEEN tags and still logs a summary; a runner timeout
    # kills mid-tag with no report. The first must land well before the second.
    step = next(s for s in lane["jobs"]["draw"]["steps"]
                if s.get("name") == "Draw candidates")
    budget_s = int(step["run"].split("--max-seconds ")[1].split()[0].strip('"'))
    assert budget_s < lane["jobs"]["draw"]["timeout-minutes"] * 60


def test_inputs_reach_the_script_through_env_not_interpolation(lane: dict[str, Any]) -> None:
    # `${{ inputs.x }}` pasted into a run block is shell injection by construction.
    # Values go via env; the run body may only reference the shell variables.
    step = next(s for s in lane["jobs"]["draw"]["steps"]
                if s.get("name") == "Draw candidates")
    assert "${{" not in step["run"]
    assert set(step["env"]) == {"TAG_ID", "COUNT", "TARGET", "CATEGORY", "DRY_RUN"}


# --- the exit code ----------------------------------------------------------


def _run_main(monkeypatch: pytest.MonkeyPatch, *, raises: bool) -> int:
    """Drive scripts.draw_tag_candidates.main over one fake tag."""
    import scripts.draw_tag_candidates as mod
    from toolkit import tag_candidates as tc

    class _Cur:
        def __enter__(self) -> "_Cur":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            return None

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [(1, "interier - koupelna", 0)]

    class _Conn:
        def __enter__(self) -> "_Conn":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def cursor(self) -> _Cur:
            return _Cur()

    monkeypatch.setattr(mod, "__doc__", mod.__doc__)
    monkeypatch.setattr("scraper.db.connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(tc, "count_verified_positives",
                        lambda *a, **k: tc.MIN_VERIFIED_POSITIVES + 1)

    def _draw(*a: Any, **k: Any) -> dict[str, Any]:
        if raises:
            raise RuntimeError("pool query cancelled")
        return {"inserted": 7, "status": "drawn", "requested": 120,
                "dropped_near_dup": 0, "dropped_property_cap": 0, "categories": []}

    monkeypatch.setattr(tc, "draw_candidates", _draw)
    monkeypatch.setattr("sys.argv", ["draw_tag_candidates", "--all-ready"])
    return mod.main()


def test_a_run_whose_every_draw_raised_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _run_main(monkeypatch, raises=True) == 1


def test_a_clean_run_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_main(monkeypatch, raises=False) == 0
