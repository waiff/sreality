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
