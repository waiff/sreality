"""Tests for toolkit.verify_listing_freshness.

Hermetic: monkeypatches the four DB lookups and the wrapped
freshness_check. No live psycopg connection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from toolkit import freshness as toolkit_freshness


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _listing(
    sreality_id: int = 1,
    last_seen_at: datetime | None = None,
    is_active: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "sreality_id": sreality_id,
        "first_seen_at": last_seen_at,
        "last_seen_at": last_seen_at if last_seen_at is not None else _now(),
        "is_active": is_active,
        "category_main": "byt", "category_type": "pronajem",
        "price_czk": 20000, "price_unit": "měsíc",
        "area_m2": 50.0, "disposition": "2+kk",
        "locality": "Praha 1", "district": "Praha 1",
        "locality_district_id": 5001, "locality_region_id": 19,
        "floor": 3, "total_floors": 5,
        "has_balcony": True, "has_parking": False, "has_lift": True,
        "building_type": "cihla", "condition": "novostavba",
        "energy_rating": "B",
        **fields,
    }


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    listing: dict[str, Any] | None = None,
    listing_after: dict[str, Any] | None = None,
    last_check_at: datetime | None = None,
    snapshot_id: int | None = 99,
    fresh_result: dict[str, Any] | None = None,
) -> dict[str, list]:
    """Patch all 4 DB helpers + freshness_check. Returns recorded calls."""
    listings = [listing, listing_after if listing_after is not None else listing]
    state = {"i": 0}

    def fake_fetch_listing(
        _c: Any, sreality_id: Any = None, listing_id: Any = None,
    ) -> Any:
        i = state["i"]
        state["i"] += 1
        return listings[i] if i < len(listings) else listings[-1]

    monkeypatch.setattr(toolkit_freshness, "_fetch_listing", fake_fetch_listing)
    monkeypatch.setattr(
        toolkit_freshness, "_fetch_last_check_at",
        lambda _c, _sid: last_check_at,
    )
    monkeypatch.setattr(
        toolkit_freshness, "_fetch_latest_snapshot_id",
        lambda _c, _sid: snapshot_id,
    )

    fresh_calls: list[int] = []

    def fake_fresh(_conn: Any, _client: Any, sid: int) -> dict[str, Any]:
        fresh_calls.append(sid)
        return fresh_result or {
            "sreality_id": sid, "outcome": "unchanged",
            "prev_hash": "h", "new_hash": "h", "what_changed": [],
            "error_message": None, "checked_at": _now().isoformat(),
            "snapshot_id": snapshot_id,
        }

    monkeypatch.setattr("scraper.freshness.freshness_check", fake_fresh)
    return {"fresh": fresh_calls}


def test_recent_listing_returns_cached_no_api_call(monkeypatch):
    listing = _listing(last_seen_at=_now() - timedelta(hours=2))
    calls = _patch(monkeypatch, listing=listing)

    res = toolkit_freshness.verify_listing_freshness(
        conn=None, client=None, sreality_id=1, max_age_hours=24,
    )

    assert calls["fresh"] == []
    d = res["data"]
    assert d["outcome"] == "cached"
    assert d["cached"] is True
    assert d["verified"] is False
    assert d["age_hours"] is not None and 1.5 < d["age_hours"] < 2.5
    assert d["what_changed"] == []
    assert d["snapshot_id"] == 99
    assert d["current"]["sreality_id"] == 1
    assert d["current"]["last_seen_at"]  # iso-formatted

    md = res["metadata"]
    assert md["tool"] == "verify_listing_freshness"
    assert md["filters_used"] == {"sreality_id": 1, "max_age_hours": 24}
    assert md["result_count"] == 1


def test_recent_check_throttles_even_if_listing_old(monkeypatch):
    """last_check_at is the more recent signal; the cache uses GREATEST."""
    listing = _listing(last_seen_at=_now() - timedelta(days=10))
    calls = _patch(
        monkeypatch,
        listing=listing,
        last_check_at=_now() - timedelta(hours=1),
    )

    res = toolkit_freshness.verify_listing_freshness(
        conn=None, client=None, sreality_id=1, max_age_hours=6,
    )

    assert calls["fresh"] == []
    assert res["data"]["outcome"] == "cached"
    assert res["data"]["age_hours"] < 2


def test_stale_triggers_fetch_and_returns_freshness_outcome(monkeypatch):
    listing = _listing(last_seen_at=_now() - timedelta(days=5))
    fresh = {
        "sreality_id": 1, "outcome": "updated",
        "prev_hash": "old", "new_hash": "new",
        "what_changed": ["price_czk"],
        "error_message": None,
        "checked_at": _now().isoformat(),
        "snapshot_id": 200,
    }
    listing_after = {**listing, "price_czk": 22000}
    calls = _patch(
        monkeypatch,
        listing=listing, listing_after=listing_after,
        fresh_result=fresh,
    )

    res = toolkit_freshness.verify_listing_freshness(
        conn=None, client=None, sreality_id=1, max_age_hours=24,
    )

    assert calls["fresh"] == [1]
    d = res["data"]
    assert d["outcome"] == "updated"
    assert d["verified"] is True
    assert d["cached"] is False
    assert d["age_hours"] == 0.0
    assert d["what_changed"] == ["price_czk"]
    assert d["snapshot_id"] == 200
    assert d["current"]["price_czk"] == 22000
    assert res["metadata"]["data_freshness"] == fresh["checked_at"]


def test_stale_with_fetch_error_reports_old_age(monkeypatch):
    old_seen = _now() - timedelta(days=3)
    listing = _listing(last_seen_at=old_seen)
    fresh = {
        "sreality_id": 1, "outcome": "fetch_error",
        "prev_hash": "h", "new_hash": None,
        "what_changed": [],
        "error_message": "ConnectionError: dns",
        "checked_at": _now().isoformat(),
        "snapshot_id": None,
    }
    calls = _patch(monkeypatch, listing=listing, fresh_result=fresh)

    res = toolkit_freshness.verify_listing_freshness(
        conn=None, client=None, sreality_id=1, max_age_hours=24,
    )

    assert calls["fresh"] == [1]
    d = res["data"]
    assert d["outcome"] == "fetch_error"
    assert d["verified"] is True
    assert d["cached"] is False
    # age_hours kept the pre-fetch value because fetch failed.
    assert d["age_hours"] is not None and d["age_hours"] > 24
    # data_freshness falls back to last_seen_at since the fetch didn't refresh.
    assert res["metadata"]["data_freshness"] == old_seen.isoformat()


def test_no_listing_in_db_triggers_fetch(monkeypatch):
    fresh = {
        "sreality_id": 99, "outcome": "gone",
        "prev_hash": None, "new_hash": None, "what_changed": [],
        "error_message": None, "checked_at": _now().isoformat(),
        "snapshot_id": None,
    }
    calls = _patch(
        monkeypatch,
        listing=None, listing_after=None,
        fresh_result=fresh,
    )

    res = toolkit_freshness.verify_listing_freshness(
        conn=None, client=None, sreality_id=99, max_age_hours=24,
    )

    assert calls["fresh"] == [99]
    d = res["data"]
    assert d["outcome"] == "gone"
    assert d["current"] is None


def test_recent_failed_check_still_throttles_to_avoid_hammering(monkeypatch):
    """A recent fetch_error is still a recent check — don't hammer sreality."""
    listing = _listing(last_seen_at=_now() - timedelta(days=10))
    calls = _patch(
        monkeypatch,
        listing=listing,
        last_check_at=_now() - timedelta(minutes=5),
    )

    res = toolkit_freshness.verify_listing_freshness(
        conn=None, client=None, sreality_id=1, max_age_hours=1,
    )

    assert calls["fresh"] == []
    assert res["data"]["outcome"] == "cached"


