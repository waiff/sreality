"""Tests for scraper.main._run_full — focused on the mark_inactive guard.

Hermetic: monkeypatches db.* functions and the SrealityClient builder so
no network is touched. Asserts that mark_inactive is called only when
the index walk is complete (limit is None) and that it is scoped per
category pair so a rental walk doesn't clobber sale listings.
"""

from __future__ import annotations

from typing import Any

import pytest

from scraper import main as scraper_main


class _FakeClient:
    """Yields a deterministic per-category id range so tests can assert
    that mark_inactive is scoped correctly."""

    pages_fetched = 1

    def __init__(
        self,
        category_main: int,
        category_type: int,
        country_id: int = 10001,
    ) -> None:
        self.category_main = category_main
        self.category_type = category_type
        self.country_id = country_id

    def iter_index(self):
        # Distinct id range per (cm, ct) so the per-category seen_ids
        # set is observable in mark_inactive call args.
        base = self.category_main * 10000 + self.category_type * 1000
        total = _FakeClient.total_entries
        for i in range(total):
            yield {
                "hash_id": base + i,
                "price_czk": {"value_raw": 10000 + i},
            }


_FakeClient.total_entries = 5  # type: ignore[attr-defined]


@pytest.fixture()
def patched_db(monkeypatch):
    """Patch every db.* helper used by _run_full + the per-category client."""
    calls: dict[str, list] = {
        "mark_inactive": [],
        "touch_listings": [],
        "index_summary": [],
    }

    class _FakeConn:
        def close(self) -> None:
            pass

    monkeypatch.setattr(scraper_main.db, "connect", _FakeConn)
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
        lambda _conn, cm, ct, ids: (
            calls["mark_inactive"].append((cm, ct, set(ids))) or 0
        ),
    )
    monkeypatch.setattr(
        scraper_main, "_process_one",
        lambda *_a, **_kw: ("unchanged", 0),
    )
    # Intercept SrealityClient construction in _build_client.
    monkeypatch.setattr(scraper_main, "SrealityClient", _FakeClient)
    return calls


def test_run_full_calls_mark_inactive_per_category_when_no_limit(patched_db):
    rc, _agg = scraper_main._run_full(limit=None, dry_run=False)
    assert rc == 0
    # One mark_inactive call per category in CATEGORIES.
    assert len(patched_db["mark_inactive"]) == len(scraper_main.CATEGORIES)

    # Each call is scoped to its own (cm_text, ct_text) and carries the
    # ids that came from that category's index walk only.
    expected_pairs = {
        ("byt", "pronajem"),
        ("byt", "prodej"),
        ("dum", "pronajem"),
        ("dum", "prodej"),
        ("komercni", "pronajem"),
        ("komercni", "prodej"),
    }
    actual_pairs = {(cm, ct) for cm, ct, _ids in patched_db["mark_inactive"]}
    assert actual_pairs == expected_pairs

    # Spot-check that ids are category-scoped: byt/pronajem (1, 2) base = 12000.
    by_pair = {(cm, ct): ids for cm, ct, ids in patched_db["mark_inactive"]}
    assert by_pair[("byt", "pronajem")] == {12000, 12001, 12002, 12003, 12004}
    assert by_pair[("byt", "prodej")] == {11000, 11001, 11002, 11003, 11004}


def test_run_full_skips_mark_inactive_when_limit_set(patched_db):
    rc, _agg = scraper_main._run_full(limit=3, dry_run=False)
    assert rc == 0
    assert patched_db["mark_inactive"] == []


def test_run_full_skips_mark_inactive_when_limit_zero(patched_db):
    """limit=0 still means partial view, even if no listings were seen."""
    rc, _agg = scraper_main._run_full(limit=0, dry_run=False)
    assert rc == 0
    assert patched_db["mark_inactive"] == []


def test_dry_run_never_calls_mark_inactive(patched_db, monkeypatch):
    """dry_run skips the connection altogether, so mark_inactive can't run."""
    monkeypatch.setattr(scraper_main.db, "connect", lambda: None)
    rc, _agg = scraper_main._run_full(limit=None, dry_run=True)
    assert rc == 0
    assert patched_db["mark_inactive"] == []


def test_run_full_preserves_mark_inactive_for_earlier_categories_when_later_walk_crashes(
    patched_db, monkeypatch
):
    """Regression: a later category crashing mid-walk must NOT discard the
    mark_inactive work for categories that already walked successfully.

    Previously mark_inactive ran in a single post-loop block, so any
    exception inside the per-category loop dropped the marking step for
    every category — including the ones that walked cleanly. The fix
    moves mark_inactive into the per-category body so each category's
    marking commits before the next walk starts.
    """
    # CATEGORIES order: (1,2) byt/pronajem, (1,1) byt/prodej,
    # (2,2) dum/pronajem, ... — make the 3rd one raise during iteration.
    def crashing_iter_index(self):
        if (self.category_main, self.category_type) == (2, 2):
            yield {"hash_id": 99999, "price_czk": {"value_raw": 1}}
            raise RuntimeError("simulated outage mid-iteration")
        base = self.category_main * 10000 + self.category_type * 1000
        for i in range(_FakeClient.total_entries):
            yield {
                "hash_id": base + i,
                "price_czk": {"value_raw": 10000 + i},
            }

    monkeypatch.setattr(_FakeClient, "iter_index", crashing_iter_index)

    with pytest.raises(RuntimeError):
        scraper_main._run_full(limit=None, dry_run=False)

    marked = {(cm, ct) for cm, ct, _ in patched_db["mark_inactive"]}
    assert ("byt", "pronajem") in marked
    assert ("byt", "prodej") in marked
    assert ("dum", "pronajem") not in marked
    assert ("dum", "prodej") not in marked
    assert ("komercni", "pronajem") not in marked
    assert ("komercni", "prodej") not in marked
