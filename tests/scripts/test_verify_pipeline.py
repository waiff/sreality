"""Hermetic tests for scripts.verify_pipeline — pure status logic + thresholds.

No DB: the check functions' SQL is exercised in production; here we test the pure
status-derivation helpers, thresholds loading/fallback, and the one-failing-check
isolation of run_checks.
"""

from __future__ import annotations

import json
from typing import Any

from pathlib import Path

from scripts.migration_objects import load_migrations
from scripts.verify_pipeline import (
    DEFAULT_THRESHOLDS,
    _SAFE_IDENT,
    check_acquisition_lag,
    check_migration_drift,
    check_worker_lane_stall,
    check_walk_coverage,
    _status_for_cron,
    _status_for_burn,
    _status_for_llm_errors,
    _status_for_llm_silence,
    _status_for_worker,
    load_thresholds,
    run_checks,
)

T = DEFAULT_THRESHOLDS
_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


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
    # Derived from the threshold, not hardcoded: the dirty lines move whenever
    # resolve_brokers_full.yml's backstop does (they are asserted against it above),
    # and a literal sentinel silently decays into a warn when they do.
    status, offenders = _status_for_broker_resolution(
        2.0, DEFAULT_THRESHOLDS["broker_dirty_fail_hours"] + 1.0, DEFAULT_THRESHOLDS)
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


class _OneRow:
    """Minimal single-row connection, for the checks that are one O(1) scalar query."""

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


def test_broker_resolution_check_reads_all_three_axes_off_one_row() -> None:
    """One O(1) round trip: the finished-run axis is another scalar in the same
    single-row query, not a second read in the hourly lane's 5-min budget."""
    from scripts.verify_pipeline import check_broker_resolution_freshness

    conn = _OneRow((0.4, 0.4, 61.0, 0.3, 12))
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
    from scripts.verify_pipeline import check_broker_merge_suppression

    assert "s.lifted_at is null" in sql
    assert sql.count("join broker_identities") == 2
    assert "lo.broker_id = hi.broker_id" in sql
    out = check_broker_merge_suppression(_OneRow((3, 9, 0)), DEFAULT_THRESHOLDS)
    assert out["status"] == "ok" and out["details"]["lifted"] == 9


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
    # the suppression invariant is O(1) and binary: registration alone would leave it
    # ringing the in-app bell only, and a bypassed operator NO never emails anyone
    assert "broker_merge_suppression" in only
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


# --- per-m2 measure plausibility (W9) --------------------------------------
#
# Every cell below is a LIVE measurement of production taken 2026-08-25, before the
# W2 backfill healed either defect. The point of the wave is that the existing health
# surfaces are structurally blind to both — data_quality_by_source tests 29 fields for
# IS NOT NULL and both defects produce 100% non-NULL values — so these tests pin that
# the new checks see what the null-checks cannot, and equally that they stay quiet on
# the eight portals that were fine all along.


_MEASURE_CHECK_KEYS = {
    "ppm2_median_shift", "ppm2_basis_floor_share", "area_vs_usable_divergence",
    "ppm2_measure_coverage",
}


def _cell(
    source: str, cat: str | None, typ: str | None, **kw: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": source, "category_main": cat, "category_type": typ,
        "price_per_m2_basis": None, "n_active": 1000.0,
        "median_area_m2": None, "median_usable_area": None, "median_price_per_m2": None,
        "n_floor_eligible": None, "floor_null_share": None,
        "n_floor_eligible_7d": None, "floor_null_share_7d": None,
        "n_area_pairs": None, "area_divergence_share": None,
        "n_area_pairs_7d": None, "area_divergence_share_7d": None,
        # Coverage + median SUPPORT. The defaults describe a cell whose measure
        # resolves everywhere, so a test that says nothing about coverage keeps
        # scoring the axis it is actually about.
        "n_area_valued": 1000.0, "n_ppm2_valued": 1000.0, "n_active_7d": 200.0,
        "measure_input_gap_share": 0.0, "measure_input_gap_share_7d": 0.0,
    }
    base.update(kw)
    return base


class _PlausibilityConn:
    """Serves measure_plausibility_by_source rows + the week-old baseline row."""

    def __init__(
        self, cells: list[dict[str, Any]],
        baseline: dict[str, Any] | None = None,
        history_days: float | None = None,
    ) -> None:
        self._cells, self._baseline, self._history = cells, baseline, history_days
        self.executed: list[str] = []
        self._rows: list[tuple[Any, ...]] = []
        self._one: tuple[Any, ...] | None = None

    def cursor(self) -> "_PlausibilityConn":
        return self

    def transaction(self) -> "_PlausibilityConn":
        return self

    def __enter__(self) -> "_PlausibilityConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if sql.startswith("SET LOCAL"):
            return
        self.executed.append(sql)
        if "measure_plausibility_by_source" in sql:
            from scripts.verify_pipeline import _PLAUSIBILITY_COLS

            self._rows = [tuple(c[k] for k in _PLAUSIBILITY_COLS) for c in self._cells]
        elif "baseline_cells" in sql:
            self._one = (self._baseline, self._history)

    def fetchall(self) -> Any:
        return self._rows

    def fetchone(self) -> Any:
        return self._one


