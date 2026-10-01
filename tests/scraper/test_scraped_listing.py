"""Contract tests for scraper.scraped_listing (the multi-portal row shape)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from scraper.scraped_listing import _HASH_FIELDS, ScrapedListing


def _listing(**overrides) -> ScrapedListing:
    base = dict(
        source="bazos",
        source_id_native="219122924",
        source_url="https://reality.bazos.cz/inzerat/219122924/x.php",
        category_main="byt",
        category_type="prodej",
        price_czk=5_499_000,
        disposition="2+kk",
        locality="Letovice",
        street="Husova 12",
    )
    base.update(overrides)
    return ScrapedListing(**base)


def test_the_place_reading_never_reaches_a_column():
    """W4-c dropped every place column from `listings`, so `to_row` emits none.
    The parser still READS the page's locality/street/pin — that reading is the
    claim `location_data` resolves into `listing_location`, and it rides
    raw_json, not a column."""
    listing = _listing()
    row = listing.to_row(-5)
    assert listing.street == "Husova 12" and listing.locality == "Letovice"
    for key in ("locality", "district", "street", "house_number", "zip", "lat", "lon"):
        assert key not in row, f"to_row still emits {key!r}"


def test_street_is_not_hashed():
    # street is derived/extracted (like lat/lon): a backfill or extraction
    # refinement must never churn snapshots.
    assert "street" not in _HASH_FIELDS
    a = _listing(street=None)
    b = replace(a, street="Husova 12")
    assert a.content_hash() == b.content_hash()


def test_content_hash_still_sees_real_content():
    a = _listing()
    b = replace(a, locality="Brno")
    assert a.content_hash() != b.content_hash()


def test_published_at_lands_in_to_row():
    row = _listing(published_at=date(2026, 7, 2)).to_row(-5)
    assert row["published_at"] == date(2026, 7, 2)


def test_published_at_is_not_hashed():
    # Portal lifecycle metadata: bazos re-stamps the date on every bump / TOP
    # renewal, and backfills from stored raw must stay snapshot-free — a
    # published_at change must NEVER change the content hash.
    assert "published_at" not in _HASH_FIELDS
    a = _listing(published_at=None)
    b = replace(a, published_at=date(2026, 7, 2))
    c = replace(a, published_at=date(2026, 7, 3))
    assert a.content_hash() == b.content_hash() == c.content_hash()


def test_area_basis_lands_in_to_row_and_in_the_column_list():
    from scraper import db

    row = _listing(area_m2=70.0, area_basis="usable").to_row(-5)
    assert row["area_basis"] == "usable"
    assert "area_basis" in db.LISTING_COLUMNS


def test_area_basis_is_not_hashed():
    # It is a PROVENANCE observation of a value that does not change, so stamping
    # it (now, or by a later backfill) must not append a snapshot row — rule 2.
    assert "area_basis" not in _HASH_FIELDS
    a = _listing(area_m2=70.0, area_basis=None)
    b = replace(a, area_basis="usable")
    assert a.content_hash() == b.content_hash()


def test_source_url_lands_in_to_row_and_is_required():
    # Identity, not content: in the row (so it rides LISTING_COLUMNS), never hashed, and
    # refused when empty — the write is preserve-if-null, so a None would keep a stale URL.
    row = _listing().to_row(-5)
    assert row["source_url"] == "https://reality.bazos.cz/inzerat/219122924/x.php"
    assert "source_url" not in _HASH_FIELDS
    import pytest
    with pytest.raises(ValueError):
        _listing(source_url="")


def test_a_plot_that_is_the_floor_area_is_declined_before_the_content_hash():
    """The ONE call site of `scraper.area.stated_plot` for every portal that crosses this
    contract (rule 21): a komerční "plot" equal to the usable measure, and ANY plot on a
    byt, is absence — absence in the HASH too, so the row and its newest snapshot agree
    (rules 2/8) instead of a NULL landing after the hash at the write boundary."""
    echo = _listing(category_main="komercni", area_m2=10.0, area_basis="usable",
                    usable_area=10.0, estate_area=10.0)
    assert echo.estate_area is None
    assert echo.to_row(-5)["estate_area"] is None
    assert echo.content_hash() == replace(echo, estate_area=None).content_hash()
    assert _listing(category_main="byt", area_m2=59.0, area_basis="usable",
                    usable_area=59.0, estate_area=64.0).estate_area is None
    parcel = _listing(category_main="komercni", area_m2=852.0, area_basis="total",
                      estate_area=6841.0)
    assert parcel.estate_area == 6841.0
    assert parcel.content_hash() != replace(parcel, estate_area=None).content_hash()
    house = _listing(category_main="dum", area_m2=210.0, area_basis="usable",
                     usable_area=210.0, estate_area=80.0)
    assert house.estate_area == 80.0
