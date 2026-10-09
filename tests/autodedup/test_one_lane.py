"""W5, the one lane: the worker's pass clusters as the batch pass does and reconciles production.

F2 (E909): the lane's re-cluster reads the D43 relation the batch pass reads, off the same stored
rows. Must-links (Decision 8, E910): the operator's `same` rulings bind the clustering. The
reconcile (A9, E911) and the in-DB calibration (A10, E912) have their own sections below.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

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
    RT_LOCK_GUARD_SQL,
    RT_SCOPE_SCAN_SEEN_SQL,
    RT_STATEMENT_GUARD_SQL,
)
from autodedup.incremental_store import MemoryStore
from autodedup.indistinguishable import FEATURE_SLOTS
from autodedup.incremental_store import CohortFacts, Schedule
from autodedup.settings import Settings
from tests.autodedup.fake_pg import FakePg, _Cursor
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
    facts = CohortFacts(ds)
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
    from tests.autodedup.whole_cohort import build_all

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
    assert [d["invariant"] for d in operator.dissolved] == ["must_not_link"], (
        "and the contradiction is recorded")


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
    ds = _floors_cohort()
    store = MemoryStore()
    _known(store, A, C)
    store.ml = {(A, C), (B, 999)}
    calibration = Calibration(generation="rt", feature_version=0, built_at="", n_listings=0)
    run_pass(store, CohortFacts(ds), Schedule([]), D43_ON, hand_initialised(),
             calibration)
    assert sorted(map(sorted, store.clusters.values())) == [[A, C]], (
        "joined; the ruling reaching outside the store (B, 999) links nothing")
    # The operator changes their mind: the newest ruling is `different`, which also writes the
    # must-not-link (api/routes/autodedup.py) — and that seeds the component on the next pass.
    store.ml = set()
    store.mnl = {(A, C)}
    run_pass(store, CohortFacts(ds), Schedule([]), D43_ON, hand_initialised(),
             calibration)
    assert store.clusters == {}, "the pair the ruling held is released: nothing else joins it"


# ------------------------------------------------- E926: a dissolved closure leaves a record

D = 404
SAME = {(A, C), (B, D)}


def _sale_and_rent() -> Dataset:
    """The operator ruled A and C one flat, but A is offered for sale and C for rent — a union
    no invariant admits (never a rental with a sale). B and D he joined too, and nothing
    refuses them."""
    listings = {A: _listing(A), B: _listing(B), C: _listing(C, category_type="pronajem"),
                D: _listing(D)}
    return Dataset(meta=Meta(), listings=listings, images_by_listing={})


CLOSURE = {"lo": A, "hi": C, "invariant": "category_type", "members": [A, C],
           "must_link": [[A, C]]}


def test_a_refused_closure_is_one_record_naming_its_rulings_and_its_limb() -> None:
    ds = _sale_and_rent()
    for settings in (Settings(), Settings(repartition=True)):
        result = _cluster(ds, settings, [], must_link=frozenset(SAME))
        assert _groups(result) == [[B, D]], "the honoured closure binds, the refused one nothing"
        assert result.dissolved == [CLOSURE], "one record, and none for the honoured closure"
        assert result.conflicts == [], "no edge was refused"


def _idle_pass(store: MemoryStore, ds: Dataset) -> PassResult:
    from autodedup.incremental import Calibration, run_pass
    from autodedup.model import hand_initialised

    return run_pass(store, CohortFacts(ds), Schedule([]), D43_ON, hand_initialised(),
                    Calibration(generation="rt", feature_version=0, built_at="", n_listings=0))


def test_a_refused_closure_is_recorded_once_across_passes_and_the_lane_goes_on() -> None:
    """The contradiction seeds its component every pass (`_ruling_seeds`), so every pass
    dissolves the closure again: the heartbeat counts it each time, the store files it once."""
    ds = _sale_and_rent()
    store = MemoryStore()
    _known(store, A, B, C, D)
    store.ml = set(SAME)
    for _ in range(3):
        result = _idle_pass(store, ds)
        assert not result.aborted
        assert result.to_json()["counts"]["must_link_dissolved"] == 1
        assert sorted(map(sorted, store.clusters.values())) == [[B, D]]
        records = [c for c in store.conflicts if c.get("must_link")]
        assert records == [{**CLOSURE, "kind": "invariant", "generation": "rt"}]
    assert store.conflicts == records, "the honoured closure left nothing"


def test_the_sql_store_files_the_record_once_as_its_twin_does() -> None:
    ds = _sale_and_rent()
    db = FakePg()
    db.ml = set(SAME)
    sql, twin = SqlStore(db, "rt"), MemoryStore()
    twin.ml = set(SAME)
    for store in (sql, twin):
        _known(store, A, B, C, D)
        for _ in range(2):
            _recluster_all(store, ds, D43_ON)
    rows = [row for row in db.cluster_conflicts if (row["detail"] or {}).get("must_link")]
    assert [(r["kind"], r["listing_lo"], r["listing_hi"], r["invariant"]) for r in rows] == [
        ("invariant", A, C, "category_type")]
    assert {k: rows[0]["detail"][k] for k in ("generation", "members", "must_link")} == {
        "generation": "rt", "members": [A, C], "must_link": [[A, C]]}
    assert [c for c in twin.conflicts if c.get("must_link")] == [
        {**CLOSURE, "kind": "invariant", "generation": "rt"}]


def test_a_closure_the_rail_reclusters_again_is_counted_once() -> None:
    """The E64 rail re-clusters a demoted pair's component inside the same pass, so the pass
    dissolves one closure twice: the heartbeat counts closures, not re-clusters."""
    ds = _sale_and_rent()
    store = MemoryStore()
    _known(store, A, B, C, D)
    store.ml = set(SAME)
    facts = CohortFacts(ds)
    result = PassResult(generation="rt", calibration_digest="x")
    for _ in range(2):
        _recluster(store, facts, D43_ON, _Working(facts, D43_ON), {A, B, C, D}, Limits(),
                   result)
    assert result.to_json()["counts"]["must_link_dissolved"] == 1


def _mnl_then_rental() -> Dataset:
    """A, B and C are flats for sale, D one for rent."""
    listings = {i: _listing(i) for i in (A, B, C)}
    listings[D] = _listing(D, category_type="pronajem")
    return Dataset(meta=Meta(), listings=listings, images_by_listing={})


def test_a_closure_that_changes_and_changes_back_is_filed_again() -> None:
    """Review of #1652: the record was filed when no identical one existed EVER. A-B and B-C
    `same` with A-C `different` dissolve {A, B, C} on the must-not-link (R1); C-D `same` makes
    it {A, B, C, D}, a sale with a rental (R2); withdrawing C-D gives R1's closure back, and
    nothing was filed — so the newest record naming A and B, the one the rulings page shows,
    blamed the rental. It is filed unless the NEWEST record sharing an advert is identical."""
    ds = _mnl_then_rental()
    db = FakePg()
    sql, twin = SqlStore(db, "rt"), MemoryStore()
    chain = {(A, B), (B, C)}
    for store, rulings in ((sql, db), (twin, twin)):
        _known(store, A, B, C, D)
        rulings.mnl = {(A, C)}
        for same in (chain, chain | {(C, D)}, chain, chain):
            rulings.ml = set(same)
            _recluster_all(store, ds, D43_ON)
    first = ("must_not_link", [A, B, C], [[A, B], [B, C]])
    filed = [first, ("category_type", [A, B, C, D], [[A, B], [B, C], [C, D]]), first]
    assert [(r["invariant"], r["detail"]["members"], r["detail"]["must_link"])
            for r in db.cluster_conflicts] == filed
    assert [(c["invariant"], c["members"], c["must_link"]) for c in twin.conflicts] == filed


def test_a_batch_run_refuses_no_edge_with_a_dissolved_closure(tmp_path) -> None:
    import json

    from autodedup import harness
    from autodedup.model import hand_initialised

    harness.run(_sale_and_rent(), Settings(), hand_initialised(), tmp_path, frozenset(),
                frozenset(SAME))
    written = json.loads((tmp_path / harness.CLUSTERS_FILE).read_text(encoding="utf-8"))
    assert written["conflicts"] == [] and written["stats"]["n_edges_refused"] == 0


def test_the_sql_store_reads_the_newest_pair_ruling_only() -> None:
    from autodedup.incremental_sql import RT_MUST_LINK_SQL

    assert "distinct on" in RT_MUST_LINK_SQL and "v.verdict = 'same'" in RT_MUST_LINK_SQL
    db = FakePg()
    db.ml = {(A, C)}
    assert SqlStore(db, "rt").must_link() == {(A, C)}


def test_a_withdrawn_same_re_clusters_its_component_on_the_next_pass() -> None:
    """G4 (E920): a withdrawal contradicts nothing — once the `same` is withdrawn, the group it
    held together breaks no ruling — so only the change itself can seed the component. Without
    that seed the group survives, and the reconcile, which sweeps every group, could still merge
    the two adverts after the operator took the word back."""
    from autodedup.incremental import Calibration, run_pass
    from autodedup.model import hand_initialised
    ds = _floors_cohort()
    store = MemoryStore()
    _known(store, A, C)
    store.ml = {(A, C)}
    calibration = Calibration(generation="rt", feature_version=0, built_at="", n_listings=0)

    def idle_pass() -> None:
        run_pass(store, CohortFacts(ds), Schedule([]), D43_ON, hand_initialised(),
                 calibration)

    idle_pass()
    assert sorted(map(sorted, store.clusters.values())) == [[A, C]], "the must-link joins them"
    # The operator withdraws the word: a newer `unsure`, so the pair is no must-link, and no
    # veto is written. The control: nothing contradicts the group, so nothing re-clusters it.
    store.ml = set()
    idle_pass()
    assert sorted(map(sorted, store.clusters.values())) == [[A, C]]
    # The changed ruling names its two adverts (the lane's `rt_rulings` read).
    store.ruled = {A, C}
    idle_pass()
    assert store.clusters == {}, "the change seeds the component: nothing else holds the pair"
    assert store.ruled == set(), "the pass that honoured the change consumed it"


def test_a_changed_ruling_on_listings_the_store_never_read_seeds_nothing() -> None:
    store = MemoryStore()
    _known(store, A)
    store.ruled = {A, 999}
    from autodedup.incremental import read_rulings

    assert read_rulings(store).changed == frozenset({A}), (
        "a listing outside the store cannot be clustered here")


def test_the_sql_store_reads_the_rulings_changed_since_the_previous_pass() -> None:
    from datetime import timedelta

    from autodedup.incremental_lane import CURSOR_RULINGS, RESET_CURSORS, RULINGS_OVERLAP_S
    from autodedup.incremental_sql import RT_CURSOR_WRITE_SQL, RT_RULINGS_CHANGED_SQL

    assert CURSOR_RULINGS in RESET_CURSORS, "a fresh seed restarts it at the present"
    flat = " ".join(RT_RULINGS_CHANGED_SQL.split())
    assert "v.kind = 'pair' then array[v.listing_lo, v.listing_hi] else v.member_ids" in flat
    db = FakePg()
    start = db.now
    db.rulings.append((start - timedelta(days=3), [A, C]))
    store = SqlStore(db, "rt")
    assert store.rulings_changed() == set(), "the first pass starts at the present"
    store.flush()
    assert db.cursors[CURSOR_RULINGS]["watermark"] == start

    db.now = start + timedelta(minutes=1)
    db.rulings.append((db.now, [A, B]))
    assert store.rulings_changed() == {A, B}
    # A pass that rolls back never flushes, so the next pass reads the same change.
    assert SqlStore(db, "rt").rulings_changed() == {A, B}
    # A ruling whose transaction began before the previous pass started and committed after it
    # read is inside the overlap; one older than the overlap was read by that pass.
    db.rulings.append((start - timedelta(seconds=RULINGS_OVERLAP_S / 2), [C]))
    db.rulings.append((start - timedelta(seconds=RULINGS_OVERLAP_S * 2), [999]))
    assert store.rulings_changed() == {A, B, C}
    store.flush()
    assert db.cursors[CURSOR_RULINGS]["watermark"] == db.now
    assert RT_CURSOR_WRITE_SQL in db.statements_in_tx or RT_CURSOR_WRITE_SQL in db.statements


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
        self.guards: list[dict] = []

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
        if sql == AS.RC_OUTCOME_HISTORY_SQL:
            # One event per (member set, run), newest first, `depth` of them per set.
            events: dict[tuple[int, ...], dict[str, tuple]] = {}
            for row in self.ledger:
                members = tuple(row["member_ids"] or ())
                if (row["generation"] == p["generation"] and not row["dry_run"]
                        and set(members) & set(p["listing_ids"])):
                    events.setdefault(members, {})[row["run_id"]] = (
                        row["id"], list(members), row["outcome"], row["error"],
                        row["applied_at"])
            out = []
            for members in sorted(events):
                ranked = sorted(events[members].values(), key=lambda e: -e[0])[:p["depth"]]
                out += [e[1:] for e in ranked]
            return out
        if sql in (RT_STATEMENT_GUARD_SQL, RT_LOCK_GUARD_SQL):
            self.guards.append(dict(p))
            return [("set",)]
        return super().dispatch(sql, p)


def _lane(db: LaneDb, *, deadline: float = 10 ** 9, touched=(), merge=None,
          run_id: str = "rt:test") -> dict:
    return reconcile.run(db, RT, list(touched), run_id=run_id, deadline=deadline,
                         blocks=[BLOCK], merge=merge or db.merge([]), clock=lambda: 0.0,
                         now=lambda: db.now)


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
                    self.rows = []
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


# ------------------------------------------------------------------ the brake (review A3)


@pytest.mark.parametrize("interval", [60, "60", 1])
def test_a_live_unapply_or_apply_refuses_while_the_lane_is_running(tmp_path, interval) -> None:
    """The brake is interval 0, THEN unapply: while the lane runs, an undone group is
    re-merged by its next sweep minutes later, and a live apply races it. Both refuse and name
    the row; a dry run still reads."""
    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    db.settings[AP.LANE_INTERVAL_SETTING] = interval

    for run, args in ((AP.run_unapply, {"generation": "g12", "dry_run": "0"}),
                      (AP.run_apply, {"generation": "g12", "dry_run": "0"})):
        with pytest.raises(SystemExit, match="realtime_autodedup_interval_seconds is") as raised:
            run(lambda: db, args, tmp_path)
        assert "Nothing was written" in str(raised.value)
    assert not db.ledger and db.listings[11]["property_id"] == 200
    assert not db.lease, "it refused before taking the lease"
    assert AP.run_apply(lambda: db, {"generation": "g12"}, tmp_path)["dry_run"]


@pytest.mark.parametrize("interval", [0, None, "junk"])
def test_a_stopped_lane_lets_the_brake_run(tmp_path, monkeypatch, interval) -> None:
    db = LaneDb()
    original = AP.apply_plan
    monkeypatch.setattr(AP, "apply_plan", lambda conn, plan, dry_run: original(
        conn, plan, dry_run, merge=db.merge([])))
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    if interval is not None:
        db.settings[AP.LANE_INTERVAL_SETTING] = interval
    assert AP.run_apply(lambda: db, {"generation": "g12", "dry_run": "0"},
                        tmp_path)["counts"]["applied"] == 1


def test_a_dry_run_of_the_live_stream_takes_the_lease(tmp_path) -> None:
    """Review B8: G3's prediction is a dry run of `rt`; it holds the lane's lease so no pass
    moves the plan under it, and refuses (naming the holder) while a pass holds it."""
    from autodedup.incremental_lane import LANE_NAME

    db = LaneDb()
    db.live_scope()
    db.lease[LANE_NAME] = {"holder": "worker:1:1", "live": True}
    with pytest.raises(SystemExit, match="held by 'worker:1:1'"):
        AP.run_apply(lambda: db, {"generation": RT}, tmp_path)
    db.lease[LANE_NAME]["live"] = False
    assert AP.run_apply(lambda: db, {"generation": RT}, tmp_path)["dry_run"]
    assert db.lease[LANE_NAME]["holder"].startswith("dispatch:") and not db.lease[LANE_NAME]["live"]


def test_a_failing_release_never_masks_the_error_that_ended_the_run() -> None:
    """Review A11: a run that dies with its connection dies again releasing its lease; the
    first error is the one raised, with the release failure noted on it."""
    from autodedup import rt_lease

    class _Dead:
        def cursor(self):
            raise ConnectionError("server closed the connection")

    original = RuntimeError("the reconcile failed")
    rt_lease.release_after(_Dead(), "worker:1:1", original)
    assert any("expires by itself" in note for note in original.__notes__)
    with pytest.raises(ConnectionError):
        rt_lease.release_after(_Dead(), "worker:1:1", None)



# ------------------------------------------------------------------ the reconcile, hardened (A5-A7)


def test_the_whole_scope_row_is_re_read_before_every_group() -> None:
    """Review A6/B7: narrowing the blocks between two groups stops the second at its re-check,
    because the re-check reads the row as it is NOW, not the one the plan read."""
    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _pair_group(db, 20, [20, 21], [300, 400], gen=RT)
    _read(db, 10, 11, 20, 21)
    calls: list = []
    inner = db.merge(calls)

    def merge_then_narrow(conn, property_ids, **kw):
        out = inner(conn, property_ids, **kw)
        db.settings[AP.SCOPE_SETTING] = {"category_types": ["pronajem"],
                                         "blocks": ["town:563510"]}
        return out

    out = _lane(db, merge=merge_then_narrow)

    assert out["counts"]["applied"] == 1 and out["counts"]["skipped_at_apply"] == 1
    assert out["skipped_at_apply"][0]["reasons"] == [AP.SKIP_CARRIES_OUT_OF_SCOPE]
    assert db.listings[21]["property_id"] == 400


def test_the_scope_rows_run_cap_bounds_the_merges_of_one_pass() -> None:
    """Review A6: `max_clusters_per_run` is not dropped by the lane; the rest wait a pass."""
    db = LaneDb()
    db.live_scope(max_clusters_per_run=1)
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _pair_group(db, 20, [20, 21], [300, 400], gen=RT)
    _read(db, 10, 11, 20, 21)

    first = _lane(db, run_id="rt:1")
    second = _lane(db, run_id="rt:2")

    assert (first["counts"]["applied"], first["counts"]["deferred_run_cap"]) == (1, 1)
    assert second["counts"]["applied"] == 1
    assert db.listings[11]["property_id"] == 100 and db.listings[21]["property_id"] == 300


def test_an_apply_time_skip_that_repeats_files_no_new_row(monkeypatch) -> None:
    """Review A7: a late re-check skip surfaces only at apply time, so the plan's dedupe never
    saw it; the lane re-tries the group every sweep and must not file a row each time."""
    db = LaneDb()
    db.live_scope()
    monkeypatch.setattr(AP, "recheck_group",
                        lambda conn, group, scope: ([AP.SKIP_CHANGED_SINCE_PLAN], {}))
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _read(db, 10, 11)

    first = _lane(db, run_id="rt:1")
    rows = len(db.ledger)
    second = _lane(db, run_id="rt:2")

    assert first["counts"]["skipped_at_apply"] == second["counts"]["skipped_at_apply"] == 1
    assert first["skipped_at_apply"][0]["reasons"] == [AP.SKIP_CHANGED_SINCE_PLAN]
    assert rows == 1 and len(db.ledger) == rows, "one row for the one outcome"


def test_an_error_nothing_names_is_counted_and_quarantines_after_three_passes() -> None:
    """Review A7: an error inside one group never escapes the pass (the worker would fail every
    minute); it is filed as `failed`, and a member set that failed QUARANTINE_AFTER passes in a
    row is reported, not attempted, until QUARANTINE_RETRY_H after its last failure."""
    from datetime import timedelta

    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _pair_group(db, 20, [20, 21], [300, 400], gen=RT)
    _read(db, 10, 11, 20, 21)
    good = db.merge([])

    def broken(conn, property_ids, **kw):
        if 100 in property_ids:
            raise RuntimeError("an error nothing names")
        return good(conn, property_ids, **kw)

    for n in range(1, reconcile.QUARANTINE_AFTER + 1):
        out = _lane(db, merge=broken, run_id=f"rt:{n}")
        assert out["counts"]["failed"] == 1 and "RuntimeError" in out["failed"][0]["error"]
    assert db.listings[21]["property_id"] == 300, "the other group merged on the first pass"
    rows = len(db.ledger)

    held = _lane(db, merge=broken, run_id="rt:held")
    assert held["counts"][reconcile.QUARANTINED] == 1 and held["counts"]["failed"] == 0
    assert held[reconcile.QUARANTINED][0]["cluster_key"] == 10 and len(db.ledger) == rows

    db.now += timedelta(hours=reconcile.QUARANTINE_RETRY_H + 1)
    retried = _lane(db, run_id="rt:retry")
    assert retried["counts"]["applied"] == 1 and db.listings[11]["property_id"] == 100


def test_each_group_transaction_carries_its_own_local_timeouts() -> None:
    """Review A5: the pass's guards ended with its commit; each group sets its own."""
    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _read(db, 10, 11)
    _lane(db)
    assert {"statement_timeout_ms": reconcile.GROUP_STATEMENT_TIMEOUT_MS} in db.guards
    assert {"lock_timeout_ms": reconcile.GROUP_LOCK_TIMEOUT_MS} in db.guards
    assert reconcile.GROUP_STATEMENT_TIMEOUT_MS / 1000 < reconcile.GROUP_MARGIN_S


