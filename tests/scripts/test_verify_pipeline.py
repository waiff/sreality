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
    _status_for_cron,
    _status_for_burn,
    _status_for_llm_errors,
    _status_for_llm_silence,
    _status_for_worker,
    load_thresholds,
    run_checks,
)

T = DEFAULT_THRESHOLDS


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


def test_property_maintenance_healthy_day_is_ok() -> None:
    """~24-25h sweep age just before the next daily sweep is the healthy
    steady state, not a warning; an empty dirty queue (None) trips nothing."""
    from scripts.verify_pipeline import _status_for_property_maintenance

    t = DEFAULT_THRESHOLDS
    assert _status_for_property_maintenance(24.5, 0.1, t) == ("ok", [])
    assert _status_for_property_maintenance(2.0, None, t) == ("ok", [])


def test_property_maintenance_missing_stamp_warns_not_fails() -> None:
    """No stamp on record = the state between deploying this check and the
    first complete sweep. Permanently red would train the operator to ignore
    the check; silently green would hide a sweep that has never completed."""
    from scripts.verify_pipeline import _status_for_property_maintenance

    status, offenders = _status_for_property_maintenance(None, 0.1, DEFAULT_THRESHOLDS)
    assert status == "warn"
    assert any("no complete-sweep stamp" in o for o in offenders)


def test_property_maintenance_worst_axis_wins() -> None:
    from scripts.verify_pipeline import _status_for_property_maintenance

    t = DEFAULT_THRESHOLDS
    # Warn axis alone → warn; a fail axis anywhere → fail overall.
    status, offenders = _status_for_property_maintenance(27.0, None, t)
    assert status == "warn" and "last complete sweep" in offenders[0]
    status, offenders = _status_for_property_maintenance(27.0, 4.0, t)
    assert status == "fail"
    assert any("dirty-queue" in o for o in offenders)
    # The 2026-08-06 incident shape: sweep dead for days + frozen dirt.
    status, offenders = _status_for_property_maintenance(49.0, 2.8, t)
    assert status == "fail" and len(offenders) == 2


def test_property_dirty_warn_clears_a_full_length_sweep() -> None:
    """The daily sweep holds the maintenance lease for its whole budget
    (_MAX_BUDGET_SECONDS, 100 min) and only clears dirty_properties at the end,
    so oldest-dirt ages 1:1 with sweep elapsed. The warn threshold must sit
    ABOVE that hold or a perfectly healthy long sweep turns the axis amber and
    trains the operator to ignore it — the anti-pattern this module warns about.
    Raising the sweep budget means raising this threshold in the same change."""
    import scripts.recompute_property_stats as rps
    from scripts.verify_pipeline import _status_for_property_maintenance

    max_hold_h = rps._MAX_BUDGET_SECONDS / 3600
    assert DEFAULT_THRESHOLDS["property_dirty_warn_hours"] > max_hold_h
    # ...and a sweep running its full budget stays green on the dirty axis.
    assert _status_for_property_maintenance(1.0, max_hold_h, DEFAULT_THRESHOLDS) == ("ok", [])


def test_property_maintenance_offenders_do_not_round_a_fractional_threshold() -> None:
    """`(warn > 2h)` next to `2.6h` reads as a contradiction. The dirty
    thresholds are fractional, so the rendering must not round them away."""
    from scripts.verify_pipeline import _status_for_property_maintenance

    t = dict(DEFAULT_THRESHOLDS, property_dirty_warn_hours=2.5)
    _, offenders = _status_for_property_maintenance(1.0, 2.6, t)
    assert offenders == ["oldest dirty-queue row 2.6h (warn > 2.5h)"]


def test_property_maintenance_sql_is_o1() -> None:
    """The check runs in the hourly acute lane (job timeout 5 min): it must
    read the completion stamp + the tiny dirty queue, never scan `properties`
    — the per-row staleness variant measured ~3.5 min live and would have
    killed the whole lane's rows and alerts each hour."""
    from scripts.verify_pipeline import _PROPERTY_MAINTENANCE_SQL as sql

    assert "property_sweep_last_complete" in sql
    assert "dirty_properties" in sql
    assert "from properties" not in sql.lower()
    assert "listings" not in sql.lower()


