"""Pairwise features: the (value, present) contract, the E9/E10/E19/E20 mechanics, E11 diversity.

`Fingerprint` and `Settings` are built concurrently in W2, so the stand-ins here carry exactly
the contract's field names — the test pins the interface, not the sibling module.
"""

from __future__ import annotations

import base64
import math
import struct
from dataclasses import dataclass, field
from typing import Any

import pytest

from autodedup import dataset as ds
from autodedup import features as ft


@dataclass(slots=True)
class StubSettings:
    area_band_tol: float = 0.20
    area_reject_pct: float = 0.08
    area_band_pct: float = 0.03
    catalog_df: int = 8
    anchor_images: int = 3
    max_block_size: int = 200
    max_candidates_per_listing: int = 60
    phash_tight: int = 6
    phash_loose: int = 11
    band_bits: int = 16
    simhash_bands: int = 4
    text_min_chars: int = 200
    t_hi: float = 0.97
    t_lo: float = 0.30
    store_floor: float = 0.02
    cluster_area_spread: float = 0.08
    max_cluster_size: int = 8
    rare_token_df: int = 2
    vocabulary_attr_keys: tuple[str, ...] = ("price_unit", "area_basis")
    numeral_conflict_units: tuple[str, ...] = ("floor", "rooms", "unit")


@dataclass(slots=True)
class StubFingerprint:
    listing_id: int
    source: str | None = "sreality"
    block_key: str | int | None = "554782"
    obec_kod: int | None = 554782
    cast_obce_kod: int | None = None
    cat_group: str | None = "byt"
    category_main: str | None = "byt"
    category_type: str | None = "prodej"
    area_m2: float | None = None
    area_band: int | None = None
    disposition: str | None = None
    floor: int | None = None
    total_floors: int | None = None
    broker_key: str | None = None
    broker_identity_id: int | None = None
    street_key: str | None = None
    house_number: str | None = None
    ruian_adm_kod: int | None = None
    psc: str | None = None
    lat: float | None = None
    lon: float | None = None
    granularity_rank: int | None = None
    is_address_grain: bool | None = None
    pin_key: str | None = None
    price: float | None = None
    price_events: list[tuple[str, float]] = field(default_factory=list)
    desc_norm: str = ""
    desc_tokens: list[str] = field(default_factory=list)
    desc_shingles: set[int] = field(default_factory=set)
    desc_simhash: int | None = None
    desc_bands: list[tuple[int, int]] = field(default_factory=list)
    numerals: set[tuple[str, float]] = field(default_factory=set)
    image_hashes: set[int] = field(default_factory=set)
    all_hashes: set[int] = field(default_factory=set)
    catalog_ratio: float | None = None
    anchor_bands: list[tuple[int, int, int]] = field(default_factory=list)
    image_ids: list[int] = field(default_factory=list)
    interior_image_ids: list[int] = field(default_factory=list)
    exterior_image_ids: list[int] = field(default_factory=list)
    plan_image_ids: list[int] = field(default_factory=list)
    n_images: int = 0
    country_status: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    inactive_at: str | None = None
    is_active: bool = True
    has_text: bool = False


SETTINGS = StubSettings()


def clip_b64(seed: float) -> str:
    values = [math.sin(seed + index * 0.01) for index in range(ds.CLIP_DIM)]
    return base64.b64encode(struct.pack(ds.CLIP_STRUCT, *values)).decode("ascii")


def listing(listing_id: int, **kwargs: Any) -> ds.Listing:
    location = kwargs.pop("location", None) or ds.Location()
    price_history = kwargs.pop("price_history", [])
    return ds.Listing(
        id=listing_id,
        block=kwargs.pop("block", "554782"),
        location=location,
        price_history=price_history,
        **kwargs,
    )


def image(listing_id: int, image_id: int, **kwargs: Any) -> ds.Image:
    return ds.Image(listing_id=listing_id, image_id=image_id, **kwargs)


def context(
    fps: dict[int, Any],
    listings: dict[int, ds.Listing] | None = None,
) -> ft.FeatureContext:
    ctx = ft.FeatureContext.build(fps, SETTINGS)
    if listings:
        ctx.index_attrs(fps, listings)
    return ctx


def compute(
    fa: StubFingerprint,
    fb: StubFingerprint,
    la: ds.Listing,
    lb: ds.Listing,
    images_a: list[ds.Image] | None = None,
    images_b: list[ds.Image] | None = None,
    ctx: ft.FeatureContext | None = None,
) -> ft.Feats:
    ctx = ctx or context({fa.listing_id: fa, fb.listing_id: fb}, {la.id: la, lb.id: lb})
    return ft.pair_features(
        fa, fb, la, lb, images_a or [], images_b or [], ctx, SETTINGS
    )


def test_feature_order_is_a_closed_versioned_vocabulary() -> None:
    assert len(FEATURE_SET := set(ft.FEATURE_ORDER)) == len(ft.FEATURE_ORDER)
    assert FEATURE_SET == set(ft.FAMILY_OF)
    assert set(ft.FAMILY_OF.values()) == {"ATTR", "PRICE", "TXT", "BRK", "LOC", "IMG", "TIME"}
    for family, rules in ft.EVIDENCE_RULES.items():
        assert family in ft.EVIDENCE_FAMILIES
        for rule in rules:
            for name, _, _ in rule:
                assert ft.FAMILY_OF[name] == family


def test_pair_features_emits_exactly_feature_order_as_value_present_pairs() -> None:
    fa = StubFingerprint(1)
    fb = StubFingerprint(2)
    feats = compute(fa, fb, listing(1), listing(2))
    assert set(feats) == set(ft.FEATURE_ORDER)
    for name, entry in feats.items():
        assert isinstance(entry, tuple) and len(entry) == 2, name
        value, present = entry
        assert isinstance(value, float) and isinstance(present, bool), name
        if not present:
            assert value == 0.0, name


def test_empty_listings_produce_almost_no_evidence_and_never_a_mismatch() -> None:
    feats = compute(StubFingerprint(1), StubFingerprint(2), listing(1), listing(2))
    present = {name for name, (_, flag) in feats.items() if flag}
    # E12: only the unconditional mask features survive a pair with no data at all.
    assert present == {"broker_known_both", "n_images_min", "both_active", "same_source"}
    assert ft.evidence_families(feats) == set()


def test_attribute_features_and_rare_agreement_weighting() -> None:
    fa = StubFingerprint(1, area_m2=64.0, disposition="3+kk", floor=3, total_floors=6)
    fb = StubFingerprint(2, area_m2=64.4, disposition="3+kk", floor=4, total_floors=6)
    la = listing(1, attrs={"energy_rating": "B", "has_lift": True, "condition": "novostavba"})
    lb = listing(2, attrs={"energy_rating": "B", "has_lift": True, "condition": "dobry"})
    others = {
        index: StubFingerprint(index)
        for index in range(10, 30)
    }
    other_listings = {
        index: listing(index, attrs={"energy_rating": "C", "has_lift": True})
        for index in others
    }
    fps: dict[int, Any] = {1: fa, 2: fb, **others}
    listings = {1: la, 2: lb, **other_listings}
    feats = compute(fa, fb, la, lb, ctx=context(fps, listings))
    assert feats["area_rel_diff"][1] and feats["area_rel_diff"][0] < 0.01
    assert feats["area_exact"] == (1.0, True)
    assert feats["dispo_equal"] == (1.0, True)
    assert feats["floor_diff"] == (1.0, True)
    assert feats["total_floors_equal"] == (1.0, True)
    assert feats["attr_agreements"][0] == 2.0
    assert feats["attr_contradictions"][0] == 1.0
    # A rare in-block agreement (energy B, 2 of 22 rows) outweighs the common one (lift, 22/22).
    assert feats["attr_agreements_rare"][0] > feats["attr_agreements"][0]
    assert "ATTR" in ft.evidence_families(feats)


