"""W9h: late evidence, a gate that could pass vacuously, and a calibration with no expiry.

Four defects the W9g verification left open, each reproduced here as a defect before it is
fixed:

* **V1 (E92/E93) — LATE EVIDENCE.** The lane claims an arrival 5-15 minutes after
  `first_seen_at`; its `phash` is written by an hourly job and its CLIP vector by another
  (p50 2.47 h, p90 4.51 h), so ~80% of arrivals were decided with every hash NULL, 78% never
  got a second snapshot, and not one of the six feeds was keyed on a hash or a vector
  ARRIVING. The listing was decided once, blind, and never again — its stored index keys
  never even gained a pHash posting, so no other listing's photo probe could reach it.
* **V2 (E94) — a gate that passed on nothing.** `checked` 0 with `breaches` 0 was `ok`, and
  one image's moved producer digest excused its listing's whole population check.
* **V3 (E95) — no staleness rail.** A frozen population can only UNDERCOUNT, which is the
  anti-conservative direction, and nothing anywhere refused on its age or its coverage.
* **V4 (E96) — a hash the export never saw.** It reads UNKNOWN, and the assertion here is
  that unknown BANDS rather than merging on photo evidence it does not have.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from autodedup.dataset import Image
from autodedup.incremental import (
    EVIDENCE_HOLD_REASON,
    Calibration,
    Evidence,
    EvidenceHold,
    FpRow,
    GuardRow,
    Limits,
    PairRow,
    evidence_of,
    run_pass,
)
from autodedup.incremental_lane import (
    EVIDENCE_HORIZON_HOURS,
    SqlStore,
    SqlWork,
    cut_calibration,
    run_incremental,
)
from autodedup.incremental_scope import Scope, ScopeBlock
from autodedup.incremental_store import MemoryStore
from autodedup.model import hand_initialised
from autodedup.replay import (
    DatasetFacts,
    ScheduleWork,
    arrival_order,
    batch_state,
    withheld_state,
)
from autodedup.export import encode_clip
from autodedup.settings import Settings
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_incremental import _dataset, _settings
from tests.autodedup.lane_world import GENERATION, seed_lane, true_population
from tests.autodedup.lane_world import world as lane_world

TOWN = Scope((ScopeBlock("obec", 563510),))


@pytest.fixture()
def world() -> FakePg:
    return lane_world()


# ============================================================ V1a: the evidence arrives late


def _blind(images: list[Image]) -> list[Image]:
    """A gallery whose photographs exist and have not been processed yet — production's
    shape for the first ten minutes to four hours of a listing's life."""
    return [replace(image, phash=None, pop=None, clip=None, tags=()) for image in images]


class _LateFacts:
    """The facts a listing has at its first claim, and what it has an hour later."""

    def __init__(self, ds: Any) -> None:
        self.ds = ds
        self.delivered: set[int] = set()
        self.reads = 0

    def facts(self, ids):
        out = {}
        for listing_id in ids:
            listing = self.ds.listings.get(listing_id)
            if listing is None:
                continue
            self.reads += 1
            images = self.ds.images(listing_id)
            out[listing_id] = (listing, images if listing_id in self.delivered
                               else _blind(images))
        return out


def _calibrated(ds: Any, settings: Settings) -> Calibration:
    from autodedup.fingerprint import build_all

    return Calibration.build(build_all(ds, settings), ds.listings, settings)


def test_a_listing_decided_before_its_photographs_carries_no_phash_posting() -> None:
    """THE DEFECT, stated as a fact about the store: a listing claimed while its gallery is
    unprocessed indexes no pHash band at all, so no other listing's photo probe can ever
    retrieve it — and W9's six feeds had nothing that would come back for it."""
    ds = _dataset(8)
    settings = _settings()
    calibration = _calibrated(ds, settings)
    store = MemoryStore(now=1_000.0)
    facts = _LateFacts(ds)
    order = arrival_order(ds)

    work = ScheduleWork(order)
    while not work.exhausted():
        run_pass(store, facts, work, settings, hand_initialised(), calibration,
                 limits=Limits(max_listings=50))

    probes = {probe for probe, _token in store.keys[order[0]]}
    assert "phash" not in probes, "a blind gallery indexes no pHash band"
    assert not store.fp[order[0]].evidence.complete, "and the store SAYS so (E92)"
    assert store.fp[order[0]].evidence.n_images == 3


