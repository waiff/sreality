"""W5, the one lane: the worker's pass clusters as the batch pass does and reconciles production.

F2 (E909): the lane's re-cluster reads the D43 relation the batch pass reads, off the same stored
rows. Must-links (Decision 8, E910): the operator's `same` rulings bind the clustering. The
reconcile (A9, E911) and the in-DB calibration (A10, E912) have their own sections below.
"""

from __future__ import annotations

from dataclasses import replace

from autodedup.d43 import relation_for
from autodedup.dataset import Dataset, Image, Meta
from autodedup.incremental import Limits, PairRow, PassResult, _recluster, _Working
from autodedup.incremental_lane import SqlStore
from autodedup.incremental_store import MemoryStore
from autodedup.indistinguishable import FEATURE_SLOTS
from autodedup.replay import DatasetFacts
from autodedup.settings import Settings
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_incremental import _listing

A, B, C = 101, 202, 303
D43_ON = Settings(d43_cluster_invariant=True)


def _floors_cohort() -> Dataset:
    """Three adverts of one flat as the portals state it: A in a 5-storey house, B silent, C in
    an 8-storey one. A-B and B-C carry no fact; A-C does, and no fingerprint invariant reads a
    house's height — only the cluster relation can see it."""
    stated = {A: 5, B: None, C: 8}
    listings = {i: _listing(i, total_floors=total) for i, total in stated.items()}
    images = {i: [Image(listing_id=i, image_id=i * 10, seq=0, phash=7_000, pop=1)]
              for i in listings}
    return Dataset(meta=Meta(), listings=listings, images_by_listing=images)


def _pair(lo: int, hi: int, zone: str, score: float, **kw) -> PairRow:
    return PairRow(lo=lo, hi=hi, probes=["attr_area"], from_lo=True, from_hi=True, zone=zone,
                   score=score, families=["ATTR", "TXT"], certificate=None, veto=None,
                   reason="model", evidence={}, context={}, fp_lo="a", fp_hi="b", **kw)


def _edges(store) -> None:
    store.upsert_pairs([_pair(A, B, "merge", 0.99), _pair(B, C, "merge", 0.98),
                        _pair(A, C, "band", 0.5)])


def _recluster_all(store, ds: Dataset, settings: Settings) -> None:
    facts = DatasetFacts(ds)
    _recluster(store, facts, settings, _Working(facts, settings), {A, B, C}, Limits(),
               PassResult(generation="rt", calibration_digest="x"))


# ------------------------------------------------------------------ F2: the relation


def test_the_lane_reclusters_with_the_d43_relation_the_batch_reads() -> None:
    """W29 turns `d43_cluster_invariant` on. The lane called `cluster_pairs` without a
    relation, so A, B and C came out one group — a union the batch pass refuses because A and
    C state different house heights."""
    ds = _floors_cohort()
    loose = MemoryStore()
    _edges(loose)
    _recluster_all(loose, ds, Settings())
    assert sorted(map(sorted, loose.clusters.values())) == [[A, B, C]], "the control"

    strict = MemoryStore()
    _edges(strict)
    _recluster_all(strict, ds, D43_ON)
    groups = sorted(map(sorted, strict.clusters.values()))
    assert [A, B, C] not in groups, "the relation refuses the transitive union"
    assert groups == [[A, B]], "the certificate-first order keeps the stronger edge"


def test_the_sql_store_reads_back_the_three_slots_the_relation_reads() -> None:
    db = FakePg()
    store = SqlStore(db, "rt")
    feats = {name: (1.0, True) for name in FEATURE_SLOTS}
    feats["clip_mean_top3_cos"] = (0.9, True)
    feats["floorplan_conflict"] = (0.0, False)
    store.upsert_pairs([_pair(A, B, "merge", 0.99, feats=feats)])
    (row,) = SqlStore(db, "rt").pairs_within([A, B])
    assert row.slots == {"tag_room_clip_min2": (1.0, True),
                         "phash_tight_matches": (1.0, True)}, (
        "the three slots only, and an absent one reads as no slot — which D43 reads as absent")


