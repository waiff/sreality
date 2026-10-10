"""The write-path wiring of listings.discovered_at (migration 444), and the end of
listings.discovery_seq (migration 368): since W5 (MS19) no code writes it; the column, the
queue's `nextval` default and the sequence stay until W6 drops them (Rule 0).

discovered_at is a PIPELINE-assigned value carried from the claimed queue row — never parsed
from portal content, so it stays out of LISTING_COLUMNS and out of ScrapedListing's contract
entirely; `listing_write.from_scraped` / `from_sreality` take it as an explicit keyword. Its
semantics are SET-ONCE (COALESCE(listings.discovered_at, EXCLUDED.discovered_at) — favor the
STORED value), not preserve-if-null: a first sighting is a fact a later fetch never corrects."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scraper import db, listing_write
from scraper.portal import _DEFAULTS
from scraper.scraped_listing import ScrapedListing


def test_discovered_at_is_not_a_listing_column() -> None:
    """Pipeline-assigned, never parsed from portal content (published_at, which IS
    parsed from the page, is the deliberate contrast)."""
    assert "discovered_at" not in db.LISTING_COLUMNS
    assert "published_at" in db.LISTING_COLUMNS


@pytest.mark.parametrize("portal", sorted(_DEFAULTS))
def test_the_one_writer_keeps_discovered_at_set_once_and_never_names_discovery_seq(
    portal: str,
) -> None:
    sql = listing_write._upsert_sql(portal)
    assert "discovered_at = COALESCE(listings.discovered_at, EXCLUDED.discovered_at)" in sql
    assert "discovery_seq" not in sql


def test_the_claim_never_returns_discovery_seq() -> None:
    import inspect

    assert "discovery_seq" not in inspect.getsource(db.claim_detail_batch)


def test_the_adapters_thread_the_claims_discovered_at() -> None:
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    listing = ScrapedListing(source="bazos", source_id_native="1", source_url="https://x/1")
    assert listing_write.from_scraped(listing, discovered_at=at).discovered_at == at
    assert listing_write.from_sreality({"hash_id": 7}, {"sreality_id": 7}, []).discovered_at is None
