"""E79 — the real-time lane's SCOPE and its storage budget, exercised.

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
from typing import Any

import pytest

from autodedup import cohort
from autodedup.incremental import Limits, WorkItem, run_pass
from autodedup.incremental_lane import (
    BUDGET_SETTING,
    CURSOR_CHANGED,
    CURSOR_ENTER,
    CURSOR_FLIPPED,
    CURSOR_NEW,
    CURSOR_SCOPE,
    ENV_FLAG,
    SCOPE_SETTING,
    STORAGE_WATERMARK,
    RetireRefusal,
    SqlStore,
    SqlWork,
    parity_baseline_key,
    resolve_scope_parents,
    run_incremental,
    scope_setting_key,
    storage_guard,
)
from autodedup.incremental_sql import (
    RT_IDLE_GUARD_SQL,
    RT_LOCK_GUARD_SQL,
    RT_STATEMENT_GUARD_SQL,
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


def _baseline(db: FakePg, generation: str = GEN) -> None:
    """The parity gate (E84) refuses a generation with no fact baseline. A fixture that
    hand-writes the calibration row hand-writes the baseline too — an empty one, because its
    `public` holds no cohort listing to compare against. What the gate is FOR is proved in
    `test_incremental_sqlstore.py` and `test_parity.py`."""
    db.settings[parity_baseline_key(generation)] = {
        "rows": {}, "exported_at": db.now.isoformat(), "n": 0}

def _calibrated(db: FakePg, generation: str = GEN) -> FakePg:
    """A SEEDED generation. A pass over an unseeded one is a green skip (W9e/R1), so every
    test about what a pass REFUSES has to seed it first."""
    db.calibration[generation] = {"digest": "d", "n_listings": 3, "payload": {},
                                  "artifact_url": None, "settings": {},
                                  "model_version": "hand_v1"}
    _baseline(db, generation)
    return db


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
    # SEEDED (W9e/R1): an UNseeded generation is a green skip, and only a seeded one can have a
    # scope row that is wrong.
    conn = _calibrated(FakePg())
    conn.settings[SCOPE_SETTING] = []
    monkeypatch.setenv(ENV_FLAG, "true")
    with pytest.raises(SystemExit) as raised:
        run_incremental(lambda: conn, {}, tmp_path)
    assert SCOPE_SETTING in str(raised.value)
    assert not conn.cursors and not conn.rt_fp


# ------------------------------------------------------------------ the storage budget


def test_the_guard_refuses_a_pass_when_the_schema_is_over_budget(tmp_path, monkeypatch):
    conn = _calibrated(FakePg())
    conn.schema_bytes = 500 * 1_048_576
    conn.settings[SCOPE_SETTING] = [{"grain": "obec", "code": 563510}]
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

    # Half this fixture's store is out of scope on purpose, so W9d-1's rail is opened for it:
    # what is under test here is WHICH feed may name an out-of-scope listing.
    items = SqlWork(db, SCOPE, GEN, max_retire_fraction=1.0).claim(50)
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
    work = SqlWork(db, SCOPE, GEN, window=25, revive_slice=0, drift_slice=0, enter_slice=0)
    items = work.claim(10)  # five feeds, so a share of two
    assert [item.listing_id for item in items] == [300, 301]
    assert work.commit(items)[CURSOR_NEW] == 301, "the window end would skip 302-309"


def test_a_refused_pass_advances_no_cursor_even_across_an_empty_window() -> None:
    db = _feeds_db()
    work = SqlWork(db, SCOPE, GEN, max_retire_fraction=1.0)
    work.claim(50)
    work.commit([])  # E75: the pair budget refused this claim
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
                             "artifact_url": None, "settings": {},
                             "model_version": "hand_v1"}
    _baseline(conn, GEN)
    conn.settings[SCOPE_SETTING] = [{"grain": "obec", "code": 563510}]
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


# ------------------------------------------------- W9d: the four defects the verification found
#
# W9d-1 a separator-only scope parsed to an EMPTY, non-whole-corpus scope and the drift sweep
# then reported every row of the store as departed; W9d-2 a dispatch-arg scope retired everything
# the seeded scope holds without ever being persisted; W9d-3 nothing claimed a listing whose
# location resolved INTO the scope after the cursor had passed it, and the straggler look-back
# was an id RANGE rather than a row count; W9d-4 the session guards were set outside the pass's
# transaction on a transaction-pooled connection. (They are the VERIFICATION's numbering: the
# program's own D1/D4 are the cohort-block and shadow-posture rulings.)


def _empty_scope() -> Scope:
    """An empty scope the constructor now refuses, built around it — the drift sweep's own
    refusal has to stand on its own feet (W9d-1)."""
    scope = object.__new__(Scope)
    object.__setattr__(scope, "blocks", ())
    object.__setattr__(scope, "whole_corpus", False)
    return scope


def test_a_separator_only_scope_is_a_hard_error_not_a_null_scope() -> None:
    for raw in (",", " , , ", ",,", ";", " ; "):
        with pytest.raises(ScopeError):
            parse_scope(raw)
    with pytest.raises(ScopeError):
        Scope(())
    with pytest.raises(ScopeError):
        parse_scope([[]])


def test_the_drift_sweep_refuses_to_retire_more_than_the_safety_fraction() -> None:
    """The W9d-1 scenario end to end: a scope that holds nothing would report the WHOLE store as
    departed. The sweep refuses rather than retiring it, and no cursor moves."""
    db = FakePg()
    for listing_id in range(1, 101):
        db.rt_fp[(GEN, listing_id)] = _fp_row()
        _place(db, listing_id, obec=999999)
    work = SqlWork(db, SCOPE, GEN)
    with pytest.raises(RetireRefusal) as raised:
        work.claim(50)
    assert "retire" in str(raised.value).lower()
    assert not db.cursors and len(db.rt_fp) == 100


def test_the_drift_sweep_refuses_outright_on_an_empty_scope() -> None:
    db = FakePg()
    db.rt_fp[(GEN, 1)] = _fp_row()
    _place(db, 1, obec=563510)
    with pytest.raises(RetireRefusal):
        SqlWork(db, _empty_scope(), GEN).claim(50)


def test_a_handful_of_departures_is_still_retired() -> None:
    """The guard is a rail against a store-wide wipe, not against the drift it exists for."""
    db = FakePg()
    for listing_id in range(1, 101):
        db.rt_fp[(GEN, listing_id)] = _fp_row()
        _place(db, listing_id, obec=563510)
    _place(db, 7, obec=999999)
    items = SqlWork(db, SCOPE, GEN).claim(50)
    assert [item.listing_id for item in items if item.retire] == [7]


def test_the_lane_stops_loudly_when_the_drift_sweep_refuses(tmp_path, monkeypatch) -> None:
    conn = FakePg()
    conn.calibration[GEN] = {"digest": "d", "n_listings": 3, "payload": {},
                             "artifact_url": None, "settings": {},
                             "model_version": "hand_v1"}
    _baseline(conn, GEN)
    conn.settings[SCOPE_SETTING] = [{"grain": "obec", "code": 563510}]
    for listing_id in range(1, 101):
        conn.rt_fp[(GEN, listing_id)] = _fp_row()
        _place(conn, listing_id, obec=999999)
    monkeypatch.setenv(ENV_FLAG, "true")
    with pytest.raises(SystemExit) as raised:
        run_incremental(lambda: conn, {"generation": GEN}, tmp_path)
    assert "retire" in str(raised.value).lower()
    assert not conn.cursors and len(conn.rt_fp) == 100


# -------------------------------------------------------- W9d-2: the seeded scope is the truth


def _seeded(scope_json: Any) -> FakePg:
    conn = FakePg()
    conn.calibration[GEN] = {"digest": "d", "n_listings": 3, "payload": {},
                             "artifact_url": None, "settings": {},
                             "model_version": "hand_v1"}
    _baseline(conn, GEN)
    conn.settings[SCOPE_SETTING] = scope_json
    return conn


def test_a_dispatch_scope_that_differs_from_the_seeded_one_is_refused(tmp_path, monkeypatch):
    conn = _seeded([{"grain": "obec", "code": 563510}, {"grain": "obec", "code": 577626}])
    monkeypatch.setenv(ENV_FLAG, "true")
    with pytest.raises(SystemExit) as raised:
        run_incremental(lambda: conn, {"generation": GEN, SCOPE_SETTING: "obec:563510"},
                        tmp_path)
    assert "rt_rescope" in str(raised.value)
    assert conn.settings[SCOPE_SETTING] == [{"grain": "obec", "code": 563510},
                                            {"grain": "obec", "code": 577626}]
    assert not conn.lease and not conn.cursors


def test_the_same_scope_spelled_differently_is_not_a_rescope(tmp_path, monkeypatch) -> None:
    conn = _seeded([{"grain": "obec", "code": 577626}, {"grain": "obec", "code": 563510}])
    monkeypatch.setenv(ENV_FLAG, "true")
    out = run_incremental(lambda: conn, {"generation": GEN,
                                         SCOPE_SETTING: "obec:563510 obec:577626"}, tmp_path)
    assert out.get("rescoped") is False


def test_rt_rescope_persists_the_new_scope_and_requeues_the_entrants(tmp_path, monkeypatch):
    conn = _seeded([{"grain": "obec", "code": 563510}, {"grain": "obec", "code": 577626}])
    conn.cursors[CURSOR_ENTER] = {"last_listing_id": 4242, "last_snapshot_id": 1}
    monkeypatch.setenv(ENV_FLAG, "true")
    out = run_incremental(lambda: conn, {"generation": GEN, SCOPE_SETTING: "obec:563510",
                                         "rt_rescope": "true"}, tmp_path)
    assert out["rescoped"] is True
    # The row the rescope persists is the GENERATION's (W9e/R4), never the legacy global one.
    assert conn.settings[scope_setting_key(GEN)] == [{"grain": "obec", "code": 563510}]
    # The entrant sweep restarts, so everything the new scope holds is re-claimed.
    assert conn.cursors[CURSOR_ENTER]["last_listing_id"] == 0


def test_a_seeded_generation_with_no_scope_row_is_a_hard_error(tmp_path, monkeypatch) -> None:
    """Never a silent fall back to the default: the row IS the generation's scope."""
    conn = FakePg()
    conn.calibration[GEN] = {"digest": "d", "n_listings": 3, "payload": {},
                             "artifact_url": None, "settings": {},
                             "model_version": "hand_v1"}
    _baseline(conn, GEN)
    monkeypatch.setenv(ENV_FLAG, "true")
    with pytest.raises(SystemExit) as raised:
        run_incremental(lambda: conn, {"generation": GEN}, tmp_path)
    assert SCOPE_SETTING in str(raised.value)
    assert not conn.cursors and not conn.rt_fp


