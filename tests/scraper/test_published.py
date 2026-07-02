"""Unit tests for scraper.published (portal publish-date parsing, migration 266)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from scraper.published import bazos_posted_date, czech_date, iso_date, iso_datetime


@pytest.mark.parametrize("text,expected", [
    ("[2.7. 2026]", date(2026, 7, 2)),
    ("[12.5. 2026]", date(2026, 5, 12)),
    ("- TOP - [9.6. 2026]", date(2026, 6, 9)),   # promotion marker prefix (live shape)
    ("[9. 6. 2026]", date(2026, 6, 9)),          # tolerate spacing drift
    ("[32.13. 2026]", None),                     # impossible calendar date
    ("2.7. 2026", None),                         # no brackets -> not the bazos shape
    ("TOP", None),
    ("", None),
    (None, None),
])
def test_bazos_posted_date(text, expected):
    assert bazos_posted_date(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("10. února 2020", date(2020, 2, 10)),
    ("27. února 2026", date(2026, 2, 27)),
    ("1. září 2021", date(2021, 9, 1)),
    ("3. října 2025", date(2025, 10, 3)),
    ("15. Července 2024", date(2024, 7, 15)),    # case-insensitive
    ("31. ledna 2026", date(2026, 1, 31)),
    ("30. února 2026", None),                    # impossible calendar date
    ("10. blahu 2020", None),                    # unknown month name
    ("včera", None),
    ("", None),
    (None, None),
])
def test_czech_date(text, expected):
    assert czech_date(text) == expected


@pytest.mark.parametrize("value,expected", [
    ("2026-05-20", date(2026, 5, 20)),
    (" 2026-05-20 ", date(2026, 5, 20)),
    ("2026-13-01", None),
    ("garbage", None),
    ("", None),
    (None, None),
    (20260520, None),                            # non-string raw value
])
def test_iso_date(value, expected):
    assert iso_date(value) == expected


def test_iso_datetime_preserves_offset():
    dt = iso_datetime("2024-05-06T10:39:22+02:00")
    assert dt is not None
    assert dt.utcoffset() is not None
    assert dt.astimezone(timezone.utc) == datetime(2024, 5, 6, 8, 39, 22, tzinfo=timezone.utc)


def test_iso_datetime_accepts_zulu():
    assert iso_datetime("2024-05-06T10:39:22Z") == datetime(
        2024, 5, 6, 10, 39, 22, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("value", ["garbage", "", None, 12345])
def test_iso_datetime_malformed_is_none(value):
    assert iso_datetime(value) is None
