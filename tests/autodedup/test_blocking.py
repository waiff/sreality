"""W2 Task A — the six probes, explosion control, priority fill and the guard at pairing."""

from __future__ import annotations

from typing import Any

from autodedup.blocking import PROBE_PRIORITY, BlockIndex, build_index, generate_pairs
from autodedup.dataset import Image, Listing, Location
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.guards import pair_veto
from autodedup.settings import Settings

SETTINGS = Settings()
LONG_TEXT = ("Prodej bytu 3+kk v Turnove s vyhledem do zahrady, plocha 68 m2, "
             "4. patro, cena 5 990 000 Kc. ") * 4
OTHER_TEXT = ("Pronajem kancelare v centru Prahy, otevreny prostor, recepce, "
              "parkovaci stani v podzemi, klimatizace. ") * 4


def fp(listing_id: int, settings: Settings = SETTINGS, **kwargs: Any) -> Fingerprint:
    images = kwargs.pop("images", [])
    location = Location(**kwargs.pop("location", {}))
    listing = Listing(id=listing_id, block=kwargs.pop("block", "turnov"),
                      location=location, **kwargs)
    return build_fingerprint(listing, images, settings)


def flat(listing_id: int, **kwargs: Any) -> Fingerprint:
    kwargs.setdefault("category_main", "byt")
    kwargs.setdefault("category_type", "prodej")
    kwargs.setdefault("disposition", "3+kk")
    kwargs.setdefault("area_m2", 68.0)
    return fp(listing_id, **kwargs)


def probes_of(index: BlockIndex, fingerprint: Fingerprint) -> set[str]:
    return {probe for probe, _key in index.index_keys(fingerprint)}


def index_of(*fingerprints: Fingerprint, settings: Settings = SETTINGS) -> BlockIndex:
    return build_index(fingerprints, settings)


def test_probe_priority_is_the_documented_order() -> None:
    assert PROBE_PRIORITY == (
        "addr", "phash", "text", "broker", "attr_dispo", "attr_area", "foreign")


def test_the_address_probe_needs_all_three_components() -> None:
    complete = flat(1, location={"obec_kod": 1, "street_key": "vinohradska",
                                 "house_number_cp": "12"})
    no_number = flat(2, location={"obec_kod": 1, "street_key": "vinohradska"})
    no_street = flat(3, location={"obec_kod": 1, "house_number_cp": "12"})
    index = index_of(complete, no_number, no_street)
    assert "addr" in probes_of(index, complete)
    assert "addr" not in probes_of(index, no_number)
    assert "addr" not in probes_of(index, no_street)


def test_the_attribute_probes_need_a_block_key_and_their_own_attribute() -> None:
    located = flat(1, location={"obec_kod": 1})
    homeless = flat(2)
    no_attrs = fp(3, category_main="byt", location={"obec_kod": 1})
    index = index_of(located, homeless, no_attrs)
    assert {"attr_dispo", "attr_area"} <= probes_of(index, located)
    assert not {"attr_dispo", "attr_area"} & probes_of(index, homeless)
    assert not {"attr_dispo", "attr_area"} & probes_of(index, no_attrs)


def test_the_broker_probe_needs_a_broker_key_and_keys_on_the_price_decile() -> None:
    with_broker = flat(1, broker_key="bk1", price=5_000_000.0)
    without = flat(2, price=5_000_000.0)
    index = index_of(with_broker, without)
    assert "broker" in probes_of(index, with_broker)
    assert "broker" not in probes_of(index, without)
    key = [k for probe, k in index.index_keys(with_broker) if probe == "broker"][0]
    assert key[0] == "bk1"
    assert key[3] == index.price_decile(with_broker)


def test_the_price_decile_is_absent_without_a_price() -> None:
    priceless = flat(1, broker_key="bk1")
    index = index_of(priceless)
    assert index.price_decile(priceless) is None


def test_the_photo_and_text_probes_are_location_free() -> None:
    images = [Image(listing_id=1, image_id=1, seq=0, phash=987654321, pop=1)]
    left = fp(1, images=images, description=LONG_TEXT)
    right = fp(2, images=[Image(listing_id=2, image_id=2, seq=0, phash=987654321, pop=1)],
               description=LONG_TEXT)
    assert left.block_key == right.block_key == ""
    pairs, stats = generate_pairs({1: left, 2: right}, SETTINGS)
    assert pairs[(1, 2)] >= {"phash", "text"}
    assert stats["pairs_per_probe"]["phash"] == 1


def test_the_text_probe_stays_silent_below_the_character_floor() -> None:
    short = fp(1, description="Prodej bytu")
    index = index_of(short)
    assert "text" not in probes_of(index, short)


