"""Hermetic tests for toolkit.system_alerts — no DB, a scripted fake connection."""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from toolkit.system_alerts import (
    AlertPolicy,
    CheckState,
    check_states,
    emit_system_alert,
    emit_transition_alerts,
    emit_weekly_heartbeat,
    latest_statuses,
)


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))
        if "app_settings" in sql:
            self._conn._fetch = (
                None if self._conn.channels_value is _MISSING
                else (self._conn.channels_value,)
            )
        elif "pipeline_check_results" in sql:
            self._conn._fetchall = self._conn.latest_rows
        elif "INSERT INTO notification_dispatches" in sql:
            self._conn.insert_sql = sql
            self._conn.insert_params = params
            self._conn.inserts.append(params)
            # Emulate `ON CONFLICT (dedupe_key) DO NOTHING` against the UNIQUE index —
            # exactly-once-per-incident is the whole mechanism, and a fake that always
            # reports rowcount 1 would let a duplicate-key bug pass every test.
            key = params[1] if params else None
            if key in self._conn.landed:
                self.rowcount = 0
                return
            self.rowcount = self._conn.insert_rowcount
            if self.rowcount:
                self._conn.landed.append(key)
            return
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._conn._fetch

    def fetchall(self) -> Any:
        return self._conn._fetchall

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


_MISSING = object()


class _FakeConn:
    def __init__(
        self, *, channels_value: Any = _MISSING, insert_rowcount: int = 1,
        latest_rows: list[tuple[str, str]] | None = None,
    ) -> None:
        self.channels_value = channels_value
        self.insert_rowcount = insert_rowcount
        self.executed: list[tuple[str, Any]] = []
        self.insert_sql: str | None = None
        self.insert_params: Any = None
        self.inserts: list[Any] = []
        self.landed: list[str] = []
        self.latest_rows = latest_rows or []
        self._fetch: Any = None
        self._fetchall: Any = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


_RUN_AT = _dt.datetime(2026, 7, 9, 15, 37, 0, tzinfo=_dt.timezone.utc)


def _dedupe_keys(conn: _FakeConn) -> list[str]:
    return [p[1] for p in conn.inserts]  # params = (message, dedupe_key, channels)


def _h(hours: float) -> _dt.timedelta:
    return _dt.timedelta(hours=hours)


def _state(status: str, *, started_h: float | None = None, last_fail_h: float | None = None,
           last_run_h: float = 0.0, runs: int = 1) -> CheckState:
    """A CheckState positioned relative to _RUN_AT (hours BEFORE it)."""
    return CheckState(
        status=status,
        incident_started_at=None if started_h is None else _RUN_AT - _h(started_h),
        last_fail_at=None if last_fail_h is None else _RUN_AT - _h(last_fail_h),
        last_run_at=_RUN_AT - _h(last_run_h),
        fail_runs=runs,
    )


def test_transition_onset_fires_once_and_keys_on_the_edge() -> None:
    conn = _FakeConn()
    results = [{"check_key": "llm_errors", "status": "fail", "message": "provider down"}]
    counts = emit_transition_alerts(conn, results, {"llm_errors": "ok"}, _RUN_AT)
    assert counts == {"onset": 1, "recovery": 0, "reescalation": 0}
    assert _dedupe_keys(conn) == ["sys:llm_errors:onset:2026-07-09T15:37:00Z"]
    assert conn.inserts[0][0] == "provider down"


def test_transition_ongoing_is_silent() -> None:
    # fail → fail with no history to anchor on: the legacy edge-only branch.
    conn = _FakeConn()
    results = [{"check_key": "engine_health", "status": "fail", "message": "stalled"}]
    counts = emit_transition_alerts(conn, results, {"engine_health": "fail"}, _RUN_AT)
    assert counts == {"onset": 0, "recovery": 0, "reescalation": 0}
    assert conn.inserts == []