def _fresh(conn: "_PlausibilityConn") -> "_PlausibilityConn":
    """The per-run cache is cleared by run_checks; a direct check call must clear it too."""
    import scripts.verify_pipeline as vp

    vp._PLAUSIBILITY_CACHE.clear()
    return conn


# Live 2026-08-25, dum/prodej, every portal that populates both area fields. mmreality
# wrote the PLOT into the floor-area column: median area_m2 905.0 against median
# usable_area 130.0 on the same 3 588 rows.
_LIVE_DUM_CELLS = [
    _cell("mmreality", "dum", "prodej", n_active=3601.0, median_area_m2=905.0,
          median_usable_area=130.0, median_price_per_m2=5701.0,
          n_area_pairs=3588.0, area_divergence_share=0.9967,
          n_area_pairs_7d=113.0, area_divergence_share_7d=1.0),
    _cell("sreality", "dum", "prodej", n_active=20152.0, median_area_m2=150.0,
          median_usable_area=150.0, median_price_per_m2=45990.0,
          n_area_pairs=19082.0, area_divergence_share=0.0,
          n_area_pairs_7d=420.0, area_divergence_share_7d=0.0),
    _cell("idnes", "dum", "prodej", n_active=28757.0, median_area_m2=163.0,
          median_usable_area=163.0, n_area_pairs=28757.0, area_divergence_share=0.0,
          n_area_pairs_7d=610.0, area_divergence_share_7d=0.0),
    # bazos populates usable_area on 0 of 10 409 rows — nothing to compare, so it must
    # be SILENT, not 100% divergent. `area_m2 IS DISTINCT FROM usable_area` taken
    # literally (the charter's wording) would have scored it 100% and fired on it.
    _cell("bazos", "dum", "prodej", n_active=10409.0, median_area_m2=165.0,
          median_usable_area=None, n_area_pairs=0.0, area_divergence_share=None,
          n_area_pairs_7d=0.0, area_divergence_share_7d=None),
]


# A cell that IS scored and IS clean, on every axis. Several tests below assert that
# some other cell is skipped; without a scored companion in the corpus the check would
# (correctly) report that it verified nothing, and the assertion would pass for the
# wrong reason.
def _clean_scored(source: str = "sreality") -> dict[str, Any]:
    return _cell(source, "byt", "prodej", n_active=40000.0, median_area_m2=68.0,
                 median_usable_area=68.0, median_price_per_m2=95000.0,
                 n_floor_eligible=38000.0, floor_null_share=0.0,
                 n_floor_eligible_7d=900.0, floor_null_share_7d=0.0,
                 n_area_pairs=38000.0, area_divergence_share=0.0,
                 n_area_pairs_7d=900.0, area_divergence_share_7d=0.0,
                 n_area_valued=39000.0, n_ppm2_valued=38000.0, n_active_7d=900.0)


def _divergence(cells: list[dict[str, Any]]) -> dict[str, Any]:
    from scripts.verify_pipeline import check_area_vs_usable_divergence

    return check_area_vs_usable_divergence(_fresh(_PlausibilityConn(cells)), T)


def test_area_divergence_catches_the_mmreality_plot_area_defect() -> None:
    res = _divergence(_LIVE_DUM_CELLS)
    assert res["status"] == "fail"
    assert res["value"] == 100.0  # the 7d arm: 113 of 113 pairs
    named = " ".join(res["details"]["offenders"])
    assert "mmreality/dum/prodej" in named
    for clean in ("sreality", "idnes", "bazos"):
        assert clean not in named


def test_area_divergence_is_silent_on_a_portal_publishing_only_one_area() -> None:
    """bazos: no pairs at all must never read 'divergent on 100%'. It is skipped, and
    the check still certifies the cells it CAN score."""
    res = _divergence(
        [c for c in _LIVE_DUM_CELLS if c["source"] == "bazos"] + [_clean_scored()])
    assert res["status"] == "ok" and res["details"]["offenders"] == []
    assert res["details"]["arms_scored"] == 2  # the companion's two arms, not bazos


def test_area_divergence_tolerates_the_realitymix_field_convention() -> None:
    """Live: realitymix byt diverges on 10.5% of pairs (16.5% over the trailing week)
    at a MEDIAN relative difference of 9.6% — a balcony/rounding convention, not a
    different physical quantity, and decaying legacy stock post-W1. Amber it and the
    operator learns to dismiss the axis that has to catch the next mmreality."""
    res = _divergence([
        _cell("realitymix", "byt", "prodej", n_active=11000.0,
              n_area_pairs=8708.0, area_divergence_share=0.105,
              n_area_pairs_7d=237.0, area_divergence_share_7d=0.165),
    ])
    assert res["status"] == "ok"


def test_area_divergence_skips_land_where_the_areas_are_meant_to_differ() -> None:
    """Option A: area_m2 IS the plot for pozemek, so divergence from usable_area is
    the correct answer there. Scoring it would red the check on healthy data forever."""
    res = _divergence([
        _cell("sreality", "pozemek", "prodej", n_active=9000.0,
              n_area_pairs=4000.0, area_divergence_share=1.0,
              n_area_pairs_7d=300.0, area_divergence_share_7d=1.0),
        _clean_scored("idnes"),
    ])
    assert res["status"] == "ok" and res["details"]["offenders"] == []


