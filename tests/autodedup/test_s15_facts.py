"""S15's readings (E300-E303), each built from the trial adverts that named it.

The trial (Jablonec nad Nisou, Turnov, Praha-Vysočany) was read against the g13 export and its
leftover duplicates named four engine causes. E300 is the main one: the attribute probes key on
the resolver's finest grain, so sreality 411764 (filed to the cast Mšeno nad Nisou) and bažoš
414785 (filed to the town) — one 3+1 of 73 m² at 4,750,000 on Mozartova, one body — were never
a candidate pair. E301 reads a floor and a storey count shifted together by one storey as one
counting camp; Mechová (3 of 7 against 2 of 8, opposite signs) needs the prepared E301b. E302
(prepared) and E303 (prepared) wait for the operator: the storey count alone (Kolmá 4 against
3, Mozartova 9 against 7, the 122 m² re-post 5 against 6) and a K-C pair at one house number a
commission apart (Pražská 930/47: 3,997,795 against 3,950,000).

Every advert here is a listing row of the g13 export (`data_s15_trial_examples.json`), never a
database read.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from autodedup.blocking import (
    PROBE_PRIORITY,
    SPLIT_CITY_OBEC_KODS,
    TOWN_PROBE,
    BlockIndex,
    build_index,
    generate_pairs,
)
from autodedup.d43 import ClusterRelation
from autodedup.dataset import Dataset, Image, Listing, Location, Meta
from autodedup.fingerprint import Fingerprint, build_all, build_fingerprint
from autodedup.incremental import Keyer, guard_row, retrieve
from autodedup.indistinguishable import CLUSTER, GATE, PROMOTE, distinguishing_facts
from autodedup.model import hand_initialised
from autodedup.replay import arrival_order, batch_state
from autodedup.settings import Settings
from tests.autodedup.test_incremental import _calibration, _drain
from tests.autodedup.test_s14_facts import EARLIER_DIGESTS as S14_PINS

HERE = Path(__file__).resolve().parent
SETTINGS = HERE.parents[1] / "autodedup/settings"
FIXTURES: dict[str, dict[str, Any]] = json.loads(
    (HERE / "data_s15_trial_examples.json").read_text(encoding="utf-8"))["listings"]
S14 = Settings.from_json(SETTINGS / "w29.json")
S15 = Settings.from_json(SETTINGS / "w30.json")

W30_ON = {"attr_probe_town_grain", "d43_floor_total_camp_shift"}
W30_PREPARED = {"d43_floor_total_camp_shift_mixed", "d43_total_floors_agreeing_unit",
                "d43_cluster_price_kc_house_number"}
W30_DIALS = W30_ON | W30_PREPARED

PRAHA, JABLONEC = 554782, 563510


def variant(**kwargs: object) -> Settings:
    return Settings.from_dict({**S15.to_dict(), **kwargs})


def trial(listing_id: int, **kwargs: object) -> Listing:
    listing = Listing.from_json(FIXTURES[str(listing_id)])
    return replace(listing, **kwargs) if kwargs else listing


def names(a: Listing, b: Listing, cfg: Settings, mode: str = CLUSTER) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, None, cfg, mode)]


def fingerprint(listing: Listing, cfg: Settings) -> Fingerprint:
    return build_fingerprint(listing, [], cfg)


# --- E300: the town probe -------------------------------------------------------------------
def test_E300_the_trial_twin_is_a_candidate_only_through_the_town_probe() -> None:
    a, b = trial(411764), trial(414785)
    assert (a.location.cast_obce_kod, b.location.cast_obce_kod) == (408093, None)
    for cfg, expected in ((S14, None), (S15, {TOWN_PROBE})):
        fps = {x.id: fingerprint(x, cfg) for x in (a, b)}
        pairs, _stats = generate_pairs(fps, cfg)
        assert pairs.get((411764, 414785)) == expected


def advert(listing_id: int, obec: int | None, cast: int | None, **kwargs: Any) -> Listing:
    fields: dict[str, Any] = dict(
        block="b", source="sreality", category_main="byt", category_type="prodej",
        disposition="3+1", area_m2=73.0, price=4_750_000.0, description="krátký text",
        location=Location(obec_kod=obec, cast_obce_kod=cast))
    fields.update(kwargs)
    return Listing(id=listing_id, **fields)


def pairs_of(listings: list[Listing], cfg: Settings) -> dict[tuple[int, int], set[str]]:
    pairs, _stats = generate_pairs({x.id: fingerprint(x, cfg) for x in listings}, cfg)
    return pairs


def test_E300_quarters_split_the_town_only_in_the_three_cities() -> None:
    assert SPLIT_CITY_OBEC_KODS == frozenset({554782, 582786, 554821})
    listings = [
        advert(1, PRAHA, 490245),    # Vysočany
        advert(2, PRAHA, 490059),    # another quarter
        advert(3, PRAHA, None),      # Praha, quarter unknown
        advert(4, PRAHA, None),
        advert(5, JABLONEC, 408093),  # Mšeno nad Nisou
        advert(6, JABLONEC, 408085),  # Jablonec nad Nisou (the cast)
        advert(7, JABLONEC, None),
    ]
    old, new = pairs_of(listings, S14), pairs_of(listings, S15)
    # two quarters of a split city never meet; an unknown quarter reaches every quarter
    assert (1, 2) not in new
    assert new[(1, 3)] == {TOWN_PROBE} and new[(2, 3)] == {TOWN_PROBE}
    assert (1, 3) not in old
    # two unknown-quarter adverts meet as they always did, through their home key
    assert "attr_dispo" in new[(3, 4)] and "attr_dispo" in old[(3, 4)]
    # outside the three cities the grain is the town
    assert new[(5, 6)] == {TOWN_PROBE} and new[(5, 7)] == {TOWN_PROBE}
    assert (5, 6) not in old and (5, 7) not in old
    # and nothing reached today is lost
    assert all(set(probes) <= new[key] for key, probes in old.items())


def test_E300_the_key_is_town_disposition_and_area_band() -> None:
    listings = [advert(1, JABLONEC, 408093), advert(2, JABLONEC, None, disposition="2+kk"),
                advert(3, JABLONEC, None, area_m2=150.0), advert(4, JABLONEC, None, area_m2=None),
                advert(5, JABLONEC, None, area_m2=69.0)]  # one band down: the +-1 neighbourhood
    new = pairs_of(listings, S15)
    assert (1, 2) not in new and (1, 3) not in new and (1, 4) not in new
    bands = {x.id: fingerprint(x, S15).area_band for x in listings}
    assert bands[5] == bands[1] - 1
    assert new[(1, 5)] == {TOWN_PROBE}
    index = build_index([fingerprint(x, S15) for x in listings], S15)
    assert TOWN_PROBE not in {probe for probe, _ in index.index_keys(fingerprint(listings[3],
                                                                              S15))}


def test_E300_the_town_probe_fills_last_so_the_cap_keeps_every_home_candidate() -> None:
    cfg = variant(max_candidates_per_listing=2)
    probe = advert(10, JABLONEC, None)
    home = [advert(11, JABLONEC, None), advert(12, JABLONEC, None)]
    cast = advert(9, JABLONEC, 408093)
    index = build_index([fingerprint(x, cfg) for x in (probe, *home, cast)], cfg)
    assert index.probes == PROBE_PRIORITY + (TOWN_PROBE,)
    assert set(index.candidates(fingerprint(probe, cfg))) == {11, 12}
    # ...the cast advert still finds it from its own side
    assert 10 in index.candidates(fingerprint(cast, cfg))


def test_E300_an_exploded_town_key_is_skipped_like_any_key() -> None:
    cfg = variant(max_block_size=2)
    listings = [advert(1, JABLONEC, 408093), advert(2, JABLONEC, 408085),
                advert(3, JABLONEC, 408086)]
    assert pairs_of(listings, cfg) == {}
    assert set(pairs_of(listings[:2], cfg)) == {(1, 2)}


def test_E300_off_is_the_old_index() -> None:
    listings = [advert(1, JABLONEC, 408093), advert(2, JABLONEC, None)]
    index = build_index([fingerprint(x, S14) for x in listings], S14)
    assert index.probes == PROBE_PRIORITY
    assert TOWN_PROBE not in index.postings and TOWN_PROBE not in index.exploded
    _pairs, stats = generate_pairs({x.id: fingerprint(x, S14) for x in listings}, S14)
    assert set(stats["pairs_per_probe"]) == set(PROBE_PRIORITY)


def test_E300_every_advert_that_probes_a_town_key_is_posted_under_it() -> None:
    """What the real-time lane's neighbourhood re-probe relies on (E71): the dirty set is read
    off the POSTINGS of a changed listing's keys, so whoever probes a key must be posted there."""
    listings = [advert(1, PRAHA, 490245), advert(2, PRAHA, None), advert(3, JABLONEC, 408093),
                advert(4, JABLONEC, None)]
    index = build_index([fingerprint(x, S15) for x in listings], S15)
    for listing in listings:
        fp = fingerprint(listing, S15)
        posted = {key for probe, key in index.index_keys(fp) if probe == TOWN_PROBE}
        probed = {key for probe, key in index.probe_keys(fp) if probe == TOWN_PROBE}
        assert len(posted) == 1
        known_quarter_of_a_split_city = listing.id == 1
        assert (probed == set()) if known_quarter_of_a_split_city else posted <= probed