def test_transition_recovery_emits_resolved_row() -> None:
    conn = _FakeConn()
    results = [{"check_key": "llm_errors", "status": "ok", "message": "healthy"}]
    counts = emit_transition_alerts(conn, results, {"llm_errors": "fail"}, _RUN_AT)
    assert counts == {"onset": 0, "recovery": 1, "reescalation": 0}
    assert _dedupe_keys(conn) == ["sys:llm_errors:recovery:2026-07-09T15:37:00Z"]
    assert conn.inserts[0][0].startswith("✓ Recovered")


def test_transition_warn_and_ok_never_ring() -> None:
    conn = _FakeConn()
    results = [
        {"check_key": "street_debt", "status": "warn", "message": "debt rising"},   # ok→warn
        {"check_key": "geo_debt", "status": "ok", "message": "fine"},                # ok→ok
        {"check_key": "merge_latency", "status": "warn", "message": "x"},            # fail? no, prev warn
    ]
    prev = {"street_debt": "ok", "geo_debt": "ok", "merge_latency": "warn"}
    counts = emit_transition_alerts(conn, results, prev, _RUN_AT)
    assert counts == {"onset": 0, "recovery": 0, "reescalation": 0}
    assert conn.inserts == []


def test_transition_new_check_fails_from_absent_baseline() -> None:
    conn = _FakeConn()
    results = [{"check_key": "db_saturation", "status": "fail", "message": "pg_cron timeouts"}]
    counts = emit_transition_alerts(conn, results, {}, _RUN_AT)  # never seen before
    assert counts == {"onset": 1, "recovery": 0, "reescalation": 0}


# --- the escalation ladder (W3.4) ------------------------------------------


def test_onset_anchors_on_the_incident_not_on_the_observing_run() -> None:
    """Every key for one incident hangs off ONE timestamp, so the 6h lane and the
    hourly lane cannot open two incidents for the same red."""
    conn = _FakeConn()
    states = {"llm_errors": _state("fail", started_h=2.0, last_fail_h=2.0, last_run_h=2.0)}
    results = [{"check_key": "llm_errors", "status": "fail", "message": "still down"}]
    emit_transition_alerts(conn, results, {"llm_errors": "fail"}, _RUN_AT, states=states)
    # The key is the incident's start (2h ago), NOT this run's stamp — which is what
    # lets ON CONFLICT suppress it for every later run and every other lane.
    assert _dedupe_keys(conn) == ["sys:llm_errors:onset:2026-07-09T13:37:00Z"]


def test_ongoing_below_the_first_rung_is_silent() -> None:
    conn = _FakeConn()
    anchor = "sys:property_maintenance:onset:2026-07-09T11:37:00Z"
    conn.landed.append(anchor)  # the onset alert already exists
    states = {"property_maintenance":
              _state("fail", started_h=4.0, last_fail_h=4.0, last_run_h=4.0)}
    counts = emit_transition_alerts(
        conn, [{"check_key": "property_maintenance", "status": "fail", "message": "stalled"}],
        {"property_maintenance": "fail"}, _RUN_AT, states=states)
    assert counts == {"onset": 0, "recovery": 0, "reescalation": 0}
    assert conn.landed == [anchor]


