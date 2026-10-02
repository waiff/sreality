"""The write-path wiring of listings.discovery_seq / listing_detail_queue.discovery_seq
(migration 368 — docs/design/portal-order-fidelity.md, Phase 1).

Unlike published_at (a parsed portal field, in LISTING_COLUMNS, preserve-if-null),
discovery_seq is a PIPELINE-assigned value carried from the claimed queue row — never
parsed from portal content, so it stays out of LISTING_COLUMNS and out of ScrapedListing's
contract entirely; `listing_write.from_scraped` / `from_sreality` take it as an explicit
keyword, carried from the claimed queue row. Its semantics are SET-ONCE
(COALESCE(listings.discovery_seq, EXCLUDED.discovery_seq) — favor the STORED value), not
preserve-if-null (which favors the INCOMING value) — a listing's discovery position is a
first-discovery fact, not something a later fetch should ever be allowed to correct."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scraper import db, listing_write
from scraper.portal import _DEFAULTS
from scraper.scraped_listing import ScrapedListing


def test_discovery_seq_is_not_a_listing_column() -> None:
    # Deliberately NOT parsed from portal content -- it must never round-trip
    # through the generic preserve-if-null / hash machinery LISTING_COLUMNS drives.
    assert "discovery_seq" not in db.LISTING_COLUMNS
    assert "discovery_seq" not in db._PRESERVE_IF_NULL_COLUMNS


def test_discovered_at_is_not_a_listing_column() -> None:
    """Pipeline-assigned, never parsed from portal content — so it stays out of
    LISTING_COLUMNS exactly like discovery_seq (published_at, which IS parsed
    from the page, is the deliberate contrast)."""
    assert "discovered_at" not in db.LISTING_COLUMNS
    assert "published_at" in db.LISTING_COLUMNS


@pytest.mark.parametrize("portal", sorted(_DEFAULTS))
def test_the_one_writer_keeps_both_discovery_fields_set_once(portal: str) -> None:
    sql = listing_write._upsert_sql(portal)
    for col in ("discovery_seq", "discovered_at"):
        assert f"{col} = COALESCE(listings.{col}, EXCLUDED.{col})" in sql


def test_the_adapters_thread_the_claims_discovery_fields() -> None:
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    listing = ScrapedListing(source="bazos", source_id_native="1", source_url="https://x/1")
    w = listing_write.from_scraped(listing, discovery_seq=42, discovered_at=at)
    assert (w.discovery_seq, w.discovered_at) == (42, at)
    s = listing_write.from_sreality({"hash_id": 7}, {"sreality_id": 7}, [])
    assert (s.discovery_seq, s.discovered_at) == (None, None)
