"""Hermetic tests for the pure per-row decision of the street/locality backfill."""

from __future__ import annotations

from scraper.scraped_listing import ScrapedListing
from scripts.backfill_bazos_street_locality import plan_update


def _listing(**overrides) -> ScrapedListing:
    base = dict(
        source="bazos",
        source_id_native="219122924",
        source_url="https://reality.bazos.cz/inzerat/219122924/x.php",
    )
    base.update(overrides)
    return ScrapedListing(**base)


def test_fills_null_locality_and_street():
    params, improved = plan_update(
        _listing(locality="Plzeň", street="ul. Koterovská"),
        current_locality=None, current_street=None, current_district=None,
    )
    assert improved
    assert params == {"locality": "Plzeň", "street": "ul. Koterovská", "district": None}


def test_nothing_parsed_is_a_stamp_only_skip():
    params, improved = plan_update(
        _listing(),
        current_locality=None, current_street=None, current_district=None,
    )
    assert not improved
    assert params == {"locality": None, "street": None, "district": None}


def test_never_offers_a_null_over_an_existing_value():
    # Row selected because street is NULL; its locality is already set. The
    # parse yields only a locality — nothing new lands, and the None params
    # combine with the SQL COALESCE so the existing values are never cleared.
    params, improved = plan_update(
        _listing(locality="Plzeň"),
        current_locality="Plzeň", current_street=None, current_district="Plzeň-město",
    )
    assert not improved
    assert params["street"] is None
    assert params["district"] is None


def test_street_onto_existing_locality_counts_as_updated():
    _, improved = plan_update(
        _listing(locality="Plzeň", street="Husova 12"),
        current_locality="Plzeň", current_street=None, current_district=None,
    )
    assert improved


def test_district_fills_when_parser_ever_yields_it():
    _, improved = plan_update(
        _listing(district="Plzeň-město"),
        current_locality="Plzeň", current_street="Husova 12", current_district=None,
    )
    assert improved