def test_ladder_rungs_fire_at_6h_24h_and_72h() -> None:
    """The pathology, both halves: property_maintenance sat fail from 2026-08-20 to
    2026-08-26 with ONE alert on day one. Each rung fires once, on the first run past
    it, and never again for the same incident."""
    for elapsed, rung in ((6.0, "6h"), (24.0, "24h"), (72.0, "72h")):
        conn = _FakeConn()
        start = _RUN_AT - _h(elapsed)
        conn.landed.append(f"sys:property_maintenance:onset:{start:%Y-%m-%dT%H:%M:%SZ}")
        states = {"property_maintenance": _state(
            "fail", started_h=elapsed, last_fail_h=6.0, last_run_h=6.0, runs=4)}
        counts = emit_transition_alerts(
            conn,
            [{"check_key": "property_maintenance", "status": "fail", "message": "stalled"}],
            {"property_maintenance": "fail"}, _RUN_AT, states=states)
        assert counts["reescalation"] == 1
        assert conn.landed[-1] == (
            f"sys:property_maintenance:reesc:{rung}:{start:%Y-%m-%dT%H:%M:%SZ}")
        assert f"Still failing ({rung})" in conn.inserts[-1][0]
        # a second run at the same rung is a DB no-op
        again = emit_transition_alerts(
            conn,
            [{"check_key": "property_maintenance", "status": "fail", "message": "stalled"}],
            {"property_maintenance": "fail"}, _RUN_AT, states=states)
        assert again["reescalation"] == 0


def test_only_the_highest_due_rung_fires() -> None:
    """An incident that predates the ladder's deploy (or a lane that was down for days)
    must produce ONE alert, not a backlog of every overdue rung."""
    conn = _FakeConn()
    start = _RUN_AT - _h(144.0)  # six days red, the real property_maintenance case
    conn.landed.append(f"sys:x:onset:{start:%Y-%m-%dT%H:%M:%SZ}")
    states = {"x": _state("fail", started_h=144.0, last_fail_h=6.0, last_run_h=6.0, runs=24)}
    counts = emit_transition_alerts(
        conn, [{"check_key": "x", "status": "fail", "message": "red"}],
        {"x": "fail"}, _RUN_AT, states=states)
    assert counts["reescalation"] == 1
    assert conn.landed[-1] == f"sys:x:reesc:72h:{start:%Y-%m-%dT%H:%M:%SZ}"


def test_weekly_rung_repeats_with_a_week_index() -> None:
    conn = _FakeConn()
    start = _RUN_AT - _h(340.0)  # week 2 of the incident
    conn.landed.append(f"sys:x:onset:{start:%Y-%m-%dT%H:%M:%SZ}")
    states = {"x": _state("fail", started_h=340.0, last_fail_h=6.0, last_run_h=6.0, runs=56)}
    emit_transition_alerts(
        conn, [{"check_key": "x", "status": "fail", "message": "red"}],
        {"x": "fail"}, _RUN_AT, states=states)
    assert conn.landed[-1] == f"sys:x:reesc:w2:{start:%Y-%m-%dT%H:%M:%SZ}"


def test_flap_inside_the_cooldown_stays_one_incident() -> None:
    """llm_errors flipped fail/ok on unchanged inputs and produced 114 alternating
    alerts. A return to fail within the cooldown re-enters the SAME incident: the onset
    key is the original one, so the DB dedupes it and no second alert exists."""
    conn = _FakeConn()
    original = f"sys:llm_errors:onset:{_RUN_AT - _h(3.0):%Y-%m-%dT%H:%M:%SZ}"
    conn.landed.append(original)
    # prev run was `ok` (the flap's green half), last fail 3h ago, inside the 6h cooldown
    states = {"llm_errors": _state("ok", started_h=3.0, last_fail_h=3.0, last_run_h=1.0)}
    counts = emit_transition_alerts(
        conn, [{"check_key": "llm_errors", "status": "fail", "message": "down again"}],
        {"llm_errors": "ok"}, _RUN_AT, states=states)
    assert counts == {"onset": 0, "recovery": 0, "reescalation": 0}
    assert _dedupe_keys(conn) == [original]


def test_green_inside_the_cooldown_announces_nothing() -> None:
    """The other half of the flap: a green run one hour after a fail is not a recovery."""
    conn = _FakeConn()
    states = {"llm_errors": _state("fail", started_h=1.0, last_fail_h=1.0, last_run_h=1.0)}
    counts = emit_transition_alerts(
        conn, [{"check_key": "llm_errors", "status": "ok", "message": "fine"}],
        {"llm_errors": "fail"}, _RUN_AT, states=states)
    assert counts == {"onset": 0, "recovery": 0, "reescalation": 0}
    assert conn.inserts == []