def test_serializes_datetimes_to_iso(monkeypatch):
    fixed = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    listing = _listing(last_seen_at=fixed)
    listing["first_seen_at"] = fixed
    _patch(monkeypatch, listing=listing)

    res = toolkit_freshness.verify_listing_freshness(
        conn=None, client=None, sreality_id=1, max_age_hours=10000,
    )

    cur = res["data"]["current"]
    assert cur["first_seen_at"] == fixed.isoformat()
    assert cur["last_seen_at"] == fixed.isoformat()


# --- Gate-2: addressable by the surrogate listing_id ----------------------


class _CaptureCursor:
    def __init__(self, row):
        self.row = row
        self.executed: list[Any] = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class _CaptureConn:
    def __init__(self, row):
        self.cur = _CaptureCursor(row)

    def cursor(self):
        return self.cur


def test_fetch_listing_uses_id_arm_for_listing_id():
    """_fetch_listing keys on listings.id when addressed by the surrogate."""
    row = tuple(range(len(toolkit_freshness._LISTING_COLS)))
    conn = _CaptureConn(row)
    out = toolkit_freshness._fetch_listing(conn, listing_id=555)
    sql, params = conn.cur.executed[0]
    assert "WHERE id = %s" in sql
    assert params == (555,)
    assert out["sreality_id"] == 0  # first column of the row


def test_fetch_listing_sreality_arm_is_byte_identical():
    row = tuple(range(len(toolkit_freshness._LISTING_COLS)))
    conn = _CaptureConn(row)
    toolkit_freshness._fetch_listing(conn, sreality_id=7)
    sql, params = conn.cur.executed[0]
    assert "WHERE sreality_id = %s" in sql
    assert params == (7,)


def test_listing_id_resolves_to_row_sreality_id_for_refetch(monkeypatch):
    """Addressed by listing_id, the sreality-native refetch + freshness_checks
    must key on the RESOLVED row's sreality_id, not the surrogate handle."""
    listing = _listing(sreality_id=1, last_seen_at=_now() - timedelta(days=5))
    calls = _patch(monkeypatch, listing=listing)

    res = toolkit_freshness.verify_listing_freshness(
        conn=None, client=None, listing_id=987654, max_age_hours=24,
    )

    # freshness_check saw the row's sreality_id (1), never the listing_id (987654).
    assert calls["fresh"] == [1]
    assert res["data"]["sreality_id"] == 1


def test_neither_id_raises_clean_value_error():
    with pytest.raises(ValueError):
        toolkit_freshness.verify_listing_freshness(conn=None, client=None)