def _spread(seed: int) -> int:
    """A 64-bit hash with every band populated, so no two galleries share a pHash band."""
    return int(hashlib.sha256(str(seed).encode()).hexdigest()[:16], 16)


def _cross_grain_dataset() -> Dataset:
    """Three towns' worth of adverts whose only path to one another is the town probe: short
    bodies (no text probe), no house numbers, no brokers, galleries that share no frame."""
    places = [(PRAHA, 490245), (PRAHA, 490059), (PRAHA, None), (JABLONEC, 408093),
              (JABLONEC, 408085), (JABLONEC, None)]
    listings: dict[int, Listing] = {}
    images: dict[int, list[Image]] = {}
    for index in range(18):
        listing_id = 2000 + index * 11
        obec, cast = places[index % len(places)]
        listings[listing_id] = advert(
            listing_id, obec, cast, area_m2=70.0 + (index % 3),
            first_seen_at=f"2026-02-{(index % 27) + 1:02d}T08:00:00+00:00",
            last_seen_at="2026-03-01T08:00:00+00:00", is_active=True)
        images[listing_id] = [Image(listing_id=listing_id, image_id=listing_id * 10 + seq,
                                    seq=seq, phash=_spread(listing_id * 10 + seq), pop=1)
                              for seq in range(2)]
    return Dataset(meta=Meta(), listings=listings, images_by_listing=images)