def test_recovery_fires_exactly_once_after_the_cooldown() -> None:
    conn = _FakeConn()
    start = _RUN_AT - _h(30.0)
    # last fail 7h ago (past the 6h cooldown), and the previous run was 7h ago too — so
    # THIS run is the first one that can see the incident closed.
    states = {"llm_errors": _state(
        "fail", started_h=30.0, last_fail_h=7.0, last_run_h=7.0, runs=5)}
    results = [{"check_key": "llm_errors", "status": "ok", "message": "fine"}]
    counts = emit_transition_alerts(
        conn, results, {"llm_errors": "fail"}, _RUN_AT, states=states)
    assert counts == {"onset": 0, "recovery": 1, "reescalation": 0}
    assert conn.landed == [f"sys:llm_errors:recovery:{start:%Y-%m-%dT%H:%M:%SZ}"]
    assert conn.inserts[0][0].startswith("✓ Recovered")
    # the NEXT green run (previous run already saw it closed) must not repeat it
    later = _FakeConn()
    counts2 = emit_transition_alerts(
        later, results, {"llm_errors": "ok"}, _RUN_AT,
        states={"llm_errors": _state(
            "ok", started_h=30.0, last_fail_h=7.0, last_run_h=0.5, runs=5)})
    assert counts2 == {"onset": 0, "recovery": 0, "reescalation": 0}
    assert later.inserts == []


def test_recovery_message_reports_how_long_it_was_red() -> None:
    conn = _FakeConn()
    emit_transition_alerts(
        conn, [{"check_key": "x", "status": "ok", "message": "fine"}], {"x": "fail"},
        _RUN_AT,
        states={"x": _state("fail", started_h=31.0, last_fail_h=7.0, last_run_h=7.0, runs=4)})
    assert "24h red" in conn.inserts[0][0] and "4 failing runs" in conn.inserts[0][0]


def test_ladder_never_collides_with_the_migration_274_dead_man_switch() -> None:
    """emit_verification_stale_alert (mig 274, pg_cron, hourly) owns
    `sys:verification_stale:{date}` and rings from INSIDE the DB when the harness stops
    writing rows. Nothing here may take that key, or the watchdog's insert would be
    swallowed by ON CONFLICT and the dead-man switch would go quiet."""
    conn = _FakeConn()
    start = _RUN_AT - _h(200.0)
    conn.landed.append(f"sys:verification_stale:onset:{start:%Y-%m-%dT%H:%M:%SZ}")
    emit_transition_alerts(
        conn, [{"check_key": "verification_stale", "status": "fail", "message": "m"}],
        {"verification_stale": "fail"}, _RUN_AT,
        states={"verification_stale": _state(
            "fail", started_h=200.0, last_fail_h=6.0, last_run_h=6.0)})
    emit_weekly_heartbeat(conn, [], {}, _RUN_AT)
    for key in _dedupe_keys(conn):
        assert not key.startswith("sys:verification_stale:2")  # the {date} shape


# --- the policy ------------------------------------------------------------


def test_policy_reads_scalar_threshold_keys() -> None:
    pol = AlertPolicy.from_thresholds({
        "alert_reescalate_1_hours": 2, "alert_reescalate_2_hours": 8,
        "alert_reescalate_3_hours": 48, "alert_reescalate_weekly_hours": 100,
        "alert_flap_cooldown_hours": 3,
    })
    assert pol.reescalate_hours == (2.0, 8.0, 48.0)
    assert pol.weekly_hours == 100.0 and pol.flap_cooldown_hours == 3.0
    assert pol.due_rung(1.0) is None
    assert pol.due_rung(9.0) == "8h"
    assert pol.due_rung(250.0) == "w2"


