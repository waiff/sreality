"""Contract tests for scraper.scraped_listing (the multi-portal row shape)."""

from __future__ import annotations

from dataclasses import replace

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