def test_unrelated_text_does_not_share_a_simhash_band() -> None:
    left = fp(1, description=LONG_TEXT)
    right = fp(2, description=OTHER_TEXT)
    pairs, _ = generate_pairs({1: left, 2: right}, SETTINGS)
    assert pairs == {}


def test_a_catalog_only_gallery_emits_no_photo_probe() -> None:
    images = [Image(listing_id=1, image_id=1, seq=0, phash=42, pop=SETTINGS.catalog_df)]
    catalog_only = fp(1, images=images)
    index = index_of(catalog_only)
    assert "phash" not in probes_of(index, catalog_only)


def test_the_foreign_probe_fires_only_abroad() -> None:
    abroad = flat(1, location={"country_status": "foreign"})
    home = flat(2, location={"country_status": "domestic"})
    index = index_of(abroad, home)
    assert "foreign" in probes_of(index, abroad)
    assert "foreign" not in probes_of(index, home)


def test_the_area_probe_reaches_one_band_out_but_not_two() -> None:
    base = flat(1, area_m2=68.0, location={"obec_kod": 1}, disposition=None)
    near = flat(2, area_m2=70.0, location={"obec_kod": 1}, disposition=None)
    far = flat(3, area_m2=129.0, location={"obec_kod": 1}, disposition=None)
    assert abs(base.area_band - near.area_band) == 1
    assert abs(base.area_band - far.area_band) >= 2
    index = build_index([base, near, far], SETTINGS)
    assert set(index.candidates(base)) == {2}
    pairs, _ = generate_pairs({1: base, 2: near, 3: far}, SETTINGS)
    assert "attr_area" in pairs[(1, 2)]
    assert (1, 3) not in pairs


def test_an_oversized_key_explodes_and_stops_expanding() -> None:
    settings = Settings(max_block_size=2)
    fps = {n: flat(n, settings=settings, location={"obec_kod": 1}) for n in range(1, 5)}
    pairs, stats = generate_pairs(fps, settings)
    assert pairs == {}
    assert stats["exploded_keys_per_probe"]["attr_dispo"] == 1
    assert stats["exploded_keys_per_probe"]["attr_area"] == 1


def test_a_key_at_the_limit_is_not_exploded() -> None:
    settings = Settings(max_block_size=4)
    fps = {n: flat(n, settings=settings, location={"obec_kod": 1}) for n in range(1, 5)}
    pairs, stats = generate_pairs(fps, settings)
    assert stats["exploded_keys_per_probe"]["attr_dispo"] == 0
    assert len(pairs) == 6


def test_the_cap_fills_in_priority_order_so_the_address_probe_survives() -> None:
    settings = Settings(max_candidates_per_listing=1)
    address = {"obec_kod": 1, "street_key": "vinohradska", "house_number_cp": "12"}
    subject = flat(1, settings=settings, location=address)
    neighbour = flat(2, settings=settings, location=address)
    attr_only = flat(3, settings=settings, location={"obec_kod": 1})
    index = build_index([subject, neighbour, attr_only], settings)
    found = index.candidates(subject)
    assert set(found) == {2}
    assert "addr" in found[2]


def test_a_guarded_pair_is_never_emitted_and_is_counted_by_its_rule() -> None:
    images_a = [Image(listing_id=1, image_id=1, seq=0, phash=555, pop=1)]
    images_b = [Image(listing_id=2, image_id=2, seq=0, phash=555, pop=1)]
    left = fp(1, category_main="byt", images=images_a)
    right = fp(2, category_main="dum", images=images_b)
    pairs, stats = generate_pairs({1: left, 2: right}, SETTINGS)
    assert pairs == {}
    assert stats["guarded_pairs"] == {"category_main": 1}
    assert stats["n_guarded_pairs"] == 1


def test_a_guarded_pair_is_counted_once_however_many_probes_find_it() -> None:
    images_a = [Image(listing_id=1, image_id=1, seq=0, phash=777, pop=1)]
    images_b = [Image(listing_id=2, image_id=2, seq=0, phash=777, pop=1)]
    left = fp(1, category_type="prodej", description=LONG_TEXT, images=images_a)
    right = fp(2, category_type="pronajem", description=LONG_TEXT, images=images_b)
    _, stats = generate_pairs({1: left, 2: right}, SETTINGS)
    assert stats["guarded_pairs"] == {"category_type": 1}


def test_the_cap_is_never_spent_on_a_candidate_the_rule_floor_will_veto() -> None:
    # The true duplicate shares a photo band with the subject; a crowd of same-band listings
    # that all veto on area would otherwise fill the cap before it is reached (E2-E5 > E17).
    settings = Settings(max_candidates_per_listing=2)
    gallery = [Image(listing_id=1, image_id=1, seq=0, phash=4242, pop=1)]
    subject = flat(1, settings=settings, images=gallery, area_m2=68.0)
    twin = flat(900, settings=settings, area_m2=68.0,
                images=[Image(listing_id=900, image_id=900, seq=0, phash=4242, pop=1)])
    crowd = {
        n: flat(n, settings=settings, area_m2=300.0,
                images=[Image(listing_id=n, image_id=n, seq=0, phash=4242, pop=1)])
        for n in range(10, 30)
    }
    fps = {1: subject, 900: twin, **crowd}
    index = build_index(fps.values(), settings)
    assert 900 in index.candidates(subject)
    pairs, stats = generate_pairs(fps, settings)
    assert (1, 900) in pairs
    assert stats["guarded_pairs"]["area"] > 0