def test_a_pair_ruling_is_read_newest_first_by_the_plan_as_by_the_lane() -> None:
    """Review A8: a pair ruled different and LATER ruled same no longer refuses the merge (the
    lane's clustering already reads it as a must-link, RT_MUST_LINK_SQL); ruled same and later
    different, it does."""
    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _pair_group(db, 20, [20, 21], [300, 400], gen=RT)
    _read(db, 10, 11, 20, 21)
    db.verdicts += [{"kind": "pair", "lo": 10, "hi": 11, "verdict": "different"},
                    {"kind": "pair", "lo": 10, "hi": 11, "verdict": "same"},
                    {"kind": "pair", "lo": 20, "hi": 21, "verdict": "same"},
                    {"kind": "pair", "lo": 20, "hi": 21, "verdict": "different"}]

    out = _lane(db)

    assert out["counts"]["applied"] == 1 and db.listings[11]["property_id"] == 100
    assert out["counts"]["skipped_by_reason"] == {AP.SKIP_PAIR_VERDICT: 1}


def test_a_seed_resets_the_rate_and_a_pass_times_its_reconcile(tmp_path, monkeypatch) -> None:
    """Review B3: the 09-21 rate row (0.585503) would have "passed" G2 before any W5 pass ran;
    a seed starts the build's rate from the conservative default, and every pass that
    reconciles says how long the reconcile took."""
    from autodedup.incremental import SEED_VERSION, bootstrap_key, seed_version_key
    from autodedup.incremental_lane import PASS_RATE_PER_S, pass_rate_key, run_incremental
    from tests.autodedup import lane_world

    conn = lane_world.world()
    conn.settings[pass_rate_key("rt")] = 0.585503
    out = lane_world.seed_lane(conn, tmp_path)
    assert conn.settings[pass_rate_key("rt")] == PASS_RATE_PER_S == out["pass_rate_per_s"]

    conn.settings[bootstrap_key()] = False
    assert conn.settings[seed_version_key()] == SEED_VERSION
    seen: list = []
    monkeypatch.setattr(reconcile, "run", lambda *a, **k: seen.append(k) or {
        "counts": {"groups": 7, "applied": 2}})
    ran = run_incremental(lambda: conn)
    assert seen and ran["reconcile"]["counts"]["groups"] == 7
    assert ran["reconcile"]["seconds"] >= 0


