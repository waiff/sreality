"""W9e — the six rails the re-verification of the scoped lane (W9d) left open.

Every test here reproduces a DEFECT of the shipped W9d lane before it asserts the fix, because
five of the six are invisible from a green run: a seed that exits 0 having done nothing, a
retirement rail that cannot fire, an entrant sweep whose cost is 11 GB a day of cold reads on
the instance that serves Browse, a scope row a second generation silently rescopes, a feed with
no settle lag, and a rail a dispatch argument could switch off.

R1 the seed · R2 retirement over a rolling day · R3 what the entrant feed costs
R4 one scope row · R5 the entrant settle lag · R6 the controls are constants (E914)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from autodedup.incremental import WorkItem
from autodedup.incremental_lane import (
    CURSOR_ENTER,
    CURSOR_NEW,
    ENTER_INTERVAL_HOURS,
    SCOPE_SETTING,
    RetireRefusal,
    SqlWork,
    bootstrap_setting_key,
    run_incremental,
    run_rt_seed,
    scope_setting_key,
)
from autodedup.incremental_scope import Scope, ScopeBlock
from autodedup.incremental_sql import RT_SCOPE_BLOCK_SQL
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.lane_world import seed

GEN = "rt"
TOWN = Scope((ScopeBlock("obec", 563510),))
SCOPE = Scope((ScopeBlock("obec", 563510), ScopeBlock("cast_obce", 490245)))
PARENTS = {490245: 554782}

# A generation is scored by ONE scorer and the lane refuses to guess which (E90a), so every
# seed here NAMES it — `default`/`prior` being the two words for the uncalibrated defaults
# these fixtures were already running on.
SCORER: dict[str, str] = {"settings": "default", "model": "prior"}


# A SETTLED store row: its photographs were counted when the generation decided it, long
# enough ago that the evidence sweep (E92) has no business in it. A hand-written row that left
# these NULL would be claimed by the sweep on every pass — unmeasured is not the same as
# nothing to measure — which is what `test_shipped_w9h.py` proves it does.
_SETTLED = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _fp_row(is_active: bool = True) -> dict:
    return {"category_main": None, "category_type": None, "area_m2": None,
            "disposition": None, "floor": None, "fp_digest": "d",
            "cell_key": "o1", "cell_group": "byt", "is_active": is_active,
            "ev_images": 0, "ev_phash": 0, "ev_clip": 0, "ev_tags": 0,
            "ev_complete": True, "first_decided_at": _SETTLED}


def _place(db: FakePg, listing_id: int, *, obec: int | None = None,
           cast_obce: int | None = None, resolved_at: Any = None) -> None:
    db.locations[int(listing_id)] = {"obec_kod": obec, "cast_obce_kod": cast_obce,
                                     "resolved_at": resolved_at}


def _registry(db: FakePg) -> FakePg:
    """`public.ruian_admin_units`: the quarter block's parent obec (W9d-3)."""
    db.admin_parents[490245] = 554782
    return db


def _seeded(db: FakePg, generation: str = GEN, scope: Any = None) -> FakePg:
    db.calibration[generation] = {"digest": "d", "n_listings": 3, "payload": {},
                                  "artifact_url": None, "settings": {},
                                  "model_version": "hand_v1"}
    db.settings[scope_setting_key(generation)] = (
        scope if scope is not None else [{"grain": "obec", "code": 563510}])
    return db


def _work(db: FakePg, scope: Scope = TOWN, **kw: Any) -> SqlWork:
    params: dict[str, Any] = {"straggler_window": 0, "revive_slice": 0, "drift_slice": 0,
                              "parents": PARENTS}
    params.update(kw)
    return SqlWork(db, scope, GEN, **params)


# --------------------------------------------------------- R1: the seed runs while it is dark


