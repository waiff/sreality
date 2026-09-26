"""W5, the one lane: the worker's pass clusters as the batch pass does and reconciles production.

F2 (E909): the lane's re-cluster reads the D43 relation the batch pass reads, off the same stored
rows. Must-links (Decision 8, E910): the operator's `same` rulings bind the clustering. The
reconcile (A9, E911) and the in-DB calibration (A10, E912) have their own sections below.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from autodedup import apply as AP
from autodedup import apply_sql as AS
from autodedup import reconcile
from autodedup.d43 import relation_for
from autodedup.dataset import Dataset, Image, Meta
from autodedup.incremental import Limits, PairRow, PassResult, _recluster, _Working
from autodedup.incremental_lane import SqlStore
from autodedup.incremental_sql import (
    RT_CURSOR_READ_SQL,
    RT_CURSOR_SET_SQL,
    RT_SCOPE_SCAN_SEEN_SQL,
)
from autodedup.incremental_store import MemoryStore
from autodedup.indistinguishable import FEATURE_SLOTS
from autodedup.replay import DatasetFacts
from autodedup.settings import Settings
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_apply import FakeDb, _pair_group
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


# ------------------------------------------------------------------ A9: the reconcile

RT = "rt"
BLOCK = "obec:563510"


class LaneDb(FakeDb):
    """test_apply's fake — THE apply path's statements, faked once — plus the lane's own reads
    the reconcile adds: its cursor, the swept groups, which blocks the lane has fully read, and
    the newest ledger row per member set."""

    def __init__(self) -> None:
        super().__init__()
        self.cursors: dict[str, int] = {}
        self.walked: set[str] = {BLOCK}
        self.snapshot: dict[int, str] = {}
        self.read: set[int] = set()

    def dispatch(self, sql: str, p: dict) -> list[tuple]:  # noqa: C901
        if sql == RT_CURSOR_READ_SQL:
            return [(name, self.cursors[name], None, None) for name in p["names"]
                    if name in self.cursors]
        if sql == RT_CURSOR_SET_SQL:
            self.cursors[p["name"]] = int(p["last_listing_id"])
            return []
        if sql == AS.RC_SWEEP_SQL:
            keys = sorted(k for g, k in self.clusters if g == p["generation"]
                          and k > p["after"])
            return [(k,) for k in keys[:p["limit"]]]
        if sql == AS.RC_CLUSTERS_SQL:
            return [row for row in self.dispatch(AS.CLUSTERS_SQL, p) if row[0] in p["keys"]]
        if sql == AS.RC_MEMBERS_SQL:
            return [row for row in self.dispatch(AS.MEMBERS_SQL, p) if row[0] in p["keys"]]
        if sql == AS.RC_GROUP_OF_SQL:
            return [(lid, k) for g, k, lid in self.members
                    if g == p["generation"] and lid in p["listing_ids"]]
        if sql == RT_SCOPE_SCAN_SEEN_SQL:
            return [(block,) for block in sorted(self.walked)]
        if sql == AS.RC_UNREAD_BLOCKS_SQL:
            return sorted({(block,) for lid, block in self.snapshot.items()
                           if lid not in self.read})
        if sql == AS.RC_MEMBER_BLOCKS_SQL:
            return [(lid, block) for lid, block in self.snapshot.items()
                    if lid in p["listing_ids"]]
        if sql == AS.RC_LAST_OUTCOME_SQL:
            newest: dict[tuple[int, ...], tuple] = {}
            for row in self.ledger:
                members = tuple(sorted(row["member_ids"] or ()))
                if (row["generation"] == p["generation"] and not row["dry_run"]
                        and set(members) & set(p["listing_ids"])):
                    newest[members] = (list(members), row["outcome"], row["error"])
            return list(newest.values())
        return super().dispatch(sql, p)


def _lane(db: LaneDb, *, deadline: float = 10 ** 9, touched=(), merge=None) -> dict:
    return reconcile.run(db, RT, list(touched), run_id="rt:test", deadline=deadline,
                         blocks=[BLOCK], merge=merge or db.merge([]), clock=lambda: 0.0)


def _read(db: LaneDb, *lids: int) -> None:
    for lid in lids:
        db.snapshot[lid] = BLOCK
        db.read.add(lid)


def test_the_reconcile_merges_an_rt_group_through_the_chokepoint() -> None:
    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _read(db, 10, 11)
    calls: list = []

    out = _lane(db, merge=db.merge(calls))

    assert out["counts"]["applied"] == 1 and out["counts"]["listings_moved"] == 1
    assert [c["source"] for c in calls] == [AP.MERGE_SOURCE]
    assert calls[0]["markers"]["generation"] == RT
    assert db.listings[11]["property_id"] == 100
    (row,) = [r for r in db.ledger if r["outcome"] == "applied"]
    assert (row["generation"], row["run_id"], row["member_ids"]) == (RT, "rt:test", [10, 11])
    assert db.cursors[reconcile.CURSOR] == 0, "a short sweep wraps the cursor"


def test_the_reconciles_refusals_are_the_batch_applys_refusals() -> None:
    """ONE brain (E903): the same groups under a batch generation and under `rt` are refused
    for the same reasons, whichever path plans them."""
    db = LaneDb()
    db.live_scope()
    for gen in ("g12", RT):
        _pair_group(db, 10, [10, 11], [100, 200], gen=gen)
        _pair_group(db, 20, [20, 21], [300, 400], gen=gen)
        _pair_group(db, 30, [30, 31], [500, 600], gen=gen)
        _pair_group(db, 40, [40, 41], [700, 800], gen=gen)
        _pair_group(db, 50, [50, 51], [900, 901], gen=gen)
        _pair_group(db, 60, [60, 61], [950, 951], gen=gen)
    db.verdicts.append({"kind": "pair", "lo": 10, "hi": 11, "verdict": "different"})
    db.mnl.append((20, 21, "operator"))
    db.listing(31, 600, ct="pronajem")                   # a sale/rental mix
    db.listing(42, 800)                                   # carried, grouped by nobody
    db.listing(52, 901, where=(999999, None))             # carried, located outside
    db.listing(61, None)                                  # unattached member
    _read(db, 10, 11, 20, 21, 30, 31, 40, 41, 50, 51, 60, 61)
    scope = AP.effective_scope(db.settings[AP.SCOPE_SETTING], {}, live=True)

    batch = AP.plan_apply(db, "g12", scope)
    lane = _lane(db)

    assert lane["counts"]["skipped_by_reason"] == batch.counts["skipped_by_reason"]
    assert lane["counts"]["out_of_scope_by_reason"] == batch.counts["out_of_scope_by_reason"]
    assert set(lane["counts"]["skipped_by_reason"]) == {
        AP.SKIP_PAIR_VERDICT, AP.SKIP_MUST_NOT_LINK, AP.SKIP_CARRIES_UNGROUPED,
        AP.SKIP_CARRIES_OUT_OF_SCOPE, AP.SKIP_UNATTACHED}
    assert lane["counts"]["out_of_scope_by_reason"] == {AP.OUT_CATEGORY: 1}
    assert lane["counts"]["applied"] == 0


def test_the_reconcile_never_splits() -> None:
    """A property the stream groups apart is a PROPOSAL (Decision 9): nothing is detached, and
    an operator negative over one property is reported, never acted on."""
    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10], [100], gen=RT)
    db.listing(11, 100)
    _pair_group(db, 20, [20, 21], [300, 300], gen=RT)
    db.verdicts.append({"kind": "pair", "lo": 20, "hi": 21, "verdict": "different"})
    _read(db, 10, 11, 20, 21)
    detached: list = []

    out = _lane(db)

    assert db.listings[11]["property_id"] == 100 and not detached
    assert [g["cluster_key"] for g in out[AP.RULED_AFTER_MERGE]] == [20]
    assert out["counts"]["applied"] == 0


def test_the_reconcile_is_idempotent_and_files_a_waiting_reason_once() -> None:
    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _pair_group(db, 20, [20, 21], [300, 400], gen=RT)
    db.mnl.append((20, 21, "operator"))
    _read(db, 10, 11, 20, 21)

    first = _lane(db)
    rows = len(db.ledger)
    second = _lane(db)

    assert first["counts"]["applied"] == 1 and second["counts"]["applied"] == 0
    assert second["counts"]["already_one_property"] == 1
    assert len(db.ledger) == rows, "the same group waiting on the same reason files nothing"
    assert second["counts"]["skipped_rows_written"] == 0
    db.mnl.clear()
    db.verdicts.append({"kind": "pair", "lo": 20, "hi": 21, "verdict": "different"})
    third = _lane(db)
    assert third["counts"]["skipped_rows_written"] == 1, "a CHANGED first reason is filed"


def test_the_reconcile_waits_on_a_block_it_has_not_fully_read() -> None:
    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _read(db, 10, 11)
    db.snapshot[99] = BLOCK                            # in the block, not fingerprinted yet

    out = _lane(db)

    assert out["counts"][reconcile.WAITING] == 1 and out["counts"]["applied"] == 0
    db.read.add(99)
    assert _lane(db)["counts"]["applied"] == 1


def test_the_reconcile_stops_between_groups_when_the_pass_time_is_spent() -> None:
    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _read(db, 10, 11)

    out = _lane(db, deadline=reconcile.GROUP_MARGIN_S - 1)

    assert out["stopped"] and out["counts"]["not_attempted"] == 1
    assert out["counts"]["applied"] == 0 and db.listings[11]["property_id"] == 200


def test_a_closed_scope_row_is_the_reconciles_stop() -> None:
    """Until W6 the operator's scope row gates the lane's merges: empty, it merges nothing."""
    db = LaneDb()
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _read(db, 10, 11)
    out = _lane(db)
    assert out["skipped"] == "scope_closed" and not db.ledger


def test_a_live_dispatch_refuses_while_the_lane_holds_the_lease(tmp_path, monkeypatch) -> None:
    """One writer (A9): a live apply or unapply takes the lane's lease for its run; a dry run
    reads only and takes nothing."""
    from autodedup.incremental_lane import LANE_NAME

    db = LaneDb()
    original = AP.apply_plan
    monkeypatch.setattr(AP, "apply_plan", lambda conn, plan, dry_run: original(
        conn, plan, dry_run, merge=db.merge([])))
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    db.lease[LANE_NAME] = {"holder": "worker:1:1", "live": True}

    with pytest.raises(SystemExit, match="rt_lease"):
        AP.run_apply(lambda: db, {"generation": "g12", "dry_run": "0"}, tmp_path)
    with pytest.raises(SystemExit, match="rt_lease"):
        AP.run_unapply(lambda: db, {"generation": "g12", "dry_run": "0"}, tmp_path)
    assert AP.run_apply(lambda: db, {"generation": "g12"}, tmp_path)["dry_run"]
    assert db.listings[11]["property_id"] == 200

    db.lease[LANE_NAME]["live"] = False
    out = AP.run_apply(lambda: db, {"generation": "g12", "dry_run": "0"}, tmp_path)
    assert out["counts"]["applied"] == 1
    assert not db.lease[LANE_NAME]["live"], "and the run frees it"


def test_a_score_pass_never_writes_or_prunes_the_live_stream() -> None:
    """`score` writes g* generations for evaluation; `rt` is the worker lane's alone (E914)."""
    from autodedup import score_lane

    with pytest.raises(SystemExit, match="live stream"):
        score_lane.parse_args({"cohort": "c.jsonl.gz", "generation": "rt"})

    class _Conn:
        def __init__(self) -> None:
            self.deleted: list[list[str]] = []

        def transaction(self):
            from contextlib import nullcontext

            return nullcontext()

        def cursor(self):
            conn = self

            class _Cur:
                rows: list = []

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, sql, params=None):
                    if "group by c.generation" in sql:
                        self.rows = [("rt",), ("g11",), ("g12",)]
                    elif params:
                        conn.deleted.append(list(params["generations"]))

                def fetchall(self):
                    return list(self.rows)

            return _Cur()

    conn = _Conn()
    out = score_lane.prune_generations(conn, keep=1, current="g12")
    assert out["pruned"] == ["g11"] and all("rt" not in d for d in conn.deleted)


