"""A10 (E912): the lane cuts its calibration and its pHash population from the database, and
reads the facts the calibration was cut from (W9g's gate, answered by construction).

The world is `lane_world`'s: nine listings in one town, consecutive ones sharing their
photographs and one catalogue frame on all of them, in the FakePg's `public`. It is driven end
to end — `rt_seed` then `run_incremental` — because the three defects W9f shipped were all in
the seam between them: a population table with no writer, a generation seeded and run on a
scorer nobody chose, and a re-cluster spelled per component.
"""

from __future__ import annotations

import json
import zlib
from datetime import timedelta
from typing import Any

import pytest

from autodedup import incremental_lane
from autodedup.export import build_image_record, build_listing_record, tag_pairs
from autodedup.dataset import Image, Listing
from autodedup.fingerprint import build_fingerprint
from autodedup.incremental import (
    Calibration,
    Limits,
    PairRow,
    PassResult,
    Rulings,
    _recluster,
    _Working,
    bootstrap_key,
)
from autodedup.incremental_lane import SqlFacts, SqlStore, run_incremental, run_rt_seed
from autodedup.incremental_sql import (
    RT_CLUSTERS_TOUCHING_SQL,
    RT_MERGE_NEIGHBOURS_SQL,
    RT_PAIRS_WITHIN_SQL,
)
from autodedup.settings import Settings
from tests.autodedup.fake_pg import LEDGER_KEY, FakePg
from tests.autodedup.lane_world import GENERATION, SCOPE, seed_lane, true_population
from tests.autodedup.lane_world import world as lane_world


@pytest.fixture()
def world() -> FakePg:
    return lane_world()


def _seed(conn: FakePg, tmp_path: Any, **args: Any) -> dict[str, Any]:
    return seed_lane(conn, tmp_path, **args)


def _built(conn: FakePg, tmp_path: Any) -> FakePg:
    """Seeded and out of its build: a re-cut never runs in a build (E944)."""
    seed_lane(conn, tmp_path)
    conn.settings[bootstrap_key(GENERATION)] = False
    return conn


# --------------------------------------------------- E91: the population table has a WRITER


def test_the_seed_measures_the_population_over_public_images(world, tmp_path) -> None:
    """`autodedup.phash_pop` held 0 rows in production and NO code in the repository wrote it.
    The seed now measures it — the export's own aggregate over `public.images`, run for the
    scope's hashes — and the lane reads a MEASURED population again."""
    out = _seed(world, tmp_path)

    assert world.phash_pop == true_population(world)
    assert out["calibration"]["hashes_measured"] == len(world.phash_pop) > 0
    images = SqlFacts(world).facts([4_000])[4_000][1]
    assert all(image.pop_is_measured() for image in images)
    assert max(image.pop or 0 for image in images) == len(world.listings), "the catalogue photo"


def test_the_cut_is_the_calibration_an_export_of_the_same_rows_would_give(world, tmp_path) -> None:
    """The parity gate asked whether the lane's facts were the export's (E91). The cut reads
    the pass's own facts, so the answer is structural — and it is the SAME calibration the
    export's record builders give over the same rows."""
    out = _seed(world, tmp_path)
    population = true_population(world)
    tags: dict[int, list[dict[str, Any]]] = {}
    for row in world.clip_tags:
        tags.setdefault(int(row["image_id"]), []).append(row)
    history: dict[int, list[dict[str, Any]]] = {}
    for row in world.snapshots:
        history.setdefault(int(row["listing_id"]), []).append(row)
    settings = Settings()
    listings: dict[int, Listing] = {}
    fps = {}
    for listing_id in sorted(world.listings):
        listing = Listing.from_json(build_listing_record(
            world.listings[listing_id], block="", location=world.locations[listing_id],
            history=history.get(listing_id, ())))
        images = [Image.from_json(build_image_record(
            row, clip=None, tags=tag_pairs(tags.get(int(row["image_id"]), [])), pop=population))
            for row in world.image_rows if row["listing_id"] == listing_id]
        listings[listing_id] = listing
        fps[listing_id] = build_fingerprint(listing, images, settings)
    expected = Calibration.build(fps, listings, settings, GENERATION)
    assert out["calibration"]["digest"] == expected.digest()