def test_rt_seed_cuts_the_generation_from_the_database(tmp_path) -> None:
    """A10 (E912): no artifact, no export run — the seed walks the scope's blocks, cuts the
    calibration and the pHash population from `public`, starts the cursors at TODAY (E76) and
    opens the build phase."""
    conn = _registry(FakePg())
    seed(conn)
    out = run_rt_seed(lambda: conn, {**SCORER, SCOPE_SETTING: "obec:563510"}, tmp_path)
    assert conn.calibration[GEN]["digest"] == out["calibration"]["digest"]
    assert conn.calibration[GEN]["artifact_url"] is None
    assert out["calibration"]["scope_listings"] == len(conn.listings)
    assert conn.phash_pop, "the population is measured, not copied from an export"
    assert conn.settings[scope_setting_key(GEN)] == [{"grain": "obec", "code": 563510}]
    assert conn.settings[bootstrap_setting_key(GEN)] is True
    assert conn.cursors[CURSOR_NEW]["last_listing_id"] == max(conn.listings)
    assert {row["block_key"] for row in conn.scope_scans} == {"obec:563510"}


def test_rt_seed_takes_no_export(tmp_path) -> None:
    conn = _registry(FakePg())
    seed(conn)
    for gone in ("export_run", "artifact", "backfill", "reseed", "parity_n", "generation"):
        with pytest.raises(SystemExit) as raised:
            run_rt_seed(lambda: conn, {**SCORER, gone: "1"}, tmp_path)
        assert "unknown arg" in str(raised.value)
    assert not conn.calibration


def test_a_second_seed_of_a_seeded_generation_is_a_rebuild(tmp_path) -> None:
    """A new scorer or scope re-decides everything the store holds: `fresh=true` says so."""
    conn = _registry(FakePg())
    seed(conn)
    run_rt_seed(lambda: conn, {**SCORER, SCOPE_SETTING: "obec:563510"}, tmp_path)
    conn.rt_fp[(GEN, 999)] = _fp_row()
    with pytest.raises(SystemExit) as raised:
        run_rt_seed(lambda: conn, dict(SCORER), tmp_path)
    assert "fresh=true" in str(raised.value)
    out = run_rt_seed(lambda: conn, {**SCORER, "fresh": "true"}, tmp_path)
    assert out["fresh"] is True and out["reset"]["rt_fp"] == 1
    assert (GEN, 999) not in conn.rt_fp
    assert conn.settings[scope_setting_key(GEN)] == [{"grain": "obec", "code": 563510}], (
        "a rebuild keeps the scope the generation was cut for unless it names another")


def test_an_empty_scope_is_refused_rather_than_cut(tmp_path) -> None:
    conn = _registry(FakePg())
    with pytest.raises(SystemExit) as raised:
        run_rt_seed(lambda: conn, {**SCORER, SCOPE_SETTING: "obec:563510"}, tmp_path)
    assert "holds no listing" in str(raised.value)
    assert not conn.calibration and not conn.cursors, "the transaction put everything back"


def test_an_unseeded_generation_skips_green_rather_than_failing() -> None:
    """With the lane on and no seed it is a loud green skip — and it writes nothing."""
    conn = FakePg()
    out = run_incremental(lambda: conn)
    assert out["skipped"] == "unseeded"
    assert "rt_seed" in out["reason"]
    assert not conn.cursors and not conn.rt_fp and not conn.lease


# ------------------------------------------------- R2: retirement is measured over a whole day


def test_the_retire_rail_is_measured_over_a_rolling_day_not_one_slice() -> None:
    """W9d compared ONE drift slice with a fraction of the store, so a small slice — a big
    store, or `drift_slice=1` — could grind the store away a share a pass with the rail silent.
    Reproduced at 1/500 scale: 1,000 rows, every one of them departed, a slice of 20."""
    db = FakePg()
    for listing_id in range(1, 1001):
        db.rt_fp[(GEN, listing_id)] = _fp_row()
        _place(db, listing_id, obec=999999)
    retired = 0
    with pytest.raises(RetireRefusal) as raised:
        for _pass in range(20):
            work = _work(db, drift_slice=20, enter_slice=0)
            items = work.claim(50)
            done = [item for item in items if item.retire]
            for item in done:
                db.rt_fp.pop((GEN, item.listing_id), None)
            retired += len(done)
            work.commit(items)
    assert "24 h" in str(raised.value)
    # 5% of the 1,000 rows the store held when the window opened, plus the share that tipped it.
    assert retired <= 60, retired
    assert len(db.rt_fp) >= 940, "the store survives; the lane stops instead"


