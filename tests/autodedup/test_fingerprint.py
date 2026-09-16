"""W2 Task A — the per-listing row every probe and feature reads (E9/E10/E14)."""

from __future__ import annotations

import math
from typing import Any

from autodedup.dataset import Dataset, Image, Listing, Location, Meta
from autodedup.fingerprint import (
    area_band_of,
    block_key_of,
    build_all,
    build_fingerprint,
    cat_group_of,
    dominant_family,
    pin_key_of,
    tag_family,
)
from autodedup.normalize import simhash_bands
from autodedup.settings import Settings

SETTINGS = Settings()
LONG_TEXT = ("Prodej bytu 3+kk v Turnove s vyhledem do zahrady, plocha 68 m2, "
             "4. patro, cena 5 990 000 Kc. ") * 4


def listing(listing_id: int = 1, **kwargs: Any) -> Listing:
    location = Location(**kwargs.pop("location", {}))
    return Listing(id=listing_id, block=kwargs.pop("block", "turnov"),
                   location=location, **kwargs)


def image(image_id: int, **kwargs: Any) -> Image:
    return Image(listing_id=kwargs.pop("listing_id", 1), image_id=image_id, **kwargs)


def test_block_key_prefixes_the_grain_so_codes_cannot_collide() -> None:
    assert block_key_of(577022, 12345) == "c12345"
    assert block_key_of(577022, None) == "o577022"
    assert block_key_of(None, None) == ""


def test_cat_group_folds_the_one_sanctioned_cross_type() -> None:
    assert cat_group_of("dum") == cat_group_of("komercni") == "dum_komercni"
    assert cat_group_of("byt") == "byt"
    assert cat_group_of(None) is None


def test_area_band_is_the_log_band_and_neighbours_cover_the_tolerance() -> None:
    width = SETTINGS.band_width()
    assert width == -math.log(1.0 - SETTINGS.area_band_tol)
    assert area_band_of(100.0, SETTINGS) == math.floor(math.log(100.0) / width)
    assert area_band_of(None, SETTINGS) is None
    assert area_band_of(0.0, SETTINGS) is None
    for area in (18.0, 42.0, 68.0, 145.0, 900.0):
        near = area * (1.0 + SETTINGS.area_band_tol - 0.001)
        gap = abs(area_band_of(area, SETTINGS) - area_band_of(near, SETTINGS))
        assert gap <= 1


def test_pin_key_rounds_to_six_decimals() -> None:
    assert pin_key_of(50.5874321987, 15.1587654321) == "50.587432,15.158765"
    assert pin_key_of(None, 15.0) is None


def test_tag_family_resolves_logical_tags_fine_anchors_and_the_unknown() -> None:
    assert tag_family("kitchen") == "interior"
    assert tag_family("exterior_facade") == "exterior"
    assert tag_family("floor_plan") == "plan"
    assert tag_family("cadastral_map") == "plan"  # fine anchor, collapsed via clip_taxonomy
    assert tag_family("staircase_interior") == "common"
    assert tag_family("nonsense") == "other"
    assert tag_family(None) == "other"


def test_dominant_family_is_the_best_scoring_family() -> None:
    assert dominant_family(image(1, tags=[("kitchen", 0.4), ("floor_plan", 0.9)])) == "plan"
    assert dominant_family(image(1, tags=[])) == "other"


def test_fingerprint_carries_the_attribute_and_location_derivations() -> None:
    fp = build_fingerprint(
        listing(
            7,
            source="sreality",
            category_main="byt",
            category_type="prodej",
            disposition="3 + KK",
            area_m2=68.0,
            floor=4,
            total_floors=6,
            price=5990000.0,
            description=LONG_TEXT,
            price_history=[("2026-01-01", 6200000.0), ("2026-02-01", None),
                           ("2026-03-01", 5990000.0)],
            location={
                "obec_kod": 577022, "cast_obce_kod": 12345, "street_key": "vinohradska",
                "house_number": "12/3", "house_number_cp": "12", "house_number_co": "3",
                "psc": "51101", "ruian_adm_kod": 999, "lat": 50.5874321987,
                "lon": 15.1587654321, "granularity_rank": 100, "is_address_grain": True,
                "country_status": "domestic",
            },
        ),
        [],
        SETTINGS,
    )
    assert fp.listing_id == 7
    assert fp.block_key == "c12345"
    assert fp.cat_group == "byt"
    assert fp.disposition == "3+kk"
    assert fp.area_band == area_band_of(68.0, SETTINGS)
    assert fp.house_number == "cp:12"  # E15 keys on čp alone, never the joined "12/3"
    assert fp.pin_key == "50.587432,15.158765"
    assert fp.price_events == [("2026-01-01", 6200000.0), ("2026-03-01", 5990000.0)]
    assert ("m2", 68.0) in fp.numerals and ("floor", 4.0) in fp.numerals
    assert fp.granularity_rank == 100 and fp.is_address_grain is True


def test_the_house_number_key_carries_which_component_supplied_it() -> None:
    joined = build_fingerprint(listing(1, location={"house_number": "12/3"}), [], SETTINGS)
    assert joined.house_number == "cp:12"  # the čp half of a joined form, never "12/3"

    orientation_only = build_fingerprint(
        listing(2, location={"house_number": "5", "house_number_co": "5"}), [], SETTINGS)
    assert orientation_only.house_number == "co:5"

    descriptive = build_fingerprint(
        listing(3, location={"house_number": "5", "house_number_cp": "5"}), [], SETTINGS)
    assert descriptive.house_number == "cp:5"
    # čo 5 and čp 5 are different buildings: the K2 key must not collapse them.
    assert orientation_only.house_number != descriptive.house_number


