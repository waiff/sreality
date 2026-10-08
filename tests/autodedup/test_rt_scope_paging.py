"""A scope block larger than one walk's page is read WHOLE, a page a walk (Praha, 2026-10-07).

The entrant walk read at most `ENTER_SLICE` (20,000) rows of a block, ordered by listing id,
and then PRUNED the snapshot to what it had read — so Praha's 157,188 rows would have been
cut to its oldest 20,000 on every walk, the build phase would have ended with the rest never
listed, and the seed would have cut its calibration over them. Reproduced here at a page of
two rows: on the shipped walk the snapshot stays {1, 2} and the phase ends on its first pass.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

import autodedup.incremental_lane as lane
from autodedup.incremental_lane import (
    CURSOR_PAGE,
    SCOPE_SETTING,
    SqlWork,
    run_rt_seed,
)
from autodedup.incremental_scope import Scope, ScopeBlock
from autodedup.incremental_sql import RT_SCOPE_SCAN_SEEN_SQL
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.lane_world import seed

GEN = "rt"
BLOCK = "obec:563510"
TOWN = Scope((ScopeBlock("obec", 563510),))
SCORER: dict[str, str] = {"settings": "default", "model": "prior"}


def _town(db: FakePg, ids: range) -> FakePg:
    for listing_id in ids:
        db.locations[listing_id] = {"obec_kod": 563510, "cast_obce_kod": None,
                                    "resolved_at": db.now - timedelta(days=1)}
    return db


def _work(db: FakePg, **kw: Any) -> SqlWork:
    params: dict[str, Any] = {"straggler_window": 0, "revive_slice": 0, "drift_slice": 0,
                              "evidence_slice": 0, "lag": 0, "enter_slice": 2}
    params.update(kw)
    return SqlWork(db, TOWN, GEN, **params)


def _held(db: FakePg) -> list[int]:
    return sorted(key[2] for key in db.scope_ids if key[:2] == (GEN, BLOCK))


def _page(db: FakePg) -> dict[str, Any]:
    return db.cursors[CURSOR_PAGE + BLOCK]


def test_a_block_larger_than_a_page_is_read_whole_over_several_walks() -> None:
    """The old walk re-read page one every hour and pruned the rest: {1, 2} for ever."""
    db = _town(FakePg(), range(1, 6))
    for snapshot in ([1, 2], [1, 2, 3, 4], [1, 2, 3, 4, 5]):
        _work(db).claim(50)
        assert _held(db) == snapshot
        db.now += timedelta(hours=2)
    assert [row["rows_found"] for row in db.scope_scans] == [2, 2, 1]
    assert _page(db) == {"last_listing_id": 0, "last_snapshot_id": 1}, "the cycle wrapped"


def test_each_page_waits_for_the_cadence_and_counts_against_the_cap() -> None:
    """Outside the build a page is a walk like any other: the per-grain interval spaces them
    and the rolling-day cap counts them, so an eight-page block costs eight walks a cycle."""
    db = _town(FakePg(), range(1, 6))
    _work(db).claim(50)
    _work(db).claim(50)
    assert len(db.scope_scans) == 1, "page two waits for the town's 1 h interval"
    db.now += timedelta(hours=2)
    capped = _work(db, max_enter_scans_per_day=1)
    capped.claim(50)
    assert len(db.scope_scans) == 1 and capped.enter_scan["skipped"] == "daily cap"


def test_a_page_prunes_only_its_own_id_range() -> None:
    """A listing that left the block leaves the snapshot when ITS page is read — and nothing
    the cycle has not reached yet is pruned on the way."""
    db = _town(FakePg(), range(1, 6))
    for _walk in range(3):
        _work(db).claim(50)
        db.now += timedelta(hours=2)
    db.locations[4]["obec_kod"] = 999999
    _work(db).claim(50)                          # page one: (0, 2]
    assert _held(db) == [1, 2, 3, 4, 5]
    db.now += timedelta(hours=2)
    _work(db).claim(50)                          # page two: (2, 5] reads {3, 5}
    assert _held(db) == [1, 2, 3, 5]


def test_the_build_pages_a_block_every_pass_until_it_is_read_whole() -> None:
    """The phase ended on "every block walked once": after page one of a five-row block it
    would have closed with three rows never listed. A block still inside its first cycle is
    paged on every pass of the phase — the cadence waits for nothing — and keeps it open."""
    db = _town(FakePg(), range(1, 6))
    for after, done in ((0, False), (2, False), (4, True)):
        work = _work(db, bootstrap=True)
        claimed = [item.listing_id for item in work.claim(50) if item.feed == "entered"]
        assert work.enter_scan["after_id"] == after
        assert work.bootstrap_done is done
        for listing_id in claimed:
            db.rt_fp[(GEN, listing_id)] = {"is_active": True}
    assert len(db.scope_scans) == 3 and _held(db) == [1, 2, 3, 4, 5]


def test_the_seen_query_spells_the_page_cursor_the_lane_writes() -> None:
    """The reconcile's "fully read" guard reads the cursor in SQL; one spelling, or it is blind."""
    assert f"'{CURSOR_PAGE}' || s.block_key" in RT_SCOPE_SCAN_SEEN_SQL


def test_a_stale_page_cursor_never_resumes_a_new_generation_mid_block() -> None:
    """`scan_cursor` is keyed on the name alone; a reset generation has no scan row, so its
    first walk starts at page one whatever an old cursor row says."""
    db = _town(FakePg(), range(1, 6))
    db.cursors[CURSOR_PAGE + BLOCK] = {"last_listing_id": 3, "last_snapshot_id": 4}
    _work(db).claim(50)
    assert _held(db) == [1, 2]
    assert _page(db) == {"last_listing_id": 2, "last_snapshot_id": 0}


def test_the_seed_reads_every_block_to_its_end_before_it_cuts(tmp_path, monkeypatch) -> None:
    """The calibration is cut over the snapshot: a seed that walked one page would have cut
    it over the block's two OLDEST listings out of nine."""
    monkeypatch.setattr(lane, "ENTER_SLICE", 2)
    conn = FakePg()
    seed(conn)
    out = run_rt_seed(lambda: conn, {**SCORER, SCOPE_SETTING: BLOCK}, tmp_path)
    assert out["calibration"]["scope_listings"] == len(conn.listings) == 9
    assert [page["rows"] for page in out["enter_scan"]["blocks"]] == [2, 2, 2, 2, 1]
    assert out["enter_scan"]["unread"] == []


def test_a_seed_the_daily_cap_cannot_cover_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lane, "ENTER_SLICE", 2)
    monkeypatch.setattr(lane, "MAX_ENTER_SCANS_PER_DAY", 3)
    conn = FakePg()
    seed(conn)
    with pytest.raises(SystemExit, match="unread to its end"):
        run_rt_seed(lambda: conn, {**SCORER, SCOPE_SETTING: BLOCK}, tmp_path)
    assert not conn.calibration and not conn.scope_ids, "the transaction put everything back"
