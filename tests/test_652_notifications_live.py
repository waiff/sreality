"""Migration 652, executed: a watchdog's active_since restarts on create, on a switch-on and on a filter
edit, and on nothing else (ruling R6 (a), extended 2026-10-21: an edited watchdog is a new start). Runs in
CI's migrations job (`TEST_DATABASE_URL`); every test rolls back. One transaction, so a restart reads as
`active_since = now()` after the row was aged by hand (an UPDATE of active_since alone fires nothing)."""

from __future__ import annotations

from typing import Any

from tests._live_property import REQUIRED_DB, _account, cur  # noqa: F401 -- `cur` is the fixture

pytestmark = REQUIRED_DB

_AGED = "2026-01-01 00:00:00+00"


def _restarted(cur: Any, sid: Any) -> bool:
    cur.execute("SELECT active_since = now() FROM notification_subscriptions WHERE id = %s", (sid,))
    return cur.fetchone()[0]


def test_a_watchdog_restarts_when_created_switched_on_or_edited(cur: Any) -> None:
    acc = _account(cur)
    cur.execute(
        "INSERT INTO notification_subscriptions (account_id, name, filter_spec, active_since) "
        "VALUES (%s, 'w', '{}'::jsonb, %s) RETURNING id", (acc, _AGED))
    sid = cur.fetchone()[0]
    assert _restarted(cur, sid), "an insert stamps now(), whatever it was handed"
    for change, restarts in (
        ("name = 'renamed'", False),
        ("is_active = true", False),                              # already on: no flip
        ("filter_spec = '{}'::jsonb", False),                     # the same spec: no edit
        ("filter_spec = '{\"min_price_czk\": 1000000}'::jsonb", True),
        ("is_active = false", False),
        ("is_active = true", True),
        ("is_active = false, filter_spec = '{}'::jsonb", True),  # an edit while paused
    ):
        cur.execute("UPDATE notification_subscriptions SET active_since = %s WHERE id = %s",
                    (_AGED, sid))
        assert not _restarted(cur, sid)
        cur.execute(f"UPDATE notification_subscriptions SET {change} WHERE id = %s", (sid,))  # noqa: S608
        assert _restarted(cur, sid) is restarts, change


def test_a_share_notification_goes_with_its_sharer(cur: Any) -> None:
    owner, recipient = _account(cur), _account(cur)
    cur.execute(
        "INSERT INTO notification_dispatches (source_kind, change_kind, account_id, "
        "actor_account_id, event_ref) VALUES ('share', 'shared_pipeline', %s, %s, "
        "'read') RETURNING id", (recipient, owner))
    did = cur.fetchone()[0]
    cur.execute("DELETE FROM accounts WHERE id = %s", (owner,))
    cur.execute("SELECT count(*) FROM notification_dispatches WHERE id = %s", (did,))
    assert cur.fetchone()[0] == 0
