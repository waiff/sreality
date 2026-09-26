"""Tests for the MF rent-map parser + the reference-rent wrapper (toolkit.rent_map).

Parser tests run against a committed fixture XLSX (hermetic, no network). The
measure itself is SQL (`mf_reference()`, migration 563) and is tested live in
tests/test_mf_reference.py; here only the wrapper's hand-off, on a fake connection.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from api.rent_map import find_latest_xlsx_url
from toolkit.rent_map import (
    MF_ENGINE,
    compute_reference_rent,
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


# --- compute_reference_rent: a thin hand-off to mf_reference() ---------------

_FACTS = {
    "category_main": "byt", "category_type": "prodej", "disposition": "3+1",
    "area_m2": 75.0, "price_czk": 5_100_000, "condition": "dobry",
    "has_balcony": True, "terrace": False, "furnished": "ne", "garage": None,
    "has_lift": True, "building_type": "cihla", "obec_kod": 586846,
    "katastr_kod": None, "country_status": "cz",
}

# mf_reference()'s declared parameter order (migration 563). The wrapper binds by name,
# so this pins that every name lands in its own positional slot of the call.
_SIGNATURE_ORDER = (
    "category_main", "category_type", "disposition", "area_m2", "price_czk", "condition",
    "has_balcony", "terrace", "furnished", "garage", "has_lift", "building_type",
    "obec_kod", "katastr_kod", "country_status",
)


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self._conn.error is not None:
            raise self._conn.error
        self._conn.calls.append((sql, params))

    def fetchone(self):
        return self._conn.row


class _FakeConn:
    def __init__(self, row=None, error=None):
        self.row, self.error, self.calls = row, error, []

    def cursor(self):
        return _FakeCursor(self)


def test_the_wrapper_hands_every_fact_to_mf_reference_in_signature_order():
    detail = {"status": "ok", "monthly_rent_czk": 19125, "vk": 3}
    conn = _FakeConn(row=(detail,))
    out = compute_reference_rent(conn, **_FACTS)
    ((sql, params),) = conn.calls
    call = sql[sql.index("mf_reference("):]
    slots = [call.index(f"%({name})s") for name in _SIGNATURE_ORDER]
    assert slots == sorted(slots)
    assert params == _FACTS
    assert out == {**detail, "engine": MF_ENGINE}


def test_katastr_and_country_default_to_unknown():
    conn = _FakeConn(row=({"status": "location_unknown", "note": "x"},))
    facts = {k: v for k, v in _FACTS.items() if k not in ("katastr_kod", "country_status")}
    compute_reference_rent(conn, **facts)
    ((_sql, params),) = conn.calls
    assert params["katastr_kod"] is None and params["country_status"] is None


@pytest.mark.parametrize("row", [None, (None,)])
def test_a_non_flat_reads_none(row):
    assert compute_reference_rent(_FakeConn(row=row), **_FACTS) is None


def test_a_database_error_reads_none():
    assert compute_reference_rent(_FakeConn(error=RuntimeError("boom")), **_FACTS) is None
