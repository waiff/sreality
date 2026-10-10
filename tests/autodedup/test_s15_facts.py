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

import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from autodedup.blocking import (
    PROBE_PRIORITY,
    QUARTER_PROBE,
    SPLIT_CITY_OBEC_KODS,
    TOWN_PROBE,
    BlockIndex,
)
from autodedup.d43 import ClusterRelation
from autodedup.dataset import Dataset, Image, Listing, Location, Meta
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.incremental import (Keyer, Limits, PairRow, PassResult, _recluster, _Working,
                                  guard_row, retrieve)
from autodedup.incremental_store import CohortFacts, MemoryStore
from autodedup.indistinguishable import CLUSTER, GATE, PROMOTE, distinguishing_facts
from autodedup.settings import Settings
from tests.autodedup.test_incremental import (
    QUARTER_GRAIN,
    _calibration,
    _drain,
    arrival_order,
    invariant_state,
    quarter_grain_dataset,
)
from tests.autodedup.whole_cohort import build_all, build_index, generate_pairs
from tests.autodedup.test_s14_facts import EARLIER_DIGESTS as S14_PINS

HERE = Path(__file__).resolve().parent
SETTINGS = HERE.parents[1] / "autodedup/settings"
FIXTURES: dict[str, dict[str, Any]] = json.loads(
    (HERE / "data_s15_trial_examples.json").read_text(encoding="utf-8"))["listings"]
S14 = Settings.from_json(SETTINGS / "w29.json")
S15 = Settings.from_json(SETTINGS / "w30.json")

W30_ON = {"attr_probe_town_grain", "d43_floor_total_camp_shift", "d43_cellar_area"}
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
    assert index.probes == PROBE_PRIORITY + (TOWN_PROBE, QUARTER_PROBE)
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


def test_E300_the_real_time_lane_reaches_one_state_in_any_order_and_claim() -> None:
    """The town probe keeps E70-E72's invariance: one final state whatever the order or the
    claim, and the town-only pairs are in it."""
    ds = _cross_grain_dataset()
    pairs, _groups = invariant_state(ds, S15, _calibration(ds, S15))
    assert any(probes == (TOWN_PROBE,) for *_rest, probes in pairs.values())


# --- E71's LIVE defect, named and not fixed in SW1 (the lane code is unchanged) -------------
# `run_pass` re-probes the postings of an arrival's EXACT index keys (incremental.py
# `neighbourhood`, and `touched_keys` += `new_keys` ahead of `dirty`). A listing that reaches
# the arrival only through its OWN +-1 band, or through the town key the arrival posts but does
# not probe (`probes_town_key`), is posted under a different key, so it is never re-probed and
# the pair is missed when it arrives first. `neighbourhood`'s symmetry docstring is false for
# exactly these. This is the c17/c18 B0 pair delta (84+15 / 4+1 reject pairs, SW1_MEASURE
# difference 1). The fix is owed to a re-seed wave: widen the neighbourhood with the touched
# listings' probe keys, including the reverse town-key postings, proven by B0 through fake_pg.


def _band_edge_pair() -> Dataset:
    """Two Praha adverts one area band apart: 1 has no quarter (it probes the town key, +-1
    band); 2 has one (it posts the town key and never probes it). Only 1 can find 2."""
    edge = math.exp(20 * S15.band_width())
    stamps = dict(last_seen_at="2026-03-01T08:00:00+00:00", is_active=True)
    listings = {1: advert(1, PRAHA, None, area_m2=round(edge - 0.4, 1),
                          first_seen_at="2026-02-01T08:00:00+00:00", **stamps),
                2: advert(2, PRAHA, 490245, area_m2=round(edge + 0.4, 1),
                          first_seen_at="2026-02-02T08:00:00+00:00", **stamps)}
    return Dataset(meta=Meta(), listings=listings, images_by_listing={1: [], 2: []})


def test_E71_the_band_edge_pair_is_found_from_one_side_only() -> None:
    ds = _band_edge_pair()
    fps = {i: fingerprint(x, S15) for i, x in ds.listings.items()}
    assert (fps[1].area_band, fps[2].area_band) == (19, 20)
    index = build_index(fps.values(), S15)
    assert index.candidates(fps[1]) == {2: {TOWN_PROBE}} and index.candidates(fps[2]) == {}
    store, _ = _drain(ds, S15, _calibration(ds, S15), [2, 1], batch=1)
    assert {key: sorted(row.probes) for key, row in store.pairs.items()} == {(1, 2): [TOWN_PROBE]}