def test_the_evidence_sweep_re_claims_the_listing_and_its_postings_gain_the_band() -> None:
    """The fix: re-decide when the evidence arrives. The same store, the same listings, and a
    re-claim once the producers have run — the pHash postings exist and the pairs are
    re-scored."""
    ds = _dataset(8)
    settings = _settings()
    calibration = _calibrated(ds, settings)
    store = MemoryStore(now=1_000.0)
    facts = _LateFacts(ds)
    order = arrival_order(ds)
    work = ScheduleWork(order)
    while not work.exhausted():
        run_pass(store, facts, work, settings, hand_initialised(), calibration,
                 limits=Limits(max_listings=50))
    stamped = store.fp[order[0]].first_decided_at

    facts.delivered = set(order)
    from autodedup.replay import RedecideWork

    sweep = RedecideWork(order)
    scored = 0
    while not sweep.exhausted():
        result = run_pass(store, facts, sweep, settings, hand_initialised(), calibration,
                          limits=Limits(max_listings=50))
        scored += result.pairs_scored

    assert scored > 0, "a re-claim re-scores the pairs the blind decision took"
    assert "phash" in {probe for probe, _t in store.keys[order[0]]}, (
        "and the postings now carry the band another listing's photo probe looks up")
    evidence = store.fp[order[0]].evidence
    assert evidence.n_phash == evidence.n_images
    assert store.fp[order[0]].first_decided_at == stamped, (
        "a re-decision does not restart the horizon clock (E92)")


def test_the_sweep_claims_a_listing_whose_probe_disagrees_with_the_stored_counts() -> None:
    """The feed itself, through the SQL statements: `rt_fp` says two of three images were
    hashed, `public.images` says three, so the listing is handed over — and when the two
    agree, nothing is."""
    db = FakePg()
    db.rt_fp[("rt", 7)] = {
        "category_main": None, "category_type": None, "area_m2": None, "disposition": None,
        "floor": None, "fp_digest": "d", "cell_key": "o1", "cell_group": "byt",
        "is_active": True, "ev_images": 3, "ev_phash": 2, "ev_clip": 3, "ev_tags": 3,
        "ev_complete": False, "first_decided_at": db.now - timedelta(hours=1),
    }
    for seq in range(3):
        db.image_rows.append({"image_id": 70 + seq, "listing_id": 7, "sequence": seq,
                              "storage_path": f"p{seq}", "phash": 100 + seq,
                              "clip_tagged_at": db.now})
    work = SqlWork(db, TOWN, "rt", straggler_window=0, revive_slice=0, drift_slice=0,
                   enter_slice=0)

    items = work.claim(50)

    assert [(item.listing_id, item.feed, item.redecide) for item in items] == [
        (7, "evidence", True)]
    assert work.evidence["moved"] == 1 and work.evidence["images_probed"] == 3

    # And once the counts agree, the sweep is silent — it must not re-claim every pass.
    db.rt_fp[("rt", 7)].update({"ev_phash": 3, "ev_complete": True})
    assert SqlWork(db, TOWN, "rt", straggler_window=0, revive_slice=0,
                   drift_slice=0, enter_slice=0).claim(50) == []