def test_a_day_of_real_drift_is_still_retired() -> None:
    """The rail is against a store-wide wipe, not against the drift it exists for."""
    db = FakePg()
    for listing_id in range(1, 1001):
        db.rt_fp[(GEN, listing_id)] = _fp_row()
        _place(db, listing_id, obec=563510)
    _place(db, 7, obec=999999)
    work = _work(db, drift_slice=20000, enter_slice=0)
    items = work.claim(50)
    assert [item.listing_id for item in items if item.retire] == [7]
    work.commit(items)
    assert len(db.retire_events) == 1
    assert db.retire_events[0]["store_rows"] == 1000


# ------------------------------------------- R3: what the entrant feed costs the live instance


def test_an_ordinary_pass_reads_nothing_on_public_for_the_entrant_feed() -> None:
    """The headline. W9d's sweep re-read a scope block on `public` every cycle — the quarter
    block is 29,265 heap blocks (231 MB, 6.4 s, cold every time), ~48 times a day. The wide
    scan now fills a snapshot on a cadence and an ordinary pass claims out of the snapshot."""
    db = FakePg()
    _place(db, 10, obec=563510)
    first = _work(db, enter_slice=20000)
    assert [item.listing_id for item in first.claim(50) if item.feed == "entered"] == [10]
    assert RT_SCOPE_BLOCK_SQL in db.statements, "the first pass fills the snapshot"
    first.commit([WorkItem(10, "entered", None, None)])
    db.statements.clear()
    second = _work(db, enter_slice=20000)
    assert [item.listing_id for item in second.claim(50) if item.feed == "entered"] == [10]
    assert RT_SCOPE_BLOCK_SQL not in db.statements, "no block of `public` for this feed"


def test_the_wide_scan_cadence_is_per_grain_and_is_data() -> None:
    """A town block is a 21 MB index-served scan; a quarter has no index of its own at all and
    costs 231 MB. They do not deserve the same cadence, and neither is per-pass work."""
    db = FakePg()
    _place(db, 10, obec=563510)
    _place(db, 11, obec=554782, cast_obce=490245)
    work = _work(db, SCOPE, enter_slice=20000)
    work.claim(50)                                   # walks one block
    work.commit([WorkItem(item.listing_id, "entered", None, None)
                 for item in work.claim(50) if item.feed == "entered"])
    scanned = {row["block_key"] for row in db.scope_scans}
    db.now += timedelta(hours=2)                     # the town is due, the quarter is not
    db.statements.clear()
    _work(db, SCOPE, enter_slice=20000).claim(50)
    due = [row["block_key"] for row in db.scope_scans][len(scanned):]
    assert due and all(key.startswith("obec:") for key in due), due
    assert ENTER_INTERVAL_HOURS["cast_obce"] > ENTER_INTERVAL_HOURS["obec"]


def test_the_lane_refuses_to_exceed_the_daily_scan_cap() -> None:
    """The cadence is what the lane intends to spend; the cap is what it is allowed to."""
    db = FakePg()
    _place(db, 10, obec=563510)
    _place(db, 11, obec=554782, cast_obce=490245)
    work = _work(db, SCOPE, enter_slice=20000, max_enter_scans_per_day=1)
    work.claim(50)
    assert len(db.scope_scans) == 1
    db.now += timedelta(hours=12)                    # both blocks overdue, the cap says no
    second = _work(db, SCOPE, enter_slice=20000, max_enter_scans_per_day=1)
    second.claim(50)
    assert len(db.scope_scans) == 1
    assert second.enter_scan["skipped"] == "daily cap"


def test_a_listing_that_leaves_a_block_leaves_its_snapshot() -> None:
    db = FakePg()
    _place(db, 10, obec=563510)
    _work(db, enter_slice=20000).claim(50)
    assert (GEN, "obec:563510", 10) in db.scope_ids
    _place(db, 10, obec=999999)
    db.now += timedelta(hours=2)
    _work(db, enter_slice=20000).claim(50)
    assert (GEN, "obec:563510", 10) not in db.scope_ids