def test_the_sql_store_clusters_like_the_twin() -> None:
    ds = _floors_cohort()
    twin = MemoryStore()
    _edges(twin)
    _recluster_all(twin, ds, D43_ON)

    db = FakePg()
    store = SqlStore(db, "rt")
    _edges(store)
    _recluster_all(store, ds, D43_ON)
    stored = {}
    for _g, key, listing_id in db.cluster_members:
        stored.setdefault(key, []).append(listing_id)
    assert sorted(map(sorted, stored.values())) == sorted(map(sorted, twin.clusters.values()))


def test_a_designator_veto_row_is_kept_so_it_still_binds_the_clustering() -> None:
    """E61 names the two units on the row and scores 0. The SQL store evicted it as a reject
    below the floor, so the lane forgot the veto the batch pass unions into must-not-link."""
    db = FakePg()
    store = SqlStore(db, "rt", store_floor=0.02)
    veto = replace(_pair(A, C, "veto", 0.0), veto="unit_designator", reason="guard:x",
                   evidence={"unit_lo": "12", "unit_hi": "14"})
    store.upsert_pairs([veto, _pair(A, B, "reject", 0.001)])
    assert ("rt", A, C) in db.pairs and db.pairs[("rt", A, C)]["guard_veto"] == "unit_designator"
    assert ("rt", A, B) not in db.pairs, "a plain reject below the floor is still not kept"


def test_the_relation_is_the_batch_relation_on_the_same_slots() -> None:
    ds = _floors_cohort()
    relation = relation_for(D43_ON, ds.listings, {})
    assert relation is not None
    assert relation.ok(A, B) and relation.ok(B, C) and not relation.ok(A, C)


# ------------------------------------------------------------------ E910: must-links


def _fps(ds: Dataset, settings: Settings) -> dict:
    from autodedup.fingerprint import build_all

    return build_all(ds, settings)


def _decision(lo: int, hi: int, zone: str, score: float):
    from autodedup.decide import Decision

    return Decision(lo, hi, zone, score, {"ATTR", "TXT"}, None, None, "model")


def _cluster(ds: Dataset, settings: Settings, decisions, **kw):
    from autodedup.cluster import cluster_pairs

    return cluster_pairs(decisions, ds.listings, _fps(ds, settings), settings,
                         kw.pop("must_not_link", frozenset()),
                         relation_for(settings, ds.listings, {}), **kw)


def _groups(result) -> list[list[int]]:
    return sorted(map(sorted, result.clusters.values()))


EDGES = [_decision(A, B, "merge", 0.99), _decision(B, C, "merge", 0.98)]


def test_a_same_ruling_joins_two_adverts_the_engine_never_paired() -> None:
    ds = _floors_cohort()
    for settings in (Settings(), Settings(repartition=True)):
        result = _cluster(ds, settings, [], must_link=frozenset({(A, B)}))
        assert _groups(result) == [[A, B]]
        assert result.stats["n_must_link_closures"] == 1


def test_a_same_ruling_overrides_the_relation_between_its_own_two_adverts() -> None:
    """A and C state different house heights and the operator ruled them one flat anyway: the
    relation may not separate what the ruling joined (Decision 8)."""
    ds = _floors_cohort()
    for settings in (D43_ON, replace(D43_ON, repartition=True)):
        assert _groups(_cluster(ds, settings, EDGES)) == [[A, B]], "the control"
        ruled = _cluster(ds, settings, EDGES, must_link=frozenset({(A, C)}))
        assert _groups(ruled) == [[A, B, C]]


def test_a_same_ruling_exempts_its_pair_from_the_machine_veto_only() -> None:
    ds = _floors_cohort()
    veto = frozenset({(A, B)})
    blocked = _cluster(ds, Settings(), EDGES[:1], machine_vetoes=veto)
    assert _groups(blocked) == [], "E61 refuses the union"
    ruled = _cluster(ds, Settings(), EDGES[:1], machine_vetoes=veto,
                     must_link=frozenset({(A, B)}))
    assert _groups(ruled) == [[A, B]]
    operator = _cluster(ds, Settings(), EDGES[:1], must_not_link=veto,
                        must_link=frozenset({(A, B)}))
    assert _groups(operator) == [], "an operator must-not-link inside the closure still binds"
    assert operator.stats["n_must_link_dissolved"] == 1, "and the contradiction is counted"