def test_the_sweep_leaves_a_settled_row_outside_the_horizon_alone() -> None:
    """The horizon bounds the sweep as well as the hold: a listing decided a week ago with
    everything its producers were ever going to give it is not probed at all."""
    db = FakePg()
    db.rt_fp[("rt", 9)] = {
        "category_main": None, "category_type": None, "area_m2": None, "disposition": None,
        "floor": None, "fp_digest": "d", "cell_key": "o1", "cell_group": "byt",
        "is_active": True, "ev_images": 0, "ev_phash": 0, "ev_clip": 0, "ev_tags": 0,
        "ev_complete": True, "first_decided_at": db.now - timedelta(days=7),
    }
    work = SqlWork(db, TOWN, "rt", straggler_window=0, revive_slice=0, drift_slice=0,
                   enter_slice=0)

    assert work.claim(50) == []
    assert work.evidence["candidates"] == 0 and work.evidence["listings_probed"] == 0


# ============================================================ V1b: do not act on what you lack

# A cohort built for the one shape this rule is about: a pair that MERGES on text while its
# photographs are still unprocessed, and merges on the PHOTOGRAPHS once they arrive. Blind it
# is `model` at 0.999 on a shared unit number; with the hashes it is K-C. Beside it, a K-B
# re-post — one portal, one broker, windows that never overlapped — which rests on no
# photograph at all and must never wait for one.
_UNIT_BODY = ("Prodej bytu 2+kk o vymere 60 m2 v cihlovem dome po rekonstrukci, "
              "jednotka 12. " * 6)
_REPOST_BODY = ("Pronajem krasneho bytu 3+1 o vymere 95 m2 v panelovem dome u parku, "
                "jednotka 44. " * 6)


_CLIP = encode_clip([0.05] * 512)


def _evidence_cohort():
    from autodedup.dataset import Dataset, Meta
    from tests.autodedup.test_incremental import _listing

    listings: dict[int, Any] = {}
    images: dict[int, list[Image]] = {}

    def add(listing_id: int, **kw: Any) -> None:
        listings[listing_id] = _listing(listing_id, **kw)
        images[listing_id] = [
            Image(listing_id=listing_id, image_id=listing_id * 10 + seq, seq=seq,
                  phash=5_000 + seq, pop=1, clip=_CLIP, tags=[("kitchen", 0.9)])
            for seq in range(6)]

    add(101, source="sreality", source_id_native="n101", description=_UNIT_BODY,
        area_m2=60.0)
    add(202, source="bezrealitky", source_id_native="n202", description=_UNIT_BODY,
        area_m2=60.0)
    add(301, source="idnes", source_id_native="n301", description=_REPOST_BODY,
        area_m2=95.0, disposition="3+1", broker_key="bb",
        first_seen_at="2026-01-02T08:00:00+00:00",
        last_seen_at="2026-01-20T08:00:00+00:00", is_active=False,
        inactive_at="2026-01-20T08:00:00+00:00")
    add(302, source="idnes", source_id_native="n302", description=_REPOST_BODY,
        area_m2=95.0, disposition="3+1", broker_key="bb",
        first_seen_at="2026-03-01T08:00:00+00:00",
        last_seen_at="2026-04-01T08:00:00+00:00")
    return Dataset(meta=Meta(), listings=listings, images_by_listing=images)


def _decide_blind(hold: EvidenceHold | None, deliver: bool = False) -> MemoryStore:
    ds = _evidence_cohort()
    settings = _settings()
    calibration = _calibrated(ds, settings)
    store = MemoryStore(now=1_000.0)
    facts = _LateFacts(ds)
    if deliver:
        facts.delivered = set(ds.listings)
    order = arrival_order(ds)
    work = ScheduleWork(order)
    while not work.exhausted():
        run_pass(store, facts, work, settings, hand_initialised(), calibration,
                 limits=Limits(max_listings=50), hold=hold)
    return store


