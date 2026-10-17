"""E940: a scope block larger than one walk's page is read WHOLE, a page a walk.

Before E940 the entrant walk read at most `ENTER_SLICE` (20,000) rows of a block in listing-id
order and pruned the snapshot to them, so a block over the slice kept its OLDEST 20,000 rows for
ever and nothing said so — Praha as a town is 167,548 rows (9 pages), Praha-východ 19,812 and
growing ~105 a day. Reproduced here at a page of two rows: on the old walk a five-row block
stays {1, 2} for ever and the build ends on its first pass.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import autodedup.incremental_lane as lane
from autodedup import apply, rt_equivalence
from autodedup import incremental_sql as S
from autodedup.incremental_lane import (
    PAGE_STALL_PAGES,
    PAGES_SETTING,
    EnterBlock,
    SqlWork,
    pages_key,
    reset_generation,
)
from autodedup.incremental_scope import Scope, ScopeBlock
from tests.autodedup import lane_world
from tests.autodedup.fake_pg import FakePg

GEN = "rt"
TOWN = 563510
BLOCK = f"obec:{TOWN}"
SCOPE = Scope((ScopeBlock("obec", TOWN),))


def _town(db: FakePg, ids: range, obec: int = TOWN) -> FakePg:
    for listing_id in ids:
        db.locations[listing_id] = {"obec_kod": obec, "cast_obce_kod": None,
                                    "resolved_at": db.now - timedelta(days=1)}
    return db


def _work(db: FakePg, scope: Scope = SCOPE, **kw: Any) -> SqlWork:
    params: dict[str, Any] = {"straggler_window": 0, "revive_slice": 0, "drift_slice": 0,
                              "evidence_slice": 0, "lag": 0, "enter_slice": 2}
    params.update(kw)
    return SqlWork(db, scope, GEN, **params)


def _held(db: FakePg, block: str = BLOCK) -> list[int]:
    return sorted(key[2] for key in db.scope_ids if key[:2] == (GEN, block))


def _pages(db: FakePg) -> dict[str, Any]:
    return db.settings.get(pages_key(GEN)) or {}


def _fingerprint(db: FakePg, ids: list[int]) -> None:
    for listing_id in ids:
        db.rt_fp[(GEN, listing_id)] = {"is_active": True}


# ------------------------------------------------------------------ the walk


def test_a_block_of_two_and_a_half_pages_is_listed_whole_over_three_walks() -> None:
    """One page a walk, each walk on the town's 1 h cadence: the old walk re-read page one and
    pruned the rest, so the snapshot stayed {1, 2} for ever."""
    db = _town(FakePg(), range(1, 6))
    seen: list[tuple[list[int], dict[str, Any]]] = []
    for _walk in range(3):
        work = _work(db)
        work.claim(50)
        seen.append((_held(db), dict(_pages(db).get(BLOCK) or {})))
        db.now += timedelta(hours=2)
    assert seen == [([1, 2], {"after": 2, "page": 1, "of": 0}),
                    ([1, 2, 3, 4], {"after": 4, "page": 2, "of": 0}),
                    ([1, 2, 3, 4, 5], {"after": 0, "page": 0, "of": 3})]
    assert [row["rows_found"] for row in db.scope_scans] == [2, 2, 1]
    assert [(walk["page"], walk["of"], walk["end"]) for walk in work.enter_scan["blocks"]
            ] == [(3, 3, True)]


def test_the_feed_claims_page_one_before_page_two_lands() -> None:
    db = _town(FakePg(), range(1, 6))
    first = [item.listing_id for item in _work(db).claim(50) if item.feed == "entered"]
    assert first == [1, 2] and _held(db) == [1, 2], "page one is offered as it lands"
    _fingerprint(db, first)
    db.now += timedelta(hours=2)
    second = [item.listing_id for item in _work(db).claim(50) if item.feed == "entered"]
    assert second == [3, 4]


def test_a_page_prunes_only_the_ids_it_read_and_never_past_them() -> None:
    """A page proves membership for ITS id range only. An advert that left the block in the
    last page's range stays in the snapshot until that last page lands; one in the first
    page's range leaves with the first page; nothing the cycle has not reached is pruned."""
    db = _town(FakePg(), range(1, 6))
    for _walk in range(3):
        _work(db).claim(50)
        db.now += timedelta(hours=2)
    db.locations[1]["obec_kod"] = 999999
    db.locations[5]["obec_kod"] = 999999
    _work(db).claim(50)                                     # page one: (0, 3] reads {2, 3}
    assert _held(db) == [2, 3, 4, 5], "1 left inside the page; 5 is past it and stays"
    db.now += timedelta(hours=2)
    _work(db).claim(50)                                     # page two, short: (3, end] = {4}
    assert _held(db) == [2, 3, 4], "the block's end prunes the rest of the range"
    assert _pages(db) == {BLOCK: {"after": 0, "page": 0, "of": 2}}


