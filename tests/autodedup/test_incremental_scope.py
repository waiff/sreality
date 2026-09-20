"""E74 — the real-time lane's SCOPE and its storage budget, exercised.

W9 built a lane whose four feeds claimed the arrivals of the WHOLE corpus: ~178,000 new
listings in 30 days, a fingerprint, 17.3 postings and a pair fan-out for each, into a store the
operator pays for by the megabyte. Everything below is a rail on the two rules that close it —
a pass holds only what `rt_scope` holds, and no pass writes anything while the schema is over
`rt_max_schema_mb` — plus the two things scoping OWES: a feed has to cross a corpus that is
99.4% out of scope without stalling, and a listing that LEAVES the scope has to be retired
rather than left half-indexed.
"""

from __future__ import annotations

import json
from dataclasses import replace as dc_replace
from datetime import timedelta
from pathlib import Path

import pytest

from autodedup import cohort
from autodedup.incremental import Limits, WorkItem, run_pass
from autodedup.incremental_lane import (
    BUDGET_SETTING,
    CURSOR_CHANGED,
    CURSOR_FLIPPED,
    CURSOR_NEW,
    CURSOR_SCOPE,
    ENV_FLAG,
    SCOPE_SETTING,
    STORAGE_WATERMARK,
    SqlStore,
    SqlWork,
    run_incremental,
    storage_guard,
)
from autodedup.incremental_scope import (
    CORPUS_PROJECTION_MB,
    DEFAULT_SCOPE,
    Scope,
    ScopeBlock,
    ScopeError,
    parse_scope,
    resolve_scope,
)
from autodedup.incremental_store import MemoryStore
from autodedup.model import hand_initialised
from autodedup.replay import DatasetFacts, arrival_order
from autodedup.score_lane import storable
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_incremental import _calibration, _dataset, _drain, _settings

GEN = "rt"
SCOPE = Scope((ScopeBlock("obec", 563510), ScopeBlock("cast_obce", 490245)))


def _place(db: FakePg, listing_id: int, *, obec: int | None = None,
           cast_obce: int | None = None) -> None:
    db.locations[int(listing_id)] = {"obec_kod": obec, "cast_obce_kod": cast_obce}


def _fp_row(is_active: bool = True) -> dict:
    return {"category_main": None, "category_type": None, "area_m2": None,
            "disposition": None, "floor": None, "fp_digest": "d",
            "cell_key": "o1", "cell_group": "byt", "is_active": is_active}


# ------------------------------------------------------------------- the scope as data


def test_the_default_scope_is_the_trial_blocks_and_never_the_negative_control() -> None:
    """The negative control is a RULE over Praha address groups evaluated at export time. It
    has no arrival feed, so nothing in the corpus could tell the lane a new listing belongs to
    it — it is out of every real-time scope by construction, not by omission."""
    assert [(b.grain, b.code) for b in DEFAULT_SCOPE.blocks] == [
        ("obec", 563510), ("obec", 577626), ("cast_obce", 490245)]
    assert not DEFAULT_SCOPE.whole_corpus
    negctl = cohort.block_by_key("negctl")
    assert negctl.grain == "assembled"
    assert all(block.code != negctl.code for block in DEFAULT_SCOPE.blocks)


def test_a_scope_reads_from_a_token_string_a_json_list_or_all() -> None:
    assert parse_scope("obec:563510 cast_obce:490245").blocks == SCOPE.blocks
    assert parse_scope([{"grain": "obec", "code": 563510},
                        {"grain": "cast_obce", "code": "490245"}]).blocks == SCOPE.blocks
    assert parse_scope("all").whole_corpus is True
    assert parse_scope("obec:1 obec:1").blocks == (ScopeBlock("obec", 1),)
    with pytest.raises(ScopeError):
        parse_scope("okres:1234")
    with pytest.raises(ScopeError):
        parse_scope("obec:not-a-number")