def test_a_photo_dependent_merge_is_held_in_the_band_while_the_hashes_are_missing() -> None:
    """THE DEFECT: the pair (101, 202) merges at 0.999 on text alone while both galleries sit
    unhashed, and the photographs that would have decided it arrive hours later to a lane that
    has stopped looking."""
    blind = _decide_blind(None)
    assert blind.pairs[(101, 202)].zone == "merge"
    assert blind.pairs[(101, 202)].certificate is None, "merged on no photograph at all"

    held = _decide_blind(EvidenceHold(now=1_000.0, horizon_s=48 * 3600.0))
    row = held.pairs[(101, 202)]
    assert row.zone == "band" and row.reason == EVIDENCE_HOLD_REASON
    assert row.evidence["held_zone"] == "merge" and row.evidence["held_reason"] == "model"
    assert row.certificate is None, "a held pair certifies nothing"
    assert not any({101, 202} <= set(members) for members in held.clusters.values()), (
        "and nothing is clustered on a merge that is waiting")


def test_a_photograph_free_certificate_never_waits_for_a_photograph() -> None:
    """K-B is one portal, one broker, one body and live windows that never overlapped — every
    clause a clock or a text fact. A pair that earns it is merged on the spot."""
    held = _decide_blind(EvidenceHold(now=1_000.0, horizon_s=48 * 3600.0))
    row = held.pairs[(301, 302)]
    assert row.certificate == "K-B" and row.zone == "merge"
    assert row.reason != EVIDENCE_HOLD_REASON


def test_only_merges_wait_and_the_hold_names_what_it_is_holding() -> None:
    held = _decide_blind(EvidenceHold(now=1_000.0, horizon_s=48 * 3600.0))
    waiting = [row for row in held.pairs.values() if row.reason == EVIDENCE_HOLD_REASON]
    assert waiting
    for row in waiting:
        assert row.evidence["held_zone"] == "merge", "a reject is never held"
        assert row.evidence.get("held_certificate") not in ("K-B", "K-R")


def test_the_hold_does_not_fire_once_the_photographs_are_there() -> None:
    """A gallery every producer has answered (pHash, CLIP and tags on every photograph) is
    complete evidence, and the hold has nothing to say about it."""
    delivered = _decide_blind(EvidenceHold(now=1_000.0, horizon_s=48 * 3600.0), deliver=True)
    assert delivered.pairs[(101, 202)].zone == "merge"
    assert delivered.pairs[(101, 202)].certificate == "K-C"
    assert not any(row.reason == EVIDENCE_HOLD_REASON for row in delivered.pairs.values())


def test_the_hold_expires_with_the_horizon_even_though_no_fact_moved() -> None:
    """The half that needs a feed of its own: when the horizon passes with the photographs
    still absent, nothing about the listing has changed — no digest, no snapshot, no flag — so
    only an explicit re-decision can reopen the pair."""
    from autodedup.replay import RedecideWork

    ds = _evidence_cohort()
    settings = _settings()
    calibration = _calibrated(ds, settings)
    store = MemoryStore(now=1_000.0)
    facts = _LateFacts(ds)
    order = arrival_order(ds)
    work = ScheduleWork(order)
    while not work.exhausted():
        run_pass(store, facts, work, settings, hand_initialised(), calibration,
                 limits=Limits(max_listings=50),
                 hold=EvidenceHold(now=1_000.0, horizon_s=48 * 3600.0))
    assert store.pairs[(101, 202)].reason == EVIDENCE_HOLD_REASON

    expiry = RedecideWork(order)
    late = EvidenceHold(now=1_000.0 + 49 * 3600.0, horizon_s=48 * 3600.0)
    while not expiry.exhausted():
        result = run_pass(store, facts, expiry, settings, hand_initialised(), calibration,
                          limits=Limits(max_listings=50), hold=late)

    assert result.released >= 1
    assert store.pairs[(101, 202)].zone == "merge", (
        "released onto what the lane HAS, which is the batch engine's own verdict")
    assert not any(row.reason == EVIDENCE_HOLD_REASON for row in store.pairs.values())