def test_policy_ignores_non_scalar_and_missing_keys() -> None:
    """load_thresholds drops any non-scalar from the DB merge, so an operator who set an
    ARRAY would silently get the code defaults. The policy must survive that, not crash."""
    pol = AlertPolicy.from_thresholds({"alert_reescalate_1_hours": [6, 24, 72]})
    assert pol.reescalate_hours == (6.0, 24.0, 72.0)
    assert AlertPolicy.from_thresholds({}).flap_cooldown_hours == 6.0


def test_policy_rung_can_be_disabled_with_zero() -> None:
    pol = AlertPolicy.from_thresholds({
        "alert_reescalate_1_hours": 0, "alert_reescalate_weekly_hours": 0})
    assert pol.reescalate_hours == (24.0, 72.0)
    assert pol.due_rung(1000.0) == "72h"


# --- reading the history ---------------------------------------------------


_HIST_SQL_ROWS = [
    # newest first, per check_key — the shape check_states orders by
    ("llm_errors", "fail", _RUN_AT - _h(1)),
    ("llm_errors", "ok", _RUN_AT - _h(2)),      # a green blip INSIDE the cooldown
    ("llm_errors", "fail", _RUN_AT - _h(3)),
    ("llm_errors", "fail", _RUN_AT - _h(5)),
    ("llm_errors", "ok", _RUN_AT - _h(40)),     # ...and the real edge of the incident
    ("street_debt", "warn", _RUN_AT - _h(1)),
]


def test_check_states_collapses_history_into_one_incident() -> None:
    conn = _FakeConn(latest_rows=_HIST_SQL_ROWS)
    states = check_states(conn)
    sql = conn.executed[0][0]
    assert "pipeline_check_results" in sql and "ORDER BY check_key, run_at DESC" in sql
    s = states["llm_errors"]
    assert s.status == "fail"
    assert s.incident_started_at == _RUN_AT - _h(5)   # the blip did not restart it
    assert s.last_fail_at == _RUN_AT - _h(1)
    assert s.fail_runs == 3
    assert states["street_debt"].status == "warn"
    assert states["street_debt"].incident_started_at is None


def test_check_states_splits_incidents_across_a_long_green_gap() -> None:
    conn = _FakeConn(latest_rows=[
        ("x", "fail", _RUN_AT - _h(1)),
        ("x", "ok", _RUN_AT - _h(20)),
        ("x", "fail", _RUN_AT - _h(30)),   # a different incident, 29h earlier
    ])
    s = check_states(conn)["x"]
    assert s.incident_started_at == _RUN_AT - _h(1) and s.fail_runs == 1


def test_unbroken_red_survives_a_throttled_observation_gap() -> None:
    """The 6h lane really runs 80-256 min late. Consecutive fails 9h apart are ONE
    incident — the cooldown bounds green stretches, never gaps between runs, or a
    throttled run would restart the incident and re-alert onset every single time."""
    conn = _FakeConn(latest_rows=[
        ("x", "fail", _RUN_AT - _h(1)),
        ("x", "fail", _RUN_AT - _h(10)),
        ("x", "fail", _RUN_AT - _h(19)),
    ])
    s = check_states(conn)["x"]
    assert s.incident_started_at == _RUN_AT - _h(19) and s.fail_runs == 3
    # ...and the emitter agrees: still the original anchor, no second onset.
    out = _FakeConn()
    emit_transition_alerts(
        out, [{"check_key": "x", "status": "fail", "message": "red"}], {"x": "fail"},
        _RUN_AT, states={"x": s})
    assert _dedupe_keys(out)[0] == f"sys:x:onset:{_RUN_AT - _h(19):%Y-%m-%dT%H:%M:%SZ}"


def test_latest_statuses_is_the_status_only_view() -> None:
    conn = _FakeConn(latest_rows=_HIST_SQL_ROWS)
    assert latest_statuses(conn) == {"llm_errors": "fail", "street_debt": "warn"}


# --- the weekly heartbeat --------------------------------------------------


