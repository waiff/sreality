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
    _status_for_cron,
    _status_for_burn,
    _status_for_llm_errors,
    _status_for_llm_silence,
    _status_for_merge_latency,
    _status_for_worker,
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


def test_cycle_no_row_or_no_progress_is_warn() -> None:
    assert _status_for_cycle(has_row=False, updated_age_hours=None, thresholds=T) == "warn"
    assert _status_for_cycle(has_row=True, updated_age_hours=None, thresholds=T) == "warn"


def test_cycle_progressing_is_ok_even_if_never_completes() -> None:
    # The street cycle takes ~2 weeks and never "completes" — a recently-advanced cursor is
    # healthy, NOT a failure (the old age-based check was structurally always-red here).
    assert _status_for_cycle(has_row=True, updated_age_hours=4.0, thresholds=T) == "ok"


def test_cycle_stalled_cursor_fails() -> None:
    stall = T["cycle_stall_fail_hours"]
    assert _status_for_cycle(has_row=True, updated_age_hours=stall, thresholds=T) == "ok"
    assert _status_for_cycle(has_row=True, updated_age_hours=stall + 0.1, thresholds=T) == "fail"


# --- dirty / candidates ----------------------------------------------------


def test_dirty_and_candidate_warn_bands() -> None:
    assert _status_for_dirty(None, T) == "ok"
    assert _status_for_dirty(T["dirty_age_p95_warn_hours"], T) == "ok"
    assert _status_for_dirty(T["dirty_age_p95_warn_hours"] + 0.1, T) == "warn"
    assert _status_for_candidates(None, T) == "ok"
    assert _status_for_candidates(T["candidate_age_p95_warn_days"] + 0.1, T) == "warn"


# --- llm_errors ------------------------------------------------------------


def test_llm_errors_credit_live_forces_fail() -> None:
    status, offenders = _status_for_llm_errors(
        [], credit_live=True, currently_failing=True, thresholds=T,
    )
    assert status == "fail" and offenders == []


def test_llm_errors_recovered_credit_window_is_ok() -> None:
    # The 2026-07-09 stale-alarm regression: credit errors sit in the 24h window but a
    # success has flowed since, so it's NOT live — must be ok, not "everything is down".
    assert _status_for_llm_errors(
        [{"called_for": "compare_listings_visually", "total": 40, "errors": 30}],
        credit_live=False, currently_failing=False, thresholds=T,
    ) == ("ok", [])


def test_llm_errors_rate_only_fails_while_live() -> None:
    offender = [{"called_for": "score_listing_condition", "total": 20, "errors": 6}]  # 30% > 20%, >= 20
    # Live → fail, named.
    status, offenders = _status_for_llm_errors(offender, False, True, T)
    assert status == "fail" and offenders == ["score_listing_condition"]
    # Same window but recovered (not live) → ok. This is the trailing-window fix.
    assert _status_for_llm_errors(offender, False, False, T) == ("ok", [])
    # Live but only 5 calls (< 20) → too little signal → ok.
    low_volume = [{"called_for": "parse_url", "total": 5, "errors": 3}]
    assert _status_for_llm_errors(low_volume, False, True, T)[0] == "ok"


def test_llm_errors_clean_is_ok() -> None:
    clean = [{"called_for": "parse_url", "total": 100, "errors": 1}]
    assert _status_for_llm_errors(clean, False, True, T) == ("ok", [])


def test_llm_silence_fails_when_stale_or_absent() -> None:
    fail_h = T["llm_silence_fail_hours"]
    assert _status_for_llm_silence(0.02, fail_h) == "ok"      # ~1 min ago (normal)
    assert _status_for_llm_silence(fail_h, fail_h) == "ok"    # exactly at threshold, not over
    assert _status_for_llm_silence(fail_h + 0.1, fail_h) == "fail"  # silent past threshold
    assert _status_for_llm_silence(None, fail_h) == "fail"    # no calls on record at all


def test_burn_rate_thresholds() -> None:
    warn, fail = T["llm_spend_24h_warn_usd"], T["llm_spend_24h_fail_usd"]
    assert _status_for_burn(10.0, warn, fail) == "ok"          # normal daily burn
    assert _status_for_burn(warn, warn, fail) == "ok"          # at warn boundary, not over
    assert _status_for_burn(warn + 1, warn, fail) == "warn"    # top-up cadence risk (no bell/email)
    assert _status_for_burn(fail, warn, fail) == "warn"        # at fail boundary, not over
    assert _status_for_burn(fail + 1, warn, fail) == "fail"    # runaway burn -> email


# --- db_saturation / worker_liveness (new blind-spot detectors) ------------


def test_cron_flags_only_jobs_over_rate_with_enough_runs() -> None:
    jobs = [
        {"jobname": "refresh-health-dashboard", "ok": 14, "failed": 22},  # 61% of 36 → offender
        {"jobname": "browse-list-rebuild", "ok": 71, "failed": 1},        # 1.4% → fine
        {"jobname": "flaky-but-rare", "ok": 1, "failed": 1},              # 50% but only 2 runs → ignored
    ]
    status, offenders = _status_for_cron(jobs, fail_rate=0.5)
    assert status == "fail"
    assert offenders == ["refresh-health-dashboard 22/36"]