def test_a_full_page_is_never_the_end_and_is_recorded_as_a_page() -> None:
    """THE RAIL: a walk that returns exactly the page is a page — the next walk reads past it —
    and the report says so (`pages`, `page_of`), never a silent truncation. A block of exactly
    one page costs one empty walk to prove its end."""
    db = _town(FakePg(), range(1, 3))
    work = _work(db)
    work.claim(50)
    assert work.enter_scan["pages"] == 1 and work.enter_scan["end"] is False
    assert work.enter_scan["page_of"] == {BLOCK: [1, None]}, "of: unknown before an end"
    db.now += timedelta(hours=2)
    work = _work(db)
    work.claim(50)
    assert (work.enter_scan["rows"], work.enter_scan["page"], work.enter_scan["end"]) \
        == (0, 2, True)
    assert work.enter_scan["pages"] == 0 and work.enter_scan["page_of"] == {}
    assert _held(db) == [1, 2] and _pages(db) == {BLOCK: {"after": 0, "page": 0, "of": 2}}


def test_each_page_waits_for_the_cadence_and_counts_against_the_cap() -> None:
    """A page is a walk: the per-grain interval spaces them and the rolling-day cap counts them,
    so paging adds no walk a day — a block of n pages is read whole every n intervals."""
    db = _town(FakePg(), range(1, 6))
    _work(db).claim(50)
    _work(db).claim(50)
    assert len(db.scope_scans) == 1, "page two waits for the town's 1 h interval"
    db.now += timedelta(hours=2)
    _work(db).claim(50)
    assert len(db.scope_scans) == 2


def test_a_pass_the_cap_stops_inside_a_block_says_which_block_it_left() -> None:
    db = _town(FakePg(), range(1, 6))
    _work(db, max_enter_scans_per_day=1).claim(50)
    db.now += timedelta(hours=2)
    capped = _work(db, max_enter_scans_per_day=1)
    capped.claim(50)
    assert len(db.scope_scans) == 1
    assert capped.enter_scan["skipped"] == "daily cap"
    assert capped.enter_scan["unfinished"] == [BLOCK]
    assert capped.enter_scan["page_of"] == {BLOCK: [1, None]}


def test_a_pass_the_cap_stops_before_a_blocks_first_page_names_it_unfinished() -> None:
    """The live store's case: 60 quarters the seed's cap left unwalked. A pass the cap stops
    names every due block it never reached, not only the ones inside a cycle."""
    db = FakePg()
    _town(db, range(1, 3))
    _town(db, range(11, 13), obec=577626)
    scope = Scope((ScopeBlock("obec", TOWN), ScopeBlock("obec", 577626)))
    work = _work(db, scope, enter_slice=10, bootstrap=True, max_enter_scans_per_day=1)
    work.refresh_scope_ids(0)
    assert [walk["block"] for walk in work.enter_scan["blocks"]] == [BLOCK]
    assert work.enter_scan["skipped"] == "daily cap"
    assert work.enter_scan["unfinished"] == ["obec:577626"] and work.enter_scan["page_of"] == {}


def test_a_cursor_that_does_not_advance_shows_as_stalled(monkeypatch, caplog) -> None:
    """The keyset predicate neutralised (every walk re-reads page one): the cycle never ends,
    and after `PAGE_STALL_PAGES` walks the report and the log say so."""
    original = EnterBlock.walk
    monkeypatch.setattr(EnterBlock, "walk",
                        lambda self, limit, after_id=0: original(self, limit, 0))
    db = _town(FakePg(), range(1, 6))
    db.settings[pages_key(GEN)] = {BLOCK: {"after": 2, "page": PAGE_STALL_PAGES - 2, "of": 3}}
    db.scope_scans.append({"generation": GEN, "block_key": BLOCK, "rows_found": 2,
                           "scanned_at": db.now - timedelta(hours=2), "elapsed_ms": 1.0})
    work = _work(db)
    work.claim(50)
    assert work.enter_scan["stalled"] == []
    db.now += timedelta(hours=2)
    work = _work(db)
    with caplog.at_level(logging.WARNING):
        work.claim(50)
    assert work.enter_scan["stalled"] == [BLOCK]
    assert work.enter_scan["page_of"] == {BLOCK: [PAGE_STALL_PAGES, 3]}
    assert "not advancing" in caplog.text


# ------------------------------------------------------------------ read whole, or not yet