def test_E300_the_real_time_lane_retrieves_exactly_what_the_cohort_pass_does() -> None:
    ds = _cross_grain_dataset()
    fps = build_all(ds, S15)
    calibration = _calibration(ds, S15)
    assert TOWN_PROBE in calibration.exploded
    store, _passes = _drain(ds, S15, calibration, arrival_order(ds))
    index = BlockIndex(S15)
    for listing_id in sorted(fps):
        index.add(fps[listing_id])
    index.finalize()
    keyer = Keyer(S15, calibration)
    guards = {i: guard_row(fp) for i, fp in fps.items()}
    town_only = 0
    for listing_id in sorted(fps):
        expected = index.candidates(fps[listing_id])
        assert retrieve(fps[listing_id], keyer, store, guards, S15, {}) == expected
        town_only += sum(1 for probes in expected.values() if probes == {TOWN_PROBE})
    assert town_only > 0


def test_E300_the_real_time_lane_reaches_the_cohort_pass_state_in_any_order() -> None:
    ds = _cross_grain_dataset()
    calibration = _calibration(ds, S15)
    reference, clusters, _timings = batch_state(ds, S15, hand_initialised())
    forward, _a = _drain(ds, S15, calibration, arrival_order(ds))
    backward, _b = _drain(ds, S15, calibration, list(reversed(arrival_order(ds))), batch=3)
    assert set(forward.pairs) == set(reference) == set(backward.pairs)
    assert {k: sorted(v) for k, v in forward.clusters.items()} == {
        k: sorted(v) for k, v in clusters.items()}


# --- E301 / E301b: one storey-counting camp -------------------------------------------------
def test_E301_a_floor_and_a_storey_count_shifted_together_are_one_camp() -> None:
    # Mechová's two idnes adverts in the SAME-sign shape: 3 of 7 against 2 of 6.
    a, b = trial(479152), trial(13988573, total_floors=6)
    assert {"floor", "total_floors"} <= set(names(a, b, S14))
    assert not {"floor", "total_floors"} & set(names(a, b, S15))
    assert not {"floor", "total_floors"} & set(names(a, b, S15, GATE))
    assert not {"floor", "total_floors"} & set(names(a, b, S15, PROMOTE))


