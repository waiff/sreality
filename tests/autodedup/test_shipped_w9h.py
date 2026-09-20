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
    ENV_FLAG,
    EVIDENCE_HORIZON_HOURS,
    SqlStore,
    SqlWork,
    parity_baseline_key,
    run_incremental,
    run_rt_seed,
)
from autodedup.incremental_scope import Scope, ScopeBlock
from autodedup.incremental_store import MemoryStore
from autodedup.model import hand_initialised
from autodedup.parity_digest import Floors, baseline, compare, verdict
from autodedup.replay import (
    DatasetFacts,
    ScheduleWork,
    arrival_order,
    batch_state,
    withheld_state,
)
from autodedup.settings import Settings
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_incremental import _dataset, _settings
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
TOWN = Scope((ScopeBlock("obec", 563510),))


@pytest.fixture()
def world(tmp_path: Path):
    conn = FakePg(now=EXPORTED_AT + timedelta(days=1))
    seed(conn)
    seed_calibration(conn, Settings())
    artifact = write_artifact(conn, tmp_path / "cohort.jsonl.gz", true_population(conn))
    conn.calibration.pop(GENERATION, None)
    return conn, artifact


def _seed(conn: FakePg, artifact: Path, tmp_path: Path, **args: Any) -> dict[str, Any]:
    return run_rt_seed(lambda: conn, {"artifact": str(artifact), **SEED_ARGS, **args},
                       tmp_path)


def _pass(conn: FakePg, tmp_path: Path, monkeypatch, **args: Any) -> dict[str, Any]:
    monkeypatch.setenv(ENV_FLAG, "true")
    return run_incremental(lambda: conn, {"rt_scope": SCOPE, **args}, tmp_path)


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
    assert store.fp[order[0]].evidence.pending, "and the store SAYS so (E92)"
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
    assert not store.fp[order[0]].evidence.pending
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


