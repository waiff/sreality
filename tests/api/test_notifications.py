"""Hermetic tests for the notifications backend (Phase U2.7).

Covers WatchdogFilterSpec validation + the SQL-clause generator. The
matcher loop and FastAPI routes are integration-heavy (DB + asyncio
lifespan) and exercised via the live deployment, not here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from api.notifications import WatchdogFilterSpec, _build_match_clauses


def test_filter_spec_defaults_target_byt_pronajem() -> None:
    """Blank-save watchdogs already target apartments-for-rent."""
    spec = WatchdogFilterSpec()
    assert spec.category_main == "byt"
    assert spec.category_type == "pronajem"


def test_filter_spec_spatial_requires_all_three() -> None:
    """lat/lng/radius_m must be all set or all None — partial spatial
    filters silently broke the SQL in v1, so validate eagerly."""
    with pytest.raises(ValueError):
        WatchdogFilterSpec(lat=50.0, lng=14.0)  # radius_m missing
    with pytest.raises(ValueError):
        WatchdogFilterSpec(lat=50.0, radius_m=1000)  # lng missing

    # All three set is OK.
    s = WatchdogFilterSpec(lat=50.0, lng=14.0, radius_m=1000)
    assert s.lat == 50.0


def test_build_clauses_emits_category_clauses_by_default() -> None:
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert "l.category_main = %(category_main)s" in where
    assert "l.category_type = %(category_type)s" in where
    assert params["category_main"] == "byt"
    assert params["category_type"] == "pronajem"


def test_build_clauses_skips_unset_spatial() -> None:
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert not any("ST_DWithin" in w for w in where)
    assert "radius_m" not in params


def test_build_clauses_emits_spatial_when_set() -> None:
    spec = WatchdogFilterSpec(lat=50.08, lng=14.42, radius_m=1500)
    where, params = _build_match_clauses(spec)
    assert any("ST_DWithin" in w for w in where)
    assert any("l.geom IS NOT NULL" in w for w in where)
    assert params["lat"] == 50.08
    assert params["lng"] == 14.42
    assert params["radius_m"] == 1500


def test_build_clauses_handles_price_and_area_bounds() -> None:
    spec = WatchdogFilterSpec(
        min_price_czk=15_000,
        max_price_czk=30_000,
        min_area_m2=40.0,
        max_area_m2=80.0,
    )
    where, params = _build_match_clauses(spec)
    assert "l.price_czk >= %(min_price_czk)s" in where
    assert "l.price_czk <= %(max_price_czk)s" in where
    assert "l.area_m2 >= %(min_area_m2)s" in where
    assert "l.area_m2 <= %(max_area_m2)s" in where
    assert params["min_price_czk"] == 15_000
    assert params["max_price_czk"] == 30_000


def test_build_clauses_tri_state_amenities() -> None:
    spec = WatchdogFilterSpec(
        has_balcony=True,
        terrace=False,
        garage=None,  # explicit "any" — no clause
    )
    where, params = _build_match_clauses(spec)
    assert "l.has_balcony = %(has_balcony)s" in where
    assert "l.terrace = %(terrace)s" in where
    assert not any("l.garage = " in w for w in where)
    assert params["has_balcony"] is True
    assert params["terrace"] is False


def test_build_clauses_dispositions_use_any() -> None:
    """Dispositions are a multi-select; SQL uses ANY() for index-friendly
    lookups against `l.disposition`."""
    spec = WatchdogFilterSpec(dispositions=["2+kk", "2+1", "3+kk"])
    where, params = _build_match_clauses(spec)
    assert "l.disposition = ANY(%(dispositions)s)" in where
    assert params["dispositions"] == ["2+kk", "2+1", "3+kk"]


def test_build_clauses_enumerated_columns() -> None:
    spec = WatchdogFilterSpec(furnished="ano", ownership="osobni")
    where, params = _build_match_clauses(spec)
    assert "l.furnished = %(furnished)s" in where
    assert "l.ownership = %(ownership)s" in where
    assert params["furnished"] == "ano"
    assert params["ownership"] == "osobni"


def test_build_clauses_categoryless_spec() -> None:
    """An operator who explicitly clears the category filters gets a
    spec with no category WHERE clauses — the watchdog matches every
    category. Defaults narrow; explicit None widens."""
    spec = WatchdogFilterSpec(category_main=None, category_type=None)
    where, params = _build_match_clauses(spec)
    assert not any("l.category_main" in w for w in where)
    assert not any("l.category_type" in w for w in where)
    assert "category_main" not in params
    assert "category_type" not in params