def test_a_pass_reports_the_population_it_could_measure(world, tmp_path) -> None:
    _seed(world, tmp_path)
    out = run_incremental(lambda: world)
    assert out["population"]["images_unmeasured"] == 0
    assert out["population"]["coverage"] == 1.0
    assert out["population"]["phash_pop_rows"] == len(world.phash_pop)
    assert "recut" not in out


def test_a_photograph_the_cut_never_saw_is_unknown_until_the_re_cut(world, tmp_path,
                                                                    monkeypatch) -> None:
    """The producers keep re-hashing the corpus. A hash the population table does not carry is
    UNKNOWN (E91), the pass says how much of what it read that was, and below the floor the
    next tick re-cuts instead of passing (E944) — so the table follows the corpus instead of
    aging out in 14 days (A10), and no pass pays for the cut."""
    _built(world, tmp_path)
    for row in world.image_rows:
        row["phash"] = int(row["phash"]) + 7
    for name, value in (("RECUT_MIN_IMAGES", 1), ("RECUT_MIN_AGE_H", 0.0)):
        monkeypatch.setattr(incremental_lane, name, value)
    before = world.calibration[GENERATION]["digest"]
    population = dict(world.phash_pop)

    out = run_incremental(lambda: world)

    assert out["population"]["images_unmeasured"] >= 1
    assert out["population"]["coverage"] < incremental_lane.COVERAGE_FLOOR
    assert out["population"]["recut_due"] and "recut" not in out, "the pass only marks it"
    assert world.phash_pop == population

    recut = run_incremental(lambda: world)

    assert recut["skipped"] == "recut" and "counts" not in recut, "a tick of its own"
    assert recut["recut"]["hashes_measured"] > 0 and recut["recut"]["moved"] is False
    assert all(int(row["phash"]) in world.phash_pop for row in world.image_rows)
    assert world.calibration[GENERATION]["digest"] == before, (
        "a re-hash moves the population, not the cohort statistics (E915: not a generation)")
    assert world.settings[incremental_lane.recut_key(GENERATION)]["due"] is None
    again = run_incremental(lambda: world)
    assert again["population"]["images_unmeasured"] == 0 and "recut" not in again
    assert "recut_due" not in again["population"] and "counts" in again


def test_a_re_cut_waits_out_its_minimum_age(world, tmp_path, monkeypatch) -> None:
    _built(world, tmp_path)
    for row in world.image_rows:
        row["phash"] = int(row["phash"]) + 7
    monkeypatch.setattr(incremental_lane, "RECUT_MIN_IMAGES", 1)
    monkeypatch.setattr(incremental_lane, "RECUT_MIN_AGE_H", 10 ** 6)
    out = run_incremental(lambda: world)
    assert out["population"]["coverage"] < incremental_lane.COVERAGE_FLOOR
    assert "recut_due" not in out["population"]
    assert "recut" not in run_incremental(lambda: world), "the next tick passes too"


# ------------------------------------------------------- E90a: WHICH scorer took the decision


def test_a_seed_that_names_no_scorer_is_refused(world, tmp_path) -> None:
    """W9f's seed AND its first live pass both ran on empty arguments, so the uncalibrated
    prior scored a generation the operator believed was `w8` + `w6_gold`."""
    with pytest.raises(SystemExit) as raised:
        run_rt_seed(lambda: world, {"rt_scope": SCOPE}, tmp_path)
    assert "settings=" in str(raised.value) and "model=" in str(raised.value)


def test_a_settings_row_is_named_not_pathed(world, tmp_path) -> None:
    """`settings=w8` was read as a PATH by this lane and named no file it could read — which is
    why the recipe that shipped passed nothing at all."""
    out = _seed(world, tmp_path, settings="w8", model="w6_gold")
    assert out["settings_name"] == "w8" and out["model_version"] == "w6_gold"
    assert world.calibration[GENERATION]["model_version"] == "w6_gold"


def test_the_pass_runs_the_generations_scorer(world, tmp_path) -> None:
    """The pass takes no argument: its scorer is the one the seed stamped on the calibration
    row, and changing it is a re-seed (E90a)."""
    _seed(world, tmp_path, settings="w8", model="w6_gold")
    out = run_incremental(lambda: world)
    assert out["model_version"] == "w6_gold"
    assert out["settings"]["t_lo"] == Settings.from_json("autodedup/settings/w8.json").t_lo