# ----------------------------------------------------------- W9d-3: the drift-IN feed


def test_the_straggler_sweep_looks_back_a_row_count_not_an_id_range() -> None:
    """Ids are sparse (21.7 id units a row on the live corpus), so an id RANGE of 5,000 is a
    few hundred rows, not 5,000."""
    db = FakePg()
    for listing_id in (1_000, 40_000, 80_000):
        db.listings[listing_id] = {"first_seen_at": db.now - timedelta(hours=2),
                                   "inactive_at": None, "is_active": True}
        _place(db, listing_id, obec=563510)
    db.cursors[CURSOR_NEW] = {"last_listing_id": 100_000}
    work = SqlWork(db, SCOPE, GEN, straggler_window=2, revive_slice=0, drift_slice=0,
                   enter_slice=0)
    stragglers = [item.listing_id for item in work.claim(50) if item.feed == "straggler"]
    assert stragglers == [40_000, 80_000]


def test_a_listing_whose_location_resolves_into_the_scope_later_is_claimed() -> None:
    """`listing_location` is written asynchronously, so an in-scope listing can carry no obec
    when its window passes. The sixth feed is the only thing that ever claims it."""
    db = FakePg()
    db.listings[10] = {"first_seen_at": db.now - timedelta(days=9), "inactive_at": None,
                       "is_active": True}
    _place(db, 10, obec=563510)                       # resolved AFTER the cursor passed it
    db.cursors[CURSOR_NEW] = {"last_listing_id": 5_000_000}
    work = SqlWork(db, SCOPE, GEN, straggler_window=0, revive_slice=0, drift_slice=0)
    entered = [item.listing_id for item in work.claim(50) if item.feed == "entered"]
    assert entered == [10]
    # Once it holds a fingerprint row the sweep stops offering it.
    db.rt_fp[(GEN, 10)] = _fp_row()
    assert not [item for item in SqlWork(db, SCOPE, GEN, straggler_window=0, revive_slice=0,
                                         drift_slice=0).claim(50) if item.feed == "entered"]


