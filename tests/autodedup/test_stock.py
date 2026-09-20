"""W10 — E83: a frame is stock only when its carriers are several PARTIES."""

from __future__ import annotations

from typing import Any

import pytest

from autodedup.dataset import Dataset, Image, Listing, Location, Meta
from autodedup.settings import Settings
from autodedup.stock import PLAIN, CarrierPolicy, StockIndex

EXCLUSIVE = dict(
    catalog_carrier_aware=True,
    catalog_min_broker_carriers=2,
    catalog_min_source_carriers=2,
    catalog_min_block_carriers=2,
)


def listing(listing_id: int, **kwargs: Any) -> Listing:
    location = Location(**kwargs.pop("location", {"obec_kod": 500, "street_key": "hlavni",
                                                  "house_number": "7"}))
    return Listing(id=listing_id, block=kwargs.pop("block", "turnov"), location=location,
                   **kwargs)


def dataset(listings: list[Listing], images: dict[int, list[Image]]) -> Dataset:
    return Dataset(meta=Meta(), listings={lst.id: lst for lst in listings},
                   images_by_listing=images)


def chain(n: int = 9, *, broker: str = "b1", source: str = "idnes",
          street: str = "hlavni", pop: int | None = None) -> Dataset:
    """One broker re-posting one advert n times on one portal: pop == n, one carrier party."""
    population = n if pop is None else pop
    listings, images = [], {}
    for index in range(n):
        listing_id = index + 1
        listings.append(listing(listing_id, source=source, broker_key=broker,
                                location={"obec_kod": 500, "street_key": street,
                                          "house_number": "7"}))
        images[listing_id] = [Image(listing_id=listing_id, image_id=100 + index,
                                    phash=0xABCD, pop=population)]
    return dataset(listings, images)


def index_of(ds: Dataset, **kwargs: Any) -> StockIndex:
    return StockIndex.build(ds.listings, ds.images, Settings(**kwargs))


def test_plain_e9_is_the_default_and_a_populous_frame_is_stock() -> None:
    ds = chain(9)
    image = ds.images(1)[0]
    assert PLAIN.is_stock(image, 8) is True
    assert index_of(ds).is_stock(image, 8) is True


def test_one_broker_one_portal_one_block_is_that_brokers_own_material() -> None:
    ds = chain(9)
    idx = index_of(ds, **EXCLUSIVE)
    assert idx.is_stock(ds.images(1)[0], 8) is False
    assert idx.census[0xABCD].carriers == 9
    assert idx.census[0xABCD].brokers == 1


def test_a_second_broker_makes_the_frame_stock_again() -> None:
    ds = chain(9)
    ds.listings[9].broker_key = "b2"
    assert index_of(ds, **EXCLUSIVE).is_stock(ds.images(1)[0], 8) is True


def test_a_second_portal_makes_the_frame_stock_again() -> None:
    ds = chain(9)
    ds.listings[9].source = "sreality"
    assert index_of(ds, **EXCLUSIVE).is_stock(ds.images(1)[0], 8) is True


def test_a_second_address_block_makes_the_frame_stock_again() -> None:
    ds = chain(9)
    ds.listings[9].location.street_key = "vedlejsi"
    assert index_of(ds, **EXCLUSIVE).is_stock(ds.images(1)[0], 8) is True


def test_an_unobserved_population_keeps_the_frame_subtracted() -> None:
    """The cohort sees 9 of 50 carriers, so the other 41 parties are unknown: E9 stands."""
    ds = chain(9, pop=50)
    idx = index_of(ds, **EXCLUSIVE)
    assert idx.is_stock(ds.images(1)[0], 8) is True
    assert idx.census[0xABCD].coverage() == pytest.approx(9 / 50)


def test_a_lower_coverage_bar_admits_a_partly_observed_frame() -> None:
    ds = chain(9, pop=10)
    assert index_of(ds, **EXCLUSIVE).is_stock(ds.images(1)[0], 8) is True
    idx = index_of(ds, **EXCLUSIVE, catalog_carrier_coverage_min=0.8)
    assert idx.is_stock(ds.images(1)[0], 8) is False


def test_an_unknown_broker_is_its_own_party_and_never_collapses_two_into_one() -> None:
    ds = chain(9)
    for listing_id in (8, 9):
        ds.listings[listing_id].broker_key = None
    idx = index_of(ds, catalog_carrier_aware=True, catalog_min_broker_carriers=2)
    assert idx.census[0xABCD].brokers == 3
    assert idx.is_stock(ds.images(1)[0], 8) is True


