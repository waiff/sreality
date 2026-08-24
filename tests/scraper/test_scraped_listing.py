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


def test_street_lands_in_to_row():
    row = _listing().to_row(-5)
    assert row["street"] == "Husova 12"


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