def test_area_divergence_ignores_cells_below_the_row_floor() -> None:
    res = _divergence([
        _cell("maxima", "dum", "prodej", n_active=83.0,
              n_area_pairs=40.0, area_divergence_share=1.0),
        _clean_scored(),
    ])
    assert res["status"] == "ok" and res["details"]["offenders"] == []


# Live 2026-08-25: the unit-price masquerade. A per-m2 UNIT price written into
# price_czk (136 Kc for a commercial rental) is 100% non-NULL and correctly typed —
# only the per-basis floor inside measure_price_per_m2 rejects it.
_LIVE_FLOOR_CELLS = [
    _cell("ceskereality", "komercni", "pronajem", n_active=8852.0,
          n_floor_eligible=5656.0, floor_null_share=0.2003,
          n_floor_eligible_7d=169.0, floor_null_share_7d=0.112),
    _cell("realitymix", "komercni", "pronajem", n_active=6838.0,
          n_floor_eligible=4500.0, floor_null_share=0.1896,
          n_floor_eligible_7d=85.0, floor_null_share_7d=0.094),
    _cell("realitymix", "pozemek", "pronajem", n_active=246.0,
          n_floor_eligible=115.0, floor_null_share=0.1565),
    _cell("remax", "komercni", "pronajem", n_active=593.0,
          n_floor_eligible=275.0, floor_null_share=0.1127),
    _cell("bazos", "komercni", "pronajem", n_active=3912.0,
          n_floor_eligible=2689.0, floor_null_share=0.0666,
          n_floor_eligible_7d=436.0, floor_null_share_7d=0.067),
    # The two portals that already carry a per-area price guard, same cell.
    _cell("idnes", "komercni", "pronajem", n_active=11041.0,
          n_floor_eligible=8444.0, floor_null_share=0.0054),
    _cell("sreality", "komercni", "pronajem", n_active=13984.0,
          n_floor_eligible=9529.0, floor_null_share=0.0049),
]


def _floor(cells: list[dict[str, Any]]) -> dict[str, Any]:
    from scripts.verify_pipeline import check_ppm2_basis_floor_share

    return check_ppm2_basis_floor_share(_fresh(_PlausibilityConn(cells)), T)


def test_basis_floor_share_catches_the_unit_price_masquerade() -> None:
    res = _floor(_LIVE_FLOOR_CELLS)
    assert res["status"] == "fail"
    assert res["value"] == 20.03
    named = " ".join(res["details"]["offenders"])
    for guilty in ("ceskereality/komercni/pronajem", "realitymix/komercni/pronajem",
                   "realitymix/pozemek/pronajem", "remax/komercni/pronajem",
                   "bazos/komercni/pronajem"):
        assert guilty in named
    # ...and the two portals with a working guard are not accused, in the same cell.
    assert "idnes" not in named and "sreality" not in named


def test_basis_floor_share_pozemek_is_scored_unlike_the_divergence_check() -> None:
    """Land is exempt from the AREA check (area_m2 is the plot by design) but not from
    the PRICE floor — realitymix pozemek/pronajem loses 15.7% of its prices and that is
    a defect on any basis."""
    res = _floor([c for c in _LIVE_FLOOR_CELLS if c["category_main"] == "pozemek"])
    assert res["status"] == "fail"


def test_basis_floor_share_fresh_arm_fires_before_the_stock_arm_can() -> None:
    """A regression shipped this week is ~100% of what arrived since, but only churn-
    fraction of the stock. Without the 7d arm, detection latency is weeks."""
    res = _floor([
        _cell("maxima", "komercni", "pronajem", n_active=5000.0,
              n_floor_eligible=4000.0, floor_null_share=0.004,
              n_floor_eligible_7d=300.0, floor_null_share_7d=0.98),
    ])
    assert res["status"] == "fail"
    assert "first seen in 7d" in " ".join(res["details"]["offenders"])


def _shift(
    cells: list[dict[str, Any]], baseline: dict[str, Any] | None,
    history_days: float | None = 30.0,
) -> dict[str, Any]:
    from scripts.verify_pipeline import check_ppm2_median_shift

    return check_ppm2_median_shift(
        _fresh(_PlausibilityConn(cells, baseline, history_days)), T)


def test_median_shift_fires_when_the_w2_backfill_heals_mmreality() -> None:
    """The mmreality defect predates any baseline, so this axis cannot indict it —
    area_vs_usable_divergence does that. What it MUST do is notice the correction:
    905.0 -> 130.0 is 6.96x and 5 701 -> 42 500 is 7.45x, both far above the 3.0x fail
    ratio. A heal this size passing silently would prove the axis inert."""
    res = _shift(
        [_cell("mmreality", "dum", "prodej", n_active=3601.0,
               median_area_m2=130.0, median_price_per_m2=42500.0)],
        {"mmreality/dum/prodej": {"n": 3588, "area": 905.0, "ppm2": 5701.0,
                                  "n_area": 3588, "n_ppm2": 3400}},
    )
    assert res["status"] == "fail"
    assert res["value"] == 7.455
    named = " ".join(res["details"]["offenders"])
    assert "median area_m2" in named and "median Kc/m2" in named