@pytest.mark.xfail(strict=True, reason="E71 live defect (SW1 review): the arrival's exact index "
                   "keys are re-probed, not the +-1 / town keys that reach it; owed to a re-seed wave")
def test_E71_the_band_edge_pair_is_found_in_either_arrival_order() -> None:
    ds = _band_edge_pair()
    calibration = _calibration(ds, S15)
    first, _ = _drain(ds, S15, calibration, [1, 2], batch=1)
    last, _ = _drain(ds, S15, calibration, [2, 1], batch=1)
    assert set(first.pairs) == set(last.pairs)


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


def _prazska_groups(cfg: Settings, certificate: str | None = "K-C") -> list[list[int]]:
    """The lane's own re-cluster over the two Pražská adverts and their one stored edge."""
    listings = {i: trial(i) for i in PRAZSKA}
    images = {i: [Image(listing_id=i, image_id=i * 10, seq=0, phash=7_000, pop=1)]
              for i in listings}
    facts = CohortFacts(Dataset(meta=Meta(), listings=listings, images_by_listing=images))
    store = MemoryStore()
    store.upsert_pairs([PairRow(
        lo=PRAZSKA[0], hi=PRAZSKA[1], probes=["attr_area"], from_lo=True, from_hi=True,
        zone="merge", score=1.0, families=["ATTR", "IMG", "LOC", "TXT"],
        certificate=certificate, veto=None,
        reason=f"certificate:{certificate}" if certificate else "model", evidence={},
        context={}, fp_lo="a", fp_hi="b")])
    _recluster(store, facts, cfg, _Working(facts, cfg), set(PRAZSKA), Limits(),
               PassResult(generation="rt", calibration_digest="x"))
    return sorted(map(sorted, store.clusters.values()))


def test_E303_reaches_the_lanes_recluster_through_the_stored_certificate() -> None:
    """The excuse reads the pair's certificate and `_recluster` handed the relation none, so
    the dial switched on still refused Pražská 930/47 in every lane (rt, 10-07)."""
    w31 = Settings.from_json(SETTINGS / "w31.json")
    on = Settings.from_dict({**w31.to_dict(), "d43_cluster_price_kc_house_number": True})
    assert _prazska_groups(w31) == [], "the control: w31's price limb holds the two apart"
    assert _prazska_groups(on) == [sorted(PRAZSKA)]
    assert _prazska_groups(on, certificate=None) == [], "a model edge earns no K-C excuse"


def test_E303_is_pair_grain_and_off_in_w30() -> None:
    assert not S15.d43_cluster_price_kc_house_number
    # realitymix 18223015 files no house number: the excuse never reaches its pair
    rel = ClusterRelation({i: trial(i) for i in (18223015, 19032904)}, None, E303,
                          certificates={(18223015, 19032904): "K-C"})
    assert not rel.ok(18223015, 19032904)


# --- E305: the one cellar two flat bodies state (tightening, ON in w30) -----------------------
def test_E305_reads_the_one_cellar_and_abstains_on_a_list() -> None:
    from autodedup.text_facts import cellar_areas

    assert {v for v, _ in cellar_areas(trial(13057838).description)} == {6.6}
    assert {v for v, _ in cellar_areas(trial(19042993).description)} == {3.5}
    # Mechová: a 2 m² kóje AND a share of a 13,3 m² cellar room is a list, not one cellar
    assert cellar_areas(trial(479152).description) == frozenset()
    assert cellar_areas("byt se sklepem 60 m2 v centru") == frozenset()
    assert cellar_areas("dva sklepy o velikosti 3 m2 a 4 m2") == frozenset()
    assert cellar_areas("sklep o celkové ploše 9 m²") == frozenset()   # E215's total