def test_cron_all_healthy_is_ok() -> None:
    jobs = [{"jobname": "browse-list-rebuild", "ok": 71, "failed": 1}]
    assert _status_for_cron(jobs, 0.5) == ("ok", [])
    assert _status_for_cron([], 0.5) == ("ok", [])  # no jobs in window → ok


def test_worker_liveness_fails_only_when_stale() -> None:
    assert _status_for_worker([("realtime-worker", 0.2)], stale_minutes=5) == ("ok", [])
    assert _status_for_worker([], 5) == ("ok", [])  # no worker deployed → not this check's job
    status, stale = _status_for_worker([("realtime-worker", 42.0)], 5)
    assert status == "fail" and stale == ["realtime-worker (42m)"]


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


# --- R2 dual-write parity --------------------------------------------------


class _ParityConn:
    """Serves the armed-carriers query, then one aggregate row per carrier query."""

    def __init__(self, armed: set[str], per_table: dict[str, tuple[int, ...]]) -> None:
        self._armed = armed
        self._per_table = per_table
        self.queries: list[str] = []

    def cursor(self) -> "_ParityConn":
        return self

    def transaction(self) -> "_ParityConn":
        return self

    def execute(self, sql: str, params: Any = None) -> None:
        self.queries.append(sql)
        if sql.startswith("SET LOCAL"):
            return
        if "select child from dual_write_watermark" in sql:
            self._rows = [(c,) for c in sorted(self._armed)]
            return
        # Key off the watermark predicate, not the first " from ": the counting
        # query contains subquery FROMs (listings, and the table's own max())
        # ahead of its real one. The clean default is sized from the query itself
        # — a pair carrier returns two counters per side plus the row total.
        table = sql.split("w.child = '")[1].split("'")[0]
        clean = (0,) * sql.count("count(*)")
        self._rows = [self._per_table.get(table, clean)]

    def fetchall(self) -> Any:
        return self._rows

    def __enter__(self) -> "_ParityConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _parity(armed: set[str], per_table: dict[str, tuple[int, ...]]) -> dict[str, Any]:
    from scripts.verify_pipeline import check_dual_write_parity

    return check_dual_write_parity(_ParityConn(armed, per_table), T)


def _all_carrier_names() -> set[str]:
    from scripts.verify_pipeline import _PARITY_CARRIERS

    return {c["table"] for c in _PARITY_CARRIERS}


def test_parity_unarmed_is_warn_never_ok() -> None:
    """An unarmed carrier must never read as clean: its aggregate-only query returns
    a row of zeros with no watermark, so armedness is established separately."""
    res = _parity(set(), {})
    assert res["status"] == "warn"
    assert "INERT" in res["message"]
    assert set(res["details"]["unarmed"]) == _all_carrier_names()


def test_parity_partially_armed_warns_and_names_the_gap() -> None:
    armed = _all_carrier_names() - {"images"}
    res = _parity(armed, {})
    assert res["status"] == "warn"
    assert res["details"]["unarmed"] == ["images"]


def test_parity_all_armed_and_clean_is_ok() -> None:
    res = _parity(_all_carrier_names(), {})
    assert res["status"] == "ok"
    assert res["value"] == 0
    assert res["details"]["gaps"] == {} and res["details"]["mismatches"] == {}


def test_parity_missing_surrogate_fails() -> None:
    res = _parity(_all_carrier_names(), {"images": (7, 0, 100)})
    assert res["status"] == "fail"
    assert res["details"]["gaps"] == {"images.listing_id": 7}
    assert "missing surrogate" in res["message"]


def test_parity_wrong_surrogate_fails() -> None:
    """A mismatch is the positional-zip bug: a surrogate that belongs to another row."""
    res = _parity(_all_carrier_names(), {"listing_snapshots": (0, 3, 100)})
    assert res["status"] == "fail"
    assert res["details"]["mismatches"] == {"listing_snapshots.listing_id": 3}
    assert "WRONG surrogate" in res["message"]


def test_parity_pair_carrier_reports_each_side() -> None:
    res = _parity(_all_carrier_names(), {"listing_visual_matches": (1, 0, 2, 0, 50)})
    assert res["status"] == "fail"
    assert res["details"]["gaps"] == {
        "listing_visual_matches.listing_id_a": 1,
        "listing_visual_matches.listing_id_b": 2,
    }


def test_parity_registry_is_the_shared_one() -> None:
    """The parity check and the backfill MUST walk the same carrier list — a table
    in one and not the other is a silent hole (unwatched, or never filled)."""
    from scripts.verify_pipeline import _PARITY_CARRIERS
    from toolkit.listing_identity import R2_CARRIERS

    assert _PARITY_CARRIERS is R2_CARRIERS