def test_the_release_statement_finds_only_the_pairs_whose_hold_is_over() -> None:
    """The SQL arm: one held pair still pending inside its horizon, one whose sides have aged
    out. Only the second is offered back."""
    db = FakePg()

    def _fp(listing_id: int, phash: int, age_h: int) -> None:
        db.rt_fp[("rt", listing_id)] = {
            "category_main": None, "category_type": None, "area_m2": None,
            "disposition": None, "floor": None, "fp_digest": "d", "cell_key": "o1",
            "cell_group": "byt", "is_active": True, "ev_images": 3, "ev_phash": phash,
            "ev_clip": 3, "ev_tags": 3, "ev_complete": phash == 3,
            "first_decided_at": db.now - timedelta(hours=age_h)}

    _fp(1, 0, 1)      # pending, one hour old: still waiting
    _fp(2, 3, 1)
    _fp(3, 0, 200)    # pending, but long past the horizon
    _fp(4, 3, 200)
    # `public.images` agrees with every stored count, so the sweep's OTHER arm stays silent
    # and what this test sees is the release arm alone.
    for listing_id, hashed in ((1, 0), (2, 3), (3, 0), (4, 3)):
        for seq in range(3):
            db.image_rows.append({
                "image_id": listing_id * 100 + seq, "listing_id": listing_id,
                "sequence": seq, "storage_path": f"p{seq}",
                "phash": (1_000 + seq) if seq < hashed else None,
                "clip_tagged_at": db.now})
    for lo, hi in ((1, 2), (3, 4)):
        db.pairs[("rt", lo, hi)] = {
            "probes": [], "from_lo": True, "from_hi": True, "score": 0.9, "zone": "band",
            "decision": EVIDENCE_HOLD_REASON, "guard_veto": None, "families": 0,
            "certificate": None, "evidence": {}, "context": {}, "fp_lo": "a", "fp_hi": "b"}

    work = SqlWork(db, TOWN, "rt", straggler_window=0, revive_slice=0, drift_slice=0,
                   enter_slice=0)
    items = work.claim(50)

    assert work.evidence["held_pairs"] == 2
    assert {3, 4} <= {item.listing_id for item in items}
    assert 2 not in {item.listing_id for item in items}, "its hold has not run out"
    assert all(item.redecide for item in items)


# ====================================================== V1: the replay proof, still exact


def test_the_plain_replay_is_untouched_by_the_hold() -> None:
    """A pass given no hold is W9's pass exactly — which is what keeps the replay-equivalence
    proof the property it was: the batch engine has no clock and this one is not given one."""
    ds = _dataset(10)
    settings = _settings()
    calibration = _calibrated(ds, settings)
    model = hand_initialised()
    reference, batch_clusters, _t = batch_state(ds, settings, model)

    store = MemoryStore()
    facts = DatasetFacts(ds)
    work = ScheduleWork(arrival_order(ds))
    while not work.exhausted():
        run_pass(store, facts, work, settings, model, calibration,
                 limits=Limits(max_listings=50))

    assert {key: row.zone for key, row in store.pairs.items()} == {
        key: row["zone"] for key, row in reference.items()}
    assert {tuple(sorted(v)) for v in store.clusters.values()} == {
        tuple(sorted(v)) for v in batch_clusters.values()}


def test_withholding_the_photographs_and_delivering_them_reaches_the_batch_state() -> None:
    """E92/E93's own proof: every listing decided blind, every photo-dependent merge held,
    the producers delivered, the horizon passed — and the FINAL state is the batch engine's,
    pair for pair and cluster for cluster."""
    ds = _evidence_cohort()
    settings = _settings()
    calibration = _calibrated(ds, settings)
    model = hand_initialised()
    reference, batch_clusters, _t = batch_state(ds, settings, model)

    pairs, clusters, stats = withheld_state(
        ds, settings, model, calibration, arrival_order(ds), 50,
        Limits(max_listings=50))

    assert stats["held_on_first_decision"] > 0, "the hold actually fired"
    assert stats["still_held"] == 0, "and nothing is waiting at the end"
    assert set(pairs) == set(reference)
    assert {key: row["zone"] for key, row in pairs.items()} == {
        key: row["zone"] for key, row in reference.items()}
    assert {key: row["reason"] for key, row in pairs.items()} == {
        key: row["reason"] for key, row in reference.items()}
    assert {tuple(sorted(v)) for v in clusters.values()} == {
        tuple(sorted(v)) for v in batch_clusters.values()}


