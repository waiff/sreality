"""Hermetic tests for scripts.verify_pipeline — pure status logic + thresholds.

No DB: the check functions' SQL is exercised in production; here we test the pure
status-derivation helpers, thresholds loading/fallback, and the one-failing-check
isolation of run_checks.
"""

from __future__ import annotations

import json
from typing import Any

from scripts.verify_pipeline import (
    DEFAULT_THRESHOLDS,
    _status_for_candidates,
    _status_for_cycle,
    _status_for_dirty,
    _status_for_llm_errors,
    _status_for_merge_latency,
    _status_for_street_debt,
    _worst,
    load_thresholds,
    run_checks,
)

T = DEFAULT_THRESHOLDS


# --- _worst ----------------------------------------------------------------


def test_worst_precedence() -> None:
    assert _worst(["ok", "warn", "fail"]) == "fail"
    assert _worst(["ok", "warn"]) == "warn"
    assert _worst(["ok", "ok"]) == "ok"
    assert _worst([]) == "ok"


# --- street_debt -----------------------------------------------------------


def test_street_debt_status_bands() -> None:
    assert _status_for_street_debt(0, T) == "ok"
    assert _status_for_street_debt(int(T["street_debt_warn"]), T) == "ok"        # == warn: not > → ok
    assert _status_for_street_debt(int(T["street_debt_warn"]) + 1, T) == "warn"
    assert _status_for_street_debt(int(T["street_debt_fail"]), T) == "warn"      # == fail: not > → warn
    assert _status_for_street_debt(int(T["street_debt_fail"]) + 1, T) == "fail"
    # The measured incident count (39,376) sits between warn (30k) and fail (45k).
    assert _status_for_street_debt(39_376, T) == "warn"


# --- merge_latency ---------------------------------------------------------


def test_merge_latency_status() -> None:
    assert _status_for_merge_latency(None, T) == "ok"
    assert _status_for_merge_latency(T["merge_p95_warn_hours"], T) == "ok"       # boundary: not >
    assert _status_for_merge_latency(T["merge_p95_warn_hours"] + 0.1, T) == "warn"


# --- cycle -----------------------------------------------------------------


def test_cycle_no_row_is_warn() -> None:
    assert _status_for_cycle(
        has_row=False, completed_age_hours=None, started_age_hours=None, thresholds=T,
    ) == "warn"


def test_cycle_recent_completion_ok_old_fail() -> None:
    assert _status_for_cycle(
        has_row=True, completed_age_hours=1.0, started_age_hours=2.0, thresholds=T,
    ) == "ok"
    assert _status_for_cycle(
        has_row=True, completed_age_hours=T["cycle_age_fail_hours"] + 1,
        started_age_hours=None, thresholds=T,
    ) == "fail"


def test_cycle_never_completed_gates_on_start_age() -> None:
    # In-progress first cycle that started recently: not yet a failure.
    assert _status_for_cycle(
        has_row=True, completed_age_hours=None, started_age_hours=2.0, thresholds=T,
    ) == "ok"
    # A cycle that started long ago and never completed: failure.
    assert _status_for_cycle(
        has_row=True, completed_age_hours=None,
        started_age_hours=T["cycle_age_fail_hours"] + 1, thresholds=T,
    ) == "fail"


# --- dirty / candidates ----------------------------------------------------


def test_dirty_and_candidate_warn_bands() -> None:
    assert _status_for_dirty(None, T) == "ok"
    assert _status_for_dirty(T["dirty_age_p95_warn_hours"], T) == "ok"
    assert _status_for_dirty(T["dirty_age_p95_warn_hours"] + 0.1, T) == "warn"
    assert _status_for_candidates(None, T) == "ok"
    assert _status_for_candidates(T["candidate_age_p95_warn_days"] + 0.1, T) == "warn"


# --- llm_errors ------------------------------------------------------------


