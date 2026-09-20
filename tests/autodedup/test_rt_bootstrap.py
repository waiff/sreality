"""W9j — a seeded real-time generation is built through the ARRIVAL path (E97, E98).

Two defects of DESIGN, not of mechanism, and the replay-equivalence proof was structurally
blind to both: it starts from an EMPTY store and feeds every listing as an arrival.

E97 — a backfilled seed writes fingerprints and postings and NOT ONE PAIR, and `reseed=true`
      deletes nothing, so the 15,923 pairs of the defective 2026-09-20 pass survived the
      re-seed under a scorer no longer in force. `fresh=true` is the clean reset, and it is
      THIS generation's rows or nothing.
E98 — with `backfill=false` every in-scope listing is an arrival, and the entrant feed delivers
      a seventh of a claim a pass behind a 1 h / 6 h cadence. The bootstrap phase raises the
      entrant claim to the whole slice and walks every never-walked block in one pass, under
      the same rolling-day cap; the claim is bounded by a TIME budget as well as by counts;
      and the phase ends by itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from autodedup.incremental_lane import (
    CURSOR_ENTER,
    CURSOR_NEW,
    ENV_FLAG,
    LANE_NAME,
    PASS_BUDGET_S,
    PASS_RATE_PER_S,
    RESET_CURSORS,
    SqlWork,
    bootstrap_setting_key,
    parity_baseline_key,
    pass_rate_key,
    reset_generation,
    run_rt_seed,
    scope_setting_key,
)
from autodedup.incremental_scope import Scope, ScopeBlock
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_incremental import _dataset

GEN = "rt"
OTHER = "g6"
SCOPE = Scope((ScopeBlock("obec", 563510), ScopeBlock("cast_obce", 490245)))
PARENTS = {490245: 554782}
SCORER: dict[str, str] = {"settings": "default", "model": "prior"}
_SETTLED = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _fp_row(is_active: bool = True) -> dict:
    return {"category_main": None, "category_type": None, "area_m2": None,
            "disposition": None, "floor": None, "fp_digest": "d",
            "cell_key": "o1", "cell_group": "byt", "is_active": is_active,
            "ev_images": 0, "ev_phash": 0, "ev_clip": 0, "ev_tags": 0,
            "ev_complete": True, "first_decided_at": _SETTLED}


def _pair_row() -> dict:
    return {"probes": ["K1"], "from_lo": True, "from_hi": True, "families": 3,
            "certificate": None, "features": {}, "fp_lo": "a", "fp_hi": "b",
            "score": 0.9, "zone": "merge", "decision": "merge", "guard_veto": None,
            "evidence": {}, "context": {}, "calibration_digest": "d",
            "feature_version": 1, "model_version": "w6_gold", "cluster_key": 1}


def _populated(db: FakePg) -> FakePg:
    """Both generations, every table the reset names, plus what it must never touch."""
    for generation in (GEN, OTHER):
        db.rt_fp[(generation, 11)] = _fp_row()
        db.rt_fp[(generation, 12)] = _fp_row()
        db.fp_key.add((generation, "K1", "t", 11))
        db.pairs[(generation, 11, 12)] = _pair_row()
        db.clusters[(generation, 1)] = {"size": 2, "generation": generation}
        db.cluster_members.add((generation, 1, 11))
        db.cluster_members.add((generation, 1, 12))
        db.cluster_conflicts.append({"kind": "invariant", "detail": {"generation": generation}})
        db.cells[(generation, "o1", "byt")] = {"n_listings": 2}
        db.scope_ids[(generation, "obec:563510", 11)] = {"resolved_at": _SETTLED}
        db.scope_scans.append({"generation": generation, "block_key": "obec:563510",
                               "scanned_at": db.now, "rows_found": 1, "elapsed_ms": 1.0})
        db.retire_events.append({"generation": generation, "retired_at": db.now,
                                 "n_retired": 1, "store_rows": 2})
        db.calibration[generation] = {"digest": "d", "n_listings": 2, "payload": {},
                                      "artifact_url": None, "settings": {},
                                      "model_version": "w6_gold"}
    for name in RESET_CURSORS:
        db.cursors[name] = {"last_listing_id": 7, "last_snapshot_id": 7, "watermark": None}
    db.lease[LANE_NAME] = {"holder": "someone", "expires_at": db.now + timedelta(hours=1)}
    # Generation-FREE operator evidence. The reset must not see any of it.
    db.mnl.add((11, 12))
    db.phash_pop[999] = 4
    return db


# ------------------------------------------------------------------ E97: the clean reset


def test_the_reset_empties_this_generation_and_nothing_else() -> None:
    db = _populated(FakePg())
    deleted = reset_generation(db, GEN)

    assert deleted == {"pairs": 1, "cluster_members": 2, "cluster_conflicts": 1,
                       "clusters": 1, "rt_fp": 2, "fp_key": 1, "rt_block_cell": 1,
                       "rt_scope_ids": 1, "rt_scope_scan": 1, "rt_retire_event": 1,
                       "scan_cursor": len(RESET_CURSORS), "rt_lease": 1}
    assert not [key for key in db.pairs if key[0] == GEN]
    assert not [key for key in db.rt_fp if key[0] == GEN]
    assert not db.cursors and not db.lease


def test_another_generations_rows_survive_the_reset() -> None:
    """The defect this exists to make impossible is the mirror of the one it fixes: a reset
    that took `g6` with it would destroy the evidence a published number rests on (M47)."""
    db = _populated(FakePg())
    reset_generation(db, GEN)

    assert [key for key in db.pairs if key[0] == OTHER] == [(OTHER, 11, 12)]
    assert sorted(key for key in db.rt_fp if key[0] == OTHER) == [(OTHER, 11), (OTHER, 12)]
    assert [key for key in db.clusters if key[0] == OTHER] == [(OTHER, 1)]
    assert {row for row in db.cluster_members if row[0] == OTHER} == {
        (OTHER, 1, 11), (OTHER, 1, 12)}
    assert [row for row in db.cluster_conflicts
            if row["detail"]["generation"] == OTHER]
    assert [key for key in db.fp_key if key[0] == OTHER]
    assert [key for key in db.cells if key[0] == OTHER]
    assert [key for key in db.scope_ids if key[0] == OTHER]
    assert [row for row in db.scope_scans if row["generation"] == OTHER]
    assert [row for row in db.retire_events if row["generation"] == OTHER]


def test_the_generation_free_tables_survive_the_reset() -> None:
    """Operator verdicts, must-not-links and the frozen population are generation-free — E95
    says a re-seed preserves them and a reset is a re-seed with the rows removed."""
    db = _populated(FakePg())
    reset_generation(db, GEN)

    assert db.mnl == {(11, 12)}
    assert db.phash_pop == {999: 4}
    assert db.calibration[GEN]["digest"] == "d", "the seed rewrites this row, the reset does not"


def test_fresh_without_reseed_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    db = _populated(FakePg())
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    with pytest.raises(SystemExit, match="only valid with reseed=true"):
        run_rt_seed(lambda: db, {"artifact": "c.jsonl.gz", "fresh": "true", **SCORER},
                    tmp_path)
    assert (GEN, 11, 12) in db.pairs, "a refused seed writes nothing"


def test_fresh_refuses_a_generation_this_lane_never_seeded(tmp_path, monkeypatch) -> None:
    """The reset deletes by generation NAME. A batch generation (g4..g7) carries no real-time
    calibration, so `generation=g7 reseed=true fresh=true` would otherwise empty the pass the
    operator reviewed."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    db = _populated(FakePg())
    db.calibration.pop(OTHER, None)  # a BATCH generation: pairs and clusters, no calibration
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    with pytest.raises(SystemExit, match="never seeded by this lane"):
        run_rt_seed(lambda: db, {"artifact": "c.jsonl.gz", "generation": OTHER,
                                 "fresh": "true", "reseed": "true", **SCORER}, tmp_path)
    assert [key for key in db.pairs if key[0] == OTHER], "a refused seed deletes nothing"