def test_a_block_inside_its_first_cycle_is_not_read_whole_for_the_build_or_the_reconcile(
) -> None:
    """The build phase ends on "every block walked once" and the reconcile (and W8) wait on a
    block not fully read: after page one of five rows, both would have read a block whose last
    three adverts were never listed as complete."""
    db = _town(FakePg(), range(1, 6))
    for after, done in ((0, False), (2, False), (4, True)):
        work = _work(db, bootstrap=True)
        claimed = [item.listing_id for item in work.claim(50) if item.feed == "entered"]
        assert work.enter_scan["after_id"] == after, "the phase pages every pass"
        assert (BLOCK in work._walked_ever()) is done
        assert work.bootstrap_done is done
        _fingerprint(db, claimed)
        # Every advert the snapshot holds is fingerprinted: only the page gate says "not yet".
        assert (BLOCK in apply.rt_unread_blocks(db, GEN, [BLOCK])) is not done
    assert _held(db) == [1, 2, 3, 4, 5] and len(db.scope_scans) == 3
    assert apply.rt_unread_blocks(db, GEN, [BLOCK]) == set()


def test_a_read_block_that_grows_past_a_page_stays_read() -> None:
    """A block read whole keeps no entry; when it grows past a page its next full page opens a
    LATER cycle, never its first — Praha-východ at 19,812 and +105 a day would otherwise read as
    unread to the reconcile, W8 and the build's end ~2 days after a seed, and a block hovering at
    20,000 every time it crossed."""
    db = _town(FakePg(), range(1, 3))
    work = _work(db, enter_slice=3)
    work.claim(50)
    _fingerprint(db, [1, 2])
    assert _pages(db) == {} and BLOCK in work._walked_ever()
    _town(db, range(3, 5))
    db.now += timedelta(hours=2)
    work = _work(db, enter_slice=3)
    _fingerprint(db, [item.listing_id for item in work.claim(50) if item.feed == "entered"])
    assert _pages(db) == {BLOCK: {"after": 3, "page": 1, "of": 1}}
    assert BLOCK in work._walked_ever()
    assert apply.rt_unread_blocks(db, GEN, [BLOCK]) == set()
    assert work.enter_scan["page_of"] == {BLOCK: [1, 1]}
    db.now += timedelta(hours=2)
    _work(db, enter_slice=3).claim(50)
    assert _pages(db) == {BLOCK: {"after": 0, "page": 0, "of": 2}} and _held(db) == [1, 2, 3, 4]


def test_a_block_walked_before_e940_with_no_page_row_reads_as_read_whole() -> None:
    """Every live block today is under a page: its scan rows were whole reads, so a block with
    no entry in the page row counts as walked — the deploy reopens no build."""
    db = FakePg()
    db.scope_scans.append({"generation": GEN, "block_key": BLOCK, "rows_found": 8089,
                           "scanned_at": db.now, "elapsed_ms": 1.0})
    assert _work(db)._walked_ever() == {BLOCK}


def test_the_seen_query_spells_the_page_row_the_lane_writes() -> None:
    """The reconcile's "fully read" guard reads the page row in SQL; one spelling, or it is
    blind."""
    assert f"'{PAGES_SETTING}:' || s.generation" in S.RT_SCOPE_SCAN_SEEN_SQL
    assert "->> 'of')::int = 0" in S.RT_SCOPE_SCAN_SEEN_SQL


def test_both_walks_page_by_a_keyset_and_the_prune_by_the_page_range() -> None:
    for sql in (S.RT_SCOPE_BLOCK_SQL, S.RT_SCOPE_OKRES_BLOCK_SQL):
        text = " ".join(sql.split())
        assert "and ll.listing_id > %(after_id)s::bigint order by ll.listing_id limit" in text
        assert "offset" not in text.lower()
    prune = " ".join(S.RT_SCOPE_IDS_PRUNE_SQL.split())
    assert "s.listing_id > %(after_id)s::bigint" in prune
    assert "(%(page_end)s::bigint is null or s.listing_id <= %(page_end)s::bigint)" in prune
    town = EnterBlock("obec", TOWN, TOWN).walk(7, 41)
    okres = EnterBlock("okres", 3209, obce=(1, 2)).walk(7, 41)
    assert town[1] == {"obec": TOWN, "cast_obce": None, "after_id": 41, "limit": 7}
    assert okres[1] == {"okres": 3209, "obce": [1, 2], "after_id": 41, "limit": 7}


def test_the_reset_empties_the_page_row_with_its_generation() -> None:
    db = FakePg()
    db.settings[pages_key(GEN)] = {BLOCK: {"after": 2, "page": 1, "of": 0}}
    reset_generation(db, GEN, holder="test")
    assert pages_key(GEN) not in db.settings


# ------------------------------------------------------------------ the seed


