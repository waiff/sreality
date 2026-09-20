"""The SQL half of the real-time lane, exercised — the gap W9's verification found.

W9 proved replay equivalence through the in-memory twin and shipped a SQL adapter that no test
touched. All four of the blocking defects lived there: the fingerprint row was never written
(so the dirty set was always empty and every pass reported green over zero work), the
certificate and the evidence families were dropped on read-back (so a component clustered in a
different edge order than the cohort pass), the families bitmask was written as a count, and
E64's rail queried with an empty id array. Each has a test below, and above them all sits the
one that would have caught every one: the SAME cohort replayed through `SqlStore` and through
`MemoryStore`, compared at the pair and the cluster grain.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from autodedup.incremental import (
    FpRow,
    GuardRow,
    Limits,
    PairRow,
    run_pass,
    run_pass_bounded,
)
from autodedup.incremental_lane import (
    CURSOR_CHANGED,
    CURSOR_FLIPPED,
    CURSOR_NEW,
    CURSOR_REVIVE,
    LEASE_TTL_S,
    SqlStore,
    SqlWork,
    db_enabled,
)
from autodedup.dataset import Dataset, Image, Listing, Location, Meta
from autodedup.hazard_context import ContextStamp
from autodedup.incremental_store import MemoryStore
from autodedup.model import hand_initialised
from autodedup.replay import DatasetFacts, ScheduleWork, arrival_order, batch_state
from autodedup.score_lane import FAMILY_BITS, families_bitmask, families_of_bitmask
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_incremental import (
    _calibration,
    _dataset,
    _drain,
    _listing,
    _settings,
)

GEN = "rt"


def _twins_dataset(pairs: int = 3) -> Dataset:
    """Three genuine duplicates: one order syndicated to two portals, three times over.

    A cohort of near-identical adverts produces only band pairs, and a band-only cohort cannot
    exercise the two things the SQL store nearly lost — the CERTIFICATE (E33 orders a
    component's edges by it) and a cluster to order. Each pair here shares an agency order
    code, which is E60's K-R certificate: one order, one flat, on two portals."""
    listings: dict[int, Listing] = {}
    images: dict[int, list[Image]] = {}
    for index in range(pairs):
        base = 3000 + index * 10
        for side, (source, native) in enumerate(
                (("sreality", f"s{index}"), ("bezrealitky", f"b{index}"))):
            listing_id = base + side
            listings[listing_id] = _listing(
                listing_id, source=source, source_id_native=native,
                area_m2=62.0 + index, price=6_000_000.0 + index * 100_000,
                description=(f"Prodej bytu 2+kk o vymere {62 + index} m2 v cihlovem dome po "
                             f"rekonstrukci, evidencni cislo zakazky N11{index}423. " * 4),
                first_seen_at=f"2026-0{index + 1}-05T08:00:00+00:00",
                location=Location(obec_kod=554782, cast_obce_kod=490245,
                                  street_key=f"hlavni{index}", house_number_cp=str(10 + index),
                                  lat=50.1 + index * 0.01, lon=14.4,
                                  ruian_adm_kod=1000 + index))
            images[listing_id] = [
                Image(listing_id=listing_id, image_id=listing_id * 10 + seq, seq=seq,
                      phash=index * 104_729 + seq * 13, pop=1)
                for seq in range(6)
            ]
    return Dataset(meta=Meta(), listings=listings, images_by_listing=images)


def _sql_drain(ds, settings, calibration, order, batch: int = 5, db: FakePg | None = None,
               limits: Limits | None = None):
    conn = db or FakePg()
    store = SqlStore(conn, GEN)
    facts = DatasetFacts(ds)
    work = ScheduleWork(list(order))
    model = hand_initialised()
    caps = limits or Limits(max_listings=batch, max_pairs=10 ** 9, max_component=400)
    passes = []
    while not work.exhausted():
        before = work.cursor
        passes.append(run_pass_bounded(store, facts, work, settings, model, calibration,
                                       limits=caps, generation=GEN))
        if work.cursor == before:
            break
    return conn, store, passes


def _sql_state(conn: FakePg):
    pairs = {(lo, hi): {
        "zone": "veto" if row["guard_veto"] else row["zone"],
        "score": round(float(row["score"]), 9),
        "reason": row["decision"],
        "certificate": row["certificate"],
        "families": families_of_bitmask(row["families"]),
        "probes": sorted(row["probes"]),
    } for (gen, lo, hi), row in conn.pairs.items() if gen == GEN}
    clusters: dict[int, list[int]] = {}
    for gen, key, listing_id in conn.cluster_members:
        if gen == GEN:
            clusters.setdefault(key, []).append(listing_id)
    return pairs, {key: sorted(ids) for key, ids in clusters.items()}


def _memory_state(store: MemoryStore):
    pairs = {key: {
        "zone": row.zone,
        "score": round(float(row.score), 9),
        "reason": row.reason,
        "certificate": row.certificate,
        "families": sorted(row.families),
        "probes": sorted(row.probes),
    } for key, row in store.pairs.items()}
    return pairs, {key: sorted(ids) for key, ids in store.clusters.items()}


# ------------------------------------------------------------ the equivalence that matters


@pytest.mark.parametrize("cohort", ["near_duplicates", "twins"])
def test_the_sql_store_reaches_the_same_state_as_the_twin_and_the_cohort_pass(cohort) -> None:
    ds = _dataset(24) if cohort == "near_duplicates" else _twins_dataset(4)
    settings = _settings()
    calibration = _calibration(ds, settings)
    order = arrival_order(ds)

    memory, _passes = _drain(ds, settings, calibration, order)
    conn, _store, _sql_passes = _sql_drain(ds, settings, calibration, order)
    sql_pairs, sql_clusters = _sql_state(conn)
    mem_pairs, mem_clusters = _memory_state(memory)

    assert sql_pairs == mem_pairs
    assert sql_clusters == mem_clusters

    reference, batch_clusters, _timings = batch_state(ds, settings, hand_initialised())
    assert set(sql_pairs) == set(reference)
    for key, row in sql_pairs.items():
        assert row["zone"] == reference[key]["zone"], key
        assert row["certificate"] == reference[key]["certificate"], key
        assert row["families"] == reference[key]["families"], key
    assert sorted(sorted(v) for v in sql_clusters.values()) == \
        sorted(sorted(v) for v in batch_clusters.values())


# ---------------------------------------------------------------------- F1: the fp row


def test_the_fingerprint_row_is_written_so_the_dirty_set_is_not_empty() -> None:
    """The first blocker: no `rt_fp` write meant `known()` was empty and nothing was scored."""
    ds = _dataset(12)
    settings = _settings()
    calibration = _calibration(ds, settings)
    conn, store, passes = _sql_drain(ds, settings, calibration, arrival_order(ds))
    assert len({i for gen, i in conn.rt_fp if gen == GEN}) == len(ds.listings)
    assert store.known(sorted(ds.listings)) == set(ds.listings)
    assert sum(p.pairs_scored for p in passes) > 0
    assert sum(p.dirty for p in passes) > 0
    # And the guard columns come back, so the rule floor can refuse before the fan-out cap.
    listing_id = sorted(ds.listings)[0]
    row = store.rows([listing_id])[listing_id]
    assert row.guard.category_main == ds.listings[listing_id].category_main
    assert row.digest and row.cell_key


def test_a_second_drain_scores_nothing_through_the_sql_store() -> None:
    ds = _dataset(12)
    settings = _settings()
    calibration = _calibration(ds, settings)
    conn, _store, _passes = _sql_drain(ds, settings, calibration, arrival_order(ds))
    before = _sql_state(conn)
    _conn, _store2, again = _sql_drain(ds, settings, calibration, arrival_order(ds), db=conn)
    assert sum(p.pairs_scored for p in again) == 0
    assert sum(p.pairs_deleted for p in again) == 0
    assert _sql_state(conn) == before


# -------------------------------------------------------- F2/F3: certificate and families


@pytest.mark.parametrize("families,certificate", [
    (["IMG"], "K-B"),
    (["IMG", "TXT"], None),
    (["ATTR", "BRK", "LOC"], "K-R"),
    ([], None),
])
def test_a_pair_round_trips_its_certificate_and_its_families(families, certificate) -> None:
    conn = FakePg()
    store = SqlStore(conn, GEN)
    row = PairRow(lo=10, hi=20, probes=["img"], from_lo=True, from_hi=False, zone="merge",
                  score=0.9, families=sorted(families), certificate=certificate, veto=None,
                  reason="model", evidence={"a": "b"}, context={"block": "o1"},
                  fp_lo="d1", fp_hi="d2")
    store.upsert_pairs([row])
    back = store.pairs_touching([10])[(10, 20)]
    assert back.certificate == certificate
    assert back.families == sorted(families)
    assert (back.zone, back.reason, back.fp_lo, back.fp_hi) == ("merge", "model", "d1", "d2")
    # The column itself is the W5 bitmask, which is what `families & 32` filters on.
    assert conn.pairs[(GEN, 10, 20)]["families"] == families_bitmask(families)


def test_the_families_column_is_a_bitmask_not_a_count() -> None:
    assert families_bitmask(["IMG"]) == FAMILY_BITS["IMG"] == 32
    assert families_bitmask(["IMG", "TXT"]) == 36
    assert families_of_bitmask(36) == ["IMG", "TXT"]
    for name, bit in FAMILY_BITS.items():
        assert families_of_bitmask(bit) == [name]


def test_a_certified_merge_survives_the_round_trip_and_clusters() -> None:
    """E33 orders a component's edges certificate-first, off the STORE. W9's second blocker
    hard-coded `certificate=None` on read-back, so production unioned in score order."""
    ds = _twins_dataset()
    settings = _settings()
    calibration = _calibration(ds, settings)
    conn, store, _passes = _sql_drain(ds, settings, calibration, arrival_order(ds))
    certified = [(lo, hi) for (gen, lo, hi), row in conn.pairs.items()
                 if gen == GEN and row["certificate"]]
    assert certified, "the twin cohort must produce certificate edges"
    for lo, hi in certified:
        assert conn.pairs[(GEN, lo, hi)]["certificate"] == "K-R"
        assert store.pairs_within([lo, hi])[0].certificate == "K-R"
    reference, batch_clusters, _timings = batch_state(ds, settings, hand_initialised())
    assert _sql_state(conn)[1] and len(_sql_state(conn)[1]) == len(batch_clusters)
    assert sorted(sorted(v) for v in _sql_state(conn)[1].values()) == \
        sorted(sorted(v) for v in batch_clusters.values())
    # And the cluster row says the edge was certified, which is what the UI counts.
    assert all(int(row["n_certificate_edges"]) >= 1 for row in conn.clusters.values())


# ------------------------------------------------------------------------- F4: the rail


def test_stamped_merges_reads_the_blocks_it_was_given() -> None:
    conn = FakePg()
    store = SqlStore(conn, GEN)
    stamped = PairRow(lo=1, hi=2, probes=[], from_lo=True, from_hi=True, zone="merge",
                      score=0.95, families=["IMG", "TXT"], certificate=None, veto=None,
                      reason="context_rule:fungible",
                      evidence=ContextStamp(4, 1).to_evidence(),
                      context={"block": "o554782"}, fp_lo="a", fp_hi="b")
    elsewhere = replace(stamped, lo=3, hi=4, context={"block": "o999"})
    store.upsert_pairs([stamped, elsewhere])
    assert [(lo, hi) for lo, hi, *_rest in store.stamped_merges(["o554782"])] == [(1, 2)]
    assert store.stamped_merges([]) == []
    assert len(store.stamped_merges(["o554782", "o999"])) == 2


def test_the_rail_is_offered_the_blocks_this_pass_touched() -> None:
    ds = _dataset(12)
    settings = _settings()
    calibration = _calibration(ds, settings)
    _conn, _store, passes = _sql_drain(ds, settings, calibration, arrival_order(ds))
    # The limb is on in the default settings, so the rail is genuinely owed and returns a
    # plan (an empty one, here) rather than the all-zero answer an empty id array gives.
    assert any("reopened" in p.rail for p in passes)


# --------------------------------------------------------- F5/F6/F9: the watermark feeds


def _flip_db(n: int, stamp: datetime) -> FakePg:
    db = FakePg()
    for index in range(n):
        db.listings[100 + index] = {
            "first_seen_at": db.now - timedelta(days=30),
            "inactive_at": stamp, "is_active": False}
    return db


def test_a_delisting_tie_group_larger_than_the_share_is_never_skipped() -> None:
    """`inactive_at` is the TRANSACTION timestamp, so one `mark_inactive` batch shares one
    stamp. Paging on the timestamp alone skipped every row past the limit for ever."""
    stamp = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    db = _flip_db(9, stamp)
    work = SqlWork(db, GEN, revive_slice=0)
    seen: list[int] = []
    for _ in range(12):
        items = work.claim(4)  # share = 1
        if not items:
            break
        seen.extend(item.listing_id for item in items if item.feed == "flipped")
        work.commit(items)
    assert sorted(set(seen)) == sorted(db.listings)
    assert len(seen) == len(set(seen)), "a keyset cursor must not re-serve a row"


def test_a_straggler_that_committed_after_the_cursor_is_still_claimed() -> None:
    db = FakePg()
    for listing_id in (10, 11, 12):
        db.listings[listing_id] = {"first_seen_at": db.now - timedelta(hours=1),
                                   "inactive_at": None, "is_active": True}
    db.cursors[CURSOR_NEW] = {"last_listing_id": 12}
    db.rt_fp[(GEN, 11)] = {"category_main": None, "category_type": None, "area_m2": None,
                           "disposition": None, "floor": None, "fp_digest": "d",
                           "cell_key": "o1", "cell_group": "byt", "is_active": True}
    work = SqlWork(db, GEN, revive_slice=0)
    items = work.claim(8)
    stragglers = {item.listing_id for item in items if item.feed == "straggler"}
    assert stragglers == {10, 12}
    # A straggler carries no cursor value: it is behind the watermark by definition.
    assert all(item.cursor is None for item in items if item.feed == "straggler")
    assert work.commit(items).get(CURSOR_NEW) is None


def test_a_row_younger_than_the_settle_lag_is_left_for_the_next_pass() -> None:
    db = FakePg()
    db.listings[1] = {"first_seen_at": db.now - timedelta(hours=1), "inactive_at": None,
                      "is_active": True}
    db.listings[2] = {"first_seen_at": db.now, "inactive_at": None, "is_active": True}
    work = SqlWork(db, GEN, revive_slice=0)
    items = work.claim(8)
    assert [item.listing_id for item in items if item.feed == "new"] == [1]


def test_a_revived_listing_reaches_the_lane() -> None:
    """`touch_listings` sets `is_active = true, inactive_at = null` on the same id and appends
    no snapshot, so all three forward cursors are blind to it."""
    db = FakePg()
    db.listings[7] = {"first_seen_at": db.now - timedelta(days=9), "inactive_at": None,
                      "is_active": True}
    db.rt_fp[(GEN, 7)] = {"category_main": None, "category_type": None, "area_m2": None,
                          "disposition": None, "floor": None, "fp_digest": "d",
                          "cell_key": "o1", "cell_group": "byt", "is_active": False}
    work = SqlWork(db, GEN)
    items = work.claim(8)
    assert [item.listing_id for item in items if item.feed == "revived"] == [7]
    cursors = work.commit(items)
    assert cursors[CURSOR_REVIVE] == 0, "a short slice wraps the round-robin sweep"


def test_each_feed_advances_only_its_own_cursor() -> None:
    db = FakePg()
    db.listings[5] = {"first_seen_at": db.now - timedelta(hours=2), "inactive_at": None,
                      "is_active": True}
    db.snapshots.append({"id": 99, "listing_id": 5, "scraped_at": db.now - timedelta(hours=2)})
    work = SqlWork(db, GEN, revive_slice=0)
    items = work.claim(8)
    out = work.commit(items)
    assert out[CURSOR_NEW] == 5
    assert out[CURSOR_CHANGED] == 99
    assert CURSOR_FLIPPED not in out


def test_a_refused_pass_advances_no_cursor() -> None:
    db = FakePg()
    db.listings[5] = {"first_seen_at": db.now - timedelta(hours=2), "inactive_at": None,
                      "is_active": True}
    work = SqlWork(db, GEN, revive_slice=0)
    work.claim(8)
    work.commit([])  # the pair budget refused this claim
    assert db.cursors.get(CURSOR_NEW, {}).get("last_listing_id") is None


# --------------------------------------------------------------- F7: the pair budget


def test_a_pass_that_cannot_fit_its_pair_set_writes_nothing() -> None:
    ds = _dataset(16)
    settings = _settings()
    calibration = _calibration(ds, settings)
    conn = FakePg()
    store = SqlStore(conn, GEN)
    facts = DatasetFacts(ds)
    work = ScheduleWork(arrival_order(ds))
    result = run_pass(store, facts, work, settings, hand_initialised(), calibration,
                      limits=Limits(max_listings=16, max_pairs=1), generation=GEN)
    assert result.aborted == "pair_budget"
    assert result.wanted_pairs > 1
    assert result.pairs_written == 0 and result.pairs_deleted == 0
    assert not conn.pairs
    assert work.cursor == 0, "a refused claim must not move the watermark"


class _RecordingWork(ScheduleWork):
    """A `ScheduleWork` that remembers what each attempt asked for."""

    def __init__(self, order) -> None:
        super().__init__(order)
        self.asked: list[int] = []

    def claim(self, limit: int):
        self.asked.append(limit)
        return super().claim(limit)


def test_the_bounded_runner_shrinks_the_claim_before_it_gives_up() -> None:
    """E70: a refused claim is re-claimed smaller, and a refusal that survives that STOPS.

    A pass's pair count is driven by the BLOCKS its arrivals touch rather than by how many it
    claimed, so shrinking helps where a claim spans many blocks and cannot where one block
    already exceeds the budget. Both halves are the same rule: never write a partial pair set."""
    ds = _dataset(16)
    settings = _settings()
    calibration = _calibration(ds, settings)
    conn = FakePg()
    store = SqlStore(conn, GEN)
    work = _RecordingWork(arrival_order(ds))
    result = run_pass_bounded(store, DatasetFacts(ds), work, settings, hand_initialised(),
                              calibration, limits=Limits(max_listings=16, max_pairs=1),
                              generation=GEN)
    assert work.asked == [16, 4, 1], "each refusal must re-claim a smaller slice"
    assert result.attempts == 3, "the ladder stops at a one-listing claim"
    # The third attempt fits — one arrival against an empty store has no candidate at all —
    # so the watermark moves over exactly that one listing and over nothing that was refused.
    assert not result.aborted
    assert work.cursor == 1
    assert not conn.pairs and not conn.cluster_members
    assert len(conn.rt_fp) == 1


def test_a_generous_budget_is_never_refused() -> None:
    ds = _dataset(16)
    settings = _settings()
    calibration = _calibration(ds, settings)
    _conn, _store, passes = _sql_drain(ds, settings, calibration, arrival_order(ds))
    assert all(not p.aborted and p.attempts == 1 for p in passes)


# ------------------------------------------------------- F8: a late must-not-link row


def test_an_operator_must_not_link_added_after_the_merge_is_honoured() -> None:
    ds = _twins_dataset()
    settings = _settings()
    calibration = _calibration(ds, settings)
    conn, store, _passes = _sql_drain(ds, settings, calibration, arrival_order(ds))
    grouped = [(key, sorted(ids)) for key, ids in _sql_state(conn)[1].items()
               if len(ids) > 1]
    assert grouped, "the twin cohort must produce a multi-member cluster"
    _key, members = grouped[0]
    conn.mnl.add((members[0], members[1]))

    facts = DatasetFacts(ds)
    work = ScheduleWork([])  # an IDLE pass: the operator row moves no pair
    result = run_pass(store, facts, work, settings, hand_initialised(), calibration,
                      limits=Limits(), generation=GEN)
    assert result.components >= 1
    after = _sql_state(conn)[1]
    assert not any(members[0] in ids and members[1] in ids for ids in after.values())


# ------------------------------------------------------------- F14: the census cell


def test_a_listing_that_moves_unbumps_the_cell_it_left() -> None:
    ds = _dataset(8)
    settings = _settings()
    calibration = _calibration(ds, settings)
    conn, store, _passes = _sql_drain(ds, settings, calibration, arrival_order(ds))
    listing_id = sorted(ds.listings)[0]
    old_cell = store.rows([listing_id])[listing_id].cell
    before = conn.cells[(GEN, old_cell[0], old_cell[1])]["n_listings"]

    moved = ds.listings[listing_id]
    moved.location = replace(moved.location, obec_kod=500000, cast_obce_kod=500001,
                             ruian_adm_kod=987654)
    facts = DatasetFacts(ds)
    work = ScheduleWork([listing_id])
    run_pass(store, facts, work, settings, hand_initialised(), calibration,
             limits=Limits(), generation=GEN)
    store.flush()
    new_cell = store.rows([listing_id])[listing_id].cell
    assert new_cell != old_cell
    assert conn.cells[(GEN, old_cell[0], old_cell[1])]["n_listings"] == before - 1
    assert conn.cells[(GEN, new_cell[0], new_cell[1])]["n_listings"] == 1


# ------------------------------------------------------------ F10/F11/F12: the rest


def test_the_calibration_digest_is_a_function_of_content_not_of_build_time() -> None:
    ds = _dataset(6)
    settings = _settings()
    left = _calibration(ds, settings)
    right = replace(left, built_at="2020-01-01T00:00:00+00:00")
    assert left.digest() == right.digest()
    moved = replace(left, corpus_docs=left.corpus_docs + 1)
    assert moved.digest() != left.digest()


def test_a_pass_is_bounded_in_statements() -> None:
    """E69: the per-key spelling cost ~17,700 statements for a 166-listing claim."""
    ds = _dataset(24)
    settings = _settings()
    calibration = _calibration(ds, settings)
    conn = FakePg()
    store = SqlStore(conn, GEN)
    facts = DatasetFacts(ds)
    work = ScheduleWork(arrival_order(ds))
    run_pass(store, facts, work, settings, hand_initialised(), calibration,
             limits=Limits(max_listings=24, max_pairs=10 ** 9), generation=GEN)
    # One pass over the whole 24-listing cohort, every pair of it scored.
    assert store.statements < 120, store.statements


def test_the_database_kill_switch_stops_a_lane_that_is_already_on() -> None:
    db = FakePg()
    assert db_enabled(db) is True           # absent means not blocked
    db.settings["realtime_enabled"] = False
    assert db_enabled(db) is False
    db.settings["realtime_enabled"] = {"enabled": True}
    assert db_enabled(db) is True


def test_the_lease_outlives_the_job_timeout() -> None:
    """A lease that expires while its holder still runs invites a second writer."""
    assert LEASE_TTL_S > 25 * 60


def test_the_store_serves_a_fingerprint_row_it_has_not_flushed_yet() -> None:
    conn = FakePg()
    store = SqlStore(conn, GEN)
    row = FpRow(GuardRow(5, "byt", "prodej", 60.0, "2+kk", 3), "digest", "o1", "byt", True)
    store.put_listing(5, row, [("img", "t1")])
    assert store.rows([5])[5] == row
    assert store.lookup("img", "t1") == [5]
    assert store.known([5]) == {5}


# ------------------------------------------------------------- E71: seeding a generation


def test_rt_seed_cuts_the_calibration_and_starts_the_cursors_at_today(tmp_path, monkeypatch):
    """Without the seed the lane has no calibration at all and would walk history from id 0."""
    from autodedup.incremental_lane import ENV_FLAG, CURSOR_REVIVE, run_rt_seed
    from tests.autodedup.test_dataset import RECORDS, _write

    artifact = _write(tmp_path / "cohort.jsonl.gz", RECORDS)
    conn = FakePg()
    conn.listings[9_001] = {"first_seen_at": conn.now, "inactive_at": None, "is_active": True}
    conn.snapshots.append({"id": 4_242, "listing_id": 9_001, "scraped_at": conn.now})
    monkeypatch.setenv(ENV_FLAG, "true")

    out = run_rt_seed(lambda: conn, {"artifact": str(artifact), "backfill": "true"}, tmp_path)

    assert out["calibration_digest"] and out["calibration_n_listings"] >= 1
    assert out["backfilled"] >= 1
    # The cursors start at the corpus's maxima, not at zero.
    assert conn.cursors[CURSOR_NEW]["last_listing_id"] == 9_001
    assert conn.cursors[CURSOR_CHANGED]["last_snapshot_id"] == 4_242
    assert conn.cursors[CURSOR_REVIVE]["last_listing_id"] == 0
    # The calibration is readable back as the one the lane would run under.
    stored = conn.calibration[GEN]
    assert stored["digest"] == out["calibration_digest"]
    assert conn.rt_fp and conn.fp_key
    # And it is dark like everything else in this lane.
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert run_rt_seed(lambda: conn, {"artifact": str(artifact)}, tmp_path)["skipped"] == "dark"


# --------------------------------------------- E72: the lane's facts ARE the export's facts


def _seed_public(conn: FakePg, listing_id: int = 4_242) -> None:
    conn.listings[listing_id] = {
        "id": listing_id, "source": "sreality", "source_id_native": "n1",
        "source_url": "https://example.test/1", "category_main": "byt",
        "category_type": "prodej", "subtype": None, "disposition": "2+kk",
        "area_m2": 62.0, "floor": 3, "total_floors": 6, "price_czk": 6_200_000,
        "price_unit": "celkem", "area_basis": "uzitna", "has_balcony": True,
        "has_parking": None, "has_lift": True, "building_type": "cihlova",
        "condition": "po_rekonstrukci", "energy_rating": "C", "estate_area": None,
        "usable_area": 62.0, "garden_area": None, "category_sub_cb": None,
        "furnished": "castecne", "terrace": None, "cellar": True, "garage": None,
        "parking_lots": None, "ownership": "osobni",
        "published_at": "2026-02-01T08:00:00+00:00",
        "description": "Prodej bytu 2+kk, evidencni cislo zakazky N115423. " * 4,
        "first_seen_at": conn.now - timedelta(days=9), "last_seen_at": conn.now,
        "inactive_at": None, "is_active": True,
        "broker_identity_id": 77, "broker_firm_id": 9,
        "broker_phone": "+420 777 123 456", "broker_email": "a@agency.cz",
    }
    conn.locations[listing_id] = {
        "listing_id": listing_id, "obec_kod": 554782, "obec_name": "Praha",
        "cast_obce_kod": 490245, "cast_obce_name": "Vysocany", "granularity": "address",
        "granularity_rank": 6, "is_address_grain": True, "lat": 50.1, "lon": 14.5,
        "uncertainty_radius_m": 5.0, "street_name": "Kolbenova", "house_number_cp": "12",
        "house_number_co": "3", "psc": "19000", "ruian_adm_kod": 22_349_841,
        "country_status": "cz",
    }
    conn.snapshots.extend([
        {"id": 1, "listing_id": listing_id, "scraped_at": conn.now - timedelta(days=9),
         "price_czk": 6_500_000},
        {"id": 2, "listing_id": listing_id, "scraped_at": conn.now - timedelta(days=2),
         "price_czk": 6_200_000},
    ])
    conn.image_rows.append({"image_id": 900, "listing_id": listing_id, "sequence": 0,
                            "storage_path": "img/900.jpg", "phash": 7_919})
    conn.clip_tags.append({"image_id": 900, "fine_tag": "kitchen_modern",
                           "logical_tag": "kitchen", "confidence": 0.81})
    conn.phash_pop[7_919] = 4


def test_the_lane_reads_the_facts_the_export_reads() -> None:
    """W9 hand-wrote the fact SQL. The schema gate caught `loc.lat` (the store keeps a geom);
    behind it sat the silent ones — no attrs, no price history, no broker key."""
    from autodedup.export import build_listing_record, street_key
    from autodedup.incremental_lane import SqlFacts

    conn = FakePg()
    _seed_public(conn)
    facts = SqlFacts(conn)
    listing, images = facts.facts([4_242])[4_242]

    # The attribute block the attr features read — absent in W9's version.
    assert listing.attrs["condition"] == "po_rekonstrukci"
    assert listing.attrs["has_lift"] is True
    assert "estate_area" not in listing.attrs, "an absent column is absent, never a zero"
    # E19's price events.
    assert [price for _stamp, price in listing.price_history] == [6_500_000.0, 6_200_000.0]
    # The BRK family's identity, salted exactly as the export salts it (E28).
    assert listing.broker_key and listing.broker_key == build_listing_record(
        conn.listings[4_242], block="", location=conn.locations[4_242])["broker_key"]
    # The location, through the export's own derivations: a geom, not lat/lon columns.
    assert (listing.location.lat, listing.location.lon) == (50.1, 14.5)
    assert listing.location.street_key == street_key("Kolbenova") == "kolbenova"
    assert listing.location.house_number == "12/3"
    assert listing.location.granularity == "address"
    assert listing.location.granularity_rank == 6 and listing.location.is_address_grain
    # The gallery, with the FROZEN population (E65) and the export's tag pairs.
    assert len(images) == 1
    assert images[0].phash == 7_919 and images[0].pop == 4
    assert images[0].tags == [("kitchen", 0.81), ("kitchen_modern", 0.81)]


def test_an_image_whose_hash_the_frozen_population_does_not_carry_reads_zero() -> None:
    from autodedup.incremental_lane import SqlFacts

    conn = FakePg()
    _seed_public(conn)
    conn.phash_pop.clear()
    _listing, images = SqlFacts(conn).facts([4_242])[4_242]
    assert images[0].pop == 0, "a hash the cohort never saw is unseen, not unknown"