def test_portal_vocabulary_slots_neither_agree_nor_contradict() -> None:
    """`price_unit` differs on 55% of cross-portal TRUE duplicates and 0% of same-portal ones
    (gold eval 2026-09-16) because sreality/bezrealitky say `celkem|mesic` where the other seven
    portals say `za nemovitost|za mesic`. `area_basis`: 31% of cross-portal positives, 14% of
    negatives. Neither may reach `attr_contradictions` — nor `attr_agreements`."""
    la = listing(1, attrs={"price_unit": "celkem", "area_basis": "usable", "ownership": "osobni"})
    lb = listing(2, attrs={"price_unit": "za nemovitost", "area_basis": "unknown",
                           "ownership": "osobni"})
    feats = compute(StubFingerprint(1), StubFingerprint(2), la, lb)
    assert feats["attr_contradictions"] == (0.0, True)
    assert feats["attr_agreements"] == (1.0, True)  # ownership alone
    assert [key for key, _, _ in ft.attribute_conflicts(la, lb)] == []

    agreeing = listing(3, attrs={"price_unit": "celkem", "area_basis": "usable",
                                 "ownership": "osobni"})
    same_vocabulary = compute(StubFingerprint(1), StubFingerprint(3), la, agreeing)
    assert same_vocabulary["attr_agreements"] == (1.0, True)  # the match is not evidence either


def test_the_vocabulary_set_is_settings_driven() -> None:
    la = listing(1, attrs={"price_unit": "celkem", "ownership": "osobni"})
    lb = listing(2, attrs={"price_unit": "za nemovitost", "ownership": "druzstevni"})
    ctx = context({1: StubFingerprint(1), 2: StubFingerprint(2)}, {1: la, 2: lb})
    kept = ft.pair_features(
        StubFingerprint(1), StubFingerprint(2), la, lb, [], [], ctx,
        StubSettings(vocabulary_attr_keys=()),
    )
    assert kept["attr_contradictions"] == (2.0, True)
    assert [key for key, _, _ in ft.attribute_conflicts(la, lb)] == ["ownership"]


def test_the_stored_stamp_is_the_vocabularys_own_version_not_a_second_number() -> None:
    """`autodedup.pairs.feature_version` tells a later pass which vocabulary produced a score.
    The lane must therefore stamp the number FEATURE_ORDER carries: a constant of its own would
    let a 44-column v1 vector and a 45-column v2 vector land in the table under the same stamp."""
    from autodedup import score_lane

    assert score_lane.FEATURE_VERSION == ft.FEATURE_VERSION
    assert ft.FEATURE_VERSION == 4 and len(ft.FEATURE_ORDER) == 59


def test_a_shared_unit_number_is_its_own_feature() -> None:
    """E45's last arm: `normalize.numeric_facts` isolates the `unit` slot, and FEATURE_ORDER
    grew at the END only (v2 at index 44, the v3 plot slots after it) so a stored v1 vector
    still reads."""
    assert ft.FEATURE_ORDER[44] == "unit_number_shared"
    assert ft.FEATURE_ORDER[45:47] == ("plot_area_rel_diff", "plot_area_exact")
    assert ft.FEATURE_ORDER[47:] == ft.TAG_FEATURE_NAMES + ("floor_stated_conflict",)
    assert ft.FEATURE_VERSION >= 2
    shared = compute(
        StubFingerprint(1, numerals={("unit", 705.0), ("m2", 64.0)}),
        StubFingerprint(2, numerals={("unit", 705.0), ("m2", 64.0)}),
        listing(1), listing(2),
    )
    assert shared["unit_number_shared"] == (1.0, True)
    different = compute(
        StubFingerprint(1, numerals={("unit", 705.0)}),
        StubFingerprint(2, numerals={("unit", 706.0)}),
        listing(1), listing(2),
    )
    assert different["unit_number_shared"] == (0.0, True)
    assert different["numeral_conflict"] == (1.0, True)
    one_sided = compute(
        StubFingerprint(1, numerals={("unit", 705.0)}),
        StubFingerprint(2, numerals={("m2", 64.0)}),
        listing(1), listing(2),
    )
    assert one_sided["unit_number_shared"] == (0.0, False)  # unknown is never a mismatch


def test_only_unit_level_slots_can_raise_a_numeral_conflict() -> None:
    """Prices and areas have their own features; a Kč or m² disagreement is an E6 re-listing."""
    fa = StubFingerprint(1, numerals={("kc", 6_900_000.0), ("m2", 78.0)})
    fb = StubFingerprint(2, numerals={("kc", 5_400_000.0), ("m2", 92.0)})
    assert compute(fa, fb, listing(1), listing(2))["numeral_conflict"] == (0.0, True)
    ctx = context({1: fa, 2: fb})
    widened = ft.pair_features(
        fa, fb, listing(1), listing(2), [], [], ctx,
        StubSettings(numeral_conflict_units=("floor", "rooms", "unit", "kc", "m2")),
    )
    assert widened["numeral_conflict"] == (1.0, True)


def test_area_mismatch_is_a_value_not_an_absence() -> None:
    fa = StubFingerprint(1, area_m2=60.0)
    fb = StubFingerprint(2, area_m2=90.0)
    feats = compute(fa, fb, listing(1), listing(2))
    assert feats["area_rel_diff"][1] is True
    assert feats["area_rel_diff"][0] == pytest.approx(1 / 3)
    assert feats["area_exact"] == (0.0, True)
    fb_unknown = StubFingerprint(2)
    absent = compute(fa, fb_unknown, listing(1), listing(2))
    assert absent["area_rel_diff"] == (0.0, False)


def test_price_path_event_match_counts_only_real_changes() -> None:
    history_a = [
        ("2026-01-01T00:00:00+00:00", 5_000_000.0),
        ("2026-02-01T00:00:00+00:00", 4_750_000.0),
        ("2026-03-01T00:00:00+00:00", 4_500_000.0),
    ]
    history_b = [
        ("2026-01-03T00:00:00+00:00", 5_000_000.0),
        ("2026-02-02T00:00:00+00:00", 4_750_000.0),
        ("2026-03-02T00:00:00+00:00", 4_500_000.0),
    ]
    fa, fb = StubFingerprint(1, price=4_500_000.0), StubFingerprint(2, price=4_500_000.0)
    feats = compute(
        fa, fb, listing(1, price_history=history_a), listing(2, price_history=history_b)
    )
    assert feats["price_path_event_match"] == (1.0, True)
    unrelated = [
        ("2026-01-01T00:00:00+00:00", 3_000_000.0),
        ("2026-07-01T00:00:00+00:00", 2_100_000.0),
    ]
    feats_off = compute(
        fa, fb, listing(1, price_history=history_a), listing(2, price_history=unrelated)
    )
    assert feats_off["price_path_event_match"] == (0.0, True)
    flat = compute(fa, fb, listing(1, price_history=history_a), listing(2))
    assert flat["price_path_event_match"] == (0.0, False)