def test_every_stored_pair_names_the_scorer_that_decided_it(world) -> None:
    """All 15,923 rows of the live generation carried a NULL `model_version` against `w6_gold`
    on the batch generation's, because the lane wrote `evidence["_model"]` — a key nothing has
    ever set."""
    store = SqlStore(world, GENERATION, model_version="w6_gold", calibration_digest="abc123",
                     engine_digest="e")
    store.upsert_pairs([PairRow(lo=4_000, hi=4_001, probes=["img"], from_lo=True,
                                from_hi=True, zone="merge", score=0.99, families=["IMG"],
                                certificate="K-C", veto="", reason="r", evidence={},
                                context={"block": "o1"}, fp_lo="a", fp_hi="b")])
    store.flush()
    row = world.pairs[(GENERATION, 4_000, 4_001)]
    assert row["model_version"] == "w6_gold" and row["calibration_digest"] == "abc123"


# --------------------------------------------- E74: the re-cluster is spelled per PASS, not
# per component


def _components_world(conn: FakePg, n: int) -> list[PairRow]:
    rows: list[PairRow] = []
    for index in range(n):
        lo = 4_000 + 2 * index
        rows.append(PairRow(lo=lo, hi=lo + 1, probes=["img"], from_lo=True, from_hi=True,
                            zone="merge", score=0.99, families=["IMG"], certificate="K-C",
                            veto="", reason="r", evidence={}, context={"block": "o1"},
                            fp_lo="a", fp_hi="b"))
    return rows


def _recluster_cost(conn: FakePg, n: int) -> tuple[int, dict[str, int], PassResult]:
    store = SqlStore(conn, GENERATION, **LEDGER_KEY)
    store.upsert_pairs(_components_world(conn, n))
    store.flush()
    before = dict(store.__dict__)  # noqa: F841 — readability only
    store.statements = 0
    conn.statements.clear()
    result = PassResult(generation=GENERATION, calibration_digest="d")
    working = _Working(SqlFacts(conn), Settings())
    _recluster(store, SqlFacts(conn), Settings(), working,
               {i for row in _components_world(conn, n) for i in (row.lo, row.hi)},
               Limits(), result, Rulings())
    counts = {
        "neighbours": conn.statements.count(RT_MERGE_NEIGHBOURS_SQL),
        "pairs_within": conn.statements.count(RT_PAIRS_WITHIN_SQL),
        "clusters_touching": conn.statements.count(RT_CLUSTERS_TOUCHING_SQL),
    }
    return store.statements, counts, result


def test_the_re_cluster_costs_the_same_statements_whatever_the_component_count(world) -> None:
    """W9f's first live pass re-clustered 700 components in 7,131 serial statements — 682 s of
    a 876 s pass at ~96 ms of round trip each — because every component paid for its own fact
    read, `pairs_within`, `clusters_touching` and writes. The components are computed in
    memory now and the reads and writes are batched across all of them."""
    small, small_counts, small_result = _recluster_cost(FakePg(), 2)
    large, large_counts, large_result = _recluster_cost(FakePg(), 12)

    assert small_result.components == 2 and large_result.components == 12
    assert small_result.clusters_written == 2 and large_result.clusters_written == 12
    assert small_counts == large_counts == {"neighbours": 1, "pairs_within": 1,
                                            "clusters_touching": 1}
    assert small == large, "the statement count must not grow with the components"
    assert large < 12, "and it is a handful, not ten a component"


# ------------------------------------------------------- the re-cut inside the pass's time (A5/B6)


def test_a_cut_inside_a_pass_never_outlives_its_deadline(world, tmp_path, monkeypatch) -> None:
    """Review A5/B6: the pHash aggregate is a sequential scan of public.images (~13.3M rows);
    inside a pass every statement of the cut is bounded by the time left, and past the deadline
    the cut refuses between chunks instead of running on."""
    import time as _time

    _seed(world, tmp_path)
    timeouts: list[int] = []
    original = incremental_lane._exec

    def spy(conn, sql, params=None):
        if sql == incremental_lane.RT_STATEMENT_GUARD_SQL:
            timeouts.append(int(params["statement_timeout_ms"]))
        return original(conn, sql, params)

    monkeypatch.setattr(incremental_lane, "_exec", spy)
    incremental_lane.cut_calibration(world, Settings(), None, GENERATION,
                                     deadline=_time.perf_counter() + 20.0)
    assert timeouts and max(timeouts) <= 20_000, "never past the 20 s the pass had left"
    with pytest.raises(incremental_lane.CalibrationRefusal, match="ran out"):
        incremental_lane.cut_calibration(world, Settings(), None, GENERATION,
                                         deadline=_time.perf_counter() - 1.0)