def test_the_entrant_sweep_refreshes_one_block_and_claims_from_the_snapshot() -> None:
    """W9e/R3 moved the cost, not the coverage: the wide walk of a block on `public` fills
    `autodedup.rt_scope_ids` on a cadence, one block at a time, and the pass claims what the
    snapshot holds — so both blocks' entrants are reached without either walk being per-pass."""
    db = FakePg()
    db.admin_parents[490245] = 554782
    for listing_id, place in ((1, {"obec": 563510}), (2, {"cast_obce": 490245})):
        db.listings[listing_id] = {"first_seen_at": db.now - timedelta(days=9),
                                   "inactive_at": None, "is_active": True}
        _place(db, listing_id, obec=place.get("obec", 554782),
               cast_obce=place.get("cast_obce"))
    db.cursors[CURSOR_NEW] = {"last_listing_id": 5_000_000}
    work = SqlWork(db, SCOPE, GEN, straggler_window=0, revive_slice=0, drift_slice=0,
                   parents={490245: 554782})
    first = [item.listing_id for item in work.claim(50) if item.feed == "entered"]
    work.commit([WorkItem(listing_id, "entered", None, None) for listing_id in first])
    for listing_id in first:                      # the pass fingerprints what it decided
        db.rt_fp[(GEN, listing_id)] = _fp_row()
    assert len(db.scope_scans) == 1, "one block's walk a pass, never the scope's"
    db.now += timedelta(hours=12)
    second_work = SqlWork(db, SCOPE, GEN, straggler_window=0, revive_slice=0, drift_slice=0,
                          parents={490245: 554782})
    second = [item.listing_id for item in second_work.claim(50) if item.feed == "entered"]
    assert sorted(first + second) == [1, 2], "both blocks are reached, one walk at a time"


def test_a_quarter_block_needs_its_parent_obec_resolved() -> None:
    db = FakePg()
    db.admin_parents[490245] = 554782
    assert resolve_scope_parents(db, SCOPE) == {490245: 554782}
    with pytest.raises(ScopeError):
        resolve_scope_parents(FakePg(), SCOPE)


# --------------------------- W9d-4: the session guards belong INSIDE the transaction


def test_the_session_guards_are_set_local_inside_the_pass_transaction(tmp_path, monkeypatch):
    """`db.connect` speaks to the transaction-mode pooler, which rebinds a connection between
    queries: a guard set on the session is a guard the pass's own transaction may never see."""
    conn = _seeded([{"grain": "obec", "code": 563510}])
    monkeypatch.setenv(ENV_FLAG, "true")
    run_incremental(lambda: conn, {"generation": GEN}, tmp_path)
    for guard in (RT_STATEMENT_GUARD_SQL, RT_LOCK_GUARD_SQL, RT_IDLE_GUARD_SQL):
        assert guard in conn.statements_in_tx, guard
        assert ", true)" in guard, "set_config must be LOCAL to the transaction"
