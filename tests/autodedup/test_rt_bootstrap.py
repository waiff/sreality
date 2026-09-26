"""W9j — a seeded real-time generation is built through the ARRIVAL path (E97, E98).

Two defects of DESIGN, not of mechanism, and the replay-equivalence proof was structurally
blind to both: it starts from an EMPTY store and feeds every listing as an arrival.

E97 — a re-seed that deleted nothing let the 15,923 pairs of the defective 2026-09-20 pass
      survive under a scorer no longer in force. `fresh=true` is the clean reset, and it is
      THIS generation's rows or nothing; a seeded generation is rebuilt only with it.
E98 — every in-scope listing is an arrival (the seed backfills nothing), and the entrant feed delivers
      a seventh of a claim a pass behind a 1 h / 6 h cadence. The bootstrap phase raises the
      entrant claim to the whole slice and walks every never-walked block in one pass, under
      the same rolling-day cap; the claim is bounded by a TIME budget as well as by counts;
      and the phase ends by itself.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from autodedup.incremental_lane import (
    CURSOR_ENTER,
    CURSOR_NEW,
    LANE_NAME,
    PASS_BUDGET_S,
    PASS_DEADLINE_S,
    PASS_RATE_PER_S,
    RESET_CURSORS,
    SEED_LEASE_TTL_S,
    SqlWork,
    bootstrap_setting_key,
    pass_rate_key,
    reset_generation,
    run_rt_seed,
    scope_setting_key,
)
from autodedup.incremental_scope import Scope, ScopeBlock
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.lane_world import seed as seed_public

GEN = "rt"
OTHER = "g6"
SCOPE = Scope((ScopeBlock("obec", 563510), ScopeBlock("cast_obce", 490245)))
PARENTS = {490245: 554782}
SCORER: dict[str, str] = {"settings": "default", "model": "prior"}
_SETTLED = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
SEED_HOLDER = "rt_seed:test:1:1"
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "autodedup.yml"


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
    # A lease left behind by a pass that has finished: expired, so a seed can take it.
    db.lease[LANE_NAME] = {"holder": "someone", "expires_at": db.now - timedelta(minutes=1)}
    # Generation-FREE operator evidence. The reset must not see any of it.
    db.mnl.add((11, 12))
    db.phash_pop[999] = 4
    return db


# ------------------------------------------------------------------ E97: the clean reset


def test_the_reset_empties_this_generation_and_nothing_else() -> None:
    db = _populated(FakePg())
    deleted = reset_generation(db, GEN, holder=SEED_HOLDER)

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
    reset_generation(db, GEN, holder=SEED_HOLDER)

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
    reset_generation(db, GEN, holder=SEED_HOLDER)

    assert db.mnl == {(11, 12)}
    assert db.phash_pop == {999: 4}
    assert db.calibration[GEN]["digest"] == "d", "the seed rewrites this row, the reset does not"


def _with_public(db: FakePg) -> FakePg:
    """`public` rows inside the scope, so the seed has something to cut a calibration over."""
    db.admin_parents[490245] = 554782
    seed_public(db)
    return db


def test_a_seeded_generation_is_rebuilt_only_with_fresh(tmp_path) -> None:
    db = _with_public(_populated(FakePg()))
    with pytest.raises(SystemExit, match="fresh=true"):
        run_rt_seed(lambda: db, dict(SCORER), tmp_path)
    assert (GEN, 11, 12) in db.pairs, "a refused seed writes nothing"


def test_the_seed_resets_the_lanes_own_generation_only(tmp_path) -> None:
    """The reset deletes by generation NAME, so the seed takes none: it is the lane's one
    stream, `rt`, and a batch generation can never be emptied by it."""
    db = _with_public(_populated(FakePg()))
    with pytest.raises(SystemExit, match="unknown arg"):
        run_rt_seed(lambda: db, {"generation": OTHER, "fresh": "true", **SCORER}, tmp_path)
    run_rt_seed(lambda: db, {"fresh": "true", **SCORER}, tmp_path)
    assert [key for key in db.pairs if key[0] == OTHER], "another generation is untouched"


def test_a_fresh_seed_reports_what_it_removed(tmp_path) -> None:
    db = _with_public(_populated(FakePg()))
    out = run_rt_seed(lambda: db, {"fresh": "true", **SCORER}, tmp_path)

    assert out["fresh"] is True
    assert out["reset"]["pairs"] == 1 and out["reset"]["clusters"] == 1
    assert not [key for key in db.pairs if key[0] == GEN]
    assert db.calibration[GEN]["digest"] != "d", "the seed re-cuts the calibration"
    assert db.cursors, "and restarts the cursors it just deleted"


def test_a_seed_that_refuses_after_the_reset_puts_every_row_back(tmp_path) -> None:
    """The reset runs INSIDE the seed's transaction: a scope that holds nothing to cut a
    calibration over refuses the seed, and a generation emptied by a seed that then refused
    would be a state with no recovery."""
    db = _populated(FakePg())
    db.admin_parents[490245] = 554782
    with pytest.raises(SystemExit, match="holds no listing"):
        run_rt_seed(lambda: db, {"fresh": "true", **SCORER}, tmp_path)

    assert (GEN, 11, 12) in db.pairs
    # The lease row is the seed's own by now (it took the expired one), and it went back free.
    assert db.lease[LANE_NAME]["holder"].startswith("rt_seed:")
    assert db.lease[LANE_NAME]["expires_at"] <= db.now
    assert sorted(db.cursors) == sorted(RESET_CURSORS)


# ------------------------------------------------- a seed and a pass never overlap (the lease)


def test_the_reset_keeps_the_seeds_own_lease() -> None:
    db = _populated(FakePg())
    db.lease[LANE_NAME] = {"holder": SEED_HOLDER, "expires_at": db.now + timedelta(hours=1)}

    deleted = reset_generation(db, GEN, holder=SEED_HOLDER)

    assert deleted["rt_lease"] == 0
    assert db.lease[LANE_NAME]["holder"] == SEED_HOLDER


def test_a_seed_refuses_while_a_pass_holds_the_lease(tmp_path) -> None:
    """A pass mid-transaction when a fresh seed's reset ran would land old-scorer rows in the
    generation the seed just emptied (E97), so the seed takes the lane's own lease and refuses,
    having written nothing, while a pass holds it."""
    db = _with_public(_populated(FakePg()))
    held = {"holder": "worker:1:1", "expires_at": db.now + timedelta(minutes=10)}
    db.lease[LANE_NAME] = dict(held)
    cursors = {k: dict(v) for k, v in db.cursors.items()}

    with pytest.raises(SystemExit, match="holds autodedup.rt_lease"):
        run_rt_seed(lambda: db, {"fresh": "true", **SCORER}, tmp_path)

    assert (GEN, 11, 12) in db.pairs and db.calibration[GEN]["digest"] == "d"
    assert db.cursors == cursors
    assert db.lease[LANE_NAME] == held, "the pass keeps its lease"


def test_a_fresh_seed_holds_the_lease_through_its_transaction(tmp_path, monkeypatch) -> None:
    from autodedup import incremental_lane

    db = _with_public(_populated(FakePg()))
    inside: list[dict[str, Any]] = []
    original = incremental_lane.cut_calibration

    def spy(conn: Any, *args: Any, **kwargs: Any) -> Any:
        inside.append(dict(conn.lease[LANE_NAME]))
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(incremental_lane, "cut_calibration", spy)
    out = run_rt_seed(lambda: db, {"fresh": "true", **SCORER}, tmp_path)

    assert out["reset"]["rt_lease"] == 0, "the reset kept the seed's own lease"
    assert inside and inside[0]["holder"].startswith("rt_seed:")
    assert inside[0]["expires_at"] >= db.now + timedelta(seconds=SEED_LEASE_TTL_S)
    assert db.lease[LANE_NAME]["expires_at"] <= db.now, "and freed it on the way out"


def test_the_seed_lease_outlives_the_seeding_job() -> None:
    """A seed still running when its lease expired would let a pass in mid-reset."""
    minutes = [int(m) for m in re.findall(r"timeout-minutes:\s*(\d+)",
                                           WORKFLOW.read_text(encoding="utf-8"))]
    assert minutes and SEED_LEASE_TTL_S >= max(minutes) * 60


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
    """A default read upward would be a pass that dies at its deadline having written nothing:
    half the pass's own deadline (E913), at the only rate measured before any was recorded."""
    assert PASS_BUDGET_S == PASS_DEADLINE_S / 2 == 525.0
    assert PASS_RATE_PER_S == pytest.approx(0.1)
    assert int(PASS_BUDGET_S * PASS_RATE_PER_S) == 52


# ------------------------------------------------------------------ the seed writes the phase


def test_the_seed_persists_the_phase_as_data(tmp_path) -> None:
    """The passes that do the building carry no argument, so the phase has to be a row — and
    every seed opens it: the build is the arrival path, never a backfill (E98)."""
    db = _with_public(FakePg())
    db.settings[bootstrap_setting_key(GEN)] = False
    out = run_rt_seed(lambda: db, dict(SCORER), tmp_path)

    assert out["bootstrap"] is True
    assert db.settings[bootstrap_setting_key(GEN)] is True
    assert not db.rt_fp, "the phase builds through the arrival path, not a backfill"