def test_median_shift_tolerates_the_noisiest_real_weekly_move() -> None:
    """Measured live across 21 cells with >= 200 new listings in both of two
    consecutive weeks — the new-arrival cohort, which swings far harder than the stock
    medians this check actually compares: worst 1.47x on area, 1.90x on Kc/m2."""
    res = _shift(
        [_cell("sreality", "byt", "prodej", n_active=40000.0,
               median_area_m2=147.0, median_price_per_m2=95000.0)],
        {"sreality/byt/prodej": {"n": 39000, "area": 100.0, "ppm2": 50000.0,
                                 "n_area": 39000, "n_ppm2": 38000}},
    )
    assert res["status"] == "ok"


def test_median_shift_no_baseline_is_ok_while_young_and_warn_once_stale() -> None:
    """A missing baseline in the first week after deploy is the expected state and the
    operator can do nothing about it. Two weeks of history with still no baseline means
    the check has been erroring or not running — that must not read as green."""
    cells = [_cell("sreality", "byt", "prodej", n_active=40000.0,
                   median_area_m2=100.0, median_price_per_m2=50000.0)]
    assert _shift(cells, None, history_days=3.0)["status"] == "ok"
    assert _shift(cells, None, history_days=None)["status"] == "ok"
    assert _shift(cells, None, history_days=20.0)["status"] == "warn"


def test_median_shift_skips_a_median_without_enough_support_in_either_week() -> None:
    """The gate is the number of rows CARRYING the median, in both weeks."""
    small_now = _shift(
        [_cell("maxima", "dum", "prodej", n_active=4000.0, n_area_valued=83.0,
               n_ppm2_valued=83.0, median_area_m2=900.0, median_price_per_m2=5000.0)],
        {"maxima/dum/prodej": {"n": 4000, "area": 150.0, "ppm2": 45000.0,
                               "n_area": 4000, "n_ppm2": 4000}},
    )
    small_then = _shift(
        [_cell("maxima", "dum", "prodej", n_active=4000.0, n_area_valued=4000.0,
               n_ppm2_valued=4000.0, median_area_m2=900.0, median_price_per_m2=5000.0)],
        {"maxima/dum/prodej": {"n": 4000, "area": 150.0, "ppm2": 45000.0,
                               "n_area": 83, "n_ppm2": 83}},
    )
    for res in (small_now, small_then):
        assert res["details"]["medians_compared"] == 0
        assert res["details"]["offenders"] == []


def test_median_shift_gates_on_median_support_not_on_cell_size() -> None:
    """Live: bezrealitky pozemek/prodej is 1 643 active rows of which NINE carry an
    area_m2, so both medians rest on 9 values whose Kc/m2 spread is 17.6x. Gating on
    n_active passes it and two ordinary delistings then move the median past the 3.0x
    fail — a red tile plus a bell ring, on no defect at all, over and over as it
    oscillates. The comparison must not happen."""
    cell = _cell("bezrealitky", "pozemek", "prodej", n_active=1643.0,
                 n_area_valued=9.0, n_ppm2_valued=9.0,
                 median_area_m2=6471.0, median_price_per_m2=17573.27)
    res = _shift(
        [cell],
        {"bezrealitky/pozemek/prodej": {"n": 1648, "area": 370.0, "ppm2": 3224.52,
                                        "n_area": 9, "n_ppm2": 9}},
    )
    assert res["details"]["medians_compared"] == 0
    assert res["status"] == "warn" and res["value"] is None
    assert "compared NOTHING" in res["message"]
    # ...and the same cell WITH real support is compared and indicted.
    supported = dict(cell, n_area_valued=900.0, n_ppm2_valued=900.0)
    res2 = _shift(
        [supported],
        {"bezrealitky/pozemek/prodej": {"n": 1648, "area": 370.0, "ppm2": 3224.52,
                                        "n_area": 900, "n_ppm2": 900}},
    )
    assert res2["status"] == "fail" and res2["details"]["medians_compared"] == 2


def test_median_shift_baseline_round_trips_through_its_own_details() -> None:
    """The baseline is this check's own result row from a week ago — no new table. If
    the snapshot's keys and the reader's cell keys ever disagree the axis silently
    never compares anything, so pin the round trip."""
    cells = [_cell("mmreality", "dum", "prodej", n_active=3601.0,
                   median_area_m2=905.0, median_price_per_m2=5701.0)]
    written = _shift(cells, None, history_days=1.0)["details"]["cells"]
    assert written == {"mmreality/dum/prodej": {
        "n": 3601, "area": 905.0, "ppm2": 5701.0, "n_area": 1000, "n_ppm2": 1000}}
    healed = [_cell("mmreality", "dum", "prodej", n_active=3601.0,
                    median_area_m2=130.0, median_price_per_m2=42500.0)]
    assert _shift(healed, written)["status"] == "fail"