def test_E305_byty_podlesi_two_cellars_are_two_flats() -> None:
    # idnes 13057838 `sklepní kóje 6,6 m²` (08-02..08-22) against realitymix 19042993
    # `sklepní kóje cca 3,5 m2` (09-25): one template, one 35 m², one 12,500 rent.
    a, b = trial(13057838), trial(19042993)
    assert "cellar_area" not in names(a, b, variant(d43_cellar_area=False), GATE)
    for mode in (PROMOTE, GATE, CLUSTER):
        assert "cellar_area" in names(a, b, S15, mode)


def test_E305_sizes_that_meet_are_one_cellar() -> None:
    a = trial(19042993)
    for body in ("sklepní kóje 3,5 m2", "sklepní kóje cca 3,6 m2", "sklep o velikosti 3.8 m2"):
        b = trial(13057838, description=body)
        assert "cellar_area" not in names(a, b, S15), body
    land = trial(13057838, category_main="dum")
    assert "cellar_area" not in names(a, land, S15)


# --- E305r (W31): the cellar yields to the engine's own identity evidence -------------------
S15B = Settings.from_json(SETTINGS / "w31.json")
W31_ON = {"attr_probe_town_grain", "d43_cellar_area", "d43_cellar_area_photo_yield"}


def _frames(n: float) -> dict:
    return {"phash_tight_matches": (n, True)}


def test_E305r_one_agency_correcting_its_cellar_is_one_flat() -> None:
    # Na Radouci 1045 (cohort 17): bazos 161444 `sklep o vymere 2m2` (June text) against sreality
    # 179801 and idnes 208326 `5 m2` (August text); one three-step price path, 18 / 20 tight frames.
    a = trial(161444)
    for other, tight in ((179801, 18.0), (208326, 20.0)):
        b = trial(other)
        feats = _frames(tight)
        assert "cellar_area" in [f.name for f in distinguishing_facts(a, b, feats, S15, GATE)]
        assert "cellar_area" not in [f.name for f in distinguishing_facts(a, b, feats, S15B, GATE)]
        # a pair never scored carries no frames, and the stated cellar stays a fact
        assert "cellar_area" in [f.name for f in distinguishing_facts(a, b, None, S15B, GATE)]


def test_E305r_needs_both_the_frames_and_one_price_path() -> None:
    a, b = trial(161444), trial(179801)
    assert "cellar_area" in [f.name for f in distinguishing_facts(a, b, _frames(3.0), S15B, GATE)]
    moved = trial(179801, price=5_100_000.0,
                  price_history=[["2026-06-03T00:00:00+00:00", 5_100_000.0]])
    facts = distinguishing_facts(a, moved, _frames(18.0), S15B, GATE)
    assert "cellar_area" in [f.name for f in facts]


def test_E305r_keeps_byty_podlesi_apart() -> None:
    a, b = trial(13057838), trial(19042993)   # 2 tight frames on the engine's own row
    assert "cellar_area" in [f.name for f in distinguishing_facts(a, b, _frames(2.0), S15B, GATE)]


def test_w31_is_w29_plus_its_three_dials_and_E305r_needs_E305() -> None:
    w29, w31 = S14.to_dict(), S15B.to_dict()
    assert {key for key in w31 if w29.get(key) != w31[key]} == W31_ON
    for dial in ("d43_floor_total_camp_shift", "d43_floor_total_camp_shift_mixed",
                 "d43_total_floors_agreeing_unit", "d43_cluster_price_kc_house_number"):
        assert not getattr(S15B, dial)
    assert not Settings().d43_cellar_area_photo_yield and not S15.d43_cellar_area_photo_yield
    with pytest.raises(ValueError, match="E305r"):
        variant(d43_cellar_area=False, d43_cellar_area_photo_yield=True)


# --- E936: the quarter probe ---------------------------------------------------------------------
# E300 gave a known quarter of a split city no composite key: it posts the town key and probes
# none, so when its two home keys explode (more than `max_block_size` ads) it is dark — only a
# shared photo, text, address or broker reaches it. On the live areas (2026-10-03) that is 1,670
# Praha-Vysočany flats, among them sreality 268863 against the 44-ad iDNES train of 269879.
VYSOCANY, OTHER_QUARTER = 490245, 490059


def w31(**kwargs: object) -> Settings:
    return Settings.from_dict({**S15B.to_dict(), **kwargs})