def test_a_price_that_never_moved_is_never_a_price_path_match() -> None:
    """E19 matches price CHANGES. `Fingerprint.price_events` carries absolute prices derived from
    the same history, so it can add nothing — one stamp means "no change", never "unknown"."""
    history_a = [("2026-01-10T08:00:00+00:00", 4_500_000.0)]
    history_b = [("2026-01-11T08:00:00+00:00", 4_500_000.0)]
    fa = StubFingerprint(1, price=4_500_000.0, price_events=[("2026-01-10T08:00:00+00:00", 4_500_000.0)])
    fb = StubFingerprint(2, price=4_500_000.0, price_events=[("2026-01-11T08:00:00+00:00", 4_500_000.0)])
    feats = compute(
        fa, fb, listing(1, price_history=history_a), listing(2, price_history=history_b)
    )
    assert feats["price_path_event_match"] == (0.0, False)


def test_text_features_separate_template_from_unit_specific_evidence() -> None:
    template = ["prodej", "bytu", "v", "cihlovem", "dome", "po", "rekonstrukci"]
    fa = StubFingerprint(
        1,
        desc_norm="x" * 900,
        desc_tokens=template + ["krizikova", "dvorakova"],
        desc_shingles={1, 2, 3, 4, 5, 6},
        desc_simhash=0b1011,
        numerals={("m2", 64.0), ("floor", 3.0)},
        has_text=True,
    )
    fb = StubFingerprint(
        2,
        desc_norm="x" * 450,
        desc_tokens=template + ["krizikova"],
        desc_shingles={1, 2, 3},
        desc_simhash=0b1010,
        numerals={("m2", 64.2), ("floor", 3.0)},
        has_text=True,
    )
    filler = {
        index: StubFingerprint(index, desc_tokens=list(template), has_text=True)
        for index in range(10, 32)  # the block must clear MIN_RARE_BLOCK_DOCS for "rare" to mean rare
    }
    ctx = context({1: fa, 2: fb, **filler})
    feats = compute(fa, fb, listing(1), listing(2), ctx=ctx)
    assert feats["jaccard_shingle"][0] == pytest.approx(0.5)
    assert feats["containment_max"] == (1.0, True)  # the truncated re-post is fully contained
    assert 0.0 < feats["tfidf_cos"][0] <= 1.0
    assert feats["simhash_hamming"] == (1.0, True)
    assert feats["len_ratio"][0] == pytest.approx(0.5)
    assert feats["rare_token_overlap"] == (1.0, True)  # "krizikova", df 2; the template is not rare
    assert feats["numeric_fact_overlap"][0] == 2.0
    assert feats["numeral_conflict"] == (0.0, True)
    assert "TXT" in ft.evidence_families(feats)


def test_numeral_conflict_fires_on_a_contradicted_slot() -> None:
    fa = StubFingerprint(1, numerals={("m2", 64.0), ("floor", 3.0)})
    fb = StubFingerprint(2, numerals={("m2", 64.0), ("floor", 7.0)})
    feats = compute(fa, fb, listing(1), listing(2))
    assert feats["numeral_conflict"] == (1.0, True)
    assert feats["numeric_fact_overlap"][0] == 1.0


def test_location_distance_is_masked_below_street_grain_and_discounted_by_pin_population() -> None:
    coarse_a = StubFingerprint(1, lat=50.08, lon=14.44, granularity_rank=40, pin_key="50.080000,14.440000")
    coarse_b = StubFingerprint(2, lat=50.09, lon=14.45, granularity_rank=40, pin_key="50.080000,14.440000")
    crowd = {
        index: StubFingerprint(index, pin_key="50.080000,14.440000") for index in range(10, 30)
    }
    feats = compute(coarse_a, coarse_b, listing(1), listing(2), ctx=context({1: coarse_a, 2: coarse_b, **crowd}))
    assert feats["dist_norm"] == (0.0, False)  # E16: no distance off a municipal pin
    assert feats["same_exact_pin"] == (1.0, True)
    assert feats["pin_pop"][0] == pytest.approx(math.log1p(22.0))

    fine_a = StubFingerprint(
        1, lat=50.08, lon=14.44, granularity_rank=100, ruian_adm_kod=123, street_key="krizikova",
        house_number="12", psc="18600",
    )
    fine_b = StubFingerprint(
        2, lat=50.080_1, lon=14.440_1, granularity_rank=100, ruian_adm_kod=123,
        street_key="krizikova", house_number="12", psc="18600",
    )
    la = listing(1, location=ds.Location(uncertainty_radius_m=10.0))
    lb = listing(2, location=ds.Location(uncertainty_radius_m=10.0))
    close = compute(fine_a, fine_b, la, lb)
    assert close["dist_norm"][1] is True
    assert close["dist_norm"][0] < 1.0
    assert close["same_ruian_adm_kod"] == (1.0, True)
    assert "LOC" in ft.evidence_families(close)


def test_uncertainty_radius_falls_back_to_the_granularity_rung() -> None:
    assert ft.uncertainty_radius_m(100, None) == 10.0
    assert ft.uncertainty_radius_m(40, None) == 2500.0
    assert ft.uncertainty_radius_m(100, 42.0) == 42.0
    assert ft.uncertainty_radius_m(None, None) == ft.RADIUS_BY_RANK[-1][1]


def test_broker_and_time_features() -> None:
    fa = StubFingerprint(
        1, broker_key="bk1", broker_identity_id=7, source="sreality",
        first_seen_at="2026-01-01T00:00:00+00:00", last_seen_at="2026-02-01T00:00:00+00:00",
        inactive_at="2026-02-01T00:00:00+00:00", is_active=False,
    )
    fb = StubFingerprint(
        2, broker_key="bk1", broker_identity_id=7, source="sreality",
        first_seen_at="2026-05-01T00:00:00+00:00", last_seen_at="2026-06-01T00:00:00+00:00",
    )
    la = listing(
        1, first_seen_at=fa.first_seen_at, last_seen_at=fa.last_seen_at,
        inactive_at=fa.inactive_at, is_active=False,
    )
    lb = listing(2, first_seen_at=fb.first_seen_at, last_seen_at=fb.last_seen_at)
    feats = compute(fa, fb, la, lb)
    assert feats["same_broker_key"] == (1.0, True)
    assert feats["same_broker_identity"] == (1.0, True)
    assert feats["broker_known_both"] == (1.0, True)
    assert feats["same_source"] == (1.0, True)
    assert feats["gap_days"][0] == pytest.approx(89.0)  # E6: a gap is a value, never a veto
    assert feats["overlap_days"] == (0.0, True)
    assert feats["both_active"] == (0.0, True)
    assert "BRK" in ft.evidence_families(feats)


def test_overlapping_windows_measure_overlap_not_gap() -> None:
    fa = StubFingerprint(1, first_seen_at="2026-01-01T00:00:00+00:00")
    fb = StubFingerprint(2, first_seen_at="2026-01-11T00:00:00+00:00")
    la = listing(
        1, first_seen_at="2026-01-01T00:00:00+00:00", last_seen_at="2026-02-01T00:00:00+00:00"
    )
    lb = listing(
        2, first_seen_at="2026-01-11T00:00:00+00:00", last_seen_at="2026-03-01T00:00:00+00:00"
    )
    feats = compute(fa, fb, la, lb)
    assert feats["gap_days"] == (0.0, True)
    assert feats["overlap_days"][0] == pytest.approx(21.0)