def test_measure_checks_share_one_read_of_the_plausibility_view() -> None:
    """The view is a ~12 s sequential scan of the active corpus. Three checks, one read."""
    import scripts.verify_pipeline as vp

    conn = _PlausibilityConn(_LIVE_DUM_CELLS + _LIVE_FLOOR_CELLS, None, 1.0)
    results = run_checks(conn, T, only=_MEASURE_CHECK_KEYS)
    assert len(results) == 4
    assert sum("measure_plausibility_by_source" in s for s in conn.executed) == 1
    assert not vp._PLAUSIBILITY_CACHE or "cells" in vp._PLAUSIBILITY_CACHE


def test_run_checks_clears_the_plausibility_cache_between_runs() -> None:
    """A stale corpus would make every measure check certify last run's data."""
    first = _PlausibilityConn(_LIVE_DUM_CELLS, None, 1.0)
    run_checks(first, T, only={"area_vs_usable_divergence"})
    healed = [dict(c, area_divergence_share=0.0, area_divergence_share_7d=0.0)
              for c in _LIVE_DUM_CELLS]
    second = _PlausibilityConn(healed, None, 1.0)
    out = run_checks(second, T, only={"area_vs_usable_divergence"})
    assert out[0]["status"] == "ok"


def test_empty_plausibility_view_is_warn_never_ok() -> None:
    """The view is gated on is_platform_admin(); read it from a role that fails the
    gate and it returns zero rows. That is an INERT check, not a clean bill of health —
    exactly the db_saturation precedent."""
    from scripts.verify_pipeline import (
        check_area_vs_usable_divergence, check_ppm2_basis_floor_share,
        check_ppm2_measure_coverage, check_ppm2_median_shift,
    )

    for fn in (check_area_vs_usable_divergence, check_ppm2_basis_floor_share,
               check_ppm2_median_shift, check_ppm2_measure_coverage):
        res = fn(_fresh(_PlausibilityConn([])), T)
        assert res["status"] == "warn" and "INERT" in res["message"]


class _BrokenViewConn(_PlausibilityConn):
    """migration 427 not applied yet — the relation simply is not there."""

    def execute(self, sql: str, params: Any = None) -> None:
        if "measure_plausibility_by_source" in sql:
            raise RuntimeError('relation "measure_plausibility_by_source" does not exist')
        super().execute(sql, params)


def test_missing_view_is_warn_and_names_the_migration_not_three_red_tiles() -> None:
    """Between merging this PR and applying migration 427 the view does not exist. Three
    checks red with `relation does not exist` are indistinguishable from three real
    defects, so the read is reported, never raised — the db_saturation precedent."""
    results = run_checks(_BrokenViewConn([]), T, only=_MEASURE_CHECK_KEYS)
    assert [r["status"] for r in results] == ["warn"] * 4
    for r in results:
        assert "INERT" in r["message"] and "427" in r["message"]
        assert "does not exist" in r["details"]["skipped"]


# Live 2026-08-25: the coverage hole every OTHER axis is blind to. sreality publishes
# 27 174 active `pozemek` rows with area_m2 NULL on 27 174 of them — the plot size sits
# in estate_area — so these four cells carry no floor-eligible rows, no area pairs and
# no medians. Before the coverage arm all three axes called them healthy while the per-m2
# measure did not exist for ~7% of the active corpus.
_LIVE_DARK_LAND_CELLS = [
    _cell("sreality", "pozemek", "prodej", n_active=20484.0, n_area_valued=0.0,
          n_ppm2_valued=0.0, n_active_7d=10.0, measure_input_gap_share=1.0,
          measure_input_gap_share_7d=1.0, n_floor_eligible=0.0, floor_null_share=None,
          n_area_pairs=0.0, area_divergence_share=None),
    _cell("sreality", "pozemek", "podil", n_active=5825.0, n_area_valued=0.0,
          n_ppm2_valued=0.0, n_active_7d=16.0, measure_input_gap_share=1.0,
          measure_input_gap_share_7d=1.0, n_floor_eligible=0.0, floor_null_share=None,
          n_area_pairs=0.0, area_divergence_share=None),
    _cell("bezrealitky", "pozemek", "prodej", n_active=1643.0, n_area_valued=9.0,
          n_ppm2_valued=9.0, n_active_7d=64.0, measure_input_gap_share=0.9945,
          measure_input_gap_share_7d=1.0, n_floor_eligible=9.0, floor_null_share=0.0,
          n_area_pairs=9.0, area_divergence_share=0.0),
]


def _coverage(cells: list[dict[str, Any]]) -> dict[str, Any]:
    from scripts.verify_pipeline import check_ppm2_measure_coverage

    return check_ppm2_measure_coverage(_fresh(_PlausibilityConn(cells)), T)


def test_coverage_sees_the_land_cells_every_other_axis_skips() -> None:
    """The whole point of the arm: these cells are invisible to the other three because
    every one of those is a ratio over rows that HAVE the inputs."""
    res = _coverage(_LIVE_DARK_LAND_CELLS + [_clean_scored()])
    assert res["status"] == "warn"
    named = " ".join(res["details"]["offenders"])
    for dark in ("sreality/pozemek/prodej", "sreality/pozemek/podil",
                 "bezrealitky/pozemek/prodej"):
        assert dark in named
    assert "sreality/byt/prodej" not in named
    # ...and the axes that cannot see them still report clean, which is why the arm
    # had to exist rather than the other three being "fixed".
    assert _divergence(_LIVE_DARK_LAND_CELLS + [_clean_scored()])["status"] == "ok"
    assert _floor(_LIVE_DARK_LAND_CELLS + [_clean_scored()])["status"] == "ok"


