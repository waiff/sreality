"""A10 (E912): the lane cuts its calibration and its pHash population from the database, and
reads the facts the calibration was cut from (W9g's gate, answered by construction).

The world is `lane_world`'s: nine listings in one town, consecutive ones sharing their
photographs and one catalogue frame on all of them, in the FakePg's `public`. It is driven end
to end — `rt_seed` then `run_incremental` — because the three defects W9f shipped were all in
the seam between them: a population table with no writer, a generation seeded and run on a
scorer nobody chose, and a re-cluster spelled per component.
"""

from __future__ import annotations

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
)
from autodedup.incremental_lane import SqlFacts, SqlStore, run_incremental, run_rt_seed
from autodedup.incremental_sql import (
    RT_CLUSTERS_TOUCHING_SQL,
    RT_MERGE_NEIGHBOURS_SQL,
    RT_PAIRS_WITHIN_SQL,
)
from autodedup.settings import Settings
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.lane_world import GENERATION, SCOPE, seed_lane, true_population
from tests.autodedup.lane_world import world as lane_world


@pytest.fixture()
def world() -> FakePg:
    return lane_world()


def _seed(conn: FakePg, tmp_path: Any, **args: Any) -> dict[str, Any]:
    return seed_lane(conn, tmp_path, **args)


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
    UNKNOWN (E91), the pass says how much of what it read that was, and below the floor it
    re-cuts — so the table follows the corpus instead of aging out in 14 days (A10)."""
    _seed(world, tmp_path)
    for row in world.image_rows:
        row["phash"] = int(row["phash"]) + 7
    for name, value in (("RECUT_MIN_IMAGES", 1), ("RECUT_MIN_AGE_H", 0.0)):
        monkeypatch.setattr(incremental_lane, name, value)
    before = world.calibration[GENERATION]["digest"]

    out = run_incremental(lambda: world)

    assert out["population"]["images_unmeasured"] >= 1
    assert out["population"]["coverage"] < incremental_lane.COVERAGE_FLOOR
    assert out["recut"]["hashes_measured"] > 0
    assert all(int(row["phash"]) in world.phash_pop for row in world.image_rows)
    assert world.calibration[GENERATION]["digest"] == before, (
        "a re-hash moves the population, not the cohort statistics (E915: not a generation)")
    again = run_incremental(lambda: world)
    assert again["population"]["images_unmeasured"] == 0 and "recut" not in again


def test_a_re_cut_waits_out_its_minimum_age(world, tmp_path, monkeypatch) -> None:
    _seed(world, tmp_path)
    for row in world.image_rows:
        row["phash"] = int(row["phash"]) + 7
    monkeypatch.setattr(incremental_lane, "RECUT_MIN_IMAGES", 1)
    monkeypatch.setattr(incremental_lane, "RECUT_MIN_AGE_H", 10 ** 6)
    out = run_incremental(lambda: world)
    assert out["population"]["coverage"] < incremental_lane.COVERAGE_FLOOR
    assert "recut" not in out


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
    store = SqlStore(world, GENERATION, model_version="w6_gold", calibration_digest="abc123")
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
    store = SqlStore(conn, GENERATION)
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


def test_a_re_cut_that_fails_rolls_back_and_the_pass_still_reports(world, tmp_path,
                                                                    monkeypatch) -> None:
    _seed(world, tmp_path)
    for row in world.image_rows:
        row["phash"] = int(row["phash"]) + 7
    for name, value in (("RECUT_MIN_IMAGES", 1), ("RECUT_MIN_AGE_H", 0.0)):
        monkeypatch.setattr(incremental_lane, name, value)
    before = dict(world.phash_pop)

    def cancelled(*_a, **_k):
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(incremental_lane, "cut_calibration", cancelled)
    out = run_incremental(lambda: world)
    assert "statement timeout" in out["recut"]["skipped"] and out["aborted"] == ""
    assert world.phash_pop == before