def test_an_empty_or_missing_scope_is_a_hard_error_never_the_whole_corpus() -> None:
    for empty in ("", "   ", [], {}):
        with pytest.raises(ScopeError):
            parse_scope(empty)
    with pytest.raises(ScopeError):
        parse_scope(None)
    # A PRESENT but empty setting is the dangerous one: it must not fall back to the default.
    with pytest.raises(ScopeError):
        resolve_scope(None, [])
    with pytest.raises(ScopeError):
        resolve_scope("", [])
    assert resolve_scope(None, None) == DEFAULT_SCOPE
    assert resolve_scope("obec:563510 cast_obce:490245", None).blocks == SCOPE.blocks
    # The argument wins over the row, and both win over the default.
    assert resolve_scope("all", [{"grain": "obec", "code": 1}]).whole_corpus is True


def test_the_lane_refuses_an_empty_scope_setting(tmp_path, monkeypatch) -> None:
    conn = FakePg()
    conn.settings[SCOPE_SETTING] = []
    monkeypatch.setenv(ENV_FLAG, "true")
    with pytest.raises(SystemExit) as raised:
        run_incremental(lambda: conn, {}, tmp_path)
    assert SCOPE_SETTING in str(raised.value)
    assert not conn.cursors and not conn.rt_fp


# ------------------------------------------------------------------ the storage budget


def test_the_guard_refuses_a_pass_when_the_schema_is_over_budget(tmp_path, monkeypatch):
    conn = FakePg()
    conn.schema_bytes = 500 * 1_048_576
    conn.settings[BUDGET_SETTING] = 400
    monkeypatch.setenv(ENV_FLAG, "true")
    with pytest.raises(SystemExit) as raised:
        run_incremental(lambda: conn, {}, tmp_path)
    assert "over the 400 MB" in str(raised.value)
    # Loud, non-zero, and nothing moved: no lease taken, no cursor written, no row stored.
    assert not conn.lease and not conn.cursors and not conn.rt_fp and not conn.fp_key


def test_the_guard_reports_size_rows_and_growth_since_the_last_pass() -> None:
    conn = FakePg()
    conn.schema_bytes = 100 * 1_048_576
    conn.settings[STORAGE_WATERMARK] = {"bytes": 90 * 1_048_576}
    conn.rt_fp[(GEN, 1)] = _fp_row()
    conn.fp_key.add((GEN, "img", "t1", 1))
    report = storage_guard(conn, GEN, SCOPE, 400)
    assert report["schema_mb"] == 100.0
    assert report["growth_mb"] == 10.0
    assert report["rows"]["rt_fp"] == 1 and report["rows"]["fp_key"] == 1
    assert report["scope"] == SCOPE.as_json()


def test_whole_corpus_needs_the_budget_to_agree_as_well_as_the_setting() -> None:
    conn = FakePg()
    conn.schema_bytes = 10 * 1_048_576
    every = Scope((), whole_corpus=True)
    with pytest.raises(Exception) as raised:
        storage_guard(conn, GEN, every, 400)
    assert str(CORPUS_PROJECTION_MB) in str(raised.value)
    # With a budget that could hold it, `all` is allowed — the two halves are ONE decision.
    assert storage_guard(conn, GEN, every, CORPUS_PROJECTION_MB)["schema_mb"] == 10.0


# ------------------------------------------------------------------------- every feed


