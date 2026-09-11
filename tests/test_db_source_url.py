"""The write-path wiring of listings.source_url (docs/design/portal-listing-url.md).

source_url rides the shared LISTING_COLUMNS machinery, so one entry covers both ingest
paths (upsert_listing + the batched drain) for all nine portals — the bespoke post-insert
UPDATE the crawler path used is gone. These pin: the column + its pgtype, preserve-if-null
on the ON CONFLICT SET (an incoming NULL can only mean "could not assemble", never "left
its page"), its exclusion from the ScrapedListing content hash and from both snapshot-diff
sites, and the one fetch guard that keeps sreality's page off every queue.
"""

from __future__ import annotations

import pytest

from scraper import db, freshness
from scraper.scraped_listing import _HASH_FIELDS, _LISTING_FIELDS, ScrapedListing
from toolkit import snapshots


def test_column_is_wired_into_listing_columns() -> None:
    assert "source_url" in db.LISTING_COLUMNS
    assert db._LISTING_COLUMN_PGTYPE["source_url"] == "text"
    assert "source_url text" in db._BATCH_UPSERT_SQL


def test_update_set_preserves_source_url_if_incoming_null() -> None:
    assert "source_url" in db._PRESERVE_IF_NULL_COLUMNS
    expected = "source_url = COALESCE(EXCLUDED.source_url, listings.source_url)"
    assert db._listing_update_set_sql().count(expected) == 1
    assert expected in db._BATCH_UPSERT_SQL


def test_contract_carries_but_never_hashes_source_url() -> None:
    assert "source_url" in _LISTING_FIELDS
    assert "source_url" not in _HASH_FIELDS
    row = ScrapedListing(source="bazos", source_id_native="1",
                         source_url="https://reality.bazos.cz/inzerat/1/x.php").to_row(-1)
    assert row["source_url"] == "https://reality.bazos.cz/inzerat/1/x.php"


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_contract_refuses_an_empty_source_url(bad) -> None:
    with pytest.raises(ValueError):
        ScrapedListing(source="bazos", source_id_native="1", source_url=bad)


def test_snapshot_diffs_skip_the_url() -> None:
    # A locality slug drifting, or the 2026-05-26 payload rebuild, must never read as
    # "the listing changed" in the freshness log or the snapshot timeline.
    assert "source_url" in freshness._DIFF_SKIP_KEYS
    assert "source_url" in snapshots._DIFF_SKIP_KEYS


def test_detail_ref_never_hands_srealitys_page_to_a_fetcher() -> None:
    assert db.detail_ref("sreality", "https://www.sreality.cz/detail/prodej/byt/2+1/x/1") is None
    assert db.detail_ref("bazos", "https://reality.bazos.cz/inzerat/1/x.php") == (
        "https://reality.bazos.cz/inzerat/1/x.php"
    )
    assert db.detail_ref("bazos", None) is None