def test_llm_errors_credit_balance_forces_fail() -> None:
    status, offenders = _status_for_llm_errors([], credit_balance_errors=1, thresholds=T)
    assert status == "fail" and offenders == []


def test_llm_errors_rate_needs_min_calls() -> None:
    # 3/5 = 60% but only 5 calls (< 20) → not counted.
    low_volume = [{"called_for": "parse_url", "total": 5, "errors": 3}]
    assert _status_for_llm_errors(low_volume, 0, T)[0] == "ok"
    # 6/20 = 30% > 20% with >= 20 calls → fail, and it's named.
    status, offenders = _status_for_llm_errors(
        [{"called_for": "score_listing_condition", "total": 20, "errors": 6}], 0, T,
    )
    assert status == "fail" and offenders == ["score_listing_condition"]


def test_llm_errors_clean_is_ok() -> None:
    clean = [{"called_for": "parse_url", "total": 100, "errors": 1}]
    assert _status_for_llm_errors(clean, 0, T) == ("ok", [])


# --- thresholds ------------------------------------------------------------


class _ThresholdConn:
    def __init__(self, value: Any) -> None:
        self._value = value

    def cursor(self) -> "_ThresholdConn":
        return self

    def execute(self, sql: str, params: Any = None) -> None:
        self._row = (self._value,) if self._value is not _MISSING else None

    def fetchone(self) -> Any:
        return self._row

    def __enter__(self) -> "_ThresholdConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


_MISSING = object()


def test_thresholds_missing_row_is_all_defaults() -> None:
    assert load_thresholds(_ThresholdConn(_MISSING)) == DEFAULT_THRESHOLDS


def test_thresholds_partial_override_merges_over_defaults() -> None:
    merged = load_thresholds(_ThresholdConn({"street_debt_fail": 99999}))
    assert merged["street_debt_fail"] == 99999
    assert merged["street_debt_warn"] == DEFAULT_THRESHOLDS["street_debt_warn"]


def test_thresholds_json_string_is_parsed() -> None:
    merged = load_thresholds(_ThresholdConn(json.dumps({"merge_p95_warn_hours": 6})))
    assert merged["merge_p95_warn_hours"] == 6


def test_thresholds_ignores_non_numeric_values() -> None:
    merged = load_thresholds(_ThresholdConn({"street_debt_fail": "lots"}))
    assert merged["street_debt_fail"] == DEFAULT_THRESHOLDS["street_debt_fail"]


# --- run_checks isolation --------------------------------------------------


def test_one_failing_check_does_not_abort_the_run(monkeypatch: Any) -> None:
    import scripts.verify_pipeline as vp

    def ok_check(conn: Any, thresholds: Any) -> dict[str, Any]:
        return {"check_key": "ok_one", "status": "ok", "value": 1, "details": {}}

    def boom_check(conn: Any, thresholds: Any) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(vp, "_CHECKS", [("ok_one", ok_check), ("boom", boom_check)])
    monkeypatch.setattr(vp, "_WEEKLY_CHECKS", [])

    results = run_checks(conn=None, thresholds={}, weekly=False)
    by_key = {r["check_key"]: r for r in results}
    assert by_key["ok_one"]["status"] == "ok"
    assert by_key["boom"]["status"] == "fail"
    assert "kaboom" in by_key["boom"]["details"]["error"]


def test_weekly_flag_adds_weekly_checks(monkeypatch: Any) -> None:
    import scripts.verify_pipeline as vp

    monkeypatch.setattr(vp, "_CHECKS", [])
    monkeypatch.setattr(
        vp, "_WEEKLY_CHECKS",
        [("weekly_one", lambda c, t: {"check_key": "weekly_one", "status": "ok",
                                      "value": 0, "details": {}})],
    )
    assert run_checks(None, {}, weekly=False) == []
    assert [r["check_key"] for r in run_checks(None, {}, weekly=True)] == ["weekly_one"]