def test_the_seed_lists_a_block_over_the_page_whole_and_cuts_over_all_of_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The calibration, A10's re-cut refusal and E941's second storage check are all cut over
    what the seed's walk listed: a seed that walked one page would have cut them over the
    block's two OLDEST adverts of nine."""
    monkeypatch.setattr(lane, "ENTER_SLICE", 2)
    db = lane_world.world()
    out = lane_world.seed_lane(db, tmp_path)
    assert out["calibration"]["scope_listings"] == len(db.listings) == 9
    walks = out["enter_scan"]["blocks"]
    assert [walk["rows"] for walk in walks] == [2, 2, 2, 2, 1]
    assert [walk["page"] for walk in walks] == [1, 2, 3, 4, 5] and walks[-1]["end"] is True
    assert out["enter_scan"]["page_of"] == {}, "nothing left mid-cycle"
    assert _pages(db) == {BLOCK: {"after": 0, "page": 0, "of": 5}}
    assert sorted(lid for _g, block, lid in db.scope_ids if block == BLOCK) == sorted(db.listings)
    assert out["seed_memory"] == {"scope_listings": 9, "cut_peak_mb": 0.5,
                                  "seed_memory_mb": lane.SEED_MEMORY_MB}
    assert out["peak_rss_mb"] is None or out["peak_rss_mb"] > 0, "the process's own peak"


def test_a_seed_whose_cut_would_pass_the_runners_memory_refuses_before_the_cut(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cut holds ~56 kB an advert at its peak (E940's review, the trial cohort): half the
    16 GiB runner is ~149,800 adverts, and the 323,128 of the Prague + kraj re-seed would peak
    at ~17.3 GiB — an OOM kill ~40-60 min in that strands the lease for five hours. Refused
    right after the walk, inside the transaction: nothing written, the cut never started."""
    assert int(lane.SEED_MEMORY_MB * 1_048_576 // lane.CUT_PEAK_BYTES_PER_LISTING) == 149_796
    with pytest.raises(SystemExit, match="SEED_MEMORY_MB 8,192"):
        lane.seed_memory(323_128)
    monkeypatch.setattr(lane, "CUT_PEAK_BYTES_PER_LISTING", 1 << 30)
    db = lane_world.world()
    with pytest.raises(SystemExit, match=r"the 9 adverts the walk listed .*\(E962\)"):
        lane_world.seed_lane(db, tmp_path)
    assert not db.calibration and not db.phash_pop and not db.scope_ids
    assert S.RT_CALIBRATION_WRITE_SQL not in db.statements, "the cut never started"
    assert db.lease[lane.LANE_NAME]["expires_at"] <= db.now, "the lease went back"


def test_the_seed_walks_every_block_whatever_the_daily_cap_and_the_passes_keep_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A seed is a one-off listing; the cap governs passes. Here the rolling day is already
    spent (60 walks): the seed still pages its block to its end, and the next pass that finds
    the block due is the one the cap stops."""
    monkeypatch.setattr(lane, "ENTER_SLICE", 2)
    db = lane_world.world()
    for _ in range(lane.MAX_ENTER_SCANS_PER_DAY):
        db.scope_scans.append({"generation": GEN, "block_key": "obec:1", "rows_found": 1,
                               "scanned_at": db.now - timedelta(hours=1), "elapsed_ms": 1.0})
    out = lane_world.seed_lane(db, tmp_path)
    assert "skipped" not in out["enter_scan"]
    assert len(out["enter_scan"]["blocks"]) == 5
    assert out["calibration"]["scope_listings"] == 9
    db.now += timedelta(hours=2)
    work = _work(db, bootstrap=True)
    work.refresh_scope_ids(0)
    assert work.enter_scan["skipped"] == "daily cap"


def test_the_seed_walks_every_block_of_a_scope_larger_than_the_cap() -> None:
    db = FakePg()
    towns = (563510, 577626, 531057)
    for index, obec in enumerate(towns):
        _town(db, range(10 * index + 1, 10 * index + 4), obec=obec)
    scope = Scope(tuple(ScopeBlock("obec", obec) for obec in towns))
    work = _work(db, scope, bootstrap=True, max_enter_scans_per_day=1)
    work.refresh_scope_ids(0, seed=True)
    assert [walk["block"] for walk in work.enter_scan["blocks"]] == [
        "obec:531057", "obec:531057", "obec:563510", "obec:563510", "obec:577626",
        "obec:577626"]
    assert "skipped" not in work.enter_scan
    assert {key[2] for key in db.scope_ids} == {1, 2, 3, 11, 12, 13, 21, 22, 23}


# ------------------------------------------------------------------ rt_equivalence


def test_the_equivalence_fallback_pages_a_block_as_the_sweep_does(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt_equivalence, "ENTER_SLICE", 2)
    db = _town(FakePg(), range(1, 6))
    ids, _resolved, source = rt_equivalence.scope_listings(db, GEN, SCOPE)
    assert (ids, source) == ({1, 2, 3, 4, 5}, "listing_location")
    assert db.statements.count(S.RT_SCOPE_BLOCK_SQL) == 3, "three pages, one statement each"