def test_image_features_subtract_catalog_photos_and_split_by_tag_family() -> None:
    # 101/201 interior and identical; 102/202 a shared catalogue site plan (pop >= catalog_df).
    images_a = [
        image(1, 101, seq=0, phash=0x0F0F0F0F0F0F0F0F, pop=1, clip=clip_b64(0.1)),
        image(1, 102, seq=1, phash=0x1234, pop=40, clip=clip_b64(0.2)),
        image(1, 103, seq=2, phash=0x00FF00FF00FF00FF, pop=1, clip=clip_b64(0.3)),
    ]
    images_b = [
        image(2, 201, seq=0, phash=0x0F0F0F0F0F0F0F0E, pop=1, clip=clip_b64(0.1)),
        image(2, 202, seq=1, phash=0x1234, pop=40, clip=clip_b64(0.2)),
        image(2, 203, seq=2, phash=0x00FF00FF00FF00FE, pop=1, clip=clip_b64(0.3)),
    ]
    fa = StubFingerprint(
        1, image_ids=[101, 102, 103], interior_image_ids=[101, 103], plan_image_ids=[102],
        n_images=3, catalog_ratio=1 / 3,
    )
    fb = StubFingerprint(
        2, image_ids=[201, 202, 203], interior_image_ids=[201, 203], plan_image_ids=[202],
        n_images=3, catalog_ratio=1 / 3,
    )
    feats = compute(fa, fb, listing(1), listing(2), images_a, images_b)
    assert feats["phash_tight_matches"] == (2.0, True)  # the catalogue plan never enters (E9)
    assert feats["phash_match_ratio"] == (1.0, True)
    assert feats["interior_match_ratio"] == (1.0, True)
    assert feats["plan_match_ratio"] == (0.0, False)  # E10: no non-catalog plan on either side
    assert feats["exterior_match_ratio"] == (0.0, False)
    assert feats["seq_monotone_ratio"] == (1.0, True)
    assert feats["catalog_ratio_max"][0] == pytest.approx(1 / 3)
    assert feats["n_images_min"] == (2.0, True)  # E9 subtracts the catalogue plan here too
    assert feats["clip_max_cos"][0] > 0.99
    assert feats["clip_mean_top3_cos"][1] is True
    assert ft.evidence_families(feats) == {"IMG"}  # E11: images alone are one family


def test_shared_plan_only_never_reads_as_interior_evidence() -> None:
    images_a = [image(1, 101, seq=0, phash=0xAAAA_AAAA_AAAA_AAAA, pop=2)]
    images_b = [image(2, 201, seq=0, phash=0xAAAA_AAAA_AAAA_AAAA, pop=2)]
    fa = StubFingerprint(1, plan_image_ids=[101], n_images=1)
    fb = StubFingerprint(2, plan_image_ids=[201], n_images=1)
    feats = compute(fa, fb, listing(1), listing(2), images_a, images_b)
    assert feats["plan_match_ratio"] == (1.0, True)
    assert feats["interior_match_ratio"] == (0.0, False)
    assert feats["phash_tight_matches"] == (1.0, True)
    # E10: a shared plan is a BUILDING signal, so it corroborates nothing on its own.
    assert ft.evidence_families(feats) == set()


def test_loose_matches_count_the_band_between_tight_and_loose() -> None:
    near = 0x0F0F0F0F0F0F0F0F
    loose = near ^ 0b1111_1111  # 8 bits out: inside loose (11), outside tight (6)
    images_a = [image(1, 101, seq=0, phash=near, pop=1)]
    images_b = [image(2, 201, seq=0, phash=loose, pop=1)]
    fa = StubFingerprint(1, n_images=1)
    fb = StubFingerprint(2, n_images=1)
    feats = compute(fa, fb, listing(1), listing(2), images_a, images_b)
    assert feats["phash_tight_matches"] == (0.0, True)
    assert feats["phash_loose_matches"] == (1.0, True)
    assert feats["phash_match_ratio"] == (0.0, True)


def test_gallery_reordering_drops_the_sequence_monotonicity() -> None:
    hashes = [0x1111_1111_1111_1111, 0x2222_2222_2222_2222, 0x4444_4444_4444_4444]
    images_a = [image(1, 100 + i, seq=i, phash=h, pop=1) for i, h in enumerate(hashes)]
    images_b = [
        image(2, 200 + i, seq=i, phash=h, pop=1)
        for i, h in enumerate(list(reversed(hashes)))
    ]
    fa = StubFingerprint(1, n_images=3)
    fb = StubFingerprint(2, n_images=3)
    feats = compute(fa, fb, listing(1), listing(2), images_a, images_b)
    assert feats["phash_tight_matches"] == (3.0, True)
    assert feats["seq_monotone_ratio"] == (0.0, True)


def test_features_are_symmetric_in_argument_order() -> None:
    fa = StubFingerprint(
        1, area_m2=64.0, disposition="3+kk", floor=2, price=5_000_000.0, broker_key="bk1",
        desc_norm="x" * 400, desc_tokens=["a", "b", "c"], desc_shingles={1, 2, 3},
        desc_simhash=0b1101, numerals={("m2", 64.0)}, n_images=2, catalog_ratio=0.1,
        first_seen_at="2026-01-01T00:00:00+00:00", last_seen_at="2026-02-01T00:00:00+00:00",
        lat=50.08, lon=14.44, granularity_rank=90, pin_key="50.080000,14.440000",
    )
    fb = StubFingerprint(
        2, area_m2=63.0, disposition="3+kk", floor=3, price=4_900_000.0, broker_key="bk2",
        desc_norm="x" * 800, desc_tokens=["a", "b", "d"], desc_shingles={2, 3, 9},
        desc_simhash=0b1001, numerals={("m2", 63.0)}, n_images=5, catalog_ratio=0.4,
        first_seen_at="2026-03-01T00:00:00+00:00", last_seen_at="2026-04-01T00:00:00+00:00",
        lat=50.081, lon=14.441, granularity_rank=90, pin_key="50.081000,14.441000",
    )
    la = listing(
        1, attrs={"condition": "dobry"}, first_seen_at=fa.first_seen_at,
        last_seen_at=fa.last_seen_at,
    )
    lb = listing(
        2, attrs={"condition": "dobry"}, first_seen_at=fb.first_seen_at,
        last_seen_at=fb.last_seen_at,
    )
    ctx = context({1: fa, 2: fb}, {1: la, 2: lb})
    forward = ft.pair_features(fa, fb, la, lb, [], [], ctx, SETTINGS)
    backward = ft.pair_features(fb, fa, lb, la, [], [], ctx, SETTINGS)
    for name in ft.FEATURE_ORDER:
        assert forward[name][1] == backward[name][1], name
        assert forward[name][0] == pytest.approx(backward[name][0]), name


def test_evidence_families_needs_present_positive_signals() -> None:
    weak = {name: ft.ABSENT for name in ft.FEATURE_ORDER}
    weak["jaccard_shingle"] = (0.1, True)
    weak["same_street_key"] = (0.0, True)
    assert ft.evidence_families(weak) == set()
    weak["same_street_key"] = (1.0, True)
    weak["phash_match_ratio"] = (0.8, True)
    assert ft.evidence_families(weak) == {"LOC"}  # unmeasured catalogue share: no IMG evidence
    weak["catalog_ratio_max"] = (0.0, True)
    assert ft.evidence_families(weak) == {"LOC", "IMG"}


