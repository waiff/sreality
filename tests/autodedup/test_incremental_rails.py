"""W9e — the six rails the re-verification of the scoped lane (W9d) left open.

Every test here reproduces a DEFECT of the shipped W9d lane before it asserts the fix, because
five of the six are invisible from a green run: a seed that exits 0 having done nothing, a
retirement rail that cannot fire, an entrant sweep whose cost is 11 GB a day of cold reads on
the instance that serves Browse, a scope row a second generation silently rescopes, a feed with
no settle lag, and a rail a dispatch argument could switch off.

R1 seed while dark · R2 retirement over a rolling day · R3 what the entrant feed costs
R4 one scope row per generation · R5 the entrant settle lag · R6 control numbers are data
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from autodedup.incremental import WorkItem
from autodedup.incremental_lane import (
    CURSOR_ENTER,
    CURSOR_NEW,
    ENTER_INTERVAL_HOURS,
    ENV_FLAG,
    INTERVAL_SETTING,
    RETIRE_SETTING,
    SCAN_CAP_SETTING,
    SCOPE_SETTING,
    RetireRefusal,
    SqlWork,
    run_incremental,
    run_rt_seed,
    parity_baseline_key,
    scope_setting_key,
)
from autodedup.incremental_scope import Scope, ScopeBlock
from autodedup.incremental_sql import RT_SCOPE_BLOCK_SQL
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_incremental import _dataset

GEN = "rt"
TOWN = Scope((ScopeBlock("obec", 563510),))
SCOPE = Scope((ScopeBlock("obec", 563510), ScopeBlock("cast_obce", 490245)))
PARENTS = {490245: 554782}

# A generation is scored by ONE scorer and the lane refuses to guess which (E85a), so every
# seed here NAMES it — `default`/`prior` being the two words for the uncalibrated defaults
# these fixtures were already running on.
SCORER: dict[str, str] = {"settings": "default", "model": "prior"}


def _baseline(db: Any, generation: str = "rt") -> None:
    """The parity gate (E86) refuses a generation with no fact baseline. A fixture that
    hand-writes the calibration row hand-writes the baseline too — an empty one, because its
    `public` holds no cohort listing to compare against. What the gate is FOR is proved in
    `test_incremental_sqlstore.py` and `test_parity.py`."""
    db.settings[parity_baseline_key(generation)] = {
        "rows": {}, "exported_at": db.now.isoformat(), "n": 0}



def _fp_row(is_active: bool = True) -> dict:
    return {"category_main": None, "category_type": None, "area_m2": None,
            "disposition": None, "floor": None, "fp_digest": "d",
            "cell_key": "o1", "cell_group": "byt", "is_active": is_active}


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
    _baseline(db, generation)
    db.settings[scope_setting_key(generation)] = (
        scope if scope is not None else [{"grain": "obec", "code": 563510}])
    return db


def _work(db: FakePg, scope: Scope = TOWN, **kw: Any) -> SqlWork:
    params: dict[str, Any] = {"straggler_window": 0, "revive_slice": 0, "drift_slice": 0,
                              "parents": PARENTS}
    params.update(kw)
    return SqlWork(db, scope, GEN, **params)


# --------------------------------------------------------- R1: the seed runs while it is dark


def test_rt_seed_runs_while_the_schedule_is_dark(tmp_path, monkeypatch) -> None:
    """Seeding is the step BEFORE the switch. W9 gated it on the switch, so the documented
    recipe exited 0 having seeded nothing and the schedule then hard-errored every 10 minutes."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    conn = _registry(FakePg())
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    out = run_rt_seed(lambda: conn, {"artifact": "cohort.jsonl.gz", **SCORER}, tmp_path)
    assert out.get("skipped") is None
    assert conn.calibration[GEN]["digest"]
    assert conn.settings[scope_setting_key(GEN)]
    assert conn.cursors, "the seed starts the cursors at TODAY (E76)"


def test_rt_seed_takes_the_export_runs_own_artifact(tmp_path, monkeypatch) -> None:
    """The documented recipe has to be RUNNABLE: a runner holds no cohort file until
    `gh run download` puts one there, so the seed takes the export run id the batch pass used
    (35200225251) and fetches the same artifact the score and judge lanes fetch."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    conn = _registry(FakePg())
    fetched: list[str] = []

    def _download(export_run: str, dest: Any) -> str:
        fetched.append(export_run)
        return "cohort.jsonl.gz"

    monkeypatch.setattr("autodedup.judge_lane.download_cohort", _download)
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    out = run_rt_seed(lambda: conn, {"export_run": "35200225251", **SCORER}, tmp_path)
    assert fetched == ["35200225251"] and out["export_run"] == "35200225251"
    with pytest.raises(SystemExit) as raised:
        run_rt_seed(lambda: conn, {"export_run": "not-a-run", "generation": "g9", **SCORER},
                    tmp_path)
    assert "run id" in str(raised.value)


def test_a_second_seed_of_a_seeded_generation_is_refused(tmp_path, monkeypatch) -> None:
    """A re-seed re-cuts the frozen calibration every stored decision was taken under."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    conn = _seeded(_registry(FakePg()), scope=[{"grain": "cast_obce", "code": 490245}])
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    with pytest.raises(SystemExit) as raised:
        run_rt_seed(lambda: conn, {"artifact": "cohort.jsonl.gz", **SCORER}, tmp_path)
    assert "reseed" in str(raised.value)
    out = run_rt_seed(lambda: conn, {"artifact": "cohort.jsonl.gz", "reseed": "true", **SCORER},
                       tmp_path)
    assert out["reseed"] is True


