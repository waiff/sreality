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
    # "včera" used to return None here — that was the DEFECT, not the contract:
    # it left published_at NULL on every freshly-inserted ceskereality listing.
    # Its relative arms are covered below, anchored to a fixed `today`.
    ("kdovíkdy", None),
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


# --- relative Czech dates (the freshest listings' only form) ----------------

_TODAY = date(2026, 8, 27)


def test_relative_words_resolve_against_the_anchor():
    """ceskereality renders "Datum vložení: včera" on a listing posted
    yesterday, so the absolute-date regex missed exactly the rows where the
    publish date matters most and published_at stayed NULL on every freshly
    inserted one."""
    assert czech_date("včera", today=_TODAY) == date(2026, 8, 26)
    assert czech_date("dnes", today=_TODAY) == date(2026, 8, 27)
    assert czech_date("předevčírem", today=_TODAY) == date(2026, 8, 25)


def test_relative_words_are_matched_diacritics_folded():
    assert czech_date("Vloženo: VCERA", today=_TODAY) == date(2026, 8, 26)


def test_pred_n_dny_forms():
    assert czech_date("před 3 dny", today=_TODAY) == date(2026, 8, 24)
    assert czech_date("před 1 dnem", today=_TODAY) == date(2026, 8, 26)
    assert czech_date("před týdnem", today=_TODAY) == date(2026, 8, 20)


def test_absolute_form_still_wins_and_is_unaffected():
    assert czech_date("10. února 2026", today=_TODAY) == date(2026, 2, 10)


def test_unparseable_stays_none():
    assert czech_date("kdovíkdy", today=_TODAY) is None
    assert czech_date("", today=_TODAY) is None
    assert czech_date(None, today=_TODAY) is None


def test_anchor_defaults_to_prague_not_utc():
    """A walk at 23:30 UTC is already tomorrow in Prague; resolving "dnes"
    against UTC would date the listing a day early."""
    from datetime import datetime, timezone

    from scraper.published import _PRAGUE

    late = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)
    assert late.astimezone(_PRAGUE).date() == date(2026, 7, 1)
