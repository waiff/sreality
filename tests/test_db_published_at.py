"""The write-path wiring of listings.published_at (migration 266).

published_at rides the shared LISTING_COLUMNS machinery, so one entry covers
the one listing write (scraper/listing_write.py) on all nine portals. These pin the wiring:
the column + its pgtype (the jsonb_to_recordset cast), preserve-if-null on the
ON CONFLICT SET (an intermittent portal signal must never erase a stored
value), and its exclusion from the ScrapedListing content hash (a bazos bump
re-stamp must never churn a snapshot)."""

from __future__ import annotations

from scraper import db, listing_write
from scraper.portal import _DEFAULTS
from scraper.scraped_listing import _HASH_FIELDS, _LISTING_FIELDS


def test_column_is_wired_into_listing_columns() -> None:
    assert "published_at" in db.LISTING_COLUMNS
    assert db._LISTING_COLUMN_PGTYPE["published_at"] == "timestamptz"


def test_batch_record_spec_casts_timestamptz() -> None:
    # The writer's jsonb_to_recordset must cast the isoformatted JSON string
    # back to timestamptz, or the whole ~100-listing flush fails.
    assert "published_at timestamptz" in listing_write._RECORD_SPEC


def test_update_set_preserves_published_at_if_incoming_null() -> None:
    # sreality's `edited` exists on ~40% of rows and a portal can stop
    # rendering its date — a fetch without the signal must carry the stored
    # value forward; a fresher portal date still wins (COALESCE).
    assert "published_at" in db._PRESERVE_IF_NULL_COLUMNS
    expected = "published_at = COALESCE(EXCLUDED.published_at, listings.published_at)"
    assert expected in db._listing_update_set_sql("sreality")
    for portal in _DEFAULTS:
        assert expected in listing_write._upsert_sql(portal)


def test_contract_carries_but_never_hashes_published_at() -> None:
    # In the row contract (so listing_write.from_scraped carries it), NOT in the
    # hash (so a bazos bump re-stamp or a raw_json backfill never appends a
    # snapshot).
    assert "published_at" in _LISTING_FIELDS
    assert "published_at" not in _HASH_FIELDS