# ------------------------------------------------------------------ a RAISED pass (E930)


def test_the_idle_guard_outlives_the_deadline_and_not_the_lease() -> None:
    """The scoring loop runs no statement while it decides every wanted pair, so an idle guard
    tighter than the pass's own deadline terminates a HEALTHY pass (2026-10-02: 300 s killed
    every bootstrap pass past the seventh on the worker). It must sit above the deadline plus
    one statement and under the lease E913 sized for that deadline."""
    from autodedup.incremental_lane import (IDLE_TIMEOUT_MS, LEASE_TTL_S, PASS_DEADLINE_S,
                                            STATEMENT_TIMEOUT_MS)

    assert IDLE_TIMEOUT_MS > PASS_DEADLINE_S * 1000 + STATEMENT_TIMEOUT_MS
    assert IDLE_TIMEOUT_MS < LEASE_TTL_S * 1000


def _pass_that_raises(monkeypatch, exc: BaseException) -> None:
    from autodedup import incremental_lane

    def raise_inside_the_transaction(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", raise_inside_the_transaction)


def test_a_raised_pass_halves_its_rate_on_a_fresh_connection(tmp_path, monkeypatch) -> None:
    """A raise rolls the pass back and moves no cursor, so the next pass claims the IDENTICAL
    batch; without the halving it fails the same way for ever (six passes on 2026-10-02). The
    write goes through a connection of its own: the pass's may be the one the server
    terminated."""
    from autodedup.incremental_lane import PASS_RATE_PER_S, pass_rate_key, run_incremental
    from tests.autodedup import lane_world

    conn = lane_world.world()
    lane_world.seed_lane(conn, tmp_path)
    assert conn.settings[pass_rate_key("rt")] == PASS_RATE_PER_S
    _pass_that_raises(monkeypatch, RuntimeError("server closed the connection unexpectedly"))
    fresh = FakePg()
    opened: list[FakePg] = []

    def open_fresh() -> FakePg:
        opened.append(fresh)
        return fresh

    with pytest.raises(RuntimeError, match="server closed"):
        run_incremental(lambda: conn, fresh_conn=open_fresh)

    assert opened == [fresh]
    assert fresh.settings[pass_rate_key("rt")] == PASS_RATE_PER_S / 2.0
    assert fresh.settings_by[pass_rate_key("rt")].endswith(":halved")
    # E941: the pass's own connection carries the same half, written AHEAD of the pass while
    # that connection was known to be alive — and nothing after the raise.
    assert conn.settings[pass_rate_key("rt")] == PASS_RATE_PER_S / 2.0
    assert conn.settings_by[pass_rate_key("rt")].endswith(":halved_ahead")
    assert conn.rolled_back >= 1


def test_without_a_fresh_connection_the_halving_uses_the_pass_s_own(tmp_path,
                                                                     monkeypatch) -> None:
    from autodedup.incremental_lane import PASS_RATE_PER_S, pass_rate_key, run_incremental
    from tests.autodedup import lane_world

    conn = lane_world.world()
    lane_world.seed_lane(conn, tmp_path)
    _pass_that_raises(monkeypatch, RuntimeError("canceling statement due to statement timeout"))

    with pytest.raises(RuntimeError, match="statement timeout"):
        run_incremental(lambda: conn)

    assert conn.settings[pass_rate_key("rt")] == PASS_RATE_PER_S / 2.0


def test_a_failed_halving_write_keeps_the_original_raise(tmp_path, monkeypatch) -> None:
    """The halving is best effort: a pooler that refuses the fresh connect is noted on the
    raise, never raised in its place."""
    from autodedup.incremental_lane import PASS_RATE_PER_S, pass_rate_key, run_incremental
    from tests.autodedup import lane_world

    conn = lane_world.world()
    lane_world.seed_lane(conn, tmp_path)
    _pass_that_raises(monkeypatch, RuntimeError("terminating connection due to idle-in-"
                                                "transaction timeout"))

    def pooler_down() -> FakePg:
        raise ConnectionError("pooler down")

    with pytest.raises(RuntimeError, match="idle-in-transaction") as raised:
        run_incremental(lambda: conn, fresh_conn=pooler_down)

    notes = getattr(raised.value, "__notes__", [])
    assert any("halving" in note and "pooler down" in note for note in notes)
    # E941: the halving stands anyway — it was written ahead of the pass.
    assert conn.settings[pass_rate_key("rt")] == PASS_RATE_PER_S / 2.0
    # E931: no fresh connection to fall back on, and none needed — the pass's own is live.
    from autodedup import rt_lease

    assert rt_lease.current(conn)["live"] is False
    assert not any("rt_lease" in note for note in notes)


def test_a_refusal_never_halves_the_rate(tmp_path, monkeypatch) -> None:
    """A refusal (a storage budget, a scope, the retirement rail) is repeated by design until
    the operator acts; halving it every tick would wedge the claim at one advert."""
    from autodedup.incremental_lane import (PASS_RATE_PER_S, RetireRefusal, pass_rate_key,
                                            run_incremental)
    from tests.autodedup import lane_world

    conn = lane_world.world()
    lane_world.seed_lane(conn, tmp_path)
    _pass_that_raises(monkeypatch, RetireRefusal("the sweep wants to retire too much"))
    opened: list[FakePg] = []

    with pytest.raises(SystemExit, match="retire too much"):
        run_incremental(lambda: conn, fresh_conn=lambda: opened.append(FakePg()) or opened[-1])

    assert not opened
    assert conn.settings[pass_rate_key("rt")] == PASS_RATE_PER_S
    assert conn.settings_by[pass_rate_key("rt")].endswith(":restored"), "the half went back"


# ------------------------------------------------------------ E941: the rate, halved AHEAD


class _Killed(BaseException):
    """What the system does to a pass (a deploy's SIGKILL, a restart, the OOM killer): no
    `except Exception` sees it, so nothing the pass meant to write after it is written."""


def test_a_pass_the_system_kills_has_already_halved_the_next_claim(tmp_path,
                                                                   monkeypatch) -> None:
    """E930 halved only on a raise the pass lived to see. The half is now written before the
    pass's transaction opens, committed on its own, so it stands when the pass is killed —
    and the next claim, once the lease is gone, is half the one that was killed."""
    from autodedup import incremental_lane
    from autodedup.incremental_lane import PASS_RATE_PER_S, pass_rate_key, run_incremental
    from tests.autodedup import lane_world

    conn = lane_world.world()
    lane_world.seed_lane(conn, tmp_path)
    key = pass_rate_key("rt")
    seen: dict = {}

    def killed_inside_the_transaction(*_args, **_kwargs):
        seen.update(in_tx=conn.in_transaction, rate=conn.settings[key])
        raise _Killed()

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", killed_inside_the_transaction)
    opened: list = []
    with pytest.raises(_Killed):
        run_incremental(lambda: conn, fresh_conn=lambda: opened.append(1) or FakePg())

    assert seen == {"in_tx": True, "rate": PASS_RATE_PER_S / 2.0}, "halved BEFORE the pass"
    # The pass's transaction rolled back to what it opened on, so a half written inside it
    # would be gone: it stands because it committed first.
    assert conn.rolled_back >= 1
    assert conn.settings[key] == PASS_RATE_PER_S / 2.0
    assert conn.settings_by[key].endswith(":halved_ahead")
    assert not opened, "no write after the kill"


def test_a_pass_that_cannot_measure_itself_puts_the_rate_back(tmp_path) -> None:
    """An idle pass (or one that claimed too few to measure) said nothing about the claim's
    size, so the rate it started from goes back over the half written ahead."""
    from autodedup.incremental_lane import pass_rate_key, run_incremental
    from tests.autodedup import lane_world

    conn = lane_world.world()
    lane_world.seed_lane(conn, tmp_path)
    key = pass_rate_key("rt")
    conn.settings[key] = 0.3
    first = run_incremental(lambda: conn)
    bound = first["claim_bound"]
    assert 0 < first["counts"]["claimed"] < min(20, int(bound["limit"])), "too few to measure"
    assert conn.settings[key] == 0.3 and conn.settings_by[key].endswith(":restored")


def test_a_committed_pass_puts_the_rate_back_before_its_reconcile(tmp_path,
                                                                   monkeypatch) -> None:
    """The reconcile plans outside the pass's statement guards; a raise there (a timeout at
    100 % CPU) came after the pass committed, so it must not leave the half standing — eight
    such passes would take the claim to one advert."""
    from autodedup.incremental import bootstrap_key
    from autodedup.incremental_lane import pass_rate_key, run_incremental
    from tests.autodedup import lane_world

    conn = lane_world.world()
    lane_world.seed_lane(conn, tmp_path)
    conn.settings[bootstrap_key()] = False
    key = pass_rate_key("rt")
    conn.settings[key] = 0.3
    seen: dict = {}

    def reconcile_raises(*_a, **_k):
        seen["rate"] = (conn.settings[key], conn.settings_by[key])
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(reconcile, "run", reconcile_raises)
    with pytest.raises(RuntimeError, match="statement timeout"):
        run_incremental(lambda: conn)

    assert seen["rate"][0] == 0.3 and seen["rate"][1].endswith(":restored"), "back before it"
    assert conn.settings[key] == 0.3


def test_a_pass_that_measured_itself_overwrites_the_half(tmp_path) -> None:
    from autodedup.incremental_lane import pass_rate_key, run_incremental
    from tests.autodedup import lane_world

    conn = lane_world.world()
    lane_world.seed_lane(conn, tmp_path)
    key = pass_rate_key("rt")
    conn.settings[key] = 0.0001          # a claim the time budget cuts is a measurement
    out = run_incremental(lambda: conn)
    assert out["claim_bound"]["bound_by"] == "time"
    assert conn.settings[key] > 0.0001 / 2 and conn.settings_by[key].endswith(":rate")


def test_the_fact_reads_are_sliced_by_whole_listings(tmp_path, monkeypatch) -> None:
    """Every cohort statement is a plain any(ids) filter ordered within a listing and every
    consumer keys by listing or image, so slices of whole listings read exactly what the whole
    array did — the facts of nine listings read in slices of two are the facts read in one."""
    from autodedup import incremental_lane
    from autodedup.incremental_lane import SqlFacts
    from tests.autodedup import lane_world

    conn = lane_world.world()
    ids = sorted(int(listing_id) for listing_id in conn.listings)
    assert len(ids) >= 5, "the world must span several slices"
    whole_facts = SqlFacts(conn)
    whole = whole_facts.facts(ids)
    monkeypatch.setattr(incremental_lane, "FACT_CHUNK", 2)
    sliced_facts = SqlFacts(conn)
    sliced = sliced_facts.facts(ids)

    assert sliced == whole
    assert sliced_facts.reads == whole_facts.reads == len(whole)
    assert sliced_facts.statements > whole_facts.statements, "more, smaller statements"
    assert (sliced_facts.images_with_phash, sliced_facts.images_unmeasured,
            sliced_facts.hashes_unmeasured) == (
        whole_facts.images_with_phash, whole_facts.images_unmeasured,
        whole_facts.hashes_unmeasured)


# ------------------------------------------------------------------ E931: the pass's bounds, continued


class _Session:
    """One session over a shared FakePg world: the pass's connection and the fresh one both
    see one lease row and one settings table, as two sessions of one database do. `dead`
    makes every later statement fail the way a backend the server terminated does; `cost` is
    the clock one statement spends, on the `clock` the engine's deadline check reads."""

    def __init__(self, world: FakePg, clock: SimpleNamespace | None = None,
                 cost: float = 0.0) -> None:
        self.world = world
        self.clock = clock
        self.cost = cost
        self.dead = False
        self.issued: list[str] = []
        self.closed = 0

    def cursor(self) -> "_SessionCursor":
        if self.dead:
            raise ConnectionError("server closed the connection unexpectedly")
        return _SessionCursor(self)

    def transaction(self):
        return self.world.transaction()

    def close(self) -> None:
        self.closed += 1


class _SessionCursor(_Cursor):
    def __init__(self, session: _Session) -> None:
        super().__init__(session.world)
        self.session = session

    def execute(self, sql: str, params=None) -> None:
        if self.session.dead:
            raise ConnectionError("server closed the connection unexpectedly")
        self.session.issued.append(sql)
        super().execute(sql, params)
        if self.session.clock is not None:
            self.session.clock.now += self.session.cost


def test_the_fact_reads_check_the_deadline_between_slices(monkeypatch) -> None:
    """The step before the neighbourhood read checks the clock and the step after does, and
    between them the chunked read is 49 statements of up to 120 s each on a dense scope — on
    a slow disk, far past the 1,050 s deadline with nothing to stop it. The deadline is read
    before every slice: a read crossing it stops at a slice boundary and raises PassDeadline
    (the pass rolls back and halves, E913); a read inside it is the whole read; a fact source
    given no deadline reads as before."""
    from autodedup import incremental, incremental_lane
    from autodedup.incremental import PassDeadline
    from autodedup.incremental_lane import SqlFacts
    from tests.autodedup import lane_world

    world = lane_world.world()
    ids = sorted(int(listing_id) for listing_id in world.listings)
    whole = SqlFacts(world).facts(ids)
    monkeypatch.setattr(incremental_lane, "FACT_CHUNK", 2)
    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(incremental, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    slow = _Session(world, clock=clock, cost=30.0)

    inside = SqlFacts(slow, deadline=clock.now + 10_000.0)
    assert inside.facts(ids) == whole
    assert clock.now > 1_000.0, "the slow disk spent clock on every statement"

    clock.now = 1_000.0
    crossing = SqlFacts(slow, deadline=clock.now + 100.0)
    with pytest.raises(PassDeadline):
        crossing.facts(ids)
    assert 0 < crossing.statements < inside.statements, "stopped at a slice boundary"

    clock.now = 1_000.0
    assert SqlFacts(slow).facts(ids) == whole, "no deadline: the read is the whole read"


def test_a_dead_connections_release_falls_back_to_the_live_one_for_its_own_holder_only() -> None:
    """E931 at the lease: with a `fallback`, the release a terminated backend cannot run is
    retried on the live connection; the statement is keyed by holder, so another writer's
    lease — a seed that took the row after ours expired — is never ended by it; and when
    both fail the release is noted on the original, as before, or raised when there is none."""
    from autodedup import rt_lease

    class _Dead:
        def cursor(self):
            raise ConnectionError("server closed the connection")

    world = FakePg()
    assert rt_lease.take(world, "worker:1:1", 2_400)
    original = RuntimeError("the pass raised")
    assert rt_lease.release_after(_Dead(), "worker:1:1", original, fallback=world) is True
    assert rt_lease.current(world)["live"] is False
    assert any("released on the fresh one" in note for note in original.__notes__)

    assert rt_lease.take(world, "rt_seed:other", 2_400)
    assert rt_lease.release_after(_Dead(), "worker:1:1", RuntimeError("x"), fallback=world)
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == ("rt_seed:other", True), "not that holder's row"

    both_dead = RuntimeError("the pass raised")
    assert rt_lease.release_after(_Dead(), "worker:1:1", both_dead, fallback=_Dead()) is False
    assert any("expires by itself" in note for note in both_dead.__notes__)
    with pytest.raises(ConnectionError):
        rt_lease.release_after(_Dead(), "worker:1:1", None, fallback=_Dead())


def _pass_that_kills_its_connection(monkeypatch, session: _Session, exc: BaseException) -> None:
    from autodedup import incremental_lane

    def terminated_inside_the_transaction(*_args, **_kwargs):
        session.dead = True
        raise exc

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", terminated_inside_the_transaction)


def test_a_raise_on_a_dead_connection_releases_the_lease_through_the_fresh_one(
        tmp_path, monkeypatch) -> None:
    """An idle-timeout kill ends the pass's backend; the `finally` could not release the lease
    over it, so the row sat until its 2,400 s TTL and the next passes were skipped "leased"
    for up to ~35 min. The release now rides the SAME fresh connection the halving write uses,
    under the holder the pass took the lease with."""
    from autodedup import rt_lease
    from autodedup.incremental_lane import PASS_RATE_PER_S, pass_rate_key, run_incremental
    from autodedup.incremental_sql import RT_LEASE_RELEASE_SQL
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    conn = _Session(world)
    fresh = _Session(world)
    opened: list[_Session] = []
    _pass_that_kills_its_connection(
        monkeypatch, conn, RuntimeError("terminating connection due to idle-in-transaction "
                                        "timeout"))

    with pytest.raises(RuntimeError, match="idle-in-transaction") as raised:
        run_incremental(lambda: conn, fresh_conn=lambda: opened.append(fresh) or fresh)

    assert opened == [fresh], "one fresh connect carries the halving AND the release"
    assert fresh.closed == 1
    assert world.settings[pass_rate_key("rt")] == PASS_RATE_PER_S / 2.0
    assert rt_lease.current(world)["live"] is False, "released, not left to its TTL"
    assert fresh.issued.count(RT_LEASE_RELEASE_SQL) == 1
    assert RT_LEASE_RELEASE_SQL not in conn.issued
    assert any("released on the fresh one" in note for note in raised.value.__notes__)


def test_a_raise_on_a_live_connection_releases_the_lease_the_existing_way(
        tmp_path, monkeypatch) -> None:
    """A Python error inside a healthy transaction: the halving still goes through the fresh
    connection (E930) and the lease is released over the pass's own connection, once — never
    also over the fresh one, and never a second time by the `finally`."""
    from autodedup import rt_lease
    from autodedup.incremental_lane import PASS_RATE_PER_S, pass_rate_key, run_incremental
    from autodedup.incremental_sql import RT_LEASE_RELEASE_SQL
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    releases_before = world.statements.count(RT_LEASE_RELEASE_SQL)
    conn = _Session(world)
    fresh = _Session(world)
    _pass_that_raises(monkeypatch, RuntimeError("a Python error inside the pass"))

    with pytest.raises(RuntimeError, match="Python error") as raised:
        run_incremental(lambda: conn, fresh_conn=lambda: fresh)

    assert world.settings[pass_rate_key("rt")] == PASS_RATE_PER_S / 2.0
    assert rt_lease.current(world)["live"] is False
    assert conn.issued.count(RT_LEASE_RELEASE_SQL) == 1
    assert RT_LEASE_RELEASE_SQL not in fresh.issued
    assert world.statements.count(RT_LEASE_RELEASE_SQL) == releases_before + 1, "once"
    assert not any("rt_lease" in note for note in getattr(raised.value, "__notes__", []))


# ------------------------------------------------------------- E941: the worker's shutdown
#
# Railway SIGTERMs the worker at every deploy and, by default, SIGKILLs it at once: a pass in
# flight died holding the lease, and the next worker skipped "leased" for 40 minutes. The
# worker now releases that lease at the signal, on a fresh connection, while the pass may
# still be running — so the engine must make sure a released lease never puts two writers in
# the store: the pass takes no lease while stopping, gives back one it took as the signal came,
# and its transaction ends with a fence that rolls it back once its lease is gone.


def _calls(*answers: bool):
    """A `stopping` that answers each call in turn, then the last answer for ever."""
    seq = list(answers)

    def stopping() -> bool:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return stopping


def test_a_stopping_worker_takes_no_lease_and_writes_nothing(tmp_path) -> None:
    from autodedup import rt_lease
    from autodedup.incremental_lane import PASS_RATE_PER_S, pass_rate_key, run_incremental
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    transactions = world.transactions

    out = run_incremental(lambda: world, stopping=lambda: True)

    assert out["skipped"] == "stopping"
    assert rt_lease.current(world)["holder"].startswith("rt_seed:"), "no lease taken"
    assert world.transactions == transactions
    assert world.settings[pass_rate_key("rt")] == PASS_RATE_PER_S, "nothing halved"


def test_a_shutdown_during_the_take_gives_the_lease_back(tmp_path) -> None:
    """The signal read no holder yet, or released before this take: nothing else would end
    the lease before the process does, so the pass that sees `stopping` right after the take
    releases it itself, before any transaction."""
    from autodedup import rt_lease
    from autodedup.incremental_lane import run_incremental
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    transactions = world.transactions

    out = run_incremental(lambda: world, holder="worker:1:1", stopping=_calls(False, True))

    assert out["skipped"] == "stopping"
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == ("worker:1:1", False), "taken, then given back"
    assert world.transactions == transactions


def test_a_pass_whose_lease_was_released_under_it_rolls_back_at_its_fence(
        tmp_path, monkeypatch) -> None:
    """The shutdown's release commits on its own connection while the pass decides. The pass's
    transaction then ends with the fence, finds no live lease for its holder, and rolls back:
    no cursor moves, nothing is stored, and the pass writes nothing after — the next holder's
    rows are not its own."""
    from autodedup import incremental_lane, rt_lease
    from autodedup.incremental_lane import (LEASE_LOST, PASS_RATE_PER_S, pass_rate_key,
                                            run_incremental)
    from autodedup.incremental_sql import RT_LEASE_HOLD_SQL, RT_LEASE_RELEASE_SQL
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    cursors = {k: dict(v) for k, v in world.cursors.items()}
    original = incremental_lane.run_pass_bounded

    def released_mid_pass(*args, **kwargs):
        result = original(*args, **kwargs)
        rt_lease.release(world.other_session(), "worker:1:1")   # the SIGTERM path
        return result

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", released_mid_pass)
    releases = world.statements.count(RT_LEASE_RELEASE_SQL)

    out = run_incremental(lambda: world, holder="worker:1:1", stopping=lambda: False)

    assert out["aborted"] == LEASE_LOST and out["reconcile"] == {"skipped": "pass_lease_lost"}
    assert "worker:1:1" in out["reason"]
    assert RT_LEASE_HOLD_SQL in world.statements_in_tx
    assert world.cursors == cursors and not world.rt_fp and not world.pairs
    assert rt_lease.current(world)["live"] is False, "the release stood"
    assert world.statements.count(RT_LEASE_RELEASE_SQL) == releases + 1, "only the signal's"
    assert world.settings[pass_rate_key("rt")] == PASS_RATE_PER_S / 2.0, "the half stands"
    assert out["peak_rss_mb"] is None or out["peak_rss_mb"] > 0


def test_a_lease_another_writer_took_is_never_committed_beside(tmp_path, monkeypatch) -> None:
    """The same fence when the next worker has already taken the released lease: the pass
    rolls back, and the row stays the next worker's."""
    from autodedup import incremental_lane, rt_lease
    from autodedup.incremental_lane import LEASE_LOST, run_incremental
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    original = incremental_lane.run_pass_bounded

    def taken_mid_pass(*args, **kwargs):
        result = original(*args, **kwargs)
        other = world.other_session()
        rt_lease.release(other, "worker:1:1")
        assert rt_lease.take(other, "next-worker:1:2", 2_400)
        return result

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", taken_mid_pass)
    out = run_incremental(lambda: world, holder="worker:1:1")

    assert out["aborted"] == LEASE_LOST and not world.rt_fp
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == ("next-worker:1:2", True)


def test_a_live_lease_passes_the_fence_and_the_pass_commits(tmp_path) -> None:
    from autodedup.incremental_lane import run_incremental
    from autodedup.incremental_sql import RT_LEASE_HOLD_SQL
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    out = run_incremental(lambda: world, holder="worker:1:1", stopping=lambda: False)

    assert out["aborted"] == "" and world.rt_fp, "decided and committed"
    assert RT_LEASE_HOLD_SQL in world.statements_in_tx


def test_a_shutdown_after_the_commit_starts_no_merge(tmp_path, monkeypatch) -> None:
    """Out of the build, the reconcile would merge under a lease the signal may have released
    already: it is skipped once the signal came (the re-cut's condition reads it too)."""
    from autodedup.incremental import bootstrap_key
    from autodedup.incremental_lane import run_incremental
    from tests.autodedup import lane_world

    from autodedup import rt_lease

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    world.settings[bootstrap_key()] = False
    monkeypatch.setattr(reconcile, "run", lambda *a, **k: pytest.fail("no merge after SIGTERM"))
    flag = {"stopping": False}
    original_hold = rt_lease.hold

    def hold_then_signal(conn, holder) -> None:
        original_hold(conn, holder)
        flag["stopping"] = True              # the signal lands as the pass commits

    monkeypatch.setattr(rt_lease, "hold", hold_then_signal)
    out = run_incremental(lambda: world, stopping=lambda: flag["stopping"])

    assert out["aborted"] == "" and out["reconcile"] == {"skipped": "stopping"}
    assert world.rt_fp, "the pass itself committed: the signal came after it"
    from autodedup.incremental_lane import STORAGE_WATERMARK, pass_rate_key

    assert world.settings_by[pass_rate_key("rt")].endswith(":halved_ahead"), (
        "no rate written after the signal: the next holder's row is not this pass's")
    assert STORAGE_WATERMARK not in world.settings, "nor the storage watermark"


def test_a_shutdown_mid_reconcile_stops_it_before_its_next_group(tmp_path, monkeypatch) -> None:
    """The reconcile checks its clock before every group; the pass hands it one that reads
    +inf once the signal came, so it stops the way a spent deadline stops it."""
    import math

    from autodedup.incremental import bootstrap_key
    from autodedup.incremental_lane import run_incremental
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    world.settings[bootstrap_key()] = False
    flag = {"stopping": False}
    seen: dict = {}

    def reconcile_run(*_args, **kwargs):
        clock = kwargs["clock"]
        seen["before"] = clock()
        flag["stopping"] = True
        seen["after"] = clock()
        return {"counts": {"groups": 0}, "stopped": "the pass's time is spent"}

    monkeypatch.setattr(reconcile, "run", reconcile_run)
    run_incremental(lambda: world, stopping=lambda: flag["stopping"])

    assert math.isfinite(seen["before"]) and math.isinf(seen["after"])


# --------------------------------------------- E941: every writer is fenced, not only the pass


def test_a_lease_lost_mid_reconcile_rolls_the_group_back_and_stops() -> None:
    """Each group merges in its own transaction, and each ends with the lane's lease check:
    the group whose check finds the lease gone (the worker's shutdown, a `release_lease=`
    dispatch) rolls back whole and files nothing — no `failed` row without the lease — and
    nothing after it is attempted."""
    from autodedup import rt_lease

    db = LaneDb()
    db.live_scope()
    for key in (10, 20, 30):
        _pair_group(db, key, [key, key + 1], [key * 100, key * 100 + 1], gen=RT)
        _read(db, key, key + 1)
    calls: list = []
    checks: list = []

    def fence(conn) -> None:
        checks.append(conn)
        if len(checks) >= 2:
            raise rt_lease.LeaseLost("autodedup.rt_lease is no longer held by 'worker:1:1'")

    out = reconcile.run(db, RT, [], run_id="rt:test", deadline=10 ** 9, blocks=[BLOCK],
                        merge=db.merge(calls), clock=lambda: 0.0, now=lambda: db.now,
                        fence=fence)

    assert out["stopped"] == "lease_lost"
    assert (out["counts"]["applied"], out["counts"]["failed"],
            out["counts"]["not_attempted"]) == (1, 0, 2)
    assert db.listings[11]["property_id"] == 1000, "the first group merged under the lease"
    assert db.listings[21]["property_id"] == 2001, "the second rolled back whole"
    assert [r["outcome"] for r in db.ledger] == ["applied"], "and filed nothing"
    assert len(calls) == 2 and len(checks) == 2, "the third was never attempted"


def test_a_skip_filed_at_apply_is_fenced_too() -> None:
    """The re-check's skip row is a write of its own transaction: without the lease it is not
    filed either, and the run stops."""
    from autodedup import rt_lease

    db = LaneDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _read(db, 10, 11)

    def merge_never(*_a, **_k):
        raise AssertionError("the re-check skips before the chokepoint")

    def gone(conn) -> None:
        raise rt_lease.LeaseLost("gone")

    scope = AP.effective_scope(db.settings[AP.SCOPE_SETTING], {}, live=True)
    (planned,) = AP.plan_apply(db, RT, scope).to_apply
    db.listing(11, 999)                       # moved since the plan: the re-check skips it

    with pytest.raises(rt_lease.LeaseLost):
        AP.apply_group(db, planned, scope, run_id="rt:test", generation=RT, merge=merge_never,
                       fence=gone)
    assert not db.ledger, "no skip row without the lease"


def test_the_pass_hands_its_reconcile_and_its_re_cut_the_lease_check(tmp_path,
                                                                      monkeypatch) -> None:
    """The fence the reconcile gets is the pass's own: its holder's live row. And the re-cut —
    the calibration rewritten after the pass committed — ends its transaction with it too."""
    from autodedup import incremental_lane, rt_lease
    from autodedup.incremental import bootstrap_key
    from autodedup.incremental_lane import run_incremental
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    world.settings[bootstrap_key()] = False
    seen: dict = {}

    def reconcile_run(*_a, **kwargs):
        fence = kwargs["fence"]
        with world.transaction():
            fence(world)                                   # held: passes
        rt_lease.release(world.other_session(), "worker:1:1")
        with pytest.raises(rt_lease.LeaseLost), world.transaction():
            fence(world)
        rt_lease.take(world.other_session(), "worker:1:1", 2_400)   # back, for the re-cut
        seen["fenced"] = True
        return {"counts": {"groups": 0}}

    original_cut = incremental_lane.cut_calibration

    def cut_while_the_lease_ends(conn, *args, **kwargs):
        out = original_cut(conn, *args, **kwargs)
        rt_lease.release(world.other_session(), "worker:1:1")
        return out

    monkeypatch.setattr(reconcile, "run", reconcile_run)
    monkeypatch.setattr(incremental_lane, "cut_calibration", cut_while_the_lease_ends)
    monkeypatch.setattr(incremental_lane, "COVERAGE_FLOOR", 1.5)     # force the re-cut
    monkeypatch.setattr(incremental_lane, "RECUT_MIN_IMAGES", 1)
    monkeypatch.setattr(incremental_lane, "RECUT_MIN_AGE_H", 0.0)
    digest = world.calibration["rt"]["digest"]

    out = run_incremental(lambda: world, holder="worker:1:1")

    assert seen == {"fenced": True}
    assert out["recut"]["skipped"].startswith("LeaseLost"), out.get("recut")
    assert world.calibration["rt"]["digest"] == digest, "the re-cut rolled back"


def test_a_reconcile_that_lost_the_lease_ends_the_passs_writes(tmp_path, monkeypatch) -> None:
    """After a reconcile stopped by its fence the lane is someone else's: no measured rate, no
    re-cut, no storage watermark — as after the shutdown signal."""
    from autodedup.incremental import bootstrap_key
    from autodedup.incremental_lane import STORAGE_WATERMARK, pass_rate_key, run_incremental
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    world.settings[bootstrap_key()] = False
    world.settings[pass_rate_key("rt")] = 0.0001       # a measured pass would write "rate"
    monkeypatch.setattr(reconcile, "run", lambda *a, **k: {
        "counts": {"groups": 3, "not_attempted": 2}, "stopped": "lease_lost"})

    out = run_incremental(lambda: world, holder="worker:1:1")

    assert out["reconcile"]["stopped"] == "lease_lost"
    assert not world.settings_by[pass_rate_key("rt")].endswith(":rate")
    assert STORAGE_WATERMARK not in world.settings


# ------------------------------------- E941: the pass itself reads the signal at its checkpoints


def test_the_fact_read_stops_at_a_slice_boundary_once_the_worker_is_stopping(monkeypatch) -> None:
    from autodedup import incremental_lane
    from autodedup.incremental import PassStopped
    from autodedup.incremental_lane import SqlFacts
    from tests.autodedup import lane_world

    world = lane_world.world()
    ids = sorted(int(listing_id) for listing_id in world.listings)
    monkeypatch.setattr(incremental_lane, "FACT_CHUNK", 2)
    flag = {"stopping": False}
    facts = SqlFacts(world, deadline=10 ** 12, stopping=lambda: flag["stopping"])
    whole = facts.facts(ids)
    assert len(whole) == len(ids), "not stopping: the whole read"

    flag["stopping"] = True
    stopped = SqlFacts(world, deadline=10 ** 12, stopping=lambda: flag["stopping"])
    with pytest.raises(PassStopped):
        stopped.facts(ids)
    assert stopped.statements == 0, "stopped before its first slice"


def test_a_signal_mid_pass_rolls_it_back_on_its_own_connection(tmp_path, monkeypatch) -> None:
    """The doomed pass stops at its next checkpoint, rolls back on its OWN live connection —
    its row locks free at once, not at SIGKILL 30 s later, where the next worker's first pass
    would wait them out and halve its rate again — and releases its lease the way every pass
    ends."""
    from autodedup import incremental_lane, rt_lease
    from autodedup.incremental_lane import PASS_RATE_PER_S, pass_rate_key, run_incremental
    from autodedup.incremental_sql import RT_LEASE_RELEASE_SQL
    from tests.autodedup import lane_world

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    cursors = {k: dict(v) for k, v in world.cursors.items()}
    rolled_back = world.rolled_back
    releases = world.statements.count(RT_LEASE_RELEASE_SQL)
    flag = {"stopping": False}
    original = incremental_lane.SqlFacts.facts

    def signal_during_the_first_read(self, *args, **kwargs):
        flag["stopping"] = True                       # SIGTERM, while the pass decides
        return original(self, *args, **kwargs)

    monkeypatch.setattr(incremental_lane.SqlFacts, "facts", signal_during_the_first_read)

    out = run_incremental(lambda: world, holder="worker:1:1",
                          stopping=lambda: flag["stopping"])

    assert out["aborted"] == "stopping" and out["reconcile"] == {"skipped": "pass_stopping"}
    assert world.rolled_back == rolled_back + 1
    assert world.cursors == cursors and not world.rt_fp and not world.pairs
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == ("worker:1:1", False), "released by the pass itself"
    assert world.statements.count(RT_LEASE_RELEASE_SQL) == releases + 1
    assert world.settings[pass_rate_key("rt")] == PASS_RATE_PER_S / 2.0
    assert world.settings_by[pass_rate_key("rt")].endswith(":halved_ahead"), "nothing after"