def test_a_re_cut_that_fails_rolls_back_says_why_and_waits_its_age(world, tmp_path,
                                                                     monkeypatch) -> None:
    """A failed re-cut is not retried on the next tick: an attempt counts as a cut for
    `RECUT_MIN_AGE_H` (E944), so a cut too long for its tick costs one tick per period, not
    every other tick."""
    _built(world, tmp_path)
    for row in world.image_rows:
        row["phash"] = int(row["phash"]) + 7
    for name, value in (("RECUT_MIN_IMAGES", 1), ("RECUT_MIN_AGE_H", 1.0)):
        monkeypatch.setattr(incremental_lane, name, value)
    before = dict(world.phash_pop)

    def cancelled(*_a, **_k):
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(incremental_lane, "cut_calibration", cancelled)
    assert run_incremental(lambda: world)["population"]["recut_due"]
    out = run_incremental(lambda: world)
    assert "statement timeout" in out["recut"]["skipped"] and out["skipped"] == "recut"
    assert world.phash_pop == before
    again = run_incremental(lambda: world)
    assert again["aborted"] == "" and "recut_due" not in again["population"]
    assert "recut" not in run_incremental(lambda: world), "tried within the hour: no re-cut"


# ------------------------------------- E944: read once a digest; the re-cut is a tick of its own


def _edit(world: FakePg, listing_id: int, words: str, snapshot_id: int) -> None:
    """A body edit the changed feed claims: the text and a new snapshot row."""
    world.listings[listing_id]["description"] += " " + words
    world.snapshots.append({"id": snapshot_id, "listing_id": listing_id,
                            "scraped_at": world.now - timedelta(days=1), "price_czk": 6_500_000})


def test_the_calibration_read_once_a_digest_decides_as_a_fresh_read_every_pass(
        tmp_path, monkeypatch) -> None:
    """E944's equivalence gate. Two seeded worlds in lockstep — one lane keeps the calibration it
    parsed between ticks, the other parses it afresh every tick — hold the same rows after every
    tick, the reject ledger included (E943). Between ticks both are disturbed alike: body edits
    the changed feed claims, re-cuts that leave the digest where it was and one that moves it.
    The kept calibration's payload is read only when the digest moved; after the move both
    lanes key the ledger on the new digest alike; and no pass writes into the calibration it
    was handed."""
    from tests.autodedup.test_reject_ledger import _db_state, _seeded_reject_world

    for name, value in (("COVERAGE_FLOOR", 1.5), ("RECUT_MIN_IMAGES", 1),
                        ("RECUT_MIN_AGE_H", 0.0)):
        monkeypatch.setattr(incremental_lane, name, value)
    hot, cold = _seeded_reject_world(tmp_path / "hot"), _seeded_reject_world(tmp_path / "cold")
    for world in (hot, cold):
        world.settings[bootstrap_key(GENERATION)] = False
    kept: dict = {}
    payloads: dict[int, list[bool]] = {id(hot): [], id(cold): []}
    original = incremental_lane._rows

    def spy(conn, sql, params=None):
        out = original(conn, sql, params)
        if sql == incremental_lane.RT_CALIBRATION_READ_SQL:
            payloads[id(conn)].append(out[0][3] is not None)
        return out

    monkeypatch.setattr(incremental_lane, "_rows", spy)

    def tick(world: FakePg) -> dict:
        monkeypatch.setattr(incremental_lane, "_CALIBRATIONS", kept if world is hot else {})
        return run_incremental(lambda: world)

    disturb = {2: (4005, "nova kuchyne a lodzie", 10_001),
               4: (4006, "sklep a parkovani v cene", 10_002),
               6: (4007, "tramvaj za rohem", 10_003)}
    digests, outs = [], []
    for step in range(9):
        if step in disturb:
            for world in (hot, cold):
                _edit(world, *disturb[step])
        a, b = tick(hot), tick(cold)
        for out in (a, b):        # what a clock or the process's memory says
            for part, keys in ((out, ("elapsed_s", "rss_mb", "peak_rss_mb", "memo_entries")),
                               (out.get("recut") or {}, ("seconds", "at", "rss_mb", "peak_rss_mb")),
                               (out.get("counts") or {}, ("timings",)),
                               (out.get("reconcile") or {}, ("seconds",))):
                for key in keys:
                    part.pop(key, None)
        assert _db_state(hot) == _db_state(cold), step
        assert hot.reject_ledger == cold.reject_ledger and hot.phash_pop == cold.phash_pop, step
        assert hot.calibration[GENERATION]["digest"] == cold.calibration[GENERATION]["digest"]
        assert a.get("counts") == b.get("counts") and a.get("recut") == b.get("recut"), step
        digests.append(hot.calibration[GENERATION]["digest"])
        outs.append(a)

    assert [bool(out.get("recut")) for out in outs] == [False, True] * 4 + [False]
    moved = [out["recut"]["moved"] for out in outs if out.get("recut")]
    assert moved == [False, True, True, True], moved
    assert payloads[id(cold)] == [True] * 9
    assert payloads[id(hot)] == [True, False, False, False, True, False, True, False, True], (
        "the kept calibration is read again only on the tick after its digest moved")
    assert {row["calibration_digest"] for row in hot.reject_ledger.values()} == {digests[-1]}, (
        "the last pass keyed its rejects on the digest the re-cut before it moved to")
    stored = json.loads(zlib.decompress(hot.calibration[GENERATION]["payload_zlib"]))
    assert kept[GENERATION][0] == digests[-1]
    assert kept[GENERATION][1].to_json() == Calibration.from_json(stored).to_json(), (
        "nothing writes into the calibration a pass was handed")