def test_weekly_heartbeat_is_keyed_per_iso_week() -> None:
    """verify_pipeline.yml appends --weekly to ALL FOUR of Monday's 6-hourly runs."""
    conn = _FakeConn()
    assert emit_weekly_heartbeat(conn, [], {}, _RUN_AT) is True
    assert conn.insert_params[1] == "sys:heartbeat:2026-W28"
    assert emit_weekly_heartbeat(conn, [], {}, _RUN_AT + _h(6)) is False
    assert len(conn.landed) == 1


def test_weekly_heartbeat_reports_all_clear() -> None:
    conn = _FakeConn()
    emit_weekly_heartbeat(
        conn, [{"check_key": "a", "status": "ok"}, {"check_key": "b", "status": "warn"}],
        {}, _RUN_AT)
    assert "all 2 pipeline checks are healthy" in conn.inserts[0][0]


def test_weekly_heartbeat_names_the_failing_checks_and_their_age() -> None:
    conn = _FakeConn()
    states = {"property_maintenance": _state(
        "fail", started_h=144.0, last_fail_h=6.0, last_run_h=6.0, runs=24)}
    emit_weekly_heartbeat(
        conn, [{"check_key": "property_maintenance", "status": "fail"},
               {"check_key": "geo_debt", "status": "ok"}], states, _RUN_AT)
    msg = conn.inserts[0][0]
    assert "1 of 2 checks are failing" in msg and "property_maintenance (144h)" in msg


def test_dedupe_key_shape_explicit_day() -> None:
    conn = _FakeConn()
    assert emit_system_alert(conn, "llm_health", "down", day="2026-07-05") is True
    params = conn.insert_params
    # (message, dedupe_key, channels)
    assert params[0] == "down"
    assert params[1] == "sys:llm_health:2026-07-05"


def test_dedupe_key_defaults_to_today_utc() -> None:
    conn = _FakeConn()
    emit_system_alert(conn, "street_debt", "msg")
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    assert conn.insert_params[1] == f"sys:street_debt:{today}"


def test_on_conflict_noop_returns_false() -> None:
    # ON CONFLICT (dedupe_key) DO NOTHING → rowcount 0 → a repeat call is a no-op.
    conn = _FakeConn(insert_rowcount=0)
    assert emit_system_alert(conn, "llm_health", "down", day="2026-07-05") is False


def test_insert_uses_system_health_producer_and_in_app() -> None:
    conn = _FakeConn()
    emit_system_alert(conn, "engine_health", "stuck", day="2026-07-05")
    sql = conn.insert_sql or ""
    assert "'system_health'" in sql and "'system_alert'" in sql and "'in_app'" in sql
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in sql


def test_channels_read_and_passed_as_list() -> None:
    conn = _FakeConn(channels_value=["email", "telegram"])
    emit_system_alert(conn, "llm_health", "down", day="2026-07-05")
    assert conn.insert_params[2] == ["email", "telegram"]


def test_channels_default_empty_when_setting_missing() -> None:
    conn = _FakeConn(channels_value=_MISSING)
    emit_system_alert(conn, "llm_health", "down", day="2026-07-05")
    assert conn.insert_params[2] == []


def test_channels_json_string_is_parsed() -> None:
    conn = _FakeConn(channels_value=json.dumps(["telegram"]))
    emit_system_alert(conn, "llm_health", "down", day="2026-07-05")
    assert conn.insert_params[2] == ["telegram"]


def test_channels_non_list_defaults_empty() -> None:
    conn = _FakeConn(channels_value={"not": "a list"})
    emit_system_alert(conn, "llm_health", "down", day="2026-07-05")
    assert conn.insert_params[2] == []


def test_channels_drops_non_string_entries() -> None:
    conn = _FakeConn(channels_value=["email", 42, None, "telegram"])
    emit_system_alert(conn, "llm_health", "down", day="2026-07-05")
    assert conn.insert_params[2] == ["email", "telegram"]