def test_e11_never_reads_a_template_a_facade_or_a_disposition_as_unit_evidence() -> None:
    """The hard-negative class of §2: two units of one development, one broker, one boilerplate,
    one shared facade photo, one disposition. TXT/IMG/ATTR must all refuse to corroborate."""
    template = ["rezidence", "nove", "zahrady", "moderni", "bydleni", "developer", "klientske"]
    unit_a = template + ["jednotka", "a12"]
    unit_b = template + ["jednotka", "b31"]
    address = {"street_key": "praha|kolbenova", "house_number": "88", "psc": "19000"}
    fa = StubFingerprint(
        1, area_m2=62.0, disposition="2+kk", floor=2, broker_key="dev", broker_identity_id=33,
        desc_norm="x" * 800, desc_tokens=unit_a, desc_shingles=set(range(0, 20)),
        desc_simhash=0b1011, numerals={("m2", 62.0)}, has_text=True, n_images=4,
        catalog_ratio=0.0, image_ids=[101, 102, 103, 104], interior_image_ids=[102, 103, 104],
        exterior_image_ids=[101], **address,
    )
    fb = StubFingerprint(
        2, area_m2=64.0, disposition="2+kk", floor=5, broker_key="dev", broker_identity_id=33,
        desc_norm="x" * 800, desc_tokens=unit_b, desc_shingles=set(range(2, 20)),
        desc_simhash=0b1011, numerals={("m2", 64.0)}, has_text=True, n_images=4,
        catalog_ratio=0.0, image_ids=[201, 202, 203, 204], interior_image_ids=[202, 203, 204],
        exterior_image_ids=[201], **address,
    )
    facade = 0x0F0F_0F0F_0F0F_0F0F
    images_a = [image(1, 101, seq=0, phash=facade, pop=5)] + [
        image(1, 102 + i, seq=1 + i, phash=0x1111_0000_0000_0000 + i, pop=1) for i in range(3)
    ]
    images_b = [image(2, 201, seq=0, phash=facade, pop=5)] + [
        image(2, 202 + i, seq=1 + i, phash=0x2222_0000_0000_0000 + i, pop=1) for i in range(3)
    ]
    neighbours = {
        index: StubFingerprint(index, desc_tokens=list(template), has_text=True)
        for index in range(10, 32)
    }
    la = listing(1, attrs={"building_type": "cihlova", "has_lift": True})
    lb = listing(2, attrs={"building_type": "cihlova", "has_lift": True})
    listings = {1: la, 2: lb, **{i: listing(i, attrs={"building_type": "cihlova", "has_lift": True})
                                for i in neighbours}}
    ctx = context({1: fa, 2: fb, **neighbours}, listings)
    feats = compute(fa, fb, la, lb, images_a, images_b, ctx=ctx)

    assert feats["dispo_equal"] == (1.0, True)
    assert feats["phash_tight_matches"] == (1.0, True)
    assert feats["interior_match_ratio"] == (0.0, True)
    assert feats["rare_token_overlap"][0] < ft.TXT_RARE_TOKENS  # "jednotka" alone is not identity
    # The boilerplate is fully contained in the other advert and still corroborates nothing (E20).
    assert feats["containment_max"][0] >= 0.90
    families = ft.evidence_families(feats)
    assert families & {"TXT", "IMG", "ATTR"} == set()
    # LOC and BRK stay by construction — two units of one building DO share an address and a
    # broker. E11 is one of five rails (§2), and the one it owns is "images alone never merge".
    assert families == {"LOC", "BRK"}


def test_phash_matching_is_one_to_one_and_independent_of_argument_order() -> None:
    repeated = 0x3333_3333_3333_3333
    images_a = [image(1, 100 + i, seq=i, phash=repeated, pop=1) for i in range(4)]
    images_b = [
        image(2, 200, seq=0, phash=repeated, pop=1),
        image(2, 201, seq=1, phash=0x4444_4444_4444_4444, pop=1),
    ]
    fa = StubFingerprint(1, n_images=4, catalog_ratio=0.0, image_ids=[100, 101, 102, 103],
                         interior_image_ids=[100, 101, 102, 103])
    fb = StubFingerprint(2, n_images=2, catalog_ratio=0.0, image_ids=[200, 201],
                         interior_image_ids=[200, 201])
    ctx = context({1: fa, 2: fb})
    forward = ft.pair_features(fa, fb, listing(1), listing(2), images_a, images_b, ctx, SETTINGS)
    backward = ft.pair_features(fb, fa, listing(2), listing(1), images_b, images_a, ctx, SETTINGS)
    assert forward["phash_tight_matches"] == (1.0, True)  # one photo, matched once
    assert forward["phash_match_ratio"][0] <= 1.0
    assert forward["interior_match_ratio"][0] <= 1.0
    for name in ft.FEATURE_ORDER:
        assert forward[name] == pytest.approx(backward[name]), name


def test_a_price_cut_relisting_is_not_a_numeral_conflict() -> None:
    """E6: a re-listing after a price cut is a duplicate. Only identity slots may contradict."""
    fa = StubFingerprint(1, numerals={("kc", 6_900_000.0), ("m2", 78.0), ("floor", 4.0)})
    fb = StubFingerprint(2, numerals={("kc", 6_500_000.0), ("m2", 77.0), ("floor", 4.0)})
    feats = compute(fa, fb, listing(1), listing(2))
    assert feats["numeral_conflict"] == (0.0, True)
    assert feats["numeric_fact_overlap"][0] == 2.0  # the floor, and 77 vs 78 m2 inside tolerance
    contradicted = compute(
        StubFingerprint(1, numerals={("floor", 4.0)}),
        StubFingerprint(2, numerals={("floor", 7.0)}),
        listing(1), listing(2),
    )
    assert contradicted["numeral_conflict"] == (1.0, True)


def test_numeric_fact_overlap_is_a_one_to_one_count() -> None:
    fa = StubFingerprint(1, numerals={("m2", 78.0), ("m2", 79.0)})
    fb = StubFingerprint(2, numerals={("m2", 78.0)})
    forward = compute(fa, fb, listing(1), listing(2))
    backward = compute(fb, fa, listing(2), listing(1))
    assert forward["numeric_fact_overlap"] == (1.0, True)
    assert forward["numeric_fact_overlap"] == backward["numeric_fact_overlap"]


def test_rare_tokens_need_a_block_large_enough_for_rare_to_mean_rare() -> None:
    boilerplate = [f"slovo{index}" for index in range(30)]
    fa = StubFingerprint(1, desc_tokens=list(boilerplate), has_text=True)
    fb = StubFingerprint(2, desc_tokens=list(boilerplate), has_text=True)
    alone = compute(fa, fb, listing(1), listing(2), ctx=context({1: fa, 2: fb}))
    assert alone["rare_token_overlap"] == (0.0, False)  # a two-document block has no rare tokens

    crowd = {
        index: StubFingerprint(index, desc_tokens=["jiny", "text"], has_text=True)
        for index in range(10, 40)
    }
    big = compute(fa, fb, listing(1), listing(2), ctx=context({1: fa, 2: fb, **crowd}))
    assert big["rare_token_overlap"] == (ft.RARE_TOKEN_CAP, True)  # capped, not 30