def _feeds_db() -> FakePg:
    """One in-scope and one out-of-scope listing on each of the four public feeds."""
    db = FakePg()
    old = db.now - timedelta(hours=2)
    for listing_id, obec in ((21, 563510), (22, 999999)):          # changed
        db.listings[listing_id] = {"first_seen_at": db.now - timedelta(days=9),
                                   "inactive_at": None, "is_active": True}
        _place(db, listing_id, obec=obec)
        db.rt_fp[(GEN, listing_id)] = _fp_row()
        db.snapshots.append({"id": 100 + listing_id, "listing_id": listing_id,
                             "scraped_at": old})
    for listing_id, obec in ((31, 563510), (32, 999999)):          # flipped
        db.listings[listing_id] = {"first_seen_at": db.now - timedelta(days=9),
                                   "inactive_at": old, "is_active": False}
        _place(db, listing_id, obec=obec)
        db.rt_fp[(GEN, listing_id)] = _fp_row()
    for listing_id, obec in ((41, 563510), (42, 999999)):          # revived
        db.listings[listing_id] = {"first_seen_at": db.now - timedelta(days=9),
                                   "inactive_at": None, "is_active": True}
        _place(db, listing_id, obec=obec)
        db.rt_fp[(GEN, listing_id)] = _fp_row(is_active=False)
    db.cursors[CURSOR_NEW] = {"last_listing_id": 50}
    for listing_id, obec in ((111, 563510), (112, 999999)):        # new
        db.listings[listing_id] = {"first_seen_at": old, "inactive_at": None,
                                   "is_active": True}
        _place(db, listing_id, obec=obec)
    return db


def test_every_feed_claims_only_what_the_scope_holds() -> None:
    db = _feeds_db()
    # The straggler feed too: an id behind the `new` cursor with no fingerprint row.
    db.listings[9] = {"first_seen_at": db.now - timedelta(hours=2), "inactive_at": None,
                      "is_active": True}
    db.listings[8] = {"first_seen_at": db.now - timedelta(hours=2), "inactive_at": None,
                      "is_active": True}
    _place(db, 9, obec=563510)
    _place(db, 8, obec=999999)

    items = SqlWork(db, SCOPE, GEN).claim(50)
    by_feed = {feed: sorted(item.listing_id for item in items if item.feed == feed)
               for feed in {item.feed for item in items}}
    assert by_feed["straggler"] == [9]
    assert by_feed["new"] == [111]
    assert by_feed["changed"] == [21]
    assert by_feed["flipped"] == [31]
    assert by_feed["revived"] == [41]
    # The only feed that may NAME an out-of-scope listing is the drift sweep, and it names it
    # to retire it (22/32/42 are in this generation's store and have left the scope).
    assert all(item.listing_id not in (8, 112) for item in items)
    assert all(item.retire for item in items
               if item.listing_id in (22, 32, 42))
    assert sorted(item.listing_id for item in items if item.retire) == [22, 32, 42]


def test_a_quarter_code_is_a_scope_of_its_own() -> None:
    """`cast_obce_kod` carries no index of its own, which is exactly why the predicate is
    resolved per window id rather than scanned — the grain still has to WORK."""
    db = FakePg()
    db.listings[71] = {"first_seen_at": db.now - timedelta(hours=2), "inactive_at": None,
                       "is_active": True}
    db.listings[72] = {"first_seen_at": db.now - timedelta(hours=2), "inactive_at": None,
                       "is_active": True}
    _place(db, 71, obec=554782, cast_obce=490245)
    _place(db, 72, obec=554782, cast_obce=490246)
    items = SqlWork(db, SCOPE, GEN).claim(50)
    assert [item.listing_id for item in items if item.feed == "new"] == [71]


def test_a_window_with_nothing_in_scope_still_advances_its_cursor() -> None:
    """The progress rail. The scope holds 0.6% of the corpus, so a feed that advanced only
    over survivors would need as many passes as the corpus has arrivals to reach the handful
    that matter — and would never finish."""
    db = FakePg()
    old = db.now - timedelta(hours=2)
    for listing_id in range(200, 260):
        db.listings[listing_id] = {"first_seen_at": old, "inactive_at": None,
                                   "is_active": True}
        _place(db, listing_id, obec=999999)
        db.snapshots.append({"id": listing_id, "listing_id": listing_id, "scraped_at": old})
    work = SqlWork(db, SCOPE, GEN, window=25, revive_slice=0, drift_slice=0)
    items = work.claim(50)
    assert items == []
    cursors = work.commit(items)
    assert cursors[CURSOR_NEW] == 224 and cursors[CURSOR_CHANGED] == 224
    # And it keeps going: the next pass crosses the next window rather than the same one.
    work.claim(50)
    assert work.commit([])[CURSOR_NEW] == 249


