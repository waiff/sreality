"""W9g: the live lane reads the EXPORT's facts, and a rail says so every pass (E84/E83a).

The world is `test_parity`'s: one cohort built twice, the artifact through the export's record
builders and the live side through `SqlFacts` over the same fake `public` rows. Here it is
driven end to end — `rt_seed` then `run_incremental` — because the three defects W9f shipped
were all in the seam between them: a frozen population table with no writer, a generation
seeded and run on a scorer nobody chose, and a re-cluster spelled per component.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autodedup.incremental import Limits, PairRow, PassResult, _recluster, _Working
from autodedup.incremental_lane import (
    ENV_FLAG,
    SqlFacts,
    SqlStore,
    parity_baseline_key,
    run_incremental,
    run_rt_seed,
)
from autodedup.incremental_sql import (
    RT_CLUSTERS_TOUCHING_SQL,
    RT_MERGE_NEIGHBOURS_SQL,
    RT_PAIRS_WITHIN_SQL,
)
from autodedup.settings import Settings
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_parity import (
    EXPORTED_AT,
    GENERATION,
    seed,
    seed_calibration,
    true_population,
    write_artifact,
)

SCOPE = "obec:563510"
SEED_ARGS = {"settings": "default", "model": "prior", "rt_scope": SCOPE}


@pytest.fixture()
def world(tmp_path: Path):
    """A matched cohort: `conn` holds `public`, `artifact` is the export of exactly that."""
    from datetime import timedelta

    conn = FakePg(now=EXPORTED_AT + timedelta(days=1))
    seed(conn)
    seed_calibration(conn, Settings())
    artifact = write_artifact(conn, tmp_path / "cohort.jsonl.gz", true_population(conn))
    # A seeded generation is refused a second seed unless it is asked for by name; the fixture
    # above writes the calibration row, so every seed below means it.
    conn.calibration.pop(GENERATION, None)
    return conn, artifact


def _seed(conn: FakePg, artifact: Path, tmp_path: Path, **args: Any) -> dict[str, Any]:
    return run_rt_seed(lambda: conn, {"artifact": str(artifact), **SEED_ARGS, **args},
                       tmp_path)


def _pass(conn: FakePg, tmp_path: Path, monkeypatch, **args: Any) -> dict[str, Any]:
    monkeypatch.setenv(ENV_FLAG, "true")
    return run_incremental(lambda: conn, {"rt_scope": SCOPE, **args}, tmp_path)


# --------------------------------------------------- E84: the frozen population has a WRITER


def test_the_seed_materialises_the_calibrations_own_phash_population(world, tmp_path) -> None:
    """`autodedup.phash_pop` held 0 rows in production and NO code in the repository wrote it.
    The number the live lane needs is the one the export already measured, so the seed copies
    it out of the artifact — no scan of `public.images`, and the lane joins against exactly the
    statistic the batch engine scored with."""
    conn, artifact = world
    conn.phash_pop.clear()

    out = _seed(conn, artifact, tmp_path)

    assert conn.phash_pop == true_population(conn)
    assert out["population"]["hashes_written"] == len(conn.phash_pop) > 0
    assert out["population"]["images_with_phash"] == len(conn.image_rows)
    # And with it the lane reads a MEASURED population again.
    images = SqlFacts(conn).facts([4_000])[4_000][1]
    assert all(image.pop_is_measured() for image in images)
    assert max(image.pop or 0 for image in images) == len(conn.listings), "the catalogue photo"


def test_a_seed_from_an_artifact_with_no_measured_population_is_refused(world, tmp_path):
    """An export whose population probe timed out writes every `pop` null. Seeding from it
    would freeze a generation in which no K-C certificate can ever fire."""
    conn, _artifact = world
    blind = write_artifact(conn, tmp_path / "blind.jsonl.gz", pop={})
    with pytest.raises(SystemExit) as raised:
        _seed(conn, blind, tmp_path)
    assert "MEASURED" in str(raised.value)
    assert not conn.calibration.get(GENERATION), "the refusal left nothing behind"


# ------------------------------------------------------------- E84: parity is a permanent GATE


def test_the_seed_refuses_when_the_live_facts_are_not_the_artifacts(world, tmp_path) -> None:
    conn, artifact = world
    conn.listings[4_000]["price_czk"] = 9_999_000  # no snapshot: this is not drift

    with pytest.raises(SystemExit) as raised:
        _seed(conn, artifact, tmp_path)
    message = str(raised.value)
    assert "PARITY GATE" in message and "stored" in message
    assert not conn.calibration.get(GENERATION) and not conn.phash_pop


def test_the_seed_writes_the_baseline_the_pass_re_checks(world, tmp_path) -> None:
    conn, artifact = world
    out = _seed(conn, artifact, tmp_path)

    payload = conn.settings[parity_baseline_key(GENERATION)]
    assert payload["n"] == len(payload["rows"]) == len(conn.listings)
    assert payload["exported_at"] == EXPORTED_AT.isoformat()
    assert payload["phash_pop_rows"] == len(conn.phash_pop)
    report = out["parity"]
    assert report["ok"] and report["breaches"] == 0
    assert report["checked"] == len(conn.listings) and report["tolerance"] == 0


def test_a_pass_reports_the_parity_numbers_it_checked(world, tmp_path, monkeypatch) -> None:
    conn, artifact = world
    _seed(conn, artifact, tmp_path)

    out = _pass(conn, tmp_path, monkeypatch)

    assert out["parity"]["ok"] and out["parity"]["breaches"] == 0
    assert out["parity"]["checked"] > 0
    assert out["population"]["images_unmeasured"] == 0
    assert out["population"]["phash_pop_rows"] == len(conn.phash_pop)
    assert json.loads(Path(tmp_path, "incremental.json").read_text())["parity"]["ok"]


def test_a_pass_whose_facts_moved_refuses_and_moves_no_cursor(world, tmp_path, monkeypatch):
    """Loud, non-zero, nothing written, cursor unmoved — the shape W9f's live pass should have
    had instead of 15,923 rows scored on facts nobody had checked."""
    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    cursors = {name: dict(row) for name, row in conn.cursors.items()}
    conn.listings[4_003]["description"] = "a description nothing snapshotted"

    with pytest.raises(SystemExit) as raised:
        _pass(conn, tmp_path, monkeypatch)

    assert "PARITY GATE" in str(raised.value)
    assert conn.cursors == cursors, "a refused pass moves no cursor"
    assert not conn.pairs and not conn.rt_fp
    assert all(row["expires_at"] <= conn.now for row in conn.lease.values()), \
        "the refusal released the lease on its way out"


def test_a_pass_refuses_when_the_frozen_population_has_been_emptied(world, tmp_path,
                                                                   monkeypatch) -> None:
    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    conn.phash_pop.clear()

    with pytest.raises(SystemExit) as raised:
        _pass(conn, tmp_path, monkeypatch)
    assert "phash_pop is EMPTY" in str(raised.value)


def test_a_generation_with_no_baseline_at_all_is_refused(world, tmp_path, monkeypatch) -> None:
    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    conn.settings.pop(parity_baseline_key(GENERATION))

    with pytest.raises(SystemExit) as raised:
        _pass(conn, tmp_path, monkeypatch)
    assert "no fact baseline" in str(raised.value)


def test_a_re_run_pHash_job_is_producer_movement_not_a_breach(world, tmp_path, monkeypatch):
    """The pHash and CLIP producers re-run over the corpus on their own cadence: 557 of 2,847
    images in the live sample carried a hash, a vector or a tag set the export had not. That is
    the corpus moving under a frozen calibration, not the lane reading a fact two ways."""
    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    for row in conn.image_rows:
        if row["listing_id"] == 4_002:
            row["phash"] = int(row["phash"]) + 7

    out = _pass(conn, tmp_path, monkeypatch, rt_parity_sample="99")

    assert out["parity"]["ok"] and out["parity"]["breaches"] == 0
    assert out["parity"]["producer_moved"] == 1
    # A hash the frozen population never saw is UNKNOWN, and the pass says how many.
    assert out["population"]["images_unmeasured"] >= 1


def test_a_re_sighted_listing_is_the_scrapers_clock_not_a_breach(world, tmp_path, monkeypatch):
    """Rule #4 bumps `last_seen_at` on every index sighting and rule #3 flips `is_active`, and
    neither appends a snapshot. Live, 78 of 200 sampled listings had a newer `last_seen_at` and
    74 of those carried no newer snapshot — a gate that read the sighting clock as a stored
    fact would refuse every re-seed of a corpus that is still being scraped."""
    from datetime import timedelta

    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    for listing_id in (4_000, 4_001, 4_002):
        conn.listings[listing_id]["last_seen_at"] = EXPORTED_AT + timedelta(days=1)
    conn.listings[4_004]["is_active"] = False
    conn.listings[4_004]["inactive_at"] = EXPORTED_AT + timedelta(days=1)

    out = _pass(conn, tmp_path, monkeypatch, rt_parity_sample="99")
    assert out["parity"]["ok"] and out["parity"]["breaches"] == 0
    assert out["parity"]["sighting_moved"] == 4


def test_a_listing_changed_since_the_export_is_drift_not_a_breach(world, tmp_path, monkeypatch):
    from datetime import timedelta

    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    conn.listings[4_005]["price_czk"] = 5_000_000
    conn.snapshots.append({"id": 99_999, "listing_id": 4_005,
                           "scraped_at": EXPORTED_AT + timedelta(hours=6),
                           "price_czk": 5_000_000})

    out = _pass(conn, tmp_path, monkeypatch, rt_parity_sample="99")
    assert out["parity"]["ok"] and out["parity"]["drifted_skipped"] == 1


# ------------------------------------------------------- E83a: WHICH scorer took the decision


def test_a_seed_that_names_no_scorer_is_refused(world, tmp_path) -> None:
    """W9f's seed AND its first live pass both ran on empty arguments, so the uncalibrated
    prior scored a generation the operator believed was `w8` + `w6_gold`."""
    conn, artifact = world
    with pytest.raises(SystemExit) as raised:
        run_rt_seed(lambda: conn, {"artifact": str(artifact), "rt_scope": SCOPE}, tmp_path)
    assert "settings=" in str(raised.value) and "model=" in str(raised.value)


def test_a_settings_row_is_named_not_pathed(world, tmp_path) -> None:
    """`settings=w8` was read as a PATH by this lane and named no file it could read — which is
    why the recipe that shipped passed nothing at all."""
    conn, artifact = world
    out = _seed(conn, artifact, tmp_path, settings="w8", model="w6_gold")
    assert out["settings_name"] == "w8" and out["model_version"] == "w6_gold"
    assert conn.calibration[GENERATION]["model_version"] == "w6_gold"


def test_the_pass_runs_the_generations_scorer_and_refuses_another(world, tmp_path, monkeypatch):
    conn, artifact = world
    _seed(conn, artifact, tmp_path, settings="w8", model="w6_gold")

    out = _pass(conn, tmp_path, monkeypatch)
    assert out["model_version"] == "w6_gold"
    assert out["settings"]["t_lo"] == Settings.from_json("autodedup/settings/w8.json").t_lo

    with pytest.raises(SystemExit) as raised:
        _pass(conn, tmp_path, monkeypatch, settings="default")
    assert "re-seed" in str(raised.value)


def test_every_stored_pair_names_the_scorer_that_decided_it(world, tmp_path) -> None:
    """All 15,923 rows of the live generation carried a NULL `model_version` against `w6_gold`
    on the batch generation's, because the lane wrote `evidence["_model"]` — a key nothing has
    ever set."""
    conn, _artifact = world
    store = SqlStore(conn, GENERATION, model_version="w6_gold", calibration_digest="abc123")
    store.upsert_pairs([PairRow(lo=4_000, hi=4_001, probes=["img"], from_lo=True,
                                from_hi=True, zone="merge", score=0.99, families=["IMG"],
                                certificate="K-C", veto="", reason="r", evidence={},
                                context={"block": "o1"}, fp_lo="a", fp_hi="b")])
    store.flush()
    row = conn.pairs[(GENERATION, 4_000, 4_001)]
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
               Limits(), result, frozenset())
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