# --- broker resolution freshness (2026-08-12 E2E review) --------------------


def test_broker_resolution_healthy_day_is_ok() -> None:
    """A lap that closed on the second daily run — the measured steady state, since
    attribution truncates on its budget on most days — is the healthy case."""
    from scripts.verify_pipeline import _status_for_broker_resolution

    assert _status_for_broker_resolution(24.5, 0.2, DEFAULT_THRESHOLDS) == ("ok", [])
    assert _status_for_broker_resolution(49.0, 0.2, DEFAULT_THRESHOLDS) == ("ok", [])
    assert _status_for_broker_resolution(2.0, None, DEFAULT_THRESHOLDS) == ("ok", [])


def test_broker_resolution_missing_stamp_warns_not_fails() -> None:
    """The state between deploying this check and the first complete sweep."""
    from scripts.verify_pipeline import _status_for_broker_resolution

    status, offenders = _status_for_broker_resolution(None, 0.1, DEFAULT_THRESHOLDS)
    assert status == "warn"
    assert any("no complete-sweep stamp" in o for o in offenders)


def test_broker_resolution_catches_the_rotation_that_stopped_advancing() -> None:
    """The bug this check exists for: the sweep broke out on its budget every day
    and still exited 0, so the tail above the break was never attributed. The signal
    is now the ROTATION's lap age — a rotation that stops getting round the corpus
    (a cursor that stops advancing, a lock never won, a sweep that dies before its
    first chunk) ages past the fail line even though every individual run is red-free."""
    from scripts.verify_pipeline import _status_for_broker_resolution

    status, offenders = _status_for_broker_resolution(90.0, 0.1, DEFAULT_THRESHOLDS)
    assert status == "fail"
    assert any("last complete broker sweep" in o for o in offenders)
    # ...and the two axes are independent: a frozen incremental drain alone reds.
    status, offenders = _status_for_broker_resolution(2.0, 5.0, DEFAULT_THRESHOLDS)
    assert status == "fail"
    assert any("broker dirty-queue" in o for o in offenders)


def test_broker_sweep_axis_is_reachable_as_fail_before_any_lap_closes() -> None:
    """Without the open-lap fallback a rotation that NEVER completes a lap would sit
    on the missing-stamp warn forever, and warn rings nothing: emit_transition_alerts
    only fires on fail, and --exit-nonzero-on-fail only exits on fail. The check
    would have been inert for exactly the condition it was written for."""
    from scripts.verify_pipeline import _status_for_broker_resolution

    label = "open rotation lap (no lap closed yet)"
    status, offenders = _status_for_broker_resolution(
        200.0, 0.1, DEFAULT_THRESHOLDS, sweep_label=label)
    assert status == "fail"
    assert any(label in o for o in offenders)


def test_broker_thresholds_clear_the_workflow_backstop() -> None:
    """The full sweep holds broker_resolution_lock for its whole run (every */10
    incremental skips meanwhile) and clears dirty_broker_listings only at finalize,
    so the dirty axis ages 1:1 with the sweep — bounded by resolve_brokers_full.yml's
    timeout-minutes. The sweep axis measures a rotation LAP, which takes one or two
    daily runs at the measured attribution throughput, so warn must clear two runs
    plus that backstop and fail must clear three: below either, a healthy rotation
    turns the check amber (or emails hourly) and trains the operator to ignore it.
    Raising the workflow backstop means raising these in the same change."""
    import pathlib
    import re

    from scripts.verify_pipeline import _status_for_broker_resolution

    yml = pathlib.Path(__file__).resolve().parents[2] / (
        ".github/workflows/resolve_brokers_full.yml")
    backstop_h = int(re.search(r"^\s*timeout-minutes:\s*(\d+)", yml.read_text(),
                               re.MULTILINE)[1]) / 60
    assert DEFAULT_THRESHOLDS["broker_dirty_warn_hours"] > backstop_h
    assert DEFAULT_THRESHOLDS["broker_sweep_warn_hours"] > 2 * 24 + backstop_h
    assert DEFAULT_THRESHOLDS["broker_sweep_fail_hours"] > 3 * 24 + backstop_h
    # ...and a two-run lap finishing at its full backstop stays green on both axes.
    assert _status_for_broker_resolution(
        48 + backstop_h, backstop_h, DEFAULT_THRESHOLDS) == ("ok", [])