def test_E301_mechova_is_opposite_signed_and_needs_E301b() -> None:
    # idnes 479152 states 3 of 7, idnes 13988573 states 2 of 8: floor +1, total -1.
    a, b = trial(479152), trial(13988573)
    assert (a.floor, a.total_floors, b.floor, b.total_floors) == (3, 7, 2, 8)
    assert "floor" in names(a, b, S15, GATE)
    mixed = variant(d43_floor_total_camp_shift_mixed=True)
    assert not {"floor", "total_floors"} & set(names(a, b, mixed, GATE))
    assert not {"floor", "total_floors"} & set(names(a, b, mixed))


def test_E301_needs_both_numbers_on_both_sides_and_exactly_one_storey() -> None:
    a = trial(479152)
    for b in (trial(13988573, floor=1, total_floors=5),       # two storeys
              trial(13988573, floor=2, total_floors=7),       # the floor moved alone
              trial(13988573, floor=2, total_floors=None)):   # a storey count unstated
        assert "floor" in names(a, b, S15), (b.floor, b.total_floors)


def test_E301b_extends_E301_and_cannot_stand_alone() -> None:
    with pytest.raises(ValueError, match="E301b"):
        variant(d43_floor_total_camp_shift=False, d43_floor_total_camp_shift_mixed=True)


# --- E302 (prepared): the storey count alone ------------------------------------------------
E302 = variant(d43_total_floors_agreeing_unit=True)


@pytest.mark.parametrize("lo, hi, totals", [
    (18721451, 18766660, (4, 3)),      # Kolmá 4654/4a: bezrealitky against realitymix
    (18918891, 18927220, (5, 6)),      # the 122 m² idnes re-post
    (411764, 414785, (9, 7)),          # Mozartova: sreality against bažoš
])
def test_E302_the_storey_count_alone_does_not_part_one_unit(lo: int, hi: int,
                                                             totals: tuple[int, int]) -> None:
    a, b = trial(lo), trial(hi)
    assert (a.total_floors, b.total_floors) == totals
    assert "total_floors" in names(a, b, S15, GATE)
    assert "total_floors" not in names(a, b, E302, GATE)
    assert "total_floors" not in names(a, b, E302)


def test_E302_every_other_unit_fact_must_be_stated_and_agree() -> None:
    a = trial(18721451)
    for b in (trial(18766660, price=4_890_000.0, price_history=[]),  # 9 % apart
              trial(18766660, floor=None),
              trial(18766660, area_m2=64.0),
              trial(18766660, disposition="3+1")):
        assert "total_floors" in names(a, b, E302), b


def test_E302_is_off_in_w30() -> None:
    assert not S15.d43_total_floors_agreeing_unit


# --- E303 (prepared): a K-C pair at one house number, a commission apart ---------------------
PRAZSKA = (17902432, 19032904)   # bezrealitky 3,950,000 / sreality 3,997,795, both 930/47
E303 = variant(d43_cluster_price_kc_house_number=True)


def relation(cfg: Settings, *ids: int, certificate: str | None = "K-C",
             **overrides: Any) -> ClusterRelation:
    listings = {i: trial(i, **overrides.get(str(i), {})) for i in ids}
    certificates = {PRAZSKA: certificate} if certificate else None
    return ClusterRelation(listings, None, cfg, certificates=certificates)


def test_E303_the_commission_apart_pair_passes_the_cluster_price_limb() -> None:
    assert names(trial(PRAZSKA[0]), trial(PRAZSKA[1]), S15) == []
    assert not relation(S15, *PRAZSKA).ok(*PRAZSKA)
    assert relation(E303, *PRAZSKA).ok(*PRAZSKA)
    assert relation(E303, *PRAZSKA).strict().ok(*PRAZSKA)


