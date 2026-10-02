"""Offline tests for scraper.listing_write: the adapters, validation before any SQL, the
pure helpers and the per-source statement text. Semantics (what lands in which table) are
executed against the replayed schema in tests/test_listing_write_live.py."""

from __future__ import annotations

from typing import Any

import pytest

from scraper import db, hashing, listing_write
from scraper.listing_write import ListingWrite, WriteOutcome
from scraper.portal import _DEFAULTS
from scraper.scraped_listing import _HASH_FIELDS, ScrapedListing
from toolkit.broker_sources import BROKER_FINGERPRINT_KEYS, BROKER_FINGERPRINTED_SOURCES

PORTALS = sorted(_DEFAULTS)


class _NoSqlConn:
    """Proves only "no SQL before validation": any DB touch fails the test."""

    def cursor(self) -> Any:
        raise AssertionError("SQL issued")

    def transaction(self) -> Any:
        raise AssertionError("SQL issued")


def _scraped(**overrides: Any) -> ScrapedListing:
    base: dict[str, Any] = dict(
        source="bazos", source_id_native="219122924",
        source_url="https://reality.bazos.cz/inzerat/219122924/x.php",
        category_main="byt", category_type="prodej", price_czk=5_499_000,
        disposition="2+kk", locality="Letovice",
    )
    base.update(overrides)
    return ScrapedListing(**base)


def _outcome(result: str, images: int = 0) -> WriteOutcome:
    return WriteOutcome("bazos", "1", 1, result, 1, "h", images)  # type: ignore[arg-type]


def test_from_scraped_maps_every_listing_column_and_hashes_the_parsed_document() -> None:
    listing = _scraped()
    w = listing_write.from_scraped(listing, discovery_seq=5)
    assert set(w.row) == set(db.LISTING_COLUMNS)
    assert "sreality_id" not in w.row
    assert w.row["disposition"] == "2+kk" and w.row["price_czk"] == 5_499_000
    assert (w.source, w.source_id_native, w.sreality_id) == ("bazos", "219122924", None)
    assert w.discovery_seq == 5
    assert w.content_hash == hashing.digest(listing.hash_doc())
    assert set(listing.hash_doc()) == set(_HASH_FIELDS)


def test_from_scraped_media_split_keeps_original_positions() -> None:
    """A leading video leaves sequence 0 absent: a re-scrape stays idempotent on stored rows."""
    w = listing_write.from_scraped(_scraped(raw={"image_urls": [
        "https://cdn.example/clip.mp4", "https://cdn.example/a.jpg", "https://cdn.example/b.jpg",
    ]}))
    assert [i["sequence"] for i in w.images] == [1, 2]
    assert [v["sequence"] for v in w.videos] == [0]


def test_from_sreality_is_keyed_on_its_own_id_and_copies_the_row() -> None:
    raw = {"hash_id": 123, "k": "v"}
    row = {"sreality_id": 123, "price_czk": 1_000_000}
    w = listing_write.from_sreality(raw, row, [{"url": "https://x/a.jpg", "sequence": 0}])
    assert (w.source, w.source_id_native, w.sreality_id) == ("sreality", "123", 123)
    assert w.videos == ()
    assert w.content_hash == hashing.digest(hashing.sreality_hash_doc(raw))
    row["price_czk"] = 1
    row["lat"] = 50.0
    assert w.row == {"sreality_id": 123, "price_czk": 1_000_000}


def test_a_write_never_accepts_a_hash_string() -> None:
    with pytest.raises(TypeError):
        ListingWrite(source="bazos", source_id_native="1", row={}, raw={}, hash_doc={},
                     content_hash="x")  # type: ignore[call-arg]


def test_an_empty_call_issues_no_sql() -> None:
    assert listing_write.write_listings(_NoSqlConn(), []) == []  # type: ignore[arg-type]


def test_mixed_sources_raise_before_any_sql() -> None:
    bazos = listing_write.from_scraped(_scraped())
    idnes = listing_write.from_scraped(_scraped(source="idnes", source_id_native="abc"))
    with pytest.raises(ValueError, match="one source per call"):
        listing_write.write_listings(_NoSqlConn(), [bazos, idnes])  # type: ignore[arg-type]