def test_a_window_with_more_in_scope_than_the_share_stops_at_what_it_took() -> None:
    db = FakePg()
    old = db.now - timedelta(hours=2)
    for listing_id in range(300, 310):
        db.listings[listing_id] = {"first_seen_at": old, "inactive_at": None,
                                   "is_active": True}
        _place(db, listing_id, obec=563510)
    work = SqlWork(db, SCOPE, GEN, window=25, revive_slice=0, drift_slice=0)
    items = work.claim(10)  # five feeds, so a share of two
    assert [item.listing_id for item in items] == [300, 301]
    assert work.commit(items)[CURSOR_NEW] == 301, "the window end would skip 302-309"


def test_a_refused_pass_advances_no_cursor_even_across_an_empty_window() -> None:
    db = _feeds_db()
    work = SqlWork(db, SCOPE, GEN)
    work.claim(50)
    work.commit([])  # E70: the pair budget refused this claim
    assert db.cursors == {CURSOR_NEW: {"last_listing_id": 50}}, "the fixture's own row only"


# --------------------------------------------------------------- leaving the scope


def test_the_drift_sweep_claims_a_listing_the_scope_stopped_holding() -> None:
    db = FakePg()
    db.rt_fp[(GEN, 51)] = _fp_row()
    db.rt_fp[(GEN, 52)] = _fp_row()
    _place(db, 51, obec=563510)
    _place(db, 52, obec=999999)      # the geocoder moved it out
    items = SqlWork(db, SCOPE, GEN).claim(50)
    drifted = [item for item in items if item.feed == "drifted"]
    assert [item.listing_id for item in drifted] == [52]
    assert all(item.retire for item in drifted)
    assert SqlWork(db, SCOPE, GEN).claim(50) and db.cursors.get(CURSOR_SCOPE) is None


def test_a_listing_that_leaves_the_scope_is_retired_not_left_half_indexed() -> None:
    """Half-indexed is worse than unindexed: the postings stay, so every neighbour goes on
    retrieving a listing nothing maintains, and the component keeps an edge to it."""
    ds = _dataset(12)
    settings = _settings()
    calibration = _calibration(ds, settings)
    store, _passes = _drain(ds, settings, calibration, arrival_order(ds))
    victim = sorted(ds.listings)[0]
    assert store.keys.get(victim) and store.known([victim]) == {victim}
    before = [key for key in store.pairs if victim in key]
    assert before, "the fixture must give the retired listing at least one pair"

    class _Retire:
        def claim(self, limit: int) -> list[WorkItem]:
            return [WorkItem(victim, "drifted", None, None, retire=True)]

        def commit(self, done) -> dict:
            return {}

    result = run_pass(store, DatasetFacts(ds), _Retire(), settings, hand_initialised(),
                      calibration, limits=Limits(max_listings=50, max_pairs=10 ** 9))
    assert result.retired == 1
    assert not store.keys.get(victim), "postings survived the retirement"
    assert store.known([victim]) == set()
    assert [key for key in store.pairs if victim in key] == []
    assert all(victim not in members for members in store.clusters.values())


def test_a_retired_listing_is_never_also_refreshed() -> None:
    ds = _dataset(6)
    settings = _settings()
    calibration = _calibration(ds, settings)
    store = MemoryStore()
    victim = sorted(ds.listings)[0]

    class _Both:
        def claim(self, limit: int) -> list[WorkItem]:
            return [WorkItem(victim, "new", None, None),
                    WorkItem(victim, "drifted", None, None, retire=True)]

        def commit(self, done) -> dict:
            return {}

    result = run_pass(store, DatasetFacts(ds), _Both(), settings, hand_initialised(),
                      calibration, limits=Limits(max_listings=50, max_pairs=10 ** 9))
    assert result.claimed == []
    assert store.known([victim]) == set()