def test_a_dead_sweep_tail_is_caught_by_the_finished_run_axis() -> None:
    """_record_sweep_progress stamps lap completion right after attribution, ~17-25
    min before the tail (cross-source merge, rollups, matview, candidates, the
    dirty-clear) that _finalize closes with ended_at. So a sweep whose tail dies
    leaves a MINUTES-old lap stamp, and the */10 incrementals keep the dirty queue
    young — both of the original axes report healthy while the leaderboard and
    rollups have silently stopped. Only the finished-run axis sees it."""
    from scripts.verify_pipeline import _status_for_broker_resolution

    # the exact shape: lap just stamped, dirty queue fresh, tail dead for 3 days
    assert _status_for_broker_resolution(0.4, 0.3, DEFAULT_THRESHOLDS) == ("ok", [])
    status, offenders = _status_for_broker_resolution(
        0.4, 0.3, DEFAULT_THRESHOLDS, finished_age_hours=72.0)
    assert status == "fail"
    assert any("last finished full sweep" in o for o in offenders)
    status, offenders = _status_for_broker_resolution(
        0.4, 0.3, DEFAULT_THRESHOLDS, finished_age_hours=36.0)
    assert status == "warn"
    assert any("last finished full sweep" in o for o in offenders)


def test_finished_run_axis_grades_tighter_than_the_lap_axis() -> None:
    """The lap spans one to three daily runs, so 52/84 is deliberately slack. The
    tail runs on EVERY sweep regardless of lap closure, so steady state is ~24h and
    reusing the lap thresholds would let two dead nights pass as healthy."""
    assert (DEFAULT_THRESHOLDS["broker_finished_warn_hours"]
            < DEFAULT_THRESHOLDS["broker_sweep_warn_hours"])
    assert (DEFAULT_THRESHOLDS["broker_finished_fail_hours"]
            < DEFAULT_THRESHOLDS["broker_sweep_fail_hours"])
    # ...and one ordinary daily sweep plus the workflow backstop stays green.
    assert DEFAULT_THRESHOLDS["broker_finished_warn_hours"] > 24 + 1.9


# The gap between consecutive ended_at is a whole number of daily runs plus the
# spread in when a run finishes: GH's scheduled-run delay, the <=21-min lock wait
# and the run itself. Live spread to 2026-08-12 is ~2.5h (06:25 to 08:52 UTC on a
# 04:35 cron), and the workflow backstop bounds only the last of those three.
_FINISH_SPREAD_H = 2.5