def test_tally_is_the_drain_flush_shape() -> None:
    counts = listing_write.tally([
        _outcome("new", 3), _outcome("updated", 1), _outcome("unchanged"), _outcome("unchanged"),
    ])
    assert counts == {"new": 1, "updated": 1, "unchanged": 2, "images_discovered": 4}
    assert listing_write.tally([]) == {
        "new": 0, "updated": 0, "unchanged": 0, "images_discovered": 0}


def test_coerce_numerics_rounds_half_even_and_drops_non_finite() -> None:
    obj: dict[str, Any] = {"floor": 2.5, "total_floors": 3.5, "price_czk": 4_500_000.0,
                           "area_m2": float("inf"), "usable_area": float("nan"),
                           "has_lift": True, "estate_area": 72.5}
    listing_write._coerce_numerics(obj)
    assert obj["floor"] == 2 and obj["total_floors"] == 4
    assert obj["price_czk"] == 4_500_000 and isinstance(obj["price_czk"], int)
    assert obj["area_m2"] is None and obj["usable_area"] is None
    assert obj["has_lift"] is True
    assert obj["estate_area"] == 72.5


def test_stage_dedupes_media_and_sanitises_the_snapshot_price() -> None:
    w = listing_write.from_scraped(_scraped(price_czk=1, raw={"image_urls": [
        "https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]}))
    dup = ListingWrite(source="bazos", source_id_native="2", row={}, raw={}, hash_doc={},
                       images=({"url": "https://x/a.jpg", "sequence": 0},
                               {"url": "https://x/b.jpg", "sequence": 0},
                               {"url": "", "sequence": 1},
                               {"url": "https://x/clip.mp4", "sequence": 2}))
    rows, snaps, images, videos = listing_write._stage({"219122924": w, "2": dup})
    assert rows[0]["price_czk"] is None and snaps[0]["price_czk"] is None
    assert rows[0]["source_id_native"] == "219122924" and rows[0]["sreality_id"] is None
    assert [(i["source_id_native"], i["sequence"]) for i in images] == [
        ("219122924", 0), ("219122924", 1), ("2", 0)]
    assert images[-1]["url"] == "https://x/a.jpg"
    assert videos == []


def test_broker_fingerprint_survives_a_malformed_block() -> None:
    """This runs inside the live write transaction: a portal that emits a list, a scalar or
    nothing must degrade to daily-sweep-only attribution, never abort the listing's write."""
    assert listing_write._broker_fingerprint(None) == ()
    assert listing_write._broker_fingerprint([{"name": "x"}]) == ()
    assert listing_write._broker_fingerprint("broker") == ()
    at = BROKER_FINGERPRINT_KEYS.index("broker_id")
    assert listing_write._broker_fingerprint({"broker_id": 17})[at] == "17"


def test_broker_fields_stay_out_of_the_crawler_hash() -> None:
    """Rule 2: listing_snapshots is for CONTENT changes. The broker block stays out of the
    crawlers' hash document; a broker-only change reaches the resolver by fingerprint."""
    assert not {f for f in _HASH_FIELDS if "broker" in f or f == "raw"}
    a = _scraped(source="idnes", source_id_native="1", raw={"broker": {"account_oid": "a"}})
    b = _scraped(source="idnes", source_id_native="1", raw={"broker": {"account_oid": "b"}})
    assert (listing_write.from_scraped(a).content_hash
            == listing_write.from_scraped(b).content_hash)


@pytest.mark.parametrize("portal", PORTALS)
def test_every_per_source_upsert_carries_the_contract_and_no_dead_heal(portal: str) -> None:
    sql = listing_write._upsert_sql(portal)
    assert db._listing_update_set_sql(portal) in sql
    assert "source_id_native = COALESCE" not in sql
    reads_nothing = "NULL::jsonb AS stored_broker" in sql
    assert reads_nothing == (portal not in BROKER_FINGERPRINTED_SOURCES)
