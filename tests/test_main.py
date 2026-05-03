"""Tests for scraper.main._run_full — focused on the mark_inactive guard.

Hermetic: monkeypatches db.* functions and provides a fake client.
Asserts that mark_inactive is called only when the index walk is
complete (limit is None).
"""

from __future__ import annotations

from typing import Any

import pytest

from scraper import main as scraper_main


class _FakeClient:
    pages_fetched = 1

    def __init__(self, total_entries: int = 100) -> None:
        self._total = total_entries

    def iter_index(self):
        for i in range(self._total):
            yield {
                "hash_id": 1000 + i,
                "price_czk": {"value_raw": 10000 + i},
            }


@pytest.fixture()
def patched_db(monkeypatch):
    """Patch every db.* helper used by _run_full. Track mark_inactive calls."""
    calls: dict[str, list] = {
        "mark_inactive": [],
        "touch_listings": [],
        "index_summary": [],
    }

    class _FakeConn:
        def close(self) -> None:
            pass

    monkeypatch.setattr(
        scraper_main.db, "connect", _FakeConn,
    )
    monkeypatch.setattr(
        scraper_main.db, "index_summary",
        lambda _conn, _ids: (calls["index_summary"].append(set(_ids)) or {}),
    )
    monkeypatch.setattr(
        scraper_main.db, "touch_listings",
        lambda _conn, _ids: (calls["touch_listings"].append(list(_ids)) or 0),
    )
    monkeypatch.setattr(
        scraper_main.db, "active_failure_ids", lambda _conn, _ids: set(),
    )
    monkeypatch.setattr(
        scraper_main.db, "mark_inactive",
        lambda _conn, ids: (calls["mark_inactive"].append(set(ids)) or 0),
    )
    monkeypatch.setattr(
        scraper_main, "_process_one",
        lambda *_a, **_kw: "unchanged",
    )
    return calls


def test_run_full_calls_mark_inactive_when_no_limit(patched_db):
    rc = scraper_main._run_full(
        _FakeClient(total_entries=5), limit=None, dry_run=False,
    )
    assert rc == 0
    assert len(patched_db["mark_inactive"]) == 1
    assert patched_db["mark_inactive"][0] == {1000, 1001, 1002, 1003, 1004}


def test_run_full_skips_mark_inactive_when_limit_set(patched_db):
    rc = scraper_main._run_full(
        _FakeClient(total_entries=100), limit=3, dry_run=False,
    )
    assert rc == 0
    assert patched_db["mark_inactive"] == []  # not called


def test_run_full_skips_mark_inactive_when_limit_zero(patched_db):
    """limit=0 still means partial view, even if no listings were seen."""
    rc = scraper_main._run_full(
        _FakeClient(total_entries=100), limit=0, dry_run=False,
    )
    assert rc == 0
    assert patched_db["mark_inactive"] == []


def test_dry_run_never_calls_mark_inactive(patched_db, monkeypatch):
    """dry_run skips the connection altogether, so mark_inactive can't run."""
    monkeypatch.setattr(scraper_main.db, "connect", lambda: None)
    rc = scraper_main._run_full(
        _FakeClient(total_entries=5), limit=None, dry_run=True,
    )
    assert rc == 0
    assert patched_db["mark_inactive"] == []