def test_the_spreads_are_read_across_closures_only() -> None:
    """Two adverts the operator joined may state areas far apart (a portal typo); a third advert
    is still held to the spread against each of them."""
    listings = {A: _listing(A, area_m2=50.0), B: _listing(B, area_m2=70.0),
                C: _listing(C, area_m2=58.0)}
    ds = Dataset(meta=Meta(), listings=listings, images_by_listing={})
    same = frozenset({(A, B)})
    for settings in (Settings(), Settings(repartition=True)):
        assert _groups(_cluster(ds, settings, [], must_link=same)) == [[A, B]]
        joined = _cluster(ds, settings, [_decision(A, C, "merge", 0.9)], must_link=same)
        assert [A, B, C] not in _groups(joined), "C is 16 % from A and 17 % from B"


def test_the_repartition_moves_a_closure_as_one_node() -> None:
    """Without the ruling the cut keeps A with B; the closure {A, C} cannot be split, so the
    repartition answers with the whole closure or nothing."""
    ds = _floors_cohort()
    settings = replace(D43_ON, repartition=True)
    edges = [_decision(A, B, "merge", 0.99), _decision(B, C, "merge", 0.98)]
    ruled = _cluster(ds, settings, edges, must_link=frozenset({(A, C)}))
    groups = _groups(ruled)
    assert any({A, C} <= set(group) for group in groups)
    assert not any(A in group and C not in group for group in groups)


def _known(store: MemoryStore, *ids: int) -> None:
    from autodedup.incremental import Evidence, FpRow, GuardRow

    for listing_id in ids:
        store.put_listing(listing_id, FpRow(GuardRow(listing_id, "byt", "prodej", 60.0, "2+kk",
                                                      3), "d", "o1", "byt", True, Evidence()),
                          [])


def test_the_lane_walks_must_link_edges_and_clusters_the_closure() -> None:
    ds = _floors_cohort()
    store = MemoryStore()
    _known(store, A, B, C)
    store.ml = {(A, C)}
    _recluster_all(store, ds, D43_ON)
    assert sorted(map(sorted, store.clusters.values())) == [[A, C]]


def test_an_idle_pass_honours_a_new_same_ruling() -> None:
    """A ruling moves no pair, so nothing but the ruling itself can seed its component — the
    must-not-link's E78 rule, for the positive word."""
    from autodedup.incremental import Calibration, run_pass
    from autodedup.model import hand_initialised
    from autodedup.replay import ScheduleWork

    ds = _floors_cohort()
    store = MemoryStore()
    _known(store, A, C)
    store.ml = {(A, C), (B, 999)}
    calibration = Calibration(generation="rt", feature_version=0, built_at="", n_listings=0)
    run_pass(store, DatasetFacts(ds), ScheduleWork([]), D43_ON, hand_initialised(),
             calibration)
    assert sorted(map(sorted, store.clusters.values())) == [[A, C]], (
        "joined; the ruling reaching outside the store (B, 999) links nothing")
    # The operator changes their mind: the newest ruling is `different`, which also writes the
    # must-not-link (api/routes/autodedup.py) — and that seeds the component on the next pass.
    store.ml = set()
    store.mnl = {(A, C)}
    run_pass(store, DatasetFacts(ds), ScheduleWork([]), D43_ON, hand_initialised(),
             calibration)
    assert store.clusters == {}, "the pair the ruling held is released: nothing else joins it"


def test_the_sql_store_reads_the_newest_pair_ruling_only() -> None:
    from autodedup.incremental_sql import RT_MUST_LINK_SQL

    assert "distinct on" in RT_MUST_LINK_SQL and "v.verdict = 'same'" in RT_MUST_LINK_SQL
    db = FakePg()
    db.ml = {(A, C)}
    assert SqlStore(db, "rt").must_link() == {(A, C)}