def _evidence_cohort():
    from autodedup.dataset import Dataset, Meta
    from tests.autodedup.test_incremental import _listing

    listings: dict[int, Any] = {}
    images: dict[int, list[Image]] = {}

    def add(listing_id: int, **kw: Any) -> None:
        listings[listing_id] = _listing(listing_id, **kw)
        images[listing_id] = [
            Image(listing_id=listing_id, image_id=listing_id * 10 + seq, seq=seq,
                  phash=5_000 + seq, pop=1, tags=[("kitchen", 0.9)])
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
    """Pending is `images exist and NONE is hashed`. A gallery the producers have answered is
    evidence, and the hold has nothing to say about it."""
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


# ================================================================== V2: the gate's own floors


def _report(**kw: Any) -> dict[str, Any]:
    base = {"listings": 25, "checked": 25, "breaches": 0, "drifted_skipped": 0, "absent": 0,
            "population_images_checked": 300, "population_images_excused": 0,
            "legacy_baseline_rows": 0, "age_days": 1.0, "unknown_pop_share": 0.01}
    base.update(kw)
    return base


def test_a_gate_that_checked_nothing_refuses_instead_of_passing() -> None:
    """THE DEFECT: every baseline listing drifted (or absent) gave `checked` 0, `breaches` 0
    and `ok` true — and drift is monotone in export age, so the gate self-weakened as the
    export aged."""
    drifted = _report(checked=0, drifted_skipped=25)
    absent = _report(checked=0, absent=25)

    assert verdict(drifted, generation="rt", floors=Floors(min_checked=0,
                                                           min_checked_share=0.0),
                   what="x") is None, "with the rail off, this is W9g's behaviour"
    for report in (drifted, absent):
        message = verdict(report, generation="rt", floors=Floors(), what="the pass")
        assert message and "checked 0 of 25" in message


def test_the_floor_is_a_count_and_a_share_so_neither_end_defeats_it() -> None:
    floors = Floors()
    assert floors.required(25) == 15          # the shipped slice
    assert floors.required(120) == 72         # a seed sample: 60% of it, not 15 of it
    assert floors.required(9) == 6            # a cohort smaller than the absolute floor
    assert floors.required(0) == 1            # an EMPTY baseline is the vacuity itself
    assert Floors(min_checked=0, min_checked_share=0.0).required(0) == 0


def test_a_baseline_older_than_the_age_rail_refuses() -> None:
    assert verdict(_report(age_days=13.0), generation="rt", floors=Floors(),
                   what="the pass") is None
    message = verdict(_report(age_days=21.0), generation="rt", floors=Floors(),
                      what="the pass")
    assert message and "21.0 days old" in message and "UNDERCOUNT" in message


def test_an_unknown_population_share_over_the_ceiling_refuses() -> None:
    assert verdict(_report(unknown_pop_share=0.04), generation="rt", floors=Floors(),
                   what="the pass") is None
    message = verdict(_report(unknown_pop_share=0.31), generation="rt", floors=Floors(),
                      what="the pass")
    assert message and "31.0%" in message


def test_a_producer_move_on_one_image_no_longer_excuses_the_rest(world) -> None:
    """THE DEFECT: `compare` skipped the whole listing's population check as soon as any image
    of it had been re-hashed, so a population decaying image by image ran green."""
    conn, _artifact = world
    listing_id = 4_000
    from autodedup.incremental_lane import SqlFacts

    conn.phash_pop.update(true_population(conn))

    listing, gallery = SqlFacts(conn).facts([listing_id])[listing_id]
    rows = baseline({listing_id: listing}, {listing_id: gallery})

    # One image re-hashed (honest producer movement), and EVERY OTHER image's population
    # quietly wiped — the shape W9f shipped, one frame at a time.
    moved = [replace(gallery[0], phash=(gallery[0].phash or 0) + 1)] + [
        replace(image, pop=None) for image in gallery[1:]]
    assert len(moved) > 1, "the fixture needs a gallery to decay"

    report = compare(rows, {listing_id: listing}, {listing_id: moved})

    assert report["producer_moved"] == 1
    assert report["population_images_excused"] == 1, "the re-hashed frame, and only it"
    assert report["breaches"] == 1 and report["breaches_by_kind"] == {"population": 1}


# ==================================================== V3: the calibration has an expiry date


def test_a_pass_reports_the_calibration_age_and_the_population_coverage(
        world, tmp_path, monkeypatch) -> None:
    conn, artifact = world
    _seed(conn, artifact, tmp_path)

    out = _pass(conn, tmp_path, monkeypatch)

    assert 0.0 < out["calibration"]["export_age_days"] < 14.0
    assert out["calibration"]["max_age_days"] == 14.0
    assert out["calibration"]["built_age_days"] is not None
    assert out["population"]["coverage"] == 1.0
    assert out["evidence"]["horizon_hours"] == EVIDENCE_HORIZON_HOURS


def test_a_pass_refuses_a_calibration_past_its_age_rail(world, tmp_path, monkeypatch) -> None:
    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    conn.settings["rt_calibration_max_age_days"] = 0.5
    before = dict(conn.cursors)

    with pytest.raises(SystemExit) as raised:
        _pass(conn, tmp_path, monkeypatch)

    assert "rt_calibration_max_age_days" in str(raised.value)
    assert conn.cursors == before, "nothing was written and no cursor moved"


def test_a_baseline_that_cannot_say_when_it_was_exported_is_refused(
        world, tmp_path, monkeypatch) -> None:
    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    payload = dict(conn.settings[parity_baseline_key(GENERATION)])
    payload.pop("exported_at")
    conn.settings[parity_baseline_key(GENERATION)] = payload

    with pytest.raises(SystemExit) as raised:
        _pass(conn, tmp_path, monkeypatch)
    assert "exported_at" in str(raised.value)


# ================================================= V4: a hash the export never saw is UNKNOWN


def test_a_hash_the_frozen_population_never_measured_reads_unknown_not_zero(world) -> None:
    """E96. There is no index from a hash to its carriers (`images_phash_idx` is on
    `sreality_id`) and D8 forbids adding one, so a hash that arrived after the export cannot
    be counted without the export's own sequential scan. It therefore reads UNKNOWN — and an
    unknown population makes `catalog_ratio` absent, which is what K-C requires to be
    PRESENT, so the pair bands rather than merging on photo evidence nobody measured."""
    from autodedup.decide import certificate_c
    from autodedup.features import Feats
    from autodedup.incremental_lane import SqlFacts

    conn, _artifact = world
    conn.phash_pop.clear()
    conn.phash_pop.update(true_population(conn))
    listing_id = next(iter(conn.listings))
    # One frame re-hashed after the export: its new hash is in no frozen population.
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


def test_a_fresh_export_is_what_closes_the_gap(world, tmp_path) -> None:
    """The only writer of the frozen population is the seed, and the only source of a
    population for a NEW hash is a new export: re-seeding from a fresh artifact writes the
    new hash's count and the gap closes to whatever has moved since THAT export."""
    conn, _artifact = world
    listing_id = next(iter(conn.listings))
    for row in conn.image_rows:
        if row["listing_id"] == listing_id:
            row["phash"] = (row["phash"] or 0) + 7_777_777
            break
    fresh = write_artifact(conn, tmp_path / "fresh.jsonl.gz", true_population(conn))

    conn.phash_pop.clear()
    out = _seed(conn, fresh, tmp_path, reseed="true")

    assert out["population"]["hashes_written"] == len(conn.phash_pop)
    assert conn.phash_pop == true_population(conn)
    from autodedup.incremental_lane import SqlFacts

    reader = SqlFacts(conn)
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
    assert read.evidence == Evidence(4, 0, 0, 0) and read.evidence.pending
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
    assert not evidence_of(images).complete
    assert not evidence_of(images).pending, "one hash IS photo evidence"
    assert evidence_of([]).complete and not evidence_of([]).pending


def test_a_pair_row_that_was_held_says_what_it_was_holding() -> None:
    row = PairRow(lo=1, hi=2, probes=[], from_lo=True, from_hi=True, zone="band", score=0.99,
                  families=[], certificate=None, veto=None, reason=EVIDENCE_HOLD_REASON,
                  evidence={"held_zone": "merge", "held_reason": "model"}, context={},
                  fp_lo="a", fp_hi="b")
    assert row.decision().zone == "band"
    assert row.evidence["held_zone"] == "merge"