def test_one_missed_night_warns_and_two_fail() -> None:
    """The calibration this axis was sized for, pinned on BOTH sides. A single
    missed night is 48h plus the finish spread — up to ~50.5h — so a fail line at
    50 fired the hourly acute lane for it: an onset alert from
    emit_transition_alerts plus a non-zero exit from --exit-nonzero-on-fail, a
    second email for the night the sweep's own red run already reported. Two missed
    nights are >=69.5h even when the recovery run finishes at its earliest, so the
    fail line has to live strictly between the two — which is what makes the axis
    mean 'the tail has stopped', not 'a night was skipped'."""
    import pathlib
    import re

    from scripts.verify_pipeline import _status_for_broker_resolution

    yml = pathlib.Path(__file__).resolve().parents[2] / (
        ".github/workflows/resolve_brokers_full.yml")
    backstop_h = int(re.search(r"^\s*timeout-minutes:\s*(\d+)", yml.read_text(),
                               re.MULTILINE)[1]) / 60
    fail_h = DEFAULT_THRESHOLDS["broker_finished_fail_hours"]
    # the same convention the lap axis uses (N runs + the backstop drift), then the
    # wider real spread, which the backstop alone does not bound
    assert fail_h > 2 * 24 + backstop_h
    assert fail_h > 2 * 24 + _FINISH_SPREAD_H
    assert fail_h < 3 * 24 - _FINISH_SPREAD_H

    # ...and the graded axis agrees: worst single miss ambers, best double miss reds.
    status, offenders = _status_for_broker_resolution(
        0.4, 0.3, DEFAULT_THRESHOLDS, finished_age_hours=2 * 24 + _FINISH_SPREAD_H)
    assert status == "warn"
    assert any("last finished full sweep" in o for o in offenders)
    status, _ = _status_for_broker_resolution(
        0.4, 0.3, DEFAULT_THRESHOLDS, finished_age_hours=3 * 24 - _FINISH_SPREAD_H)
    assert status == "fail"
    # an ordinary night, finish spread included, stays green
    assert _status_for_broker_resolution(
        0.4, 0.3, DEFAULT_THRESHOLDS,
        finished_age_hours=24 + _FINISH_SPREAD_H) == ("ok", [])


def test_a_missing_finished_run_is_skipped_not_a_fail() -> None:
    """A fresh DB (or a rename on broker_resolution_runs) yields NULL. Only the LAP
    stamp's absence is the deploy-day warn; a third axis that red on NULL would ring
    the operator's hourly email lane for a database that has simply never run."""
    from scripts.verify_pipeline import _status_for_broker_resolution

    assert _status_for_broker_resolution(
        24.0, 0.2, DEFAULT_THRESHOLDS, finished_age_hours=None) == ("ok", [])
    status, offenders = _status_for_broker_resolution(
        None, 0.2, DEFAULT_THRESHOLDS, finished_age_hours=None)
    assert status == "warn"
    assert all("last finished full sweep" not in o for o in offenders)


def test_broker_resolution_check_reads_all_three_axes_off_one_row() -> None:
    """One O(1) round trip: the finished-run axis is another scalar in the same
    single-row query, not a second read in the hourly lane's 5-min budget."""
    from scripts.verify_pipeline import check_broker_resolution_freshness

    class _Row:
        def __init__(self, row: tuple[Any, ...]) -> None:
            self.row, self.executed = row, []

        def cursor(self) -> "_Row":
            return self

        def transaction(self) -> "_Row":
            return self

        def __enter__(self) -> "_Row":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            self.executed.append(sql)

        def fetchone(self) -> Any:
            return self.row

    conn = _Row((0.4, 0.4, 61.0, 0.3, 12))
    out = check_broker_resolution_freshness(conn, DEFAULT_THRESHOLDS)
    assert len([s for s in conn.executed if "select" in s.lower()]) == 1
    assert out["status"] == "fail"
    assert out["details"]["finished_age_hours"] == 61.0
    assert out["details"]["dirty_depth"] == 12


def test_broker_resolution_sql_is_o1() -> None:
    """The hourly acute lane's 5-min job timeout. resolve_brokers deleted exactly
    this scan from its own incremental: `broker_identity_id IS NULL` is a permanent
    state for ~110k listings, so it detoasted the whole raw_json corpus for ~7
    genuine stragglers and timed out."""
    from scripts.verify_pipeline import _BROKER_RESOLUTION_SQL as sql

    assert "broker_resolution_last_complete" in sql
    assert "dirty_broker_listings" in sql
    # the completion axis reads only FINISHED full runs — an unfinished run has a
    # NULL ended_at and must not be mistaken for a sweep that got to the end
    assert "broker_resolution_runs" in sql
    assert "r.mode = 'full' and r.ended_at is not null" in sql
    assert "listings l" not in sql.lower() and "broker_identity_id" not in sql.lower()