def test_combine_all_needs_every_limb_before_a_frame_counts_as_stock() -> None:
    ds = chain(9)
    ds.listings[9].broker_key = "b2"  # two brokers, still one portal and one block
    assert index_of(ds, **EXCLUSIVE).is_stock(ds.images(1)[0], 8) is True
    idx = index_of(ds, **EXCLUSIVE, catalog_carrier_combine="all")
    assert idx.is_stock(ds.images(1)[0], 8) is False


def test_a_frame_below_the_e9_population_is_never_stock_whatever_the_carriers() -> None:
    ds = chain(3)
    assert index_of(ds, **EXCLUSIVE).is_stock(ds.images(1)[0], 8) is False
    assert PLAIN.is_stock(ds.images(1)[0], 8) is False


def test_the_policy_token_separates_two_policies_in_one_memo() -> None:
    assert CarrierPolicy.of(Settings()).token() == "off"
    a = CarrierPolicy.of(Settings(**EXCLUSIVE)).token()
    b = CarrierPolicy.of(Settings(catalog_carrier_aware=True,
                                  catalog_min_broker_carriers=2)).token()
    assert a != b and "cov1" in a


def test_a_pass_with_no_dataset_falls_back_to_plain_e9() -> None:
    idx = StockIndex.of_dataset(None, Settings(**EXCLUSIVE))
    assert idx.exempt == frozenset()
    ds = chain(9)
    assert idx.is_stock(ds.images(1)[0], 8) is True


@pytest.mark.parametrize("kwargs, message", [
    (dict(catalog_carrier_aware=True), "at least one carrier limb"),
    (dict(catalog_min_broker_carriers=1), "catalog_min_broker_carriers"),
    (dict(catalog_carrier_combine="either"), "catalog_carrier_combine"),
    (dict(catalog_carrier_coverage_min=0.0), "catalog_carrier_coverage_min"),
    (dict(catalog_carrier_coverage_min=1.5), "catalog_carrier_coverage_min"),
])
def test_validate_refuses_a_sweep_that_cannot_mean_anything(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(**kwargs)


def test_the_shipped_generation_leaves_e9_exactly_where_it_was() -> None:
    """g6 is E9 as measured; E83 may not change a shipped number by existing."""
    settings = Settings()
    assert settings.catalog_carrier_aware is False
    assert CarrierPolicy.of(settings).enabled is False


def test_carriers_that_disagree_about_the_unit_hold_the_projects_material() -> None:
    """E60's purity rail for a photograph: two stated sizes are two units, so it certifies none."""
    ds = chain(9)
    for index, lid in enumerate(sorted(ds.listings)):
        ds.listings[lid].area_m2 = 26.0 if index < 5 else 27.0
    assert index_of(ds, **EXCLUSIVE).is_stock(ds.images(1)[0], 8) is True
    idx = index_of(ds, **EXCLUSIVE)
    assert idx.census[0xABCD].area_spread == pytest.approx(1 / 27)


def test_carriers_that_agree_about_the_unit_keep_the_exemption() -> None:
    ds = chain(9)
    for lid in ds.listings:
        ds.listings[lid].area_m2 = 27.0
        ds.listings[lid].disposition = "1+kk"
    idx = index_of(ds, **EXCLUSIVE)
    assert idx.is_stock(ds.images(1)[0], 8) is False
    assert idx.census[0xABCD].area_spread == 0.0 and idx.census[0xABCD].dispositions == 1


def test_two_dispositions_among_the_carriers_keep_the_frame_subtracted() -> None:
    ds = chain(9)
    for index, lid in enumerate(sorted(ds.listings)):
        ds.listings[lid].area_m2 = 27.0
        ds.listings[lid].disposition = "1+kk" if index else "2+kk"
    assert index_of(ds, **EXCLUSIVE).is_stock(ds.images(1)[0], 8) is True


def test_a_wider_area_tolerance_is_a_settings_row_with_a_cost() -> None:
    ds = chain(9)
    for index, lid in enumerate(sorted(ds.listings)):
        ds.listings[lid].area_m2 = 26.0 if index < 5 else 27.0
    idx = index_of(ds, **EXCLUSIVE, catalog_carrier_area_tol=0.05)
    assert idx.is_stock(ds.images(1)[0], 8) is False


def test_the_incremental_pass_refuses_a_carrier_rule_it_cannot_honour() -> None:
    """E70's replay equivalence is the claim; a pass with no carrier index would break it."""
    import inspect

    from autodedup import incremental

    source = inspect.getsource(incremental.run_pass)
    assert "catalog_carrier_aware" in source and "NotImplementedError" in source