# ======================================= V3: the calibration follows the corpus (A10, E912)


def test_a_pass_reports_the_calibration_age_and_the_population_coverage(world, tmp_path) -> None:
    """The export's 14-day age rail is gone with the export: the calibration is cut from the
    database and re-cut when the coverage falls, so the pass REPORTS both."""
    seed_lane(world, tmp_path)

    out = run_incremental(lambda: world)

    assert out["calibration"]["built_age_days"] is not None
    assert out["population"]["coverage"] == 1.0
    assert out["population"]["floor"] == 0.85
    assert out["evidence"]["horizon_hours"] == EVIDENCE_HORIZON_HOURS


# ======================================= V4: a hash the population never saw is UNKNOWN


def test_a_hash_the_population_never_measured_reads_unknown_not_zero(world) -> None:
    """E96. There is no index from a hash to its carriers (`images_phash_idx` is on
    `sreality_id`) and D8 forbids adding one, so a hash that arrived after the last cut cannot
    be counted without a sequential scan. It therefore reads UNKNOWN — and an unknown
    population makes `catalog_ratio` absent, which is what K-C requires to be PRESENT, so the
    pair bands rather than merging on photo evidence nobody measured."""
    from autodedup.decide import certificate_c
    from autodedup.features import Feats
    from autodedup.incremental_lane import SqlFacts

    conn = world
    conn.phash_pop.clear()
    conn.phash_pop.update(true_population(conn))
    listing_id = next(iter(conn.listings))
    # One frame re-hashed after the cut: its new hash is in no measured population.
    for row in conn.image_rows:
        if row["listing_id"] == listing_id:
            row["phash"] = (row["phash"] or 0) + 7_777_777
            break

    reader = SqlFacts(conn)
    _listing, gallery = reader.facts([listing_id])[listing_id]

    unknown = [image for image in gallery if image.pop is None]
    assert unknown and all(not image.pop_is_measured() for image in unknown)
    assert reader.images_unmeasured == len(unknown)
    # And K-C cannot fire on an absent catalogue ratio, whatever the photo match says.
    feats: Feats = {"phash_tight_matches": (9.0, True), "seq_monotone_ratio": (1.0, True),
                    "area_rel_diff": (0.0, True), "dispo_equal": (1.0, True),
                    "catalog_ratio_max": (None, False)}
    assert certificate_c(feats) is False


def test_a_re_cut_is_what_closes_the_gap(world, tmp_path) -> None:
    """The writer of the population is the cut, and a re-cut measures the new hash over
    `public.images` — no export, no artifact, no 14-day clock (A10)."""
    seed_lane(world, tmp_path)
    listing_id = next(iter(world.listings))
    for row in world.image_rows:
        if row["listing_id"] == listing_id:
            row["phash"] = (row["phash"] or 0) + 7_777_777
            break

    out = cut_calibration(world, Settings(), "hand_v1")

    assert out["hashes_measured"] == len(true_population(world))
    assert all(world.phash_pop[h] == n for h, n in true_population(world).items())
    from autodedup.incremental_lane import SqlFacts

    reader = SqlFacts(world)
    reader.facts([listing_id])
    assert reader.images_unmeasured == 0


# ============================================================ the store keeps the four counts