def test_broker_completion_stamp_key_matches_the_writer() -> None:
    """The check and the sweep are one contract across two modules — a rename on
    either side would silently produce a permanently-missing stamp (a soft warn),
    not an error. Both keys count: the stamp AND the cursor the open-lap fallback
    ages when no lap has closed."""
    import scripts.resolve_brokers as rb
    from scripts.verify_pipeline import _BROKER_RESOLUTION_SQL

    assert rb._SWEEP_COMPLETE_KEY == "broker_resolution_last_complete"
    assert f"'{rb._SWEEP_COMPLETE_KEY}'" in _BROKER_RESOLUTION_SQL
    assert f"'{rb._SWEEP_CURSOR_KEY}'" in _BROKER_RESOLUTION_SQL
    assert "completed_at" in rb._STAMP_SWEEP_COMPLETE_SQL
    assert "lap_started_at" in rb._WRITE_SWEEP_CURSOR_SQL


def test_broker_merge_suppression_is_ok_on_an_empty_rail() -> None:
    """The table starts empty and only grows by operator action, so "no rows" is the
    healthy steady state, not a missing signal."""
    from scripts.verify_pipeline import check_broker_merge_suppression

    out = check_broker_merge_suppression(_OneRow((0, 0, 0)), DEFAULT_THRESHOLDS)
    assert out["status"] == "ok"
    assert out["details"] == {"active_suppressions": 0, "lifted": 0, "violations": 0}


def test_a_single_bypassed_suppression_fails_the_check() -> None:
    """THE invariant of the rail: an active suppression whose two identities sit
    under one broker means an operator NO was bypassed. There is no warn tier — the
    pair is either separated or it is not."""
    from scripts.verify_pipeline import check_broker_merge_suppression

    out = check_broker_merge_suppression(_OneRow((4, 2, 1)), DEFAULT_THRESHOLDS)
    assert out["status"] == "fail" and out["value"] == 1
    assert out["details"] == {"active_suppressions": 4, "lifted": 2, "violations": 1}
    assert "bypassed" in out["message"]


def test_a_lifted_suppression_is_not_a_violation() -> None:
    """An explicit operator merge lifts the suppression and the two identities then
    legitimately share a broker — the query has to exclude lifted rows or every
    override would red the check."""
    from scripts.verify_pipeline import _BROKER_SUPPRESSION_SQL as sql

    assert "s.lifted_at is null" in sql
    assert sql.count("join broker_identities") == 2
    assert "lo.broker_id = hi.broker_id" in sql
    out = check_broker_merge_suppression_ok()
    assert out["status"] == "ok" and out["details"]["lifted"] == 9


def check_broker_merge_suppression_ok() -> Any:
    from scripts.verify_pipeline import check_broker_merge_suppression

    return check_broker_merge_suppression(_OneRow((3, 9, 0)), DEFAULT_THRESHOLDS)


class _OneRow:
    """Minimal single-row connection (the check is one O(1) scalar query)."""

    def __init__(self, row: tuple[Any, ...]) -> None:
        self.row, self.executed = row, []

    def cursor(self) -> "_OneRow":
        return self

    def transaction(self) -> "_OneRow":
        return self

    def __enter__(self) -> "_OneRow":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)

    def fetchone(self) -> Any:
        return self.row


def test_broker_suppression_check_is_registered() -> None:
    """An unregistered check is dead code that never writes a row."""
    from scripts.verify_pipeline import _CHECKS, check_broker_merge_suppression

    assert ("broker_merge_suppression", check_broker_merge_suppression) in _CHECKS


def test_broker_check_is_registered() -> None:
    """An unregistered check is dead code that never writes a row."""
    from scripts.verify_pipeline import _CHECKS, check_broker_resolution_freshness

    assert ("broker_resolution_freshness", check_broker_resolution_freshness) in _CHECKS


