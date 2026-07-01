"""Hermetic tests for the assess() decision logic in check_llm_health.

No DB — psycopg import lives inside main(), so importing assess is clean.
The _pending_unscored tests use a scripted cursor, no connection.
"""

from __future__ import annotations

from typing import Any

from scripts.check_llm_health import _pending_unscored, _recent_failures, assess


def _stalled(condition_call_age_hours: float | None = 1.0, **kw):
    return assess(
        max_idle_hours=4.0,
        condition_max_idle_hours=8.0,
        min_pending=50,
        condition_call_age_hours=condition_call_age_hours,
        **kw,
    )[0]


def test_no_alarm_when_nothing_to_score():
    # Below the pending floor → legitimately idle, never alarm.
    assert _stalled(last_call_age_hours=None, pending=0) is False
    assert _stalled(last_call_age_hours=99.0, pending=10) is False
    assert _stalled(
        last_call_age_hours=1.0, condition_call_age_hours=99.0, pending=10,
    ) is False


def test_alarm_when_idle_with_pending_work():
    assert _stalled(last_call_age_hours=8.0, pending=1000) is True


def test_no_alarm_when_recent_calls():
    assert _stalled(last_call_age_hours=1.5, pending=100000) is False


def test_alarm_when_no_calls_ever_but_work_exists():
    assert _stalled(last_call_age_hours=None, pending=500) is True


def test_boundary_at_threshold():
    # Exactly at the threshold is still OK; strictly greater alarms.
    assert _stalled(last_call_age_hours=4.0, pending=1000) is False
    assert _stalled(last_call_age_hours=4.01, pending=1000) is True


def test_message_mentions_credit_when_stalled():
    _, msg = assess(
        last_call_age_hours=9.0, condition_call_age_hours=9.0, pending=1000,
        max_idle_hours=4.0, condition_max_idle_hours=8.0, min_pending=50,
    )
    assert "credit" in msg.lower()


def test_alarm_when_condition_stale_despite_fresh_global_traffic():
    # The green-masking case: agent/summarize calls keep the global
    # max(called_at) fresh while the condition batch pipeline is dead.
    assert _stalled(
        last_call_age_hours=0.5, condition_call_age_hours=12.0, pending=1000,
    ) is True


def test_alarm_when_no_condition_calls_ever_but_work_exists():
    assert _stalled(
        last_call_age_hours=0.5, condition_call_age_hours=None, pending=1000,
    ) is True


def test_condition_boundary_at_threshold():
    assert _stalled(
        last_call_age_hours=0.5, condition_call_age_hours=8.0, pending=1000,
    ) is False
    assert _stalled(
        last_call_age_hours=0.5, condition_call_age_hours=8.01, pending=1000,
    ) is True


def test_condition_stall_message_names_the_pipeline():
    _, msg = assess(
        last_call_age_hours=0.5, condition_call_age_hours=12.0, pending=1000,
        max_idle_hours=4.0, condition_max_idle_hours=8.0, min_pending=50,
    )
    assert "score_listing_condition" in msg


# ---- provider-outage alarm (independent of pending work) --------------------


def test_credit_exhausted_alarms_regardless_of_pending():
    # The blind spot: an outage must alarm even when there is NO pending condition work
    # (which is exactly when the pending-gated checks stay silent).
    stalled, msg = assess(
        last_call_age_hours=0.1, condition_call_age_hours=0.1, pending=0,
        max_idle_hours=4.0, condition_max_idle_hours=8.0, min_pending=50,
        recent_failures=5, credit_exhausted=True,
    )
    assert stalled is True
    assert "credit" in msg.lower() and "out of credit" in msg.lower()


def test_recent_failures_alarm_above_floor_regardless_of_pending():
    assert _stalled(last_call_age_hours=0.1, pending=0, recent_failures=3) is True
    # Below the floor + no credit error → falls through to the (idle) pending logic → no alarm.
    assert _stalled(last_call_age_hours=0.1, pending=0, recent_failures=2) is False


def test_recent_failures_helper_bound_pattern_and_filter():
    # count(*) + count FILTER(credit) with the ILIKE wildcard in the BOUND VALUE (no bare %).
    conn = _ScriptedConn([("fetchone", (7, 2))])
    total, credit = _recent_failures(conn, hours=4.0)
    assert total == 7 and credit is True
    sql, params = conn.cursor_obj.executed[-1]
    assert "error IS NOT NULL" in sql and "FILTER (WHERE error ILIKE %s)" in sql
    assert "%credit balance%" in params and 4.0 in params


# ---- _pending_unscored: kraj scoping mirrors the scorer ---------------------


def test_pending_unscored_zero_when_scoring_paused():
    # No enabled regions (settings row missing) -> nothing is pending, so
    # parked work never reads as a stall — and the count query never runs.
    conn = _ScriptedConn([("fetchone", None)])
    assert _pending_unscored(conn) == 0
    assert len(conn.cursor_obj.executed) == 1


def test_pending_unscored_scopes_count_to_enabled_regions():
    conn = _ScriptedConn([
        ("fetchone", ([27, 43],)),  # app_settings enabled-regions lookup
        ("fetchone", (123,)),       # scoped count
    ])
    assert _pending_unscored(conn) == 123
    sql, params = conn.cursor_obj.executed[-1]
    assert "region_id = ANY(%s::bigint[])" in sql
    assert "building_condition_level IS NULL" in sql
    assert params == ([27, 43],)


class _ScriptedCursor:
    def __init__(self, plan: list[tuple[str, Any]]) -> None:
        self._plan = plan
        self._idx = 0
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        if self._idx >= len(self._plan):
            raise AssertionError(f"execute past plan end (sql={sql[:80]!r})")
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        out = self._plan[self._idx][1]
        self._idx += 1
        return out

    def __enter__(self) -> "_ScriptedCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _ScriptedConn:
    def __init__(self, plan: list[tuple[str, Any]]) -> None:
        self.cursor_obj = _ScriptedCursor(plan)

    def cursor(self) -> _ScriptedCursor:
        return self.cursor_obj