def test_coverage_stock_gap_ambers_but_a_fresh_gap_fails() -> None:
    """Severity is a property of the ARM. A standing gap nobody can clear today is
    amber; the same share among the rows that arrived this week is a parser regression
    in flight and must be red — that is the case where the divergence and floor axes go
    QUIET (a portal writing NULL area produces no pairs and no eligible rows)."""
    standing = _coverage([
        _cell("sreality", "pozemek", "prodej", n_active=20484.0, n_active_7d=10.0,
              measure_input_gap_share=1.0, measure_input_gap_share_7d=1.0),
        _clean_scored(),
    ])
    assert standing["status"] == "warn"  # the 7d arm has 10 rows: below the gate
    shipping = _coverage([
        _cell("mmreality", "pozemek", "prodej", n_active=4190.0, n_active_7d=400.0,
              measure_input_gap_share=0.30, measure_input_gap_share_7d=1.0),
        _clean_scored(),
    ])
    assert shipping["status"] == "fail"
    assert "first seen in 7d" in " ".join(shipping["details"]["offenders"])


def test_coverage_tolerates_ordinary_portal_incompleteness() -> None:
    """Live worst non-dark cells: realitymix ostatni/pronajem 89.4% of stock and 35.8%
    on the worst scored 7d arm (bazos komercni/prodej). Amber those and the operator
    learns to dismiss the one axis that sees a portal going dark."""
    res = _coverage([
        _cell("realitymix", "ostatni", "pronajem", n_active=292.0, n_active_7d=24.0,
              measure_input_gap_share=0.894),
        _cell("bazos", "komercni", "prodej", n_active=1472.0, n_active_7d=229.0,
              measure_input_gap_share=0.358, measure_input_gap_share_7d=0.358),
    ])
    assert res["status"] == "ok" and res["details"]["offenders"] == []


def test_coverage_is_silent_where_the_basis_is_undecidable() -> None:
    """No basis means no measure BY SPECIFICATION (a visible gap, never a guess) — the
    view publishes NULL there, and a permanent amber on it would be noise."""
    res = _coverage([
        _cell("bazos", "ostatni", None, n_active=274.0, n_active_7d=40.0,
              measure_input_gap_share=None, measure_input_gap_share_7d=None),
        _clean_scored(),
    ])
    assert res["status"] == "ok" and res["details"]["arms_scored"] == 2


def test_every_measure_axis_refuses_to_certify_a_corpus_it_cannot_measure() -> None:
    """THE fail-open regression. Take the real production cells and blank their
    measurable content — the per-m2 measure 100% dead platform-wide, exactly what a
    later vocabulary or category migration would cause. Before this, _status_for_share
    started `worst` at 0.0 and _status_for_median_shift at 1.0 and every arm was
    silently skipped, so all three reported `ok` with a reassuring message ('Per-m2
    medians stable week-over-week (worst move 1.00x across 64 cells)') while nothing
    whatsoever had been verified. `_inert_measure_check` did not catch it: it only fires
    on ZERO ROWS, and there are 100 rows here."""
    dead = [
        dict(c, n_floor_eligible=0.0, floor_null_share=None, n_floor_eligible_7d=0.0,
             floor_null_share_7d=None, n_area_pairs=0.0, area_divergence_share=None,
             n_area_pairs_7d=0.0, area_divergence_share_7d=None,
             n_area_valued=0.0, n_ppm2_valued=0.0, median_area_m2=None,
             median_usable_area=None, median_price_per_m2=None,
             price_per_m2_basis=None, measure_input_gap_share=1.0,
             measure_input_gap_share_7d=1.0)
        for c in _LIVE_DUM_CELLS + _LIVE_FLOOR_CELLS
    ]
    baseline = {
        f"{c['source']}/{c['category_main']}/{c['category_type']}":
            {"n": 5000, "area": 100.0, "ppm2": 50000.0, "n_area": 5000, "n_ppm2": 5000}
        for c in dead
    }
    results = run_checks(
        _PlausibilityConn(dead, baseline, 30.0), T, only=_MEASURE_CHECK_KEYS)
    assert {r["status"] for r in results} == {"warn", "fail"}
    by_key = {r["check_key"]: r for r in results}
    for key in ("ppm2_basis_floor_share", "area_vs_usable_divergence",
                "ppm2_median_shift"):
        assert by_key[key]["status"] == "warn", key
        assert by_key[key]["value"] is None, key
        assert "NOTHING" in by_key[key]["message"], key
    # the corpus went dark THIS WEEK: coverage is the axis that says so out loud
    assert by_key["ppm2_measure_coverage"]["status"] == "fail"


def test_a_measured_and_clean_corpus_still_reports_ok() -> None:
    """The other side of the same rule — refusing to certify an unmeasurable corpus
    must not make a measurable one amber."""
    results = run_checks(
        _PlausibilityConn(_LIVE_DUM_CELLS + [_clean_scored("remax")], None, 1.0), T,
        only={"ppm2_basis_floor_share", "ppm2_measure_coverage"})
    assert [r["status"] for r in results] == ["ok", "ok"]


