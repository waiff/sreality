"""Tests for the MF rent-map parser + reference-rent calc (toolkit.rent_map).

Parser tests run against a committed fixture XLSX (hermetic, no network);
compute_reference_rent uses a fake psycopg connection.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from api.rent_map import find_latest_xlsx_url
from toolkit.rent_map import (
    compute_reference_rent,
    disposition_to_vk,
    parse_rent_map_xlsx,
    source_date_from_filename,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures" / "rent_map" / "2026-05-15_Cenova-mapa.xlsx"
)


@pytest.fixture(scope="module")
def parsed():
    return parse_rent_map_xlsx(FIXTURE.read_bytes(), source_date=date(2026, 5, 15))


def test_parses_all_territories(parsed):
    assert len({v.ruian_code for v in parsed.values}) == 7630
    assert len(parsed.values) == 7630 * 4
    assert parsed.source_date == date(2026, 5, 15)


def test_level_split_matches_admin_boundaries(parsed):
    levels = {v.ruian_code: v.level for v in parsed.values}
    assert sum(1 for lv in levels.values() if lv == "ku") == 1582
    assert sum(1 for lv in levels.values() if lv == "obec") == 6048


def test_adjustment_tables(parsed):
    old = [a for a in parsed.adjustments if not a.is_novostavba]
    nov = [a for a in parsed.adjustments if a.is_novostavba]
    assert len(old) == 20  # 4 VK × 5 attributes
    assert len(nov) == 24  # 4 VK × 6 attributes (incl. other_material)
    adj = {(a.vk, a.is_novostavba, a.attribute): a.czk_per_m2
           for a in parsed.adjustments}
    assert adj[(3, False, "elevator")] == 47
    assert adj[(3, False, "balcony")] == 4
    assert adj[(3, False, "garage")] == 37
    assert adj[(1, True, "other_material")] == 26
    # other_material only exists for novostavba
    assert (1, False, "other_material") not in adj


def test_litomerice_worked_example(parsed):
    """The MF sheet's own worked example: older 3+1, 68 m², with
    výtah + balkon + garáž in Litoměřice → 291 Kč/m² → 19 788 Kč."""
    vk3 = [v for v in parsed.values
           if v.ku_name == "Litoměřice" and v.vk == 3]
    assert vk3 and vk3[0].ref_rent_per_m2 == 203
    adj = {(a.vk, a.is_novostavba, a.attribute): a.czk_per_m2
           for a in parsed.adjustments}
    per_m2 = (203 + adj[(3, False, "elevator")]
              + adj[(3, False, "balcony")] + adj[(3, False, "garage")])
    assert per_m2 == 291
    assert round(per_m2 * 68) == 19788


@pytest.mark.parametrize("disp,vk", [
    ("1+kk", 1), ("1+1", 1), ("0+1", 1), ("2+kk", 2), ("2+1", 2),
    ("3+1", 3), ("3+kk", 3), ("4+kk", 4), ("5+1", 4), ("6+kk", 4),
    (None, None), ("", None), ("atypicke", None),
])
def test_disposition_to_vk(disp, vk):
    assert disposition_to_vk(disp) == vk


def test_source_date_from_filename():
    assert source_date_from_filename("2026-05-15_Cenova-mapa.xlsx") == date(2026, 5, 15)
    assert source_date_from_filename("no-date.xlsx") is None


def test_find_latest_xlsx_url_picks_newest_date():
    html = """
      <a href="/assets/attachments/2026-05-15_Cenova-mapa.xlsx">current</a>
      <a href="/assets/attachments/2026-02-15_Cenova-mapa.xlsx">hist</a>
      <a href="/assets/attachments/2025-11-15_Cenova-mapa.xlsx">hist</a>
    """
    assert find_latest_xlsx_url(html) == (
        "https://mf.gov.cz/assets/attachments/2026-05-15_Cenova-mapa.xlsx"
    )


def test_find_latest_xlsx_url_none_when_absent():
    assert find_latest_xlsx_url("<a href='/assets/x.pdf'>no</a>") is None


# --- compute_reference_rent (fake DB) --------------------------------------

class _FakeCursor:
    def __init__(self, territory, adjustments):
        self._territory = territory
        self._adjustments = adjustments

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._is_territory = "rent_map_values_public" in sql

    def fetchone(self):
        return self._territory

    def fetchall(self):
        return self._adjustments


class _FakeConn:
    def __init__(self, territory, adjustments):
        self._territory = territory
        self._adjustments = adjustments

    def cursor(self):
        return _FakeCursor(self._territory, self._adjustments)


def _conn(ref_std=203, ref_nov=300, adjustments=None):
    territory = (685429, "ku", "Ústecký kraj", ref_std, ref_nov, 1,
                 date(2026, 5, 15), "Litoměřice")
    if adjustments is None:
        adjustments = [("elevator", 47), ("balcony", 4), ("garage", 37),
                       ("terrace", 34), ("furnished", 28)]
    return _FakeConn(territory, adjustments)


def test_compute_reference_rent_basic():
    out = compute_reference_rent(
        _conn(), lat=50.5, lng=14.1, area_m2=68, disposition="3+1",
        amenities={"elevator": True, "balcony": True, "garage": True,
                   "terrace": False, "furnished": False, "other_material": False},
        is_novostavba=False,
    )
    assert out is not None
    assert out["vk"] == 3
    assert out["base_per_m2"] == 203
    assert {a["attribute"] for a in out["adjustments"]} == {"elevator", "balcony", "garage"}
    assert out["total_per_m2"] == 291
    assert out["monthly_rent_czk"] == 19788
    assert out["territory"]["name"] == "Litoměřice"
    assert out["source_date"] == "2026-05-15"


def test_compute_reference_rent_novostavba_uses_nov_base():
    out = compute_reference_rent(
        _conn(ref_std=203, ref_nov=300), lat=50.5, lng=14.1, area_m2=50,
        disposition="2+kk", amenities={}, is_novostavba=True,
    )
    assert out is not None
    assert out["base_per_m2"] == 300
    assert out["is_novostavba"] is True
    assert out["adjustments"] == []
    assert out["monthly_rent_czk"] == 300 * 50


def test_compute_reference_rent_territory_miss_returns_none():
    out = compute_reference_rent(
        _FakeConn(None, []), lat=0.0, lng=0.0, area_m2=50,
        disposition="2+1", amenities={},
    )
    assert out is None


def test_compute_reference_rent_no_area_returns_none():
    assert compute_reference_rent(
        _conn(), lat=50.0, lng=14.0, area_m2=None,
        disposition="2+1", amenities={},
    ) is None


def test_compute_reference_rent_unknown_disposition_returns_none():
    assert compute_reference_rent(
        _conn(), lat=50.0, lng=14.0, area_m2=50,
        disposition=None, amenities={},
    ) is None