def test_cross_block_pairs_are_symmetric_in_rarity_and_tfidf() -> None:
    """K3/K4/K5 are location-free, so a pair routinely spans two blocks (PROGRAM.md §4)."""
    tokens = ["rezidence", "kamenec", "jednotka"]
    fa = StubFingerprint(1, block_key="A", desc_tokens=list(tokens), has_text=True)
    fb = StubFingerprint(2, block_key="B", desc_tokens=list(tokens), has_text=True)
    block_a_filler = {
        index: StubFingerprint(index, block_key="A", desc_tokens=["jine", "slovo"], has_text=True)
        for index in range(10, 40)
    }
    block_b_filler = {
        index: StubFingerprint(index, block_key="B", desc_tokens=list(tokens), has_text=True)
        for index in range(50, 52)
    }
    la = listing(1, attrs={"condition": "novostavba"})
    lb = listing(2, attrs={"condition": "novostavba"})
    listings = {1: la, 2: lb}
    listings.update({i: listing(i, attrs={"condition": "dobry"}) for i in block_a_filler})
    listings.update({i: listing(i, attrs={"condition": "novostavba"}) for i in block_b_filler})
    fps: dict[int, Any] = {1: fa, 2: fb, **block_a_filler, **block_b_filler}
    ctx = context(fps, listings)
    forward = ft.pair_features(fa, fb, la, lb, [], [], ctx, SETTINGS)
    backward = ft.pair_features(fb, fa, lb, la, [], [], ctx, SETTINGS)
    assert forward["attr_agreements_rare"] == pytest.approx(backward["attr_agreements_rare"])
    assert forward["tfidf_cos"] == pytest.approx(backward["tfidf_cos"])
    assert forward["tfidf_cos"][1] is True


def test_the_clip_sample_is_taken_interior_first_not_cover_shot_first() -> None:
    interior_hash = 0x5555_5555_5555_5555
    images_a = [image(1, 100 + index, seq=index, phash=interior_hash + index, pop=1,
                      clip=clip_b64(0.5 if index == 3 else float(index)))
                for index in range(4)]
    fa = StubFingerprint(1, n_images=4, image_ids=[100, 101, 102, 103], interior_image_ids=[103])
    ctx = context({1: fa})
    gallery = ctx.clip_gallery(fa, images_a, SETTINGS)
    first = images_a[3].clip_vector()
    assert gallery[0][0] == first  # the interior photo leads the sample, whatever its sequence


def test_the_plot_is_estate_area_and_on_land_the_headline_area() -> None:
    """W4e: `estate_area` is the parcel on every category; on `pozemek` the headline area IS the
    parcel (`estate_area == area_m2` on 177 of 177 cohort rows carrying both). `garden_area` is a
    different fact — median 0.84 of the estate — and is never read as the plot."""
    house = listing(1, source="sreality", category_main="dum", area_m2=200.0,
                    attrs={"estate_area": 740.0, "garden_area": 661.0})
    land = listing(2, source="sreality", category_main="pozemek", area_m2=1373.0, attrs={})
    garden_only = listing(3, source="sreality", category_main="dum", area_m2=200.0,
                          attrs={"garden_area": 661.0})
    assert ft.plot_area(house) == 740.0
    assert ft.plot_area(land) == 1373.0
    assert ft.plot_area(garden_only) is None


def test_a_truncating_portals_plot_is_absent_not_a_contradiction() -> None:
    """ceskereality reads "5 870 m²" as 870 (its area regex has no thousands separator): 0 of its
    208 estate values reach 1,000 and 40 appear in the advert text only as the SUFFIX of a bigger
    number. Absence is never a mismatch (E12), so the carrier is dropped rather than compared."""
    trusted = listing(1, source="sreality", category_main="dum", area_m2=696.0,
                      attrs={"estate_area": 5870.0})
    truncating = listing(2, source="ceskereality", category_main="dum", area_m2=696.0,
                         attrs={"estate_area": 870.0})
    assert ft.plot_area(truncating) is None
    feats = compute(StubFingerprint(1, area_m2=696.0), StubFingerprint(2, area_m2=696.0),
                    trusted, truncating)
    assert feats["plot_area_rel_diff"] == ft.ABSENT
    assert feats["plot_area_exact"] == ft.ABSENT
    # realitymix truncates its LAND headline but not its estate slot — the table is per carrier.
    assert ft.plot_area(listing(3, source="realitymix", category_main="pozemek",
                                area_m2=310.0, attrs={})) is None
    assert ft.plot_area(listing(4, source="realitymix", category_main="dum",
                                area_m2=178.0, attrs={"estate_area": 682.0})) == 682.0


def test_the_advert_convicts_its_own_truncated_plot() -> None:
    """bazos is the LARGEST headline carrier (40 of the 70 rows the code reads through it) and
    shares the defective regex (scraper/bazos_parser.py:76), but it DOES reach 1,000 on 13 of 40 —
    the population has no signature, so the per-source table cannot convict it and gating the
    whole source would cost 34 good values to remove 6 bad ones. The listing's own advert can:
    "11 197 m²" stored as 197. 0 false flags over the 431 values on the clean carriers."""
    cut = listing(5, source="bazos", category_main="pozemek", area_m2=197.0, attrs={},
                  description="Prodam pozemek o vymere 11 197 m2 v obci.")
    assert ft.plot_area(cut) is None
    kept = listing(6, source="bazos", category_main="pozemek", area_m2=197.0, attrs={},
                   description="Prodam pozemek o vymere 197 m2 v obci.")
    assert ft.plot_area(kept) == 197.0
    # the test is a three-digit TAIL of a SPACE-GROUPED number, nothing looser
    assert ft.plot_truncated_in_text(870.0, "parcely o CP 5 870 m2")
    assert not ft.plot_truncated_in_text(870.0, "parcely o CP 5870 m2")
    assert not ft.plot_truncated_in_text(70.0, "parcely o CP 5 870 m2")  # not a whole group
    assert not ft.plot_truncated_in_text(870.0, None)


def test_an_implausibly_small_plot_never_reaches_the_comparison() -> None:
    """36 cohort rows carry `estate_area = 1`. Left in, the truncation guard reads "551".endswith
    ("1") as a cut number and turns two real disagreements into absence."""
    assert ft.plot_area(listing(1, source="idnes", category_main="dum", area_m2=132.0,
                                attrs={"estate_area": 1.0})) is None
    assert ft.plot_area(listing(2, source="idnes", category_main="dum", area_m2=132.0,
                                attrs={"estate_area": ft.PLOT_MIN_M2})) == ft.PLOT_MIN_M2
    feats = compute(
        StubFingerprint(1, category_main="dum", area_m2=132.0),
        StubFingerprint(2, category_main="dum", area_m2=132.0),
        listing(1, source="idnes", category_main="dum", area_m2=132.0,
                attrs={"estate_area": 1.0}),
        listing(2, source="sreality", category_main="dum", area_m2=132.0,
                attrs={"estate_area": 551.0}),
    )
    assert feats["plot_area_rel_diff"] == ft.ABSENT