def test_E936_a_known_quarter_of_a_split_city_keys_the_composite_at_its_quarter() -> None:
    listings = [advert(1, PRAHA, VYSOCANY), advert(2, PRAHA, None), advert(3, JABLONEC, 408093),
                advert(4, JABLONEC, None), advert(5, PRAHA, VYSOCANY, area_m2=None)]
    for cfg, posting in ((S14, set()), (S15B, {1})):
        index = build_index([fingerprint(x, cfg) for x in listings], cfg)
        for listing in listings:
            fp = fingerprint(listing, cfg)
            posted = [key for probe, key in index.index_keys(fp) if probe == QUARTER_PROBE]
            probed = [key for probe, key in index.probe_keys(fp) if probe == QUARTER_PROBE]
            if listing.id not in posting:
                assert posted == probed == []
                continue
            assert posted == [(f"c{VYSOCANY}", "byt", "prodej", "3+1", fp.area_band)]
            assert probed == [(f"c{VYSOCANY}", "byt", "prodej", "3+1", fp.area_band + offset)
                              for offset in (-1, 0, 1)]


def test_E936_the_quarter_probe_reaches_one_quarter_one_disposition_and_one_band_out() -> None:
    cfg = w31(max_block_size=1)   # any key two ads share explodes
    listings = [advert(1, PRAHA, VYSOCANY, area_m2=84.0), advert(2, PRAHA, VYSOCANY, area_m2=88.0),
                advert(3, PRAHA, OTHER_QUARTER, area_m2=84.0),
                advert(4, PRAHA, VYSOCANY, area_m2=84.0, disposition="3+kk"),
                advert(5, PRAHA, VYSOCANY, area_m2=118.0),
                advert(6, PRAHA, VYSOCANY, area_m2=88.0, disposition="2+kk")]
    fps = {x.id: fingerprint(x, cfg) for x in listings}
    assert [fps[i].area_band for i in (1, 2, 5)] == [19, 20, 21]
    index = build_index(fps.values(), cfg)
    reached = {other for probe, key in index.probe_keys(fps[1]) if probe == QUARTER_PROBE
               for other in index.postings[QUARTER_PROBE].get(key, ())}
    assert reached == {1, 2}
    assert index.candidates(fps[1]) == {2: {QUARTER_PROBE}}


def test_E936_the_quarter_probe_fills_last_so_the_cap_keeps_every_other_candidate() -> None:
    cfg = w31(max_block_size=4, max_candidates_per_listing=2)
    address = Location(obec_kod=PRAHA, cast_obce_kod=VYSOCANY, street_key="praha|kolbenova",
                       house_number_cp="12")
    subject, *same_address = [advert(n, PRAHA, VYSOCANY, location=address) for n in (1, 2, 3)]
    elsewhere = advert(4, PRAHA, VYSOCANY)
    fillers = [advert(5, PRAHA, VYSOCANY, area_m2=30.0),           # the disposition key: 5 > 4
               advert(6, PRAHA, VYSOCANY, disposition="2+kk")]     # the area key: 5 > 4
    fps = [fingerprint(x, cfg) for x in (subject, *same_address, elsewhere, *fillers)]
    index = build_index(fps, cfg)
    assert index.probes[-2:] == (TOWN_PROBE, QUARTER_PROBE)
    assert index.candidates(fps[0]) == {2: {"addr", QUARTER_PROBE}, 3: {"addr", QUARTER_PROBE}}
    # ...and the ad the cap discarded still finds it from its own side
    assert index.candidates(fps[3]) == {1: {QUARTER_PROBE}, 2: {QUARTER_PROBE}}


class _BeforeE936(BlockIndex):
    """The index as it stood before E936: every key but the quarter key."""

    def index_keys(self, fp: Fingerprint) -> list[tuple[str, Any]]:
        return [(probe, key) for probe, key in super().index_keys(fp) if probe != QUARTER_PROBE]


@pytest.mark.parametrize("cap", [2, 3, 60])
def test_E936_is_additive_every_candidate_reached_before_is_reached_as_before(cap: int) -> None:
    cfg = Settings.from_dict({**QUARTER_GRAIN.to_dict(), "max_candidates_per_listing": cap})
    fps = build_all(quarter_grain_dataset(), cfg)
    before, after = _BeforeE936(cfg), BlockIndex(cfg)
    for index in (before, after):
        for listing_id in sorted(fps):
            index.add(fps[listing_id])
        index.finalize()
    gained = 0
    for listing_id in sorted(fps):
        now = after.candidates(fps[listing_id])
        kept = {other: probes - {QUARTER_PROBE} for other, probes in now.items()
                if probes - {QUARTER_PROBE}}
        assert kept == before.candidates(fps[listing_id]), listing_id
        gained += len(now) - len(kept)
    assert gained > 0