# ------------------------------------------------------------------ the retention rule


def test_the_store_keeps_exactly_what_the_batch_lane_keeps() -> None:
    """`score_lane.persist` writes `storable(row, store_floor)` — the whole merge and band
    zones, plus the reject tail at or above the floor. The real-time store mirrors it, so the
    two generations hold the same rows and the store grows with duplicates, not comparisons."""
    from autodedup.incremental import PairRow

    conn = FakePg()
    store = SqlStore(conn, GEN, store_floor=0.02)

    def _row(lo: int, hi: int, zone: str, score: float) -> PairRow:
        return PairRow(lo=lo, hi=hi, probes=["img"], from_lo=True, from_hi=False,
                       zone=zone, score=score, families=["IMG"], certificate=None,
                       veto="", reason="r", evidence={}, context={"block": "o1"},
                       fp_lo="a", fp_hi="b")

    rows = [_row(1, 2, "merge", 0.99), _row(3, 4, "band", 0.4),
            _row(5, 6, "reject", 0.5), _row(7, 8, "reject", 0.001),
            _row(9, 10, "veto", 0.0001)]
    store.upsert_pairs(rows)
    store.flush()
    kept = {(lo, hi) for _gen, lo, hi in conn.pairs}
    assert kept == {(1, 2), (3, 4), (5, 6)}
    assert store.pairs_retained == 3 and store.pairs_evicted == 2
    for row in rows:
        assert (storable({"zone": row.zone, "score": row.score}, 0.02)
                == ((row.lo, row.hi) in kept)), "the two lanes disagree about a row"


def test_a_pair_that_falls_below_the_floor_on_a_re_score_is_deleted() -> None:
    from autodedup.incremental import PairRow

    conn = FakePg()
    store = SqlStore(conn, GEN, store_floor=0.02)
    kept = PairRow(lo=1, hi=2, probes=["img"], from_lo=True, from_hi=True, zone="band",
                   score=0.4, families=["IMG"], certificate=None, veto="", reason="r",
                   evidence={}, context={"block": "o1"}, fp_lo="a", fp_hi="b")
    store.upsert_pairs([kept])
    store.flush()
    assert (GEN, 1, 2) in conn.pairs
    store.upsert_pairs([dc_replace(kept, zone="reject", score=0.001)])
    store.flush()
    assert (GEN, 1, 2) not in conn.pairs, "a demoted pair must not linger in the store"


# ------------------------------------------------------------ the summary the operator reads


def test_the_pass_summary_carries_the_scope_the_budget_and_the_growth(tmp_path, monkeypatch):
    conn = FakePg()
    conn.schema_bytes = 64 * 1_048_576
    conn.calibration[GEN] = {"digest": "d", "n_listings": 3, "payload": {},
                             "artifact_url": None, "settings": {}, "model_version": "m"}
    monkeypatch.setenv(ENV_FLAG, "true")
    out = run_incremental(lambda: conn, {SCOPE_SETTING: "obec:563510"}, tmp_path)
    assert out["scope"] == [{"grain": "obec", "code": 563510}]
    assert out["storage"]["schema_mb"] == 64.0
    assert out["storage"]["max_schema_mb"] == 400.0
    assert "pass_growth_mb" in out["storage"] and "rows" in out["storage"]
    assert out["retention"]["store_floor"] > 0
    # The growth watermark is written, so the NEXT pass can report growth against this one.
    assert conn.settings[STORAGE_WATERMARK]["bytes"] == conn.schema_bytes
    assert json.loads(Path(tmp_path, "incremental.json").read_text())["scope"]