def test_every_measure_threshold_has_a_code_default() -> None:
    """load_thresholds merges app_settings OVER the defaults, so a key the seed lacks
    must still resolve — a KeyError here is a check that fails on every run."""
    for key in ("ppm2_median_shift_warn_ratio", "ppm2_median_shift_fail_ratio",
                "ppm2_median_shift_min_rows", "ppm2_basis_floor_share_warn",
                "ppm2_basis_floor_share_fail", "ppm2_basis_floor_min_rows",
                "area_divergence_share_warn", "area_divergence_share_fail",
                "area_divergence_min_rows", "ppm2_coverage_gap_warn",
                "ppm2_coverage_gap_fail_7d", "ppm2_coverage_min_rows"):
        assert key in DEFAULT_THRESHOLDS


# --- ingestion checks (2026-08-27) -----------------------------------------
#
# These two are the push half of the starvation incident: nothing in this harness
# had ever looked at whether listings were actually being INGESTED, so a portal
# could sit at zero for nine days without a signal leaving the database.


class _RowsConn:
    """Minimal conn whose cursor returns one canned result set."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> "_RowsConn":
        return self

    def __enter__(self) -> "_RowsConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


def test_acquisition_lag_ok_when_every_portal_is_current() -> None:
    conn = _RowsConn([("bazos", 8, 1.6, 1.3), ("realitymix", 123, 0.8, 0.1)])
    out = check_acquisition_lag(conn, T)
    assert out["status"] == "ok"
    assert out["value"] == 1.6
    assert out["details"]["offenders"] == []


def test_acquisition_lag_fails_on_the_starvation_signature() -> None:
    """The live numbers on the morning of the fix: sreality 216h / 9,873 waiting."""
    conn = _RowsConn([
        ("sreality", 9873, 216.11, 86.29),
        ("ceskereality", 2840, 89.31, 44.47),
        ("bazos", 8, 1.6, 1.3),
    ])
    out = check_acquisition_lag(conn, T)
    assert out["status"] == "fail"
    assert out["value"] == 216.11
    assert any("sreality" in o for o in out["details"]["offenders"])
    assert not any("bazos" in o for o in out["details"]["offenders"])


def test_acquisition_lag_warns_between_the_tiers() -> None:
    conn = _RowsConn([("idnes", 1125, 12.0, 8.0)])
    assert check_acquisition_lag(conn, T)["status"] == "warn"


def test_acquisition_lag_queries_the_new_class_only() -> None:
    """Keyed on the queue, not on listings.first_seen_at — a "no new rows in N
    hours" check needs a baseline that the outage itself erodes."""
    conn = _RowsConn([])
    check_acquisition_lag(conn, T)
    sql, params = conn.executed[0]
    assert "listing_detail_queue" in sql
    assert "claimed_at is null" in sql and "given_up = false" in sql
    assert params == (0,)  # db.QUEUE_PRIORITY_NEW


def _cov(source, walked, best, age, collected, total):
    return (source, walked, best, age, collected, total)


def test_walk_coverage_ok_at_the_observed_noise_floor() -> None:
    conn = _RowsConn([
        _cov("sreality", 20, 20, 2.5, 102487, 102488),
        _cov("bazos", 14, 14, 1.8, 29731, 29778),  # 0.16%
    ])
    out = check_walk_coverage(conn, T)
    assert out["status"] == "ok"


def test_walk_coverage_warns_on_the_ceskereality_facet_gap() -> None:
    """43,431 of 48,235 advertised — the top-10-popularity facet widget is not a
    partition. Amber, not red: it is a known bounded problem with its own fix."""
    conn = _RowsConn([_cov("ceskereality", 12, 12, 13.9, 43431, 48235)])
    out = check_walk_coverage(conn, T)
    assert out["status"] == "warn"
    assert out["value"] == 9.96


def test_walk_coverage_fails_on_a_deep_hole() -> None:
    conn = _RowsConn([_cov("idnes", 10, 10, 3.0, 12036, 35769)])
    assert check_walk_coverage(conn, T)["status"] == "fail"


def test_walk_coverage_flags_a_truncated_walk_even_at_zero_gap() -> None:
    """A run that reached fewer categories than the portal's own recent best is
    truncated — and truncation makes the GAP look better, because the categories
    it never reached contribute no shortfall. Caught on the category count."""
    conn = _RowsConn([_cov("idnes", 0, 10, 14.2, None, None)])
    out = check_walk_coverage(conn, T)
    assert out["status"] == "warn"
    assert out["details"]["per_source"]["idnes"]["truncated"] is True


def test_walk_coverage_never_certifies_a_self_reported_total() -> None:
    """remax and maxima derive their "advertised total" as len(seen), so their gap
    is 0% by construction; mmreality reports no total at all. Reporting any of them
    as 100% covered would be a number that cannot be wrong."""
    conn = _RowsConn([
        _cov("remax", 10, 10, 13.0, 8132, 8132),
        _cov("maxima", 10, 10, 13.0, 272, 272),
        _cov("mmreality", 1, 1, 12.1, None, None),
    ])
    out = check_walk_coverage(conn, T)
    assert out["details"]["unverified"] == ["maxima", "mmreality", "remax"]
    for source in ("remax", "maxima", "mmreality"):
        assert out["details"]["per_source"][source]["verifiable"] is False
        assert out["details"]["per_source"][source]["gap_pct"] is None