def test_a_retired_listing_leaves_the_snapshot_in_the_same_pass() -> None:
    """W9e-1: the entrant claim has no scope check of its own, so a snapshot row that outlives
    the retirement hands the listing straight back and burns the rolling-day retire budget."""
    from autodedup.incremental_lane import SqlStore

    db = FakePg()
    db.scope_ids[(GEN, "obec:563510", 10)] = {"resolved_at": None, "refreshed_at": db.now}
    db.scope_ids[(GEN, "obec:563510", 11)] = {"resolved_at": None, "refreshed_at": db.now}
    db.rt_fp[(GEN, 10)] = _fp_row()
    SqlStore(db, GEN).drop_listing(10)
    assert (GEN, 10) not in db.rt_fp
    assert (GEN, "obec:563510", 10) not in db.scope_ids
    assert (GEN, "obec:563510", 11) in db.scope_ids


def test_a_block_dropped_by_a_rescope_leaves_no_snapshot_behind() -> None:
    """W9e-1b: the per-block prune only runs for a WALKED block, and a dropped block is never
    walked again, so its rows would be claimed as entrants and retired for ever."""
    db = _registry(FakePg())
    db.scope_ids[(GEN, "cast_obce:490245", 77)] = {"resolved_at": None, "refreshed_at": db.now}
    _place(db, 10, obec=563510)
    _work(db, TOWN, enter_slice=20000).claim(50)
    assert (GEN, "cast_obce:490245", 77) not in db.scope_ids
    assert (GEN, "obec:563510", 10) in db.scope_ids


# ------------------------------------------------------- R4: one scope row, the seed's


def test_a_pass_runs_the_scope_the_seed_wrote_and_no_other() -> None:
    """The pass takes no argument (E914): the scope is the row the seed persisted, and a
    generation seeded before the row existed is refused rather than defaulted (W9d-2)."""
    conn = _seeded(FakePg())
    out = run_incremental(lambda: conn)
    assert out["scope"] == [{"grain": "obec", "code": 563510}]
    del conn.settings[scope_setting_key(GEN)]
    conn.settings[SCOPE_SETTING] = [{"grain": "obec", "code": 563510}]
    with pytest.raises(SystemExit) as raised:
        run_incremental(lambda: conn)
    assert SCOPE_SETTING in str(raised.value), "the legacy global row is not read"


# ------------------------------------------------------------ R5: the entrant settle lag


def test_the_entrant_feed_honours_the_settle_lag() -> None:
    """It was the only feed with none: a location resolved five seconds ago was claimed in the
    same pass in which every other feed's lag still held its listing back."""
    db = FakePg()
    _place(db, 10, obec=563510, resolved_at=db.now - timedelta(seconds=5))
    work = _work(db, enter_slice=20000)
    assert [item for item in work.claim(50) if item.feed == "entered"] == []
    db.now += timedelta(hours=1)
    later = _work(db, enter_slice=20000)
    claimed = [item for item in later.claim(50) if item.feed == "entered"]
    assert [item.listing_id for item in claimed] == [10]
    assert claimed[0].arrived_at is not None, "the resolution IS this feed's arrival event"


# --------------------------------------------------------- R6: the controls are constants


def test_the_retired_control_rows_are_not_read() -> None:
    """E914: the fourteen control rows became constants of the lane. A row left behind (they
    stay until W8's drop) changes nothing, whatever it holds."""
    import autodedup.incremental_lane as lane

    conn = _seeded(FakePg())
    for key in ("realtime_enabled", "rt_max_schema_mb", "rt_max_retire_fraction",
                "rt_enter_max_scans_per_day", "rt_enter_interval_hours",
                "rt_evidence_horizon_hours", "rt_evidence_slice", "rt_pass_budget_s",
                "rt_calibration_max_age_days", "rt_parity_baseline:rt", "rt_parity_sample",
                "rt_parity_min_checked", "rt_parity_min_checked_share",
                "rt_parity_max_unknown_pop_share"):
        conn.settings[key] = "0,05"
    out = run_incremental(lambda: conn)
    assert out.get("skipped") is None and not out["aborted"]
    assert out["claim_bound"]["pass_budget_s"] == lane.PASS_BUDGET_S
    assert CURSOR_NEW in conn.cursors or CURSOR_ENTER in conn.cursors