def test_E936_the_quarter_probe_never_takes_a_slot_a_stronger_probe_holds() -> None:
    cfg = w31(max_block_size=4, max_candidates_per_listing=2)
    address = Location(obec_kod=PRAHA, cast_obce_kod=VYSOCANY, street_key="praha|kolbenova",
                       house_number_cp="12")
    flats = [advert(n, PRAHA, VYSOCANY, disposition="3+kk", area_m2=84.0) for n in (1, 2, 3)]
    flats.append(advert(4, PRAHA, VYSOCANY, disposition="2+kk", area_m2=82.0))  # area key: 5 > 4
    subject = advert(7, PRAHA, VYSOCANY, disposition="3+kk", area_m2=84.0, location=address)
    # two more of the subject's disposition (its key: 6 > 4), at its address and with no area
    partners = [advert(n, PRAHA, VYSOCANY, disposition="3+kk", area_m2=None, location=address)
                for n in (8, 9)]
    fps = [fingerprint(x, cfg) for x in (*flats, subject, *partners)]
    before, after = _BeforeE936(cfg), BlockIndex(cfg)
    for index in (before, after):
        for fp in fps:
            index.add(fp)
        index.finalize()
    held = {8: {"addr"}, 9: {"addr"}}
    assert before.candidates(fps[4]) == after.candidates(fps[4]) == held
    assert after.candidates(fps[0]) == {2: {QUARTER_PROBE}, 3: {QUARTER_PROBE}}


def test_E936_an_exploded_quarter_key_is_skipped_like_any_key() -> None:
    cfg = w31(max_block_size=2)
    listings = [advert(n, PRAHA, VYSOCANY) for n in (1, 2, 3)]
    assert pairs_of(listings, cfg) == {}
    assert set(pairs_of(listings[:2], cfg)) == {(1, 2)}


def test_E936_the_268863_shape_is_a_candidate_only_through_the_quarter_key() -> None:
    """sreality 268863 against the iDNES train of 269879 (live areas, 2026-10-03): one 3+kk of
    84 m² on the ninth floor of Praha-Vysočany, no shared photo, text, address or broker, and
    both home keys over the limit (here four), so retrieval returned nothing. Both post the town
    key and neither probes it (E300); the quarter key reaches the pair."""
    flat = dict(disposition="3+kk", area_m2=84.0, floor=9)
    a = advert(268863, PRAHA, VYSOCANY, price=20_000_000.0, **flat)
    b = advert(269879, PRAHA, VYSOCANY, source="idnes", price=25_000_000.0, **flat)
    fillers = [advert(n, PRAHA, VYSOCANY, disposition="3+kk", area_m2=area)
               for n, area in ((11, 30.0), (12, 40.0), (13, 150.0), (14, 200.0))]
    fillers += [advert(n, PRAHA, VYSOCANY, disposition=disposition, area_m2=area)
                for n, disposition, area in ((21, "2+kk", 80.0), (22, "4+kk", 82.0),
                                             (23, "3+1", 78.0), (24, "2+1", 76.0))]
    listings = [a, b, *fillers]
    before = Settings.from_dict({**S14.to_dict(), "max_block_size": 4})
    for cfg, expected in ((before, None), (w31(max_block_size=4), {QUARTER_PROBE})):
        assert pairs_of(listings, cfg).get((268863, 269879)) == expected
    cfg = w31(max_block_size=4)
    index = build_index([fingerprint(x, cfg) for x in listings], cfg)
    fa = fingerprint(a, cfg)
    assert {probe for probe, key in index.index_keys(fa) if key in index.exploded[probe]} == {
        "attr_dispo", "attr_area"}
    assert TOWN_PROBE in {probe for probe, _ in index.index_keys(fa)}
    assert TOWN_PROBE not in {probe for probe, _ in index.probe_keys(fa)}