def test_a_guarded_pair_is_counted_once_across_both_directions() -> None:
    left = flat(1, area_m2=68.0, location={"obec_kod": 1}, description=LONG_TEXT)
    right = flat(2, area_m2=200.0, location={"obec_kod": 1}, description=LONG_TEXT)
    _, stats = generate_pairs({1: left, 2: right}, SETTINGS)
    assert stats["guarded_pairs"] == {"area": 1}


def test_stats_have_the_shape_the_run_summary_publishes() -> None:
    fps = {n: flat(n, location={"obec_kod": 1}) for n in range(1, 4)}
    fps[4] = fp(4)
    pairs, stats = generate_pairs(fps, SETTINGS)
    assert stats["n_listings"] == 4
    assert stats["n_pairs"] == len(pairs) == 3
    assert set(stats["candidates_per_listing"]) == {"p50", "p90", "p99", "mean", "max"}
    assert stats["listings_with_zero_candidates"] == 1
    assert stats["listings_at_cap"] == 0
    assert set(stats["pairs_per_probe"]) == set(PROBE_PRIORITY)
    assert set(stats["exploded_keys_per_probe"]) == set(PROBE_PRIORITY)
    assert set(stats["keys_per_probe"]) == set(PROBE_PRIORITY)
    assert set(stats["largest_bucket_per_probe"]) == set(PROBE_PRIORITY)
    assert stats["largest_bucket_per_probe"]["attr_dispo"] == 3
    assert stats["listings_with_null_cat_group"] == 1
    assert stats["zero_candidate_listings_by_null_attr"] == 1
    assert stats["guarded_pairs"] == {}


def test_pairs_are_keyed_low_then_high_with_the_union_of_probes() -> None:
    address = {"obec_kod": 1, "street_key": "vinohradska", "house_number_cp": "12"}
    fps = {9: flat(9, location=address), 4: flat(4, location=address)}
    pairs, _ = generate_pairs(fps, SETTINGS)
    assert list(pairs) == [(4, 9)]
    assert pairs[(4, 9)] == {"addr", "attr_dispo", "attr_area"}


def test_the_index_refuses_to_answer_before_finalize() -> None:
    index = BlockIndex(SETTINGS)
    index.add(flat(1))
    try:
        index.candidates(flat(1))
    except RuntimeError as error:
        assert "finalize" in str(error)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("candidates() answered before finalize()")


def test_an_unknown_category_never_blocks_with_a_known_one_and_the_stats_say_so() -> None:
    # E12 makes the pair rule-compatible, but K1/K3/K6 key on the category verbatim, so the
    # attr lane cannot reach it. Measured here so W3 knows the recall cost before labelling.
    known = flat(1, location={"obec_kod": 5})
    unknown = fp(2, disposition="3+kk", area_m2=68.0, location={"obec_kod": 5})
    assert pair_veto(known, unknown) is None
    pairs, stats = generate_pairs({1: known, 2: unknown}, SETTINGS)
    assert pairs == {}
    assert stats["listings_with_null_cat_group"] == 1
    assert stats["zero_candidate_listings_by_null_attr"] == 1


def test_the_index_refuses_to_build_keys_before_finalize() -> None:
    index = BlockIndex(SETTINGS)
    subject = flat(1, price=5_000_000.0, broker_key="bk1")
    index.add(subject)
    for call in (index.index_keys, index.price_decile):
        try:
            call(subject)
        except RuntimeError as error:
            assert "finalize" in str(error)
        else:  # pragma: no cover - both guards must fire
            raise AssertionError(f"{call.__name__} answered before finalize()")


def test_an_unseen_price_cohort_has_no_decile_rather_than_the_cheapest_one() -> None:
    index = index_of(flat(1, price=5_000_000.0, broker_key="bk1"))
    stranger = flat(2, category_main="pozemek", price=900_000.0, broker_key="bk1")
    assert index.price_decile(stranger) is None


def test_exploded_probes_report_membership_for_the_negative_feature() -> None:
    settings = Settings(max_block_size=2)
    fps = {n: flat(n, settings=settings, location={"obec_kod": 1}) for n in range(1, 5)}
    index = build_index(fps.values(), settings)
    assert index.exploded_probes(fps[1]) == {"attr_dispo", "attr_area"}