def test_a_fresh_seed_reports_what_it_removed(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    db = _populated(FakePg())
    db.admin_parents[490245] = 554782
    db.settings["rt_parity_min_checked"] = 0
    db.settings["rt_parity_min_checked_share"] = 0
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    out = run_rt_seed(lambda: db, {"artifact": "c.jsonl.gz", "fresh": "true",
                                   "reseed": "true", **SCORER}, tmp_path)

    assert out["fresh"] is True
    assert out["reset"]["pairs"] == 1 and out["reset"]["clusters"] == 1
    assert not [key for key in db.pairs if key[0] == GEN]
    assert db.calibration[GEN]["digest"], "the seed re-cuts the calibration after the reset"
    assert db.cursors, "and restarts the cursors it just deleted"


def test_a_seed_that_refuses_after_the_reset_puts_every_row_back(tmp_path, monkeypatch) -> None:
    """The reset runs INSIDE the seed's transaction. A parity baseline that cannot clear the
    floors refuses the seed — and a generation emptied by a seed that then refused would be a
    state with no recovery but another export."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    db = _populated(FakePg())
    db.admin_parents[490245] = 554782
    db.settings["rt_parity_min_checked"] = 5  # nothing to compare: the seed must refuse
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    with pytest.raises(SystemExit):
        run_rt_seed(lambda: db, {"artifact": "c.jsonl.gz", "fresh": "true",
                                 "reseed": "true", **SCORER}, tmp_path)

    assert (GEN, 11, 12) in db.pairs
    assert db.lease[LANE_NAME]["holder"] == "someone"
    assert sorted(db.cursors) == sorted(RESET_CURSORS)


# ------------------------------------------------------------------ E98: the bootstrap phase


def _work(db: FakePg, **kw: Any) -> SqlWork:
    params: dict[str, Any] = {"straggler_window": 0, "revive_slice": 0, "drift_slice": 0,
                              "evidence_slice": 0, "parents": PARENTS, "lag": 0}
    params.update(kw)
    return SqlWork(db, SCOPE, GEN, **params)


def _scope_rows(db: FakePg, block: str, ids: range) -> None:
    for listing_id in ids:
        code = 490245 if block.startswith("cast_obce") else 563510
        db.locations[listing_id] = {
            "obec_kod": 554782 if code == 490245 else code,
            "cast_obce_kod": 490245 if code == 490245 else None,
            "resolved_at": db.now - timedelta(days=1)}


def test_the_phase_walks_every_never_walked_block_in_one_pass() -> None:
    """Outside the phase the entrant sweep walks ONE block a pass on a 1 h / 6 h cadence, so a
    three-block scope offers nothing at all out of the blocks it has not reached. Inside it,
    every block with no scan row is walked in the same pass — and a block already walked is
    NOT re-walked, because a quarter's re-walk is 231 MB of cold heap reads (W9e/R3)."""
    db = FakePg()
    _scope_rows(db, "obec:563510", range(1, 4))
    _scope_rows(db, "cast_obce:490245", range(10, 13))

    ordinary = _work(db, enter_slice=100)
    ordinary.claim(50)
    assert len({row["block_key"] for row in db.scope_scans}) == 1

    db.scope_scans.clear()
    db.scope_ids.clear()
    boot = _work(db, enter_slice=100, bootstrap=True)
    boot.claim(50)
    assert {row["block_key"] for row in db.scope_scans} == {"obec:563510",
                                                            "cast_obce:490245"}
    assert len(boot.enter_scan["blocks"]) == 2

    # Once every block has been walked once the cadence takes them back — inside the phase as
    # much as outside it. Two hours on, the 1 h town block is due and the 6 h quarter is not,
    # so a bootstrap pass walks exactly the one the CADENCE names.
    db.now = db.now + timedelta(hours=2)
    before = len(db.scope_scans)
    later = _work(db, enter_slice=100, bootstrap=True)
    later.claim(50)
    assert [row["block_key"] for row in db.scope_scans[before:]] == ["obec:563510"]


def test_the_daily_scan_cap_still_holds_inside_the_phase() -> None:
    """The cadence is what the lane INTENDS to spend and the phase suspends it; the cap is what
    the lane is ALLOWED to spend and nothing suspends that (W9e/R3)."""
    db = FakePg()
    _scope_rows(db, "obec:563510", range(1, 4))
    _scope_rows(db, "cast_obce:490245", range(10, 13))
    work = _work(db, enter_slice=100, bootstrap=True, max_enter_scans_per_day=1)
    work.claim(50)

    assert len(db.scope_scans) == 1
    assert work.enter_scan["skipped"] == "daily cap"


def test_the_entrant_claim_is_the_whole_slice_inside_the_phase() -> None:
    """A seventh of a 500-listing claim is 100 entrants a pass — 50 passes and eight hours to
    build a 4,976-listing scope. Inside the phase the entrant feed takes the whole slice."""
    db = FakePg()
    _scope_rows(db, "obec:563510", range(1, 60))

    ordinary = _work(db, enter_slice=1000)
    assert len([i for i in ordinary.claim(50) if i.feed == "entered"]) == 10

    db.scope_ids.clear()
    db.scope_scans.clear()
    boot = _work(db, enter_slice=1000, bootstrap=True)
    assert len([i for i in boot.claim(50) if i.feed == "entered"]) == 50


def test_the_phase_ends_when_the_backlog_is_empty() -> None:
    db = FakePg()
    _scope_rows(db, "obec:563510", range(1, 4))
    _scope_rows(db, "cast_obce:490245", range(10, 13))

    work = _work(db, enter_slice=100, bootstrap=True)
    work.claim(50)
    assert work.bootstrap_backlog == 6
    assert work.bootstrap_done is True, "this pass claims all six: nothing is left to enter"

    # A pass that could not take the whole backlog does NOT end the phase.
    db.scope_ids.clear()
    db.scope_scans.clear()
    short = _work(db, enter_slice=100, bootstrap=True)
    short.claim(2)
    assert short.bootstrap_done is False


def test_a_block_the_phase_has_not_walked_keeps_the_phase_open() -> None:
    """Both halves of the end condition are load-bearing: a backlog that happens to be empty
    because a block was never LISTED is not an empty backlog."""
    db = FakePg()
    _scope_rows(db, "obec:563510", range(1, 3))
    _scope_rows(db, "cast_obce:490245", range(10, 12))
    work = _work(db, enter_slice=100, bootstrap=True, max_enter_scans_per_day=1)
    work.claim(50)

    assert work.bootstrap_done is False


# ------------------------------------------------------------------ E98: the time budget


def test_the_claim_is_bounded_by_the_clock_as_well_as_by_the_count() -> None:
    """The one live pass that did work claimed 77 listings and took 876.7 s — 14.6 minutes of a
    25-minute runner timeout at a sixth of the shipped claim. A count bound is not a clock."""
    db = FakePg()
    _scope_rows(db, "obec:563510", range(1, 200))
    work = _work(db, enter_slice=1000, bootstrap=True,
                 pass_budget_s=900.0, rate_per_s=0.1)
    entered = [i for i in work.claim(500) if i.feed == "entered"]

    assert work.claim_bound["bound_by"] == "time"
    assert work.claim_bound["limit"] == 90
    assert len(entered) == 90


def test_a_measured_rate_lets_the_count_bind_again() -> None:
    db = FakePg()
    _scope_rows(db, "obec:563510", range(1, 200))
    work = _work(db, enter_slice=1000, bootstrap=True, pass_budget_s=900.0, rate_per_s=2.0)
    work.claim(120)

    assert work.claim_bound["bound_by"] == "count"
    assert work.claim_bound["limit"] == 120


def test_the_shipped_budget_and_rate_are_the_conservative_ones() -> None:
    """A default read upward would be a pass that dies on the runner's timeout having written
    nothing. 900 s of a 1,500 s job, at the only live rate this program has measured."""
    assert PASS_BUDGET_S == 900.0
    assert PASS_RATE_PER_S == pytest.approx(0.1)
    assert int(PASS_BUDGET_S * PASS_RATE_PER_S) == 90


# ------------------------------------------------------------------ the seed writes the phase


def test_the_seed_persists_the_phase_as_data(tmp_path, monkeypatch) -> None:
    """The passes that do the building are the `*/10` schedule's and none of them carries an
    argument, so the phase has to be a row."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    db = FakePg()
    db.admin_parents[490245] = 554782
    db.settings["rt_parity_min_checked"] = 0
    db.settings["rt_parity_min_checked_share"] = 0
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    out = run_rt_seed(lambda: db, {"artifact": "c.jsonl.gz", "rt_bootstrap": "true",
                                   **SCORER}, tmp_path)

    assert out["bootstrap"] is True
    assert db.settings[bootstrap_setting_key(GEN)] is True
    assert out["backfilled"] == 0, "the phase builds through the arrival path, not a backfill"


def test_a_seed_without_the_phase_writes_the_row_false(tmp_path, monkeypatch) -> None:
    """A re-seed must never leave a stale phase on: the row is written on EVERY seed."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    db = FakePg()
    db.admin_parents[490245] = 554782
    db.settings["rt_parity_min_checked"] = 0
    db.settings["rt_parity_min_checked_share"] = 0
    db.settings[bootstrap_setting_key(GEN)] = True
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    run_rt_seed(lambda: db, {"artifact": "c.jsonl.gz", **SCORER}, tmp_path)

    assert db.settings[bootstrap_setting_key(GEN)] is False


def test_the_phase_and_a_backfill_are_refused_together(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    db = FakePg()
    monkeypatch.setattr("autodedup.dataset.load", lambda path: _dataset())
    with pytest.raises(SystemExit, match="backfill=false"):
        run_rt_seed(lambda: db, {"artifact": "c.jsonl.gz", "rt_bootstrap": "true",
                                 "backfill": "true", **SCORER}, tmp_path)