def test_a_plot_that_is_the_last_three_digits_of_the_other_is_a_cut_number() -> None:
    """The same artefact between two uncensused portals: 870 of 5,870 is a truncation, 1,328 vs
    722 is a real disagreement."""
    assert ft.thousands_truncation_suspect(5870.0, 870.0)
    assert ft.thousands_truncation_suspect(3400.0, 400.0)
    assert not ft.thousands_truncation_suspect(1328.0, 722.0)
    assert not ft.thousands_truncation_suspect(12000.0, 2000.0)  # a cut leaves ONE group
    assert not ft.thousands_truncation_suspect(740.0, 740.0)
    # exactly three digits: a shorter residue is a coincidental digit tail, not a thousands cut
    assert not ft.thousands_truncation_suspect(551.0, 51.0)
    assert not ft.thousands_truncation_suspect(681.0, 81.0)
    feats = compute(
        StubFingerprint(1, category_main="dum", area_m2=200.0),
        StubFingerprint(2, category_main="dum", area_m2=200.0),
        listing(1, source="idnes", category_main="dum", area_m2=200.0,
                attrs={"estate_area": 2239.0}),
        listing(2, source="maxima", category_main="dum", area_m2=200.0,
                attrs={"estate_area": 239.0}),
    )
    assert feats["plot_area_rel_diff"] == ft.ABSENT


def test_plot_area_features_and_their_attr_evidence_rule() -> None:
    exact = compute(
        StubFingerprint(1, category_main="dum"), StubFingerprint(2, category_main="dum"),
        listing(1, source="sreality", category_main="dum", attrs={"estate_area": 740.0}),
        listing(2, source="idnes", category_main="dum", attrs={"estate_area": 745.0}),
    )
    assert exact["plot_area_rel_diff"][1] and exact["plot_area_rel_diff"][0] < ft.PLOT_EXACT_REL
    assert exact["plot_area_exact"] == (1.0, True)
    assert "ATTR" in ft.evidence_families(exact)
    apart = compute(
        StubFingerprint(3, category_main="dum"), StubFingerprint(4, category_main="dum"),
        listing(3, source="sreality", category_main="dum", attrs={"estate_area": 722.0}),
        listing(4, source="idnes", category_main="dum", attrs={"estate_area": 1328.0}),
    )
    assert apart["plot_area_exact"] == (0.0, True)
    assert apart["plot_area_rel_diff"][0] == pytest.approx(0.4563, abs=1e-4)
    assert "ATTR" not in ft.evidence_families(apart)


def test_attribute_agreement_reads_canonical_vocabulary_not_portal_spelling() -> None:
    """W4e: one fact, several spellings. `ve_vystavbe_(hruba_stavba)` (realitymix) is
    `ve_vystavbe`, `zdeny` (bazos) is `cihla`, and `jina` (ceskereality, 123 rows) is ABSENT —
    a portal saying "other" knows no more than one saying nothing."""
    la = listing(1, source="realitymix",
                 attrs={"condition": "ve_vystavbe_(hruba_stavba)", "building_type": "zdeny",
                        "furnished": "ano", "ownership": "osobni"})
    lb = listing(2, source="sreality",
                 attrs={"condition": "ve_vystavbe", "building_type": "cihla",
                        "furnished": "castecne", "ownership": "osobni"})
    feats = compute(StubFingerprint(1), StubFingerprint(2), la, lb)
    assert feats["attr_agreements"] == (4.0, True)
    assert feats["attr_contradictions"] == (0.0, True)
    assert ft.attribute_conflicts(la, lb) == []
    unknown = compute(
        StubFingerprint(3), StubFingerprint(4),
        listing(3, source="ceskereality", attrs={"building_type": "jina"}),
        listing(4, source="sreality", attrs={"building_type": "cihla"}),
    )
    assert unknown["attr_contradictions"] == ft.ABSENT
    assert unknown["attr_agreements"] == ft.ABSENT


def test_adjacent_condition_grades_are_one_token_but_the_real_grades_still_contradict() -> None:
    """`dobry` vs `velmi_dobry` is 47 of the 75 condition contradictions on judged TRUE duplicates
    — a subjective grade the portals spell either way. Collapsing it takes the positive
    contradiction rate 16.2% -> 6.1% while the negatives only fall 25.7% -> 19.3%."""
    same_grade = compute(
        StubFingerprint(1), StubFingerprint(2),
        listing(1, source="sreality", attrs={"condition": "velmi_dobry"}),
        listing(2, source="ceskereality", attrs={"condition": "dobry"}),
    )
    assert same_grade["attr_agreements"] == (1.0, True)
    real_conflict = compute(
        StubFingerprint(3), StubFingerprint(4),
        listing(3, source="sreality", attrs={"condition": "novostavba"}),
        listing(4, source="idnes", attrs={"condition": "velmi_dobry"}),
    )
    assert real_conflict["attr_contradictions"] == (1.0, True)


def test_a_false_from_a_portal_that_never_publishes_false_is_absence() -> None:
    """A portal in `FALSE_BY_OMISSION` writes `true` or nothing (idnes: 336 cellar trues, zero
    falses), so a `false` there is a parser default and may not contradict. A portal that DOES
    publish the negative keeps contradicting — a sreality `cellar=false` is the discriminator on
    34 judged non-duplicates against 2 true duplicates."""
    default = compute(
        StubFingerprint(1), StubFingerprint(2),
        listing(1, source="idnes", attrs={"cellar": False}),
        listing(2, source="sreality", attrs={"cellar": True}),
    )
    assert default["attr_contradictions"] == ft.ABSENT
    published = compute(
        StubFingerprint(3), StubFingerprint(4),
        listing(3, source="sreality", attrs={"cellar": False}),
        listing(4, source="idnes", attrs={"cellar": True}),
    )
    assert published["attr_contradictions"] == (1.0, True)
    assert [key for key, _, _ in ft.attribute_conflicts(
        listing(3, source="sreality", attrs={"cellar": False}),
        listing(4, source="idnes", attrs={"cellar": True}),
    )] == ["cellar"]


# --- v4: room-tag-paired image evidence (W5) --------------------------------------------------


def tagged(listing_id: int, image_id: int, tag: str, seed: float, phash: int, **kwargs: Any) -> ds.Image:
    return image(listing_id, image_id, tags=[(tag, 0.9)], clip=clip_b64(seed), phash=phash, **kwargs)


def test_only_same_room_frames_are_ever_compared() -> None:
    """The whole point of v4: `clip_max_cos` pairs whatever two frames look most alike, which in a
    development is the facade on both sides. A kitchen may only meet a kitchen."""
    a = [tagged(1, 10, "kitchen", 0.1, 0), tagged(1, 11, "bathroom", 5.0, 1 << 40)]
    b = [tagged(2, 20, "kitchen", 0.1, 0), tagged(2, 21, "bathroom", 5.0, 1 << 40)]
    rows = ft._tag_room_stats(
        {"kitchen": [ft.TagFrame(0, a[0].clip_vector(), a[0].clip_norm())],
         "bathroom": [ft.TagFrame(1 << 40, a[1].clip_vector(), a[1].clip_norm())]},
        {"kitchen": [ft.TagFrame(0, b[0].clip_vector(), b[0].clip_norm())],
         "bathroom": [ft.TagFrame(1 << 40, b[1].clip_vector(), b[1].clip_norm())]},
    )
    assert [tag for tag, _, _ in rows] == ["bathroom", "kitchen"]
    assert all(hamming == 0 for _, hamming, _ in rows)
    # a kitchen never meets the OTHER side's bathroom, however alike they look
    crossed = ft._tag_room_stats(
        {"kitchen": [ft.TagFrame(0, a[0].clip_vector(), a[0].clip_norm())]},
        {"bathroom": [ft.TagFrame(0, b[1].clip_vector(), b[1].clip_norm())]},
    )
    assert crossed == []