def _drifted(world: FakePg, monkeypatch, min_age_h: float = 0.0) -> None:
    """Every photograph re-hashed since the cut, and the re-cut's gates opened."""
    for row in world.image_rows:
        row["phash"] = int(row["phash"]) + 7
    for name, value in (("RECUT_MIN_IMAGES", 1), ("RECUT_MIN_AGE_H", min_age_h)):
        monkeypatch.setattr(incremental_lane, name, value)


def _recut_row(world: FakePg) -> dict:
    return dict(world.settings.get(incremental_lane.recut_key(GENERATION)) or {})


def test_a_re_cut_never_runs_in_a_build_or_over_a_scope_no_tick_could_cut(
        world, tmp_path, monkeypatch) -> None:
    """E944 (review 1): the seed cut the calibration a build runs under, and a scope above
    RECUT_MAX_LISTINGS cannot be cut inside one tick, so in either case the drift is recorded
    with why — never marked due, never a tick spent on a cut that fails."""
    _seed(world, tmp_path)
    _drifted(world, monkeypatch)

    built = run_incremental(lambda: world)
    assert built["bootstrap"]["active"] and built["population"]["recut_due"] is False
    assert built["recut"]["skipped"] == "bootstrap" and _recut_row(world)["due"] is None
    world.settings[bootstrap_key(GENERATION)] = False
    monkeypatch.setattr(incremental_lane, "RECUT_MAX_LISTINGS", len(world.listings) - 1)

    unfit = run_incremental(lambda: world)
    assert unfit["recut"] == {**unfit["recut"], "skipped": "unfit",
                              "scope_listings": len(world.listings)}
    assert _recut_row(world)["due"] is None and _recut_row(world)["tried_at"]
    assert "counts" in run_incremental(lambda: world), "the next tick passes"
    monkeypatch.setattr(incremental_lane, "RECUT_MAX_LISTINGS", len(world.listings))

    assert run_incremental(lambda: world)["population"]["recut_due"] is True
    assert run_incremental(lambda: world)["recut"]["hashes_measured"] > 0, "fit: it re-cuts"


def test_a_due_stamped_under_an_older_cut_is_void(world, tmp_path, monkeypatch) -> None:
    """A seed or a cut between the pass that found the drift and the tick that would re-cut
    rewrites `built_at`; the drift was measured against a calibration no longer in force."""
    _built(world, tmp_path)
    _drifted(world, monkeypatch)
    assert run_incremental(lambda: world)["population"]["recut_due"]
    due = _recut_row(world)["due"]
    world.calibration[GENERATION]["built_at"] = world.now + timedelta(minutes=5)

    out = run_incremental(lambda: world)

    assert out.get("skipped") != "recut" and "counts" in out, "a pass, not a re-cut"
    assert _recut_row(world)["due"] == (world.now + timedelta(minutes=5)).isoformat() != due


