"""Hermetic tests for the assess() decision logic in check_llm_health.

No DB — psycopg import lives inside main(), so importing assess is clean.
"""

from __future__ import annotations

from scripts.check_llm_health import assess


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
