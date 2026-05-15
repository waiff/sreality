"""Hermetic tests for the notifications backend (Phase U2.7).

Covers WatchdogFilterSpec validation + the SQL-clause generator. The
matcher loop and FastAPI routes are integration-heavy (DB + asyncio
lifespan) and exercised via the live deployment, not here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from api.notifications import WatchdogFilterSpec, _build_match_clauses


def test_filter_spec_defaults_target_byt_pronajem() -> None:
    """Blank-save watchdogs already target apartments-for-rent."""
    spec = WatchdogFilterSpec()
    assert spec.category_main == "byt"
    assert spec.category_type == "pronajem"


def test_filter_spec_spatial_requires_all_three() -> None:
    """lat/lng/radius_m must be all set or all None — partial spatial
    filters silently broke the SQL in v1, so validate eagerly."""
    with pytest.raises(ValueError):
        WatchdogFilterSpec(lat=50.0, lng=14.0)  # radius_m missing
    with pytest.raises(ValueError):
        WatchdogFilterSpec(lat=50.0, radius_m=1000)  # lng missing

    # All three set is OK.
    s = WatchdogFilterSpec(lat=50.0, lng=14.0, radius_m=1000)
    assert s.lat == 50.0


def test_build_clauses_emits_category_clauses_by_default() -> None:
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert "l.category_main = %(category_main)s" in where
    assert "l.category_type = %(category_type)s" in where
    assert params["category_main"] == "byt"
    assert params["category_type"] == "pronajem"


def test_build_clauses_skips_unset_spatial() -> None:
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert not any("ST_DWithin" in w for w in where)
    assert "radius_m" not in params


def test_build_clauses_emits_spatial_when_set() -> None:
    spec = WatchdogFilterSpec(lat=50.08, lng=14.42, radius_m=1500)
    where, params = _build_match_clauses(spec)
    assert any("ST_DWithin" in w for w in where)
    assert any("l.geom IS NOT NULL" in w for w in where)
    assert params["lat"] == 50.08
    assert params["lng"] == 14.42
    assert params["radius_m"] == 1500


def test_build_clauses_handles_price_and_area_bounds() -> None:
    spec = WatchdogFilterSpec(
        min_price_czk=15_000,
        max_price_czk=30_000,
        min_area_m2=40.0,
        max_area_m2=80.0,
    )
    where, params = _build_match_clauses(spec)
    assert "l.price_czk >= %(min_price_czk)s" in where
    assert "l.price_czk <= %(max_price_czk)s" in where
    assert "l.area_m2 >= %(min_area_m2)s" in where
    assert "l.area_m2 <= %(max_area_m2)s" in where
    assert params["min_price_czk"] == 15_000
    assert params["max_price_czk"] == 30_000


def test_build_clauses_tri_state_amenities() -> None:
    spec = WatchdogFilterSpec(
        has_balcony=True,
        terrace=False,
        garage=None,  # explicit "any" — no clause
    )
    where, params = _build_match_clauses(spec)
    assert "l.has_balcony = %(has_balcony)s" in where
    assert "l.terrace = %(terrace)s" in where
    assert not any("l.garage = " in w for w in where)
    assert params["has_balcony"] is True
    assert params["terrace"] is False


def test_build_clauses_dispositions_use_any() -> None:
    """Dispositions are a multi-select; SQL uses ANY() for index-friendly
    lookups against `l.disposition`."""
    spec = WatchdogFilterSpec(dispositions=["2+kk", "2+1", "3+kk"])
    where, params = _build_match_clauses(spec)
    assert "l.disposition = ANY(%(dispositions)s)" in where
    assert params["dispositions"] == ["2+kk", "2+1", "3+kk"]


def test_build_clauses_enumerated_columns() -> None:
    spec = WatchdogFilterSpec(furnished="ano", ownership="osobni")
    where, params = _build_match_clauses(spec)
    assert "l.furnished = %(furnished)s" in where
    assert "l.ownership = %(ownership)s" in where
    assert params["furnished"] == "ano"
    assert params["ownership"] == "osobni"


def test_build_clauses_categoryless_spec() -> None:
    """An operator who explicitly clears the category filters gets a
    spec with no category WHERE clauses — the watchdog matches every
    category. Defaults narrow; explicit None widens."""
    spec = WatchdogFilterSpec(category_main=None, category_type=None)
    where, params = _build_match_clauses(spec)
    assert not any("l.category_main" in w for w in where)
    assert not any("l.category_type" in w for w in where)
    assert "category_main" not in params
    assert "category_type" not in params


# --- match_once with a fake psycopg connection ---------------------------


from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from api.notifications import match_once


class _FakeCursor:
    """Minimal cursor that returns canned rows in sequence.

    `_FakeConn.results` is a list of (predicate, rows) pairs; the cursor
    finds the first predicate that matches the executed SQL and returns
    its `rows`. Insert/UPDATE SQL is captured in `_FakeConn.executed`
    so the test can assert on what the matcher emitted.
    """

    def __init__(self, conn: "_FakeConn"):
        self._conn = conn
        self._last_rows: list[tuple[Any, ...]] = []
        self.rowcount = 0
        self.description: list[tuple[str]] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        sql_norm = " ".join(sql.split())
        self._conn.executed.append((sql_norm, params))
        for predicate, rows, rowcount in self._conn.script:
            if predicate(sql_norm):
                self._last_rows = rows
                self.rowcount = rowcount
                return
        self._last_rows = []
        self.rowcount = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._last_rows[0] if self._last_rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._last_rows)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _FakeConn:
    def __init__(
        self,
        script: list[tuple[Any, list[tuple[Any, ...]], int]],
    ):
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def test_match_once_uses_per_subscription_cursor() -> None:
    """The matcher reads `last_matched_first_seen_at` per subscription
    and injects it as the `cursor` clause — proves the per-subscription
    cursor column drives the SQL, not the old global watermark."""
    sub_id = UUID("46a6005f-dc32-4d94-9ced-4d262b57ef6b")
    cursor_ts = datetime(2026, 5, 14, 13, 57, 48, tzinfo=timezone.utc)
    upper_ts = datetime(2026, 5, 15, 21, 0, 0, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        # app_settings reads for interval / window — default both.
        (lambda s: "FROM app_settings" in s, [], 0),
        # Active subscriptions query
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {"category_main": "byt", "category_type": "pronajem", "districts": ["Praha"]}, cursor_ts)],
            1,
        ),
        # Window upper-bound query
        (lambda s: "SELECT max(first_seen_at), count(*) FROM" in s, [(upper_ts, 5)], 0),
        # INSERT dispatches — return rowcount 3 (idempotent dedup)
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 3),
        # Cursor advance UPDATE — rowcount 1
        (lambda s: "UPDATE notification_subscriptions SET last_matched_first_seen_at" in s, [], 1),
    ]
    conn = _FakeConn(script)

    stats = match_once(conn)  # type: ignore[arg-type]

    assert stats == {
        "subscriptions_evaluated": 1,
        "matches_inserted": 3,
        "listings_in_window": 5,
        "cursors_advanced": 1,
    }

    # Verify the WHERE clause references the per-subscription cursor
    # parameter, not a global watermark.
    window_query = next(
        (sql, p) for sql, p in conn.executed if "max(first_seen_at), count(*)" in sql
    )
    sql, params = window_query
    assert "l.first_seen_at > %(cursor)s" in sql
    assert isinstance(params, dict) and params["cursor"] == cursor_ts

    # And that the filter spec made it through too.
    assert params["category_main"] == "byt"
    assert params["category_type"] == "pronajem"
    assert params["districts"] == ["Praha"]


def test_match_once_skips_subscription_with_no_listings() -> None:
    """An empty window (no listings past the cursor) is the steady
    state. The matcher should record zero dispatches and zero cursor
    advances rather than blow up trying to UPDATE with a NULL value."""
    sub_id = UUID("00000000-0000-0000-0000-000000000001")
    cursor_ts = datetime(2030, 1, 1, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {}, cursor_ts)],
            1,
        ),
        # Window query returns (NULL, 0) — no fresh listings.
        (lambda s: "SELECT max(first_seen_at), count(*) FROM" in s, [(None, 0)], 0),
    ]
    conn = _FakeConn(script)

    stats = match_once(conn)  # type: ignore[arg-type]

    assert stats["subscriptions_evaluated"] == 1
    assert stats["matches_inserted"] == 0
    assert stats["cursors_advanced"] == 0
    # Critically: no INSERT or UPDATE was attempted.
    assert not any("INSERT INTO notification_dispatches" in sql for sql, _ in conn.executed)
    assert not any(
        "UPDATE notification_subscriptions" in sql for sql, _ in conn.executed
    )


def test_match_once_skips_invalid_filter_spec() -> None:
    """A subscription whose filter_spec doesn't validate (e.g. corrupt
    legacy row) must NOT crash the whole matcher pass — log + skip."""
    good_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    bad_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    cursor_ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    upper_ts = datetime(2026, 5, 15, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [
                # Invalid: partial spatial filter (no radius).
                (bad_id, {"lat": 50.0, "lng": 14.0}, cursor_ts),
                # Valid spec
                (good_id, {}, cursor_ts),
            ],
            2,
        ),
        (lambda s: "SELECT max(first_seen_at), count(*) FROM" in s, [(upper_ts, 1)], 0),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 1),
        (lambda s: "UPDATE notification_subscriptions SET last_matched_first_seen_at" in s, [], 1),
    ]
    conn = _FakeConn(script)

    stats = match_once(conn)  # type: ignore[arg-type]

    # Both rows counted as "evaluated" — the broken one was inspected
    # and skipped, but the surviving one fired one dispatch.
    assert stats["subscriptions_evaluated"] == 2
    assert stats["matches_inserted"] == 1