def test_a_shutdown_mid_cut_takes_the_attempt_back(world, tmp_path, monkeypatch) -> None:
    """E944 (review 3): the SIGTERM path cancels the statement in flight, which the cut sees as
    a plain error. The attempt is taken back — the row as it was, due kept, no wait stamped — so
    the next worker re-cuts at once; nothing reads it as a failed cut."""
    _built(world, tmp_path)
    _drifted(world, monkeypatch, min_age_h=1.0)
    assert run_incremental(lambda: world)["population"]["recut_due"]
    before, population = _recut_row(world), dict(world.phash_pop)
    signal = {"on": False}

    class QueryCanceled(Exception):
        pass

    def cancelled(*_a, **_k):
        signal["on"] = True
        raise QueryCanceled("canceling statement due to user request")

    original = incremental_lane.cut_calibration
    monkeypatch.setattr(incremental_lane, "cut_calibration", cancelled)
    out = run_incremental(lambda: world, stopping=lambda: signal["on"])

    assert out["skipped"] == "recut" and out["recut"]["skipped"].startswith("QueryCanceled")
    assert "failed" not in out["recut"]
    assert _recut_row(world) == before and world.phash_pop == population
    monkeypatch.setattr(incremental_lane, "cut_calibration", original)
    again = run_incremental(lambda: world)
    assert again["skipped"] == "recut" and again["recut"]["hashes_measured"] > 0


def test_a_worker_that_dies_mid_cut_waits_its_age_and_keeps_its_claim_cap(
        world, tmp_path, monkeypatch) -> None:
    """E944 (review 2): the attempt is stamped before the cut, so a worker the kernel kills
    inside it (no `finally`, no release) leaves `cutting`. Its successor releases the lease
    WITHOUT halving the claim cap (the claim did not kill it), records the death as a failed
    re-cut and waits RECUT_MIN_AGE_H instead of re-cutting into the same death."""
    import os
    import socket

    from autodedup.incremental_lane import LANE_NAME, claim_cap_key

    _built(world, tmp_path)
    _drifted(world, monkeypatch, min_age_h=1.0)
    booted = 1_800_000_000
    dead = f"{socket.gethostname()}:{os.getpid()}:{booted - 100}"
    assert run_incremental(lambda: world)["population"]["recut_due"]
    cap = dict(world.settings[claim_cap_key(GENERATION)])

    class Killed(BaseException):
        pass

    def killed(*_a, **_k):
        raise Killed()

    original = incremental_lane.cut_calibration
    monkeypatch.setattr(incremental_lane, "cut_calibration", killed)
    with pytest.raises(Killed):
        run_incremental(lambda: world, holder=dead)
    assert _recut_row(world)["cutting"] == dead and _recut_row(world)["due"]
    world.lease[LANE_NAME] = {"holder": dead, "expires_at": world.now + timedelta(minutes=30)}
    monkeypatch.setattr(incremental_lane, "cut_calibration", original)

    out = run_incremental(lambda: world, holder=f"{socket.gethostname()}:{os.getpid()}:"
                                                f"{booted + 60}", booted_epoch=booted)

    assert out["predecessor_released"] == dead
    assert world.settings[claim_cap_key(GENERATION)] == cap, "no halving for a cut's death"
    assert "counts" in out and out["recut"]["failed"], "a pass, the death reported"
    row = _recut_row(world)
    assert "cutting" not in row and row["last"]["skipped"].startswith("died mid-cut")


def test_the_cut_stops_reading_its_write_reserve_before_the_deadline(world, tmp_path) -> None:
    """E944 (review 6): Calibration.build, the population's upserts and the row's write keep
    `reserve_s` of the deadline; the reads stop there."""
    import time as clock_module

    _seed(world, tmp_path)
    with pytest.raises(incremental_lane.CalibrationRefusal, match="ran out"):
        incremental_lane.cut_calibration(world, Settings(), None, GENERATION,
                                         deadline=clock_module.perf_counter() + 30.0,
                                         reserve_s=60.0)
    out = incremental_lane.cut_calibration(world, Settings(), None, GENERATION,
                                           deadline=clock_module.perf_counter() + 30.0,
                                           reserve_s=incremental_lane.RECUT_WRITE_RESERVE_S / 10)
    assert out["scope_listings"] == len(world.listings)