def test_E936_the_real_time_lane_retrieves_exactly_what_the_cohort_pass_does() -> None:
    ds = quarter_grain_dataset()
    fps = build_all(ds, QUARTER_GRAIN)
    calibration = _calibration(ds, QUARTER_GRAIN)
    assert QUARTER_PROBE in calibration.exploded
    store, _passes = _drain(ds, QUARTER_GRAIN, calibration, arrival_order(ds))
    index = build_index([fps[i] for i in sorted(fps)], QUARTER_GRAIN)
    keyer = Keyer(QUARTER_GRAIN, calibration)
    guards = {i: guard_row(fp) for i, fp in fps.items()}
    quarter_only = 0
    for listing_id in sorted(fps):
        expected = index.candidates(fps[listing_id])
        assert retrieve(fps[listing_id], keyer, store, guards, QUARTER_GRAIN, {}) == expected
        quarter_only += sum(1 for probes in expected.values() if probes == {QUARTER_PROBE})
    assert quarter_only > 0


def test_E936_the_real_time_lane_reaches_one_state_in_any_order_and_claim() -> None:
    """Every ad that probes a quarter key is posted under it (E71): one final state whatever
    the order or the claim, the quarter-only pairs in it and none across two quarters."""
    ds = quarter_grain_dataset()
    pairs, _groups = invariant_state(ds, QUARTER_GRAIN, _calibration(ds, QUARTER_GRAIN))
    assert any(probes == (QUARTER_PROBE,) for *_rest, probes in pairs.values())
    other = [i for i, x in ds.listings.items() if x.location.cast_obce_kod == OTHER_QUARTER]
    assert other and not any(set(key) & set(other) for key in pairs)


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
    # w30 as cohort 17 judged it (E305 on), frozen: refused there, kept for replay parity.
    "w30.json": "f718506d7ab75e2a43a88d835b9daf5a05a522001c7f1e43d0987b566c76638f",
    "w30_land_hold.json": "3e20a55b726ad51a3b5d53ca3730d3381ffd7dcdf1c2bb7a26b12bcc642f06b3",
    "w30_rentals_hold.json": "b96e0cb8dd986120edbbb36660ca13bdb285d18dd7297ff2380ff748ee870018",
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
def test_w29_replays_every_row_the_room_tag_cannot_move(tmp_path: Path) -> None:
    """The stored S14 run was the cohort pass; `harness run` is the lane's pass (SW1). E929
    took the room tag out of every refusal, so a stored row whose tag sat at or above the
    0.90 floor (or was unknown) decides identically, and every row that moved had it below.
    E935 lets land meet a house or a commercial ad, and E938 a flat a commercial ad, so the
    only rows S14 never stored are such pairs, which its rule vetoed before scoring; the
    fan-out cap (E17) gives them slots, so a row S14 stored and this run did not is a reject
    they displaced (four flat pairs on this pack), never a merge or a band."""
    from autodedup.dataset import load
    from autodedup.harness import load_must_not_link, named_model, read_pairs, run

    ds = load(TRIAL)
    ids = set(ds.listings)
    mnl = frozenset(p for p in load_must_not_link(str(MNL)) if p[0] in ids and p[1] in ids)
    run(ds, S14, named_model("w6_gold"), tmp_path,
        mnl if S14.operator_must_not_link else frozenset())
    decided = lambda rows: {(r["lo"], r["hi"]): (r["zone"], r["reason"], r["certificate"],  # noqa: E731
                                                 r["veto"], round(r["score"], 9)) for r in rows}
    stored = read_pairs(S14_TRIAL_RUN)
    now, then = decided(read_pairs(tmp_path)), decided(stored)
    cross = [{"pozemek", "dum"}, {"pozemek", "komercni"}, {"byt", "komercni"}]
    assert all(then[key][0] == "reject" for key in then.keys() - now.keys())
    assert all({ds.listings[lo].category_main, ds.listings[hi].category_main} in cross
               for lo, hi in now.keys() - then.keys())
    tag = {(r["lo"], r["hi"]): r["feats"].get("tag_room_clip_min2") for r in stored}
    below = {key for key, slot in tag.items() if slot and slot[1] and slot[0] < 0.90}
    assert {key for key in then.keys() & now.keys() if now[key] != then[key]} <= below