def test_acute_lane_only_list_resolves_to_registered_checks() -> None:
    """Registration in _CHECKS alone only buys the 6-hourly full run, which rings
    the in-app bell and nothing else. `--exit-nonzero-on-fail` — the channel that
    actually emails the operator — runs in llm_health.yml's hourly lane, and its
    `--only` list is a string in a yml nothing pinned: dropping a key from it left
    the whole suite green while the check silently fell back to bell-only."""
    import pathlib
    import re

    from scripts.verify_pipeline import _CHECKS

    yml = (pathlib.Path(__file__).resolve().parents[2]
           / ".github/workflows/llm_health.yml").read_text()
    only = re.search(r"--only\s+([\w,]+)", yml)[1].split(",")
    registered = {key for key, _ in _CHECKS}
    assert set(only) <= registered, set(only) - registered
    assert "broker_resolution_freshness" in only
    assert "--exit-nonzero-on-fail" in yml


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
    merged = load_thresholds(_ThresholdConn({"llm_spend_24h_fail_usd": 99999}))
    assert merged["llm_spend_24h_fail_usd"] == 99999
    assert merged["llm_spend_24h_warn_usd"] == DEFAULT_THRESHOLDS["llm_spend_24h_warn_usd"]


def test_thresholds_json_string_is_parsed() -> None:
    merged = load_thresholds(_ThresholdConn(json.dumps({"llm_silence_fail_hours": 6})))
    assert merged["llm_silence_fail_hours"] == 6


def test_thresholds_ignores_non_numeric_values() -> None:
    merged = load_thresholds(_ThresholdConn({"llm_spend_24h_fail_usd": "lots"}))
    assert merged["llm_spend_24h_fail_usd"] == DEFAULT_THRESHOLDS["llm_spend_24h_fail_usd"]


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
    # (gap_a, mismatch_a, orphan_a, gap_b, mismatch_b, orphan_b, scanned)
    res = _parity(_all_carrier_names(), {"listing_visual_matches": (1, 0, 0, 2, 0, 0, 50)})
    assert res["status"] == "fail"
    assert res["details"]["gaps"] == {
        "listing_visual_matches.listing_id_a": 1,
        "listing_visual_matches.listing_id_b": 2,
    }


def test_parity_orphan_null_legacy_and_null_surrogate_is_reported() -> None:
    """Post-Gate-2-flip shape: a new non-sreality row has NULL legacy id by design,
    so the gap/mismatch filters (both anchored on legacy IS NOT NULL) can't see it —
    only the orphan bucket (legacy IS NULL and surrogate IS NULL too) catches it."""
    res = _parity(_all_carrier_names(), {"images": (0, 0, 3, 50)})
    assert res["status"] == "fail"
    assert res["details"]["gaps"] == {}
    assert res["details"]["mismatches"] == {}
    assert res["details"]["orphans"] == {"images.listing_id": 3}
    assert res["value"] == 3


def test_parity_registry_is_the_shared_one() -> None:
    """The parity check and the backfill MUST walk the same carrier list — a table
    in one and not the other is a silent hole (unwatched, or never filled)."""
    from scripts.verify_pipeline import _PARITY_CARRIERS
    from toolkit.listing_identity import R2_CARRIERS

    assert _PARITY_CARRIERS is R2_CARRIERS


def test_notification_dispatches_skips_system_health_rows() -> None:
    """A system_health bell row anchors on no listing by construction, so its NULL/NULL
    is correct — counting it as an orphan pinned the check red from 2026-08-02, and its
    own recovery alert re-broke it one run after every recovery."""
    from scripts.verify_pipeline import _parity_carrier_sql
    from toolkit.listing_identity import R2_CARRIERS_BY_TABLE

    carrier = R2_CARRIERS_BY_TABLE["notification_dispatches"]
    assert carrier["skip"] == "t.source_kind = 'system_health'"
    sql = _parity_carrier_sql(carrier)
    assert sql.count("not (t.source_kind = 'system_health')") == 3


def test_carrier_skip_is_applied_by_counting_and_by_updating() -> None:
    """Skip one side but not the other and `remaining` never reaches zero, which
    re-dispatches the self-chaining backfill workflow forever."""
    from scripts.backfill_child_listing_ids import _predicate
    from toolkit.listing_identity import R2_CARRIERS

    for carrier in R2_CARRIERS:
        skip = carrier.get("skip")
        if not skip:
            continue
        for legacy, new in carrier["cols"]:
            for repair in (False, True):
                assert f"NOT ({skip})" in _predicate(new, legacy, repair, skip)