def test_a_bare_house_number_of_unknown_provenance_keeps_k2_silent() -> None:
    bare = build_fingerprint(listing(1, location={"house_number": "5"}), [], SETTINGS)
    assert bare.house_number is None


def test_text_shorter_than_the_floor_carries_no_sketch() -> None:
    short = build_fingerprint(listing(1, description="Prodej bytu"), [], SETTINGS)
    assert short.has_text is False
    assert short.desc_simhash is None
    assert short.desc_bands == []
    assert short.desc_tokens == ["prodej", "bytu"]

    long = build_fingerprint(listing(2, description=LONG_TEXT), [], SETTINGS)
    assert long.has_text is True
    assert long.desc_simhash is not None
    assert len(long.desc_bands) == SETTINGS.simhash_bands
    assert long.desc_bands == simhash_bands(long.desc_simhash, SETTINGS.simhash_bands,
                                            SETTINGS.band_bits)
    assert long.desc_shingles


def test_catalog_images_are_subtracted_before_any_image_evidence() -> None:
    images = [
        image(10, seq=0, phash=111, pop=1),
        image(11, seq=1, phash=222, pop=2),
        image(12, seq=2, phash=333, pop=SETTINGS.catalog_df),
        image(13, seq=3, phash=444, pop=SETTINGS.catalog_df + 5),
        image(14, seq=4, phash=None, pop=None),
    ]
    fp = build_fingerprint(listing(1), images, SETTINGS)
    assert fp.image_hashes == {111, 222}
    assert fp.all_hashes == {111, 222, 333, 444}
    assert fp.catalog_ratio == 0.5
    assert fp.n_images == 5
    assert {phash for _, _, phash in fp.anchor_bands} == {111, 222}


def test_catalog_ratio_is_absent_without_a_hashed_image() -> None:
    assert build_fingerprint(listing(1), [image(1, phash=None)], SETTINGS).catalog_ratio is None


def test_catalog_ratio_is_absent_when_the_population_probe_never_ran() -> None:
    unmeasured = [image(10, seq=0, phash=111, pop=0), image(11, seq=1, phash=222, pop=None)]
    fp = build_fingerprint(listing(1), unmeasured, SETTINGS)
    # pop 0/None is "not measured", not "not stock": an affirmative 0.0 would disarm K-C.
    assert fp.catalog_ratio is None
    assert fp.image_hashes == {111, 222}

    measured = [image(10, seq=0, phash=111, pop=1), image(11, seq=1, phash=222, pop=1)]
    assert build_fingerprint(listing(1), measured, SETTINGS).catalog_ratio == 0.0


def test_anchors_are_the_top_interior_images_then_sequence_order() -> None:
    images = [
        image(10, seq=0, phash=1000, pop=1, tags=[("exterior_facade", 0.95)]),
        image(11, seq=1, phash=1001, pop=1, tags=[("kitchen", 0.90)]),
        image(12, seq=2, phash=1002, pop=1, tags=[("bedroom", 0.80)]),
        image(13, seq=3, phash=1003, pop=1, tags=[("living_room", 0.80)]),
        image(14, seq=4, phash=1004, pop=1, tags=[("floor_plan", 0.99)]),
    ]
    fp = build_fingerprint(listing(1), images, SETTINGS)
    anchors = []
    for _, _, phash in fp.anchor_bands:
        if phash not in anchors:
            anchors.append(phash)
    assert anchors == [1001, 1002, 1003]
    assert len(fp.anchor_bands) == SETTINGS.anchor_images * SETTINGS.simhash_bands
    assert fp.anchor_bands[:SETTINGS.simhash_bands] == [
        (band_no, band_val, 1001)
        for band_no, band_val in simhash_bands(1001, SETTINGS.simhash_bands, SETTINGS.band_bits)
    ]


def test_anchor_count_is_bounded_by_the_setting() -> None:
    images = [image(20 + n, seq=n, phash=2000 + n, pop=1) for n in range(6)]
    settings = Settings(anchor_images=2)
    fp = build_fingerprint(listing(1), images, settings)
    assert len({phash for _, _, phash in fp.anchor_bands}) == 2


def test_image_ids_are_split_by_tag_family() -> None:
    images = [
        image(10, seq=0, tags=[("kitchen", 0.9)]),
        image(11, seq=1, tags=[("exterior_facade", 0.9)]),
        image(12, seq=2, tags=[("site_plan", 0.9)]),
        image(13, seq=3, tags=[("staircase_interior", 0.9)]),
        image(14, seq=4, tags=[]),
    ]
    fp = build_fingerprint(listing(1), images, SETTINGS)
    assert fp.interior_image_ids == [10]
    assert fp.exterior_image_ids == [11]
    assert fp.plan_image_ids == [12]
    assert fp.image_ids == [10, 11, 12, 13, 14]


def test_build_all_covers_the_cohort_in_listing_id_order() -> None:
    dataset = Dataset(
        meta=Meta(),
        listings={2: listing(2), 1: listing(1)},
        images_by_listing={1: [image(5, listing_id=1, phash=7, pop=1)]},
    )
    fps = build_all(dataset, SETTINGS)
    assert list(fps) == [1, 2]
    assert fps[1].n_images == 1
    assert fps[2].n_images == 0