def test_E303_needs_the_certificate_the_house_number_two_portals_and_five_percent() -> None:
    assert not relation(E303, *PRAZSKA, certificate="K-B").ok(*PRAZSKA)
    assert not relation(E303, *PRAZSKA, certificate=None).ok(*PRAZSKA)
    moved = replace(trial(PRAZSKA[1]).location, house_number="931/49")
    assert not relation(E303, *PRAZSKA, **{str(PRAZSKA[1]): {"location": moved}}).ok(*PRAZSKA)
    assert not relation(E303, *PRAZSKA, **{str(PRAZSKA[1]): {"source": "bezrealitky"}}
                        ).ok(*PRAZSKA)
    assert not relation(E303, *PRAZSKA, **{str(PRAZSKA[1]): {
        "price": 4_200_000.0, "price_history": []}}).ok(*PRAZSKA)


def test_E303_is_pair_grain_and_off_in_w30() -> None:
    assert not S15.d43_cluster_price_kc_house_number
    # realitymix 18223015 files no house number: the excuse never reaches its pair
    rel = ClusterRelation({i: trial(i) for i in (18223015, 19032904)}, None, E303,
                          certificates={(18223015, 19032904): "K-C"})
    assert not rel.ok(18223015, 19032904)


# --- the table itself ------------------------------------------------------------------------
def test_every_W30_dial_is_off_by_default() -> None:
    default = Settings()
    assert not any(getattr(default, dial) for dial in W30_DIALS)
    assert not any(getattr(S14, dial) for dial in W30_DIALS)


def test_w30_differs_from_w29_only_in_the_W30_dials_it_switches_on() -> None:
    w29, w30 = S14.to_dict(), S15.to_dict()
    assert {key for key in w30 if w29.get(key) != w30[key]} == W30_ON
    assert not any(getattr(S15, dial) for dial in W30_PREPARED)


def test_the_w30_holds_are_w30_plus_one_table() -> None:
    w30 = S15.to_dict()
    for name, table in (("w30_rentals_hold", {"pronajem|*": "propose"}),
                        ("w30_land_hold", {"*|pozemek": "propose"})):
        hold = Settings.from_json(SETTINGS / f"{name}.json").to_dict()
        assert {key for key in hold if w30.get(key) != hold[key]} == {"merge_policy"}
        assert hold["merge_policy"] == table


# Replay parity: every earlier settings file, w13..w29 and every hold, byte for byte.
EARLIER_DIGESTS = {
    **S14_PINS,
    "w29.json": "76bceb5d233359c796aae194524a4271ab31234c22737224488ddcbd61ab0c79",
    "w29_land_hold.json": "38d81db5ebdcbd36d419aa3989a2e833773f7b3d0f812956a8365141fb298e24",
    "w29_rentals_hold.json": "8257cd443224cf31f75eb2c896e45685f0acb382fb1a66864be43ecbcd83a4c9",
}


def test_every_earlier_settings_file_is_byte_identical() -> None:
    for name, digest in EARLIER_DIGESTS.items():
        assert hashlib.sha256((SETTINGS / name).read_bytes()).hexdigest() == digest, name


# The W14 offline data pack: w29 over the trial cohort through THIS package, against the S14 run
# stored before W30 existed. Skips where the pack is absent (CI pins the settings above).
ART = Path("/home/hejtm/autodedup-artifacts/w14")
TRIAL = ART / "data/cohort.jsonl.gz"
S14_TRIAL_RUN = ART / "s14/runs/trial/S14"
MNL = ART / "data/autodedup-labels-35609425873/must_not_link.jsonl"


@pytest.mark.skipif(not (S14_TRIAL_RUN / "clusters.json").is_file() or not TRIAL.is_file(),
                    reason="the W14 offline data pack is not on this machine")
def test_w29_replays_byte_identical_on_the_trial_cohort(tmp_path: Path) -> None:
    from autodedup.dataset import load
    from autodedup.harness import load_model, load_must_not_link, run_engine

    ds = load(TRIAL)
    ids = set(ds.listings)
    mnl = frozenset(p for p in load_must_not_link(str(MNL)) if p[0] in ids and p[1] in ids)
    model = load_model(str(HERE.parents[1] / "autodedup/models/w6_gold.json"))
    run_engine(ds, S14, model, tmp_path, mnl if S14.operator_must_not_link else frozenset())
    assert (tmp_path / "clusters.json").read_bytes() == (
        S14_TRIAL_RUN / "clusters.json").read_bytes()
    with gzip.open(tmp_path / "pairs.jsonl.gz") as got, \
            gzip.open(S14_TRIAL_RUN / "pairs.jsonl.gz") as stored:
        assert got.read() == stored.read()
