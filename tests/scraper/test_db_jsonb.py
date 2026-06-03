"""Tests for the process-wide jsonb serialization policy in scraper.db.

psycopg's default JSON dumper can't serialize Decimal (what numeric columns
come back as) or datetime, so a payload that mixes a DB-read value into a
jsonb column would raise 'not JSON serializable' at write time. scraper.db
registers a Decimal/datetime-aware dumper globally; these tests pin that
contract.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from scraper import db


def test_jsonb_default_coerces_decimal_to_float():
    assert db._jsonb_default(Decimal("108.0")) == 108.0
    assert isinstance(db._jsonb_default(Decimal("108.0")), float)


def test_jsonb_default_coerces_datetime_and_date_to_isoformat():
    dt = datetime(2026, 6, 3, 12, 30, tzinfo=timezone.utc)
    assert db._jsonb_default(dt) == dt.isoformat()
    assert db._jsonb_default(date(2026, 6, 3)) == "2026-06-03"


def test_jsonb_default_still_raises_for_truly_unserializable():
    with pytest.raises(TypeError, match="not JSON serializable"):
        db._jsonb_default(object())


def test_jsonb_dumps_roundtrips_a_mixed_payload():
    payload = {
        "area_m2": Decimal("108.0"),
        "fetched_at": datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
        "disposition": "2+kk",
        "exclude_ids": [],
    }
    out = json.loads(db._jsonb_dumps(payload))
    assert out["area_m2"] == 108.0
    assert out["disposition"] == "2+kk"
    assert out["fetched_at"].startswith("2026-06-03T12:00")