def test_a_put_back_that_lands_while_the_next_tick_waits_is_read_under_the_lease(
        world, tmp_path, monkeypatch) -> None:
    """E944 (review 2, race 1): worker B reads `rt_recut` while A's stamp (`cutting: A`) is still
    there; A is SIGTERMed, puts its row back and lets the lease go; B takes it. B decides on the
    row it reads UNDER the lease — A's attempt taken back, so B re-cuts at once — and never on
    its earlier copy, which would record a death that did not happen, wait 6 h and report an
    error."""
    _built(world, tmp_path)
    _drifted(world, monkeypatch, min_age_h=1.0)
    assert run_incremental(lambda: world)["population"]["recut_due"]
    key = incremental_lane.recut_key(GENERATION)
    taken_back = _recut_row(world)
    world.settings[key] = {**taken_back, "tried_at": "2026-10-16T09:00:00+00:00",
                           "cutting": "worker-a:1:1"}        # A's stamp, as B reads it
    original = incremental_lane.take_lease

    def a_puts_back_then_b_takes(conn, holder, *args, **kwargs):
        world.settings[key] = dict(taken_back)                 # A's put-back lands
        return original(conn, holder, *args, **kwargs)

    monkeypatch.setattr(incremental_lane, "take_lease", a_puts_back_then_b_takes)
    out = run_incremental(lambda: world)

    assert out["skipped"] == "recut" and out["recut"]["hashes_measured"] > 0, out.get("recut")
    assert "failed" not in out["recut"] and "deaths" not in _recut_row(world)


def test_a_stopped_cut_never_puts_back_over_the_next_holders_stamp(
        world, tmp_path, monkeypatch) -> None:
    """E944 (review 2, race 2): A's re-cut is SIGTERMed; before its put-back runs, B has taken
    the released lease and stamped its own attempt. A's put-back is conditional on its own stamp
    (`cutting` = A), so B's stays: were B to die in its cut, its successor still sees one."""
    _built(world, tmp_path)
    _drifted(world, monkeypatch, min_age_h=1.0)
    assert run_incremental(lambda: world)["population"]["recut_due"]
    key = incremental_lane.recut_key(GENERATION)
    signal = {"on": False}

    class QueryCanceled(Exception):
        pass

    def b_stamps_while_a_is_cancelled(*_a, **_k):
        stamp = {**_recut_row(world), "cutting": "worker-b:1:2"}
        world.settings[key] = stamp
        world.tx_state["settings"][key] = stamp     # B's session committed it: no rollback here
        signal["on"] = True
        raise QueryCanceled("canceling statement due to user request")

    monkeypatch.setattr(incremental_lane, "cut_calibration", b_stamps_while_a_is_cancelled)
    out = run_incremental(lambda: world, holder="worker-a:1:1", stopping=lambda: signal["on"])

    assert out["recut"]["skipped"].startswith("QueryCanceled")
    assert _recut_row(world)["cutting"] == "worker-b:1:2", "B's stamp survives A's put-back"


def test_a_cut_that_kills_its_worker_twice_is_not_tried_again_until_a_seed(
        world, tmp_path, monkeypatch) -> None:
    """E944 (review 2, 3): one death waits RECUT_MIN_AGE_H (a deploy's SIGKILL reads the same);
    a second under the same calibration makes the cut `died` — no third worker restart."""
    _built(world, tmp_path)
    _drifted(world, monkeypatch)
    key = incremental_lane.recut_key(GENERATION)
    world.settings[key] = {"tried_at": "2026-10-15T00:00:00+00:00", "cutting": "worker:1:1"}

    once = run_incremental(lambda: world)
    assert once["recut"]["deaths"] == 1 and once["population"]["recut_due"] is True

    world.settings[key] = {**_recut_row(world), "cutting": "worker:1:2"}
    twice = run_incremental(lambda: world)
    assert twice["recut"]["skipped"] == "died" and _recut_row(world)["deaths"] == 2
    assert _recut_row(world)["due"] is None and "counts" in twice
    assert "counts" in run_incremental(lambda: world), "no re-cut tick follows"
    world.calibration[GENERATION]["built_at"] = world.now + timedelta(hours=1)    # a seed
    assert run_incremental(lambda: world)["population"]["recut_due"] is True