def test_a_side_with_no_tagged_frame_leaves_every_v4_slot_absent() -> None:
    """E12: missing data is its own signal, never a mismatch."""
    feats = compute(
        StubFingerprint(1), StubFingerprint(2), listing(1), listing(2),
        [image(1, 10, phash=0)], [],
    )
    for name in ft.TAG_FEATURE_NAMES:
        assert feats[name] == ft.ABSENT, name


def test_the_weakest_private_room_is_the_developer_unit_discriminator() -> None:
    """Two units of one project share a facade, a hallway and a fit-out style and differ in a
    bedroom, so `tag_room_clip_min` reads the rooms a unit OWNS — `hallway` is the tagger's
    catch-all (24% of the cohort's top tags) and is deliberately not one of them."""
    assert ft.PRIVATE_ROOM_TAGS == frozenset(
        {"bathroom", "bedroom", "kitchen", "living_room", "toilet"}
    )
    assert "hallway" not in ft.PRIVATE_ROOM_TAGS
    same = [tagged(1, 10, "kitchen", 0.1, 0), tagged(1, 11, "bedroom", 0.2, 3),
            tagged(1, 12, "hallway", 9.0, 7)]
    other = [tagged(2, 20, "kitchen", 0.1, 0), tagged(2, 21, "bedroom", 4.0, (1 << 60) - 1),
             tagged(2, 22, "hallway", 9.0, 7)]
    feats = compute(StubFingerprint(1), StubFingerprint(2), listing(1), listing(2), same, other)
    assert feats["tag_rooms_both"] == (3.0, True)
    assert feats["tag_rooms_private"] == (2.0, True)
    # the matching kitchen must not hide the contradicting bedroom
    assert feats["tag_room_clip_min"][1] and feats["tag_room_clip_min"][0] < 0.99
    assert feats["tag_clip_mean"][0] > feats["tag_room_clip_min"][0]


def test_one_odd_frame_on_a_true_duplicate_does_not_read_as_a_contradiction() -> None:
    """`tag_room_clip_min2` is the second-weakest room: the operator's own duplicate (94020 x
    140903) re-shot its kitchen and its garden, so a per-room requirement on the MINIMUM would
    have refused a pair with seven rooms of literally the same photographs."""
    a = [tagged(1, 10, "kitchen", 3.0, 0), tagged(1, 11, "bedroom", 0.2, 0),
         tagged(1, 12, "bathroom", 0.3, 0)]
    b = [tagged(2, 20, "kitchen", 9.0, (1 << 64) - 1), tagged(2, 21, "bedroom", 0.2, 0),
         tagged(2, 22, "bathroom", 0.3, 0)]
    feats = compute(StubFingerprint(1), StubFingerprint(2), listing(1), listing(2), a, b)
    assert feats["tag_room_clip_min"][0] < feats["tag_room_clip_min2"][0]
    assert feats["tag_room_clip_min2"][0] > 0.99
    assert feats["tag_tight_matches"] == (2.0, True)


def test_a_lookalike_room_is_not_a_shared_photograph() -> None:
    """The developer-unit fingerprint: every shared room LOOKS the same and no room IS the same
    file. 1.00 on eight of the operator's nine false edges, 0.11 on the true one."""
    a = [tagged(1, 10, "kitchen", 0.1, 0), tagged(1, 11, "bedroom", 0.2, 0)]
    b = [tagged(2, 20, "kitchen", 0.1, (1 << 64) - 1), tagged(2, 21, "bedroom", 0.2, (1 << 64) - 1)]
    feats = compute(StubFingerprint(1), StubFingerprint(2), listing(1), listing(2), a, b)
    assert feats["tag_lookalike_share"] == (1.0, True)
    assert feats["tag_tight_matches"] == (0.0, True)
    assert feats["tag_dhash_min"] == (64.0, True)


def test_the_floor_plan_slots_read_the_unit_drawing_not_the_site_plan() -> None:
    """`site_plan` and `property_document` are also `plan` family and are BUILDING material."""
    assert ft.FLOOR_PLAN_TAG == "floor_plan"
    a = [tagged(1, 10, "floor_plan", 0.1, 0), tagged(1, 11, "site_plan", 0.4, 0)]
    b = [tagged(2, 20, "floor_plan", 0.9, (1 << 64) - 1), tagged(2, 21, "site_plan", 0.4, 0)]
    feats = compute(StubFingerprint(1), StubFingerprint(2), listing(1), listing(2), a, b)
    assert feats["floorplan_conflict"] == (1.0, True)
    assert feats["floorplan_tight_match"] == (0.0, True)
    no_plan = compute(
        StubFingerprint(1), StubFingerprint(2), listing(1), listing(2),
        [tagged(1, 10, "site_plan", 0.4, 0)], [tagged(2, 20, "site_plan", 0.4, 0)],
    )
    assert no_plan["floorplan_conflict"] == ft.ABSENT
    assert no_plan["floorplan_tight_match"] == ft.ABSENT


def test_catalogue_frames_are_subtracted_before_a_room_is_paired() -> None:
    """E9 first, exactly as `phash_gallery` does it: a catalogue kitchen is stock, not this
    unit's kitchen, and pairing it room-to-room would make stock look like evidence."""
    a = [tagged(1, 10, "kitchen", 0.1, 0, pop=SETTINGS.catalog_df)]
    b = [tagged(2, 20, "kitchen", 0.1, 0, pop=SETTINGS.catalog_df)]
    feats = compute(StubFingerprint(1), StubFingerprint(2), listing(1), listing(2), a, b)
    for name in ft.TAG_FEATURE_NAMES:
        assert feats[name] == ft.ABSENT, name


def test_at_most_three_frames_of_one_room_are_paired() -> None:
    """A fourth bathroom carries no evidence the second one did not, and the cross-product is
    quadratic — the same argument as `judge.ROOM_TAG_CAP`, one slot wider because a pairing is a
    popcount, not a paid vision token."""
    assert ft.TAG_FRAME_CAP == 3
    ctx = context({1: StubFingerprint(1), 2: StubFingerprint(2)})
    gallery = ctx.tag_gallery(1, [tagged(1, 10 + n, "kitchen", 0.1, n) for n in range(6)], SETTINGS)
    assert len(gallery["kitchen"]) == ft.TAG_FRAME_CAP


def test_a_one_floor_gap_is_a_fact_within_a_portal_and_noise_across_two() -> None:
    """Measured on the 2,703 labelled pairs: same portal + floor_diff == 1 fires on 2.0% of
    positives against 15.5% of negatives; across portals the same gap fires on 38.7% of positives
    against 30.1% of negatives — the portals disagreeing about prizemi. A linear model on
    `floor_diff` and `same_source` cannot form the product for itself."""
    same_portal = compute(
        StubFingerprint(1, source="sreality", floor=2),
        StubFingerprint(2, source="sreality", floor=3), listing(1), listing(2),
    )
    assert same_portal["floor_stated_conflict"] == (1.0, True)
    cross_portal = compute(
        StubFingerprint(1, source="sreality", floor=2),
        StubFingerprint(2, source="bezrealitky", floor=3), listing(1), listing(2),
    )
    assert cross_portal["floor_stated_conflict"] == (0.0, True)
    unknown_floor = compute(
        StubFingerprint(1, source="sreality"), StubFingerprint(2, source="sreality"),
        listing(1), listing(2),
    )
    assert unknown_floor["floor_stated_conflict"] == ft.ABSENT