# --- migration_drift: merged is not applied --------------------------------


class _DriftConn:
    """Returns a canned presence verdict per (kind, ident) probe."""

    def __init__(self, present: dict[tuple[str, str], bool], default: bool = True) -> None:
        self._present = present
        self._default = default
        self.probed: list[tuple[str, str]] = []

    def cursor(self) -> "_DriftConn":
        return self

    def __enter__(self) -> "_DriftConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.probed = list(zip(params["kinds"], params["idents"]))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [(k, i, self._present.get((k, i), self._default)) for k, i in self.probed]


def test_migration_drift_ok_when_everything_is_present() -> None:
    out = check_migration_drift(_DriftConn({}, default=True), T)
    assert out["status"] == "ok"
    assert out["value"] == 0
    assert out["details"]["objects_probed"] > 0


def test_migration_drift_fails_when_a_migration_is_wholly_absent() -> None:
    """The 2026-08-25 signature: merged, never applied, so NONE of its objects
    exist. Nothing else in the platform noticed for 29 hours."""
    conn = _DriftConn({("column", "listings.discovered_at"): False}, default=True)
    out = check_migration_drift(conn, T)
    assert out["status"] == "fail"
    assert out["value"] == 1
    assert any("444" in m for m in out["details"]["missing_entirely"])


def test_migration_drift_only_warns_when_a_migration_is_half_present() -> None:
    """Half-applied is ambiguous — a partial apply, or the parser mis-read the
    file. Ambiguity warns; it must not page anyone at 3am."""
    migs = load_migrations(_MIGRATIONS_DIR, newest=T["migration_drift_window"])
    multi = next(m for m in migs if len(m.objects) >= 2)
    conn = _DriftConn({(multi.objects[0].kind, multi.objects[0].ident): False}, default=True)
    out = check_migration_drift(conn, T)
    assert out["status"] == "warn"
    assert any(multi.filename in p for p in out["details"]["partially_missing"])
    assert out["details"]["missing_entirely"] == []


def test_migration_drift_reports_its_own_blind_spot() -> None:
    """Migrations that only drop, grant, or update data declare nothing to
    probe. They must be COUNTED and named, not silently treated as passing —
    the failure mode this sprint already hit with a test that never ran."""
    out = check_migration_drift(_DriftConn({}, default=True), T)
    assert isinstance(out["details"]["unverifiable"], list)
    assert all(f.endswith(".sql") for f in out["details"]["unverifiable"])


def test_migration_drift_probes_only_safe_identifiers() -> None:
    """to_regclass raises on a malformed identifier instead of returning NULL,
    which would turn one odd migration into a crashed check."""
    conn = _DriftConn({}, default=True)
    check_migration_drift(conn, T)
    for _kind, ident in conn.probed:
        assert _SAFE_IDENT.match(ident), ident


# --- worker_lane_stall: a live heartbeat with a wedged lane -----------------


class _LaneConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def cursor(self) -> "_LaneConn":
        return self

    def __enter__(self) -> "_LaneConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


def _lane(name, in_flight, passes=1, failed=0, last_dur=None, uptime=600.0):
    return ("realtime-worker", name, in_flight, passes, failed, last_dur, uptime)


def test_worker_lane_stall_ok_when_lanes_are_idle_or_briefly_busy() -> None:
    out = check_worker_lane_stall(
        _LaneConn([_lane("images", None, passes=486), _lane("drain", 42.0)]), T)
    assert out["status"] == "ok"


def test_worker_lane_stall_fails_on_the_observed_wedge() -> None:
    """The live signature: heartbeat healthy, images lane at 486 passes, drain
    lane stuck inside a single pass for hours. worker_liveness reads this as
    perfectly fine, because the worker IS alive."""
    out = check_worker_lane_stall(
        _LaneConn([_lane("images", None, passes=486), _lane("drain", 9 * 3600.0, passes=1)]), T)
    assert out["status"] == "fail"
    assert any("drain" in o for o in out["details"]["offenders"])
    assert not any("images" in o for o in out["details"]["offenders"])


def test_worker_lane_stall_warns_before_it_fails() -> None:
    out = check_worker_lane_stall(_LaneConn([_lane("drain", 1500.0)]), T)
    assert out["status"] == "warn"


def test_worker_lane_stall_tolerates_a_long_but_legitimate_drain_pass() -> None:
    """Eight portals at up to DRAIN_MAX_SECONDS each is ~16 min of honest work."""
    out = check_worker_lane_stall(_LaneConn([_lane("drain", 900.0)]), T)
    assert out["status"] == "ok"


def test_worker_lane_stall_does_not_double_alarm_a_dead_worker() -> None:
    """No heartbeat at all is worker_liveness's job. Claiming ok would be a lie;
    claiming fail would ring two bells for one fault."""
    out = check_worker_lane_stall(_LaneConn([]), T)
    assert out["status"] == "warn"
    assert "worker_liveness" in out["message"]
