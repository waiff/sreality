"""The write-path derivation of listings.street_name_key (migration 256).

street_name_key is a DERIVED column — a pure function of `street` — stamped at
every street-write chokepoint so the stored key can never drift from the stored
street (the invariant the dedup --dirty scoped load relies on). These guard that
the write path derives via the ONE source (scraper.street.street_name_key) and
that the column is wired into the batch/upsert machinery (LISTING_COLUMNS +
the pgtype lockstep)."""

from __future__ import annotations

from scraper import db
from scraper.street import street_name_key


def test_column_is_wired_into_listing_columns() -> None:
    assert "street_name_key" in db.LISTING_COLUMNS
    # The import-time assertion already guards the pgtype lockstep; assert the type too.
    assert db._LISTING_COLUMN_PGTYPE["street_name_key"] == "text"


def test_set_street_name_key_derives_from_street() -> None:
    for street, expected_src in [
        ("ul. Koterovská 12", "ul. Koterovská 12"),
        ("Hlavní", "Hlavní"),
        (None, None),
        ("", ""),
    ]:
        d = {"street": street}
        db._set_street_name_key(d)
        assert d["street_name_key"] == street_name_key(expected_src)


def test_set_street_name_key_ignores_any_parsed_value() -> None:
    # The row may already carry a (stale / wrong) street_name_key from the column
    # loop; the derivation must OVERRIDE it from `street`, never trust the input.
    d = {"street": "ul. Koterovská 12", "street_name_key": "WRONG"}
    db._set_street_name_key(d)
    assert d["street_name_key"] == "koterovska"


def test_every_bulk_street_write_path_stamps_the_key() -> None:
    """Drift guard: EVERY path that writes listings.street must also write street_name_key
    in lockstep, or the dedup --dirty scoped load silently omits those rows as peers
    (rule #19). The ingest chokepoints route through _set_street_name_key (tested above);
    the bulk backfills + the coord->street resolver carry it in their UPDATE SQL. Assert
    each constant references the column so a future edit can't silently drop it (this is the
    guard the address-point resolver was missing). New street-writing scripts must add a line
    here."""
    from scripts import (
        backfill_address_point_streets,
        backfill_bazos_street_locality,
        backfill_portal_streets,
        backfill_street_name_key,
    )
    assert "street_name_key" in backfill_portal_streets._UPDATE_SQL
    assert "street_name_key" in backfill_bazos_street_locality._UPDATE_SQL
    assert "street_name_key" in backfill_address_point_streets._UPDATE_SQL
    assert "street_name_key" in backfill_street_name_key._UPDATE_SQL