# ------------------------------------------------------------------ the seed version (review A2)


def test_the_seed_writes_the_version_the_reconcile_requires(tmp_path) -> None:
    from autodedup.incremental import SEED_VERSION, seed_version_key
    from tests.autodedup import lane_world

    conn = lane_world.world()
    out = lane_world.seed_lane(conn, tmp_path)
    assert out["seed_version"] == SEED_VERSION
    assert conn.settings[seed_version_key()] == SEED_VERSION


@pytest.mark.parametrize("stale", [None, "w4"])
def test_the_reconcile_is_skipped_over_a_generation_no_w5_seed_built(tmp_path, stale) -> None:
    """REVIEW BLOCKER 2. Production `rt` was seeded 09-21, before F2: its groups are not the
    ones this build draws. A calibration alone must not let the reconcile merge from it — the
    seed's version row must match, and the pass says why it did not reconcile."""
    from autodedup.incremental import SEED_VERSION, bootstrap_key, seed_version_key
    from autodedup.incremental_lane import SEED_MISMATCH, run_incremental
    from tests.autodedup import lane_world

    conn = lane_world.world()
    lane_world.seed_lane(conn, tmp_path)
    conn.settings[bootstrap_key()] = False
    if stale is None:
        conn.settings.pop(seed_version_key())
    else:
        conn.settings[seed_version_key()] = stale

    out = run_incremental(lambda: conn)

    assert out["aborted"] == "" and out["reconcile"]["skipped"] == SEED_MISMATCH
    assert "mode=rt_seed" in out["reconcile"]["reason"]
    assert not any("applied_merges" in sql for sql in conn.statements), "no ledger read"

    conn.settings[seed_version_key()] = SEED_VERSION
    ran = run_incremental(lambda: conn)
    assert ran["reconcile"]["skipped"] == "scope_closed", "with the version it reconciles"