def test_an_unseeded_generation_skips_green_rather_than_failing(tmp_path, monkeypatch) -> None:
    """With the variable flipped and no seed, W9d's lane hard-errored on the missing scope row
    every ten minutes for ever. It is a loud green skip instead — and still writes nothing."""
    conn = FakePg()
    monkeypatch.setenv(ENV_FLAG, "true")
    out = run_incremental(lambda: conn, {"generation": GEN}, tmp_path)
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


# ------------------------------------------------------- R4: one scope row per generation


def test_the_scope_row_is_per_generation(tmp_path, monkeypatch) -> None:
    """W9d wrote ONE global `rt_scope`, so a second generation's seed silently rescoped the
    first — and a scope that differs a little is retirement under a rail built for a lot."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    conn = _seeded(_registry(FakePg()), GEN, [{"grain": "obec", "code": 563510}])
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    run_rt_seed(lambda: conn, {"artifact": "c.jsonl.gz", "generation": "g2", **SCORER,
                               SCOPE_SETTING: "cast_obce:490245"}, tmp_path)
    assert conn.settings[scope_setting_key(GEN)] == [{"grain": "obec", "code": 563510}]
    assert conn.settings[scope_setting_key("g2")] == [{"grain": "cast_obce", "code": 490245}]


def test_the_legacy_global_row_is_read_for_the_rt_generation_only(tmp_path, monkeypatch):
    """One-time, one generation: `rt` is the only generation that can have written the old row."""
    conn = FakePg()
    conn.calibration[GEN] = {"digest": "d", "n_listings": 3, "payload": {},
                             "artifact_url": None, "settings": {},
                             "model_version": "hand_v1"}
    _baseline(conn, GEN)
    conn.calibration["g2"] = dict(conn.calibration[GEN])
    conn.settings[SCOPE_SETTING] = [{"grain": "obec", "code": 563510}]
    monkeypatch.setenv(ENV_FLAG, "true")
    out = run_incremental(lambda: conn, {"generation": GEN}, tmp_path)
    assert out["scope"] == [{"grain": "obec", "code": 563510}]
    with pytest.raises(SystemExit) as raised:
        run_incremental(lambda: conn, {"generation": "g2"}, tmp_path)
    assert SCOPE_SETTING in str(raised.value)


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


# --------------------------------------------------- R6: a rail is not a dispatch argument


def test_the_retire_fraction_is_not_a_plain_dispatch_argument(tmp_path, monkeypatch) -> None:
    conn = _seeded(FakePg())
    monkeypatch.setenv(ENV_FLAG, "true")
    with pytest.raises(SystemExit) as raised:
        run_incremental(lambda: conn, {"generation": GEN, RETIRE_SETTING: "1"}, tmp_path)
    assert RETIRE_SETTING in str(raised.value) and "rt_rescope" in str(raised.value)
    out = run_incremental(lambda: conn, {"generation": GEN, RETIRE_SETTING: "1",
                                         "rt_rescope": "true"}, tmp_path)
    assert out.get("skipped") is None


@pytest.mark.parametrize("key", [RETIRE_SETTING, SCAN_CAP_SETTING, INTERVAL_SETTING])
def test_a_non_numeric_control_row_stops_the_lane(tmp_path, monkeypatch, key) -> None:
    """W9d fell back to the default, so a rail ran at a number nobody had chosen."""
    conn = _seeded(FakePg())
    conn.settings[key] = "0,05"
    monkeypatch.setenv(ENV_FLAG, "true")
    with pytest.raises(SystemExit) as raised:
        run_incremental(lambda: conn, {"generation": GEN}, tmp_path)
    assert "number" in str(raised.value)


def test_the_scan_cap_is_not_a_plain_dispatch_argument(tmp_path, monkeypatch) -> None:
    conn = _seeded(FakePg())
    monkeypatch.setenv(ENV_FLAG, "true")
    with pytest.raises(SystemExit):
        run_incremental(lambda: conn, {"generation": GEN, SCAN_CAP_SETTING: "999"}, tmp_path)


def test_the_control_numbers_are_read_from_the_settings_rows(tmp_path, monkeypatch) -> None:
    conn = _seeded(FakePg())
    conn.settings[SCAN_CAP_SETTING] = 7
    conn.settings[INTERVAL_SETTING] = {"obec": 3, "cast_obce": 12}
    monkeypatch.setenv(ENV_FLAG, "true")
    out = run_incremental(lambda: conn, {"generation": GEN}, tmp_path)
    assert out["max_enter_scans_per_day"] == 7
    assert out["enter_interval_hours"] == {"obec": 3.0, "cast_obce": 12.0}
    assert CURSOR_NEW in conn.cursors or CURSOR_ENTER in conn.cursors