def test_the_sql_store_writes_and_reads_the_evidence_counts_and_the_first_stamp() -> None:
    db = FakePg()
    store = SqlStore(db, "rt")
    row = FpRow(GuardRow(5, "byt", "prodej", 60.0, "2+kk", 3), "digest", "o1", "byt", True,
                Evidence(4, 0, 0, 0))
    store.put_listing(5, row, [("attr_area", "a")])
    store.flush()
    first = db.rt_fp[("rt", 5)]["first_decided_at"]
    assert db.rt_fp[("rt", 5)]["ev_complete"] is False

    read = SqlStore(db, "rt").rows([5])[5]
    assert read.evidence == Evidence(4, 0, 0, 0) and not read.evidence.complete
    assert read.first_decided_at == pytest.approx(first.timestamp())

    # A refresh replaces the counts and KEEPS the stamp (E92, migration 540).
    store = SqlStore(db, "rt")
    store.put_listing(5, replace(row, evidence=Evidence(4, 4, 4, 4)), [("attr_area", "a")])
    store.flush()
    assert db.rt_fp[("rt", 5)]["ev_complete"] is True
    assert db.rt_fp[("rt", 5)]["first_decided_at"] == first


def test_evidence_of_counts_what_the_producers_delivered() -> None:
    images = [Image(listing_id=1, image_id=1, seq=0, phash=7, clip="v", tags=[("a", 1.0)]),
              Image(listing_id=1, image_id=2, seq=1)]
    assert evidence_of(images) == Evidence(2, 1, 1, 1)
    assert not evidence_of(images).complete, "one frame without its signals is incomplete"
    assert evidence_of([]).complete, "no gallery is complete: there is nothing to wait for"


def test_the_hold_waits_for_complete_evidence_not_for_the_first_hash() -> None:
    """F3 (E908): the engine reads CLIP and the tags, so a gallery that is hashed but not yet
    embedded is evidence it does not have. W9h held only while NO frame was hashed."""
    hold = EvidenceHold(now=1_000.0, horizon_s=48 * 3600.0)
    assert hold.holds(Evidence(3, 3, 0, 0), None), "hashed, not embedded: waits"
    assert hold.holds(Evidence(3, 3, 3, 2), None), "one frame untagged: waits"
    assert not hold.holds(Evidence(3, 3, 3, 3), None), "complete: decides"
    assert not hold.holds(Evidence(0, 0, 0, 0), None), "no photographs: nothing to wait for"
    assert not hold.holds(Evidence(3, 3, 0, 0), 1_000.0 - 49 * 3600.0), "past the horizon"


def test_a_merge_waits_while_the_photographs_are_hashed_but_not_embedded() -> None:
    """The pass-level half of F3: the pHash job has answered, the CLIP job has not."""
    ds = _evidence_cohort()
    for listing_id, gallery in ds.images_by_listing.items():
        ds.images_by_listing[listing_id] = [replace(image, clip=None) for image in gallery]
    settings = _settings()
    store = MemoryStore(now=1_000.0)
    facts = _LateFacts(ds)
    facts.delivered = set(ds.listings)
    work = ScheduleWork(arrival_order(ds))
    while not work.exhausted():
        run_pass(store, facts, work, settings, hand_initialised(), _calibrated(ds, settings),
                 limits=Limits(max_listings=50),
                 hold=EvidenceHold(now=1_000.0, horizon_s=48 * 3600.0))
    row = store.pairs[(101, 202)]
    assert row.zone == "band" and row.reason == EVIDENCE_HOLD_REASON
    assert row.evidence["held_zone"] == "merge"


def test_a_pair_row_that_was_held_says_what_it_was_holding() -> None:
    row = PairRow(lo=1, hi=2, probes=[], from_lo=True, from_hi=True, zone="band", score=0.99,
                  families=[], certificate=None, veto=None, reason=EVIDENCE_HOLD_REASON,
                  evidence={"held_zone": "merge", "held_reason": "model"}, context={},
                  fp_lo="a", fp_hi="b")
    assert row.decision().zone == "band"
    assert row.evidence["held_zone"] == "merge"
