"""Tests for api/property_sheet.py — the property download's PDF.

The embedded fonts are subsetted, so a page's text is not greppable in the bytes: the
words are pinned through the helpers that produce them, and whole renders are pinned
for producing a sound PDF on full, empty and broken inputs.
"""

from __future__ import annotations

import io
import re
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

pytest.importorskip("reportlab")
from PIL import Image

from api import property_sheet as sheet

TODAY = date(2026, 10, 8)

FLAT = {
    "property_id": 952631,
    "listing_id": 42,
    "is_active": True,
    "last_seen_at": datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
    "category_main": "byt",
    "category_type": "pronajem",
    "price_czk": 29900,
    "price_unit": "za mesic",
    "area_m2": 89,
    "disposition": "3+kk",
    "subtype": None,
    "display_label": "Strnadových, Praha",
    "lat": 50.1126338,
    "lng": 14.5144365,
    "floor": 1,
    "total_floors": 6,
    "has_balcony": True,
    "terrace": False,
    "has_lift": True,
    "cellar": None,
    "garage": True,
    "has_parking": True,
    "parking_lots": 2,
    "building_type": "cihla",
    "condition": "velmi_dobry",
    "energy_rating": "G",
    "estate_area": None,
    "garden_area": None,
    "ownership": "osobni",
    "furnished": "castecne",
    "description": "\r\n\r\n".join(["Prostorný a světlý byt 🏡 v klidné části Vysočan. " * 12] * 8),
}
ADS = [
    {"id": 42, "source": "sreality", "is_active": True, "price_czk": 29900,
     "source_url": "https://www.sreality.cz/detail/pronajem/byt/3+kk/praha/3856601164",
     "first_seen_at": datetime(2026, 10, 8, 8, 0, tzinfo=UTC)},
    {"id": 43, "source": "idnes", "is_active": False, "price_czk": None, "source_url": None,
     "first_seen_at": datetime(2026, 9, 1, 8, 0, tzinfo=UTC)},
]
BROKERS = [
    {"broker_id": 1, "broker_display_name": "Jana Nováková", "broker_firm_label": "Reality s.r.o.",
     "primary_phone": "420777000111", "primary_email": "jana@example.cz"},
    {"broker_id": 2, "broker_display_name": None, "broker_firm_label": None,
     "has_phone": True, "has_email": False},
]


def _jpeg(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 140, 130)).save(buf, "JPEG")
    return buf.getvalue()


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type /Page[^s]", pdf))


def test_a_full_property_renders_a_multi_page_pdf_with_its_cover():
    pdf = sheet.render_property_sheet(
        FLAT, ADS, BROKERS, brokers_from_inactive=False, cover=_jpeg(1200, 800), today=TODAY
    )
    assert pdf.startswith(b"%PDF")
    assert b"/Subtype /Image" in pdf
    assert _pages(pdf) >= 2
    # The sheet's own fonts are embedded: the base-14 fonts carry no Czech letters.
    assert b"Inter" in pdf and b"Fraunces" in pdf


def test_a_property_with_every_optional_field_empty_still_renders():
    bare = {"property_id": 1, "listing_id": 2, "is_active": False}
    pdf = sheet.render_property_sheet(
        bare, [], [], brokers_from_inactive=True, cover=None, today=TODAY
    )
    assert pdf.startswith(b"%PDF") and _pages(pdf) == 1


def test_a_cover_that_will_not_decode_is_left_out():
    pdf = sheet.render_property_sheet(
        FLAT, ADS, [], brokers_from_inactive=False, cover=b"not a jpeg", today=TODAY
    )
    assert pdf.startswith(b"%PDF") and b"/Subtype /Image" not in pdf


@pytest.mark.parametrize(
    ("prop", "expected"),
    [
        ({"category_main": "byt", "disposition": "3+kk", "area_m2": 89}, "Byt 3+kk, 89\u00a0m²"),
        ({"category_main": "dum", "subtype": "rodinny_dum", "area_m2": 1180.4},
         "Rodinný dům, 1\u00a0180\u00a0m²"),
        # numeric columns arrive as Decimal; .5 rounds up, as the page's Math.round does.
        ({"category_main": "byt", "area_m2": Decimal("54.50")}, "Byt, 55\u00a0m²"),
        ({"category_main": "pozemek"}, "Pozemek"),
        ({}, "Nemovitost"),
    ],
)
def test_headline_names_the_kind_disposition_and_area(prop, expected):
    assert sheet.headline(prop) == expected


def test_facts_use_the_registry_labels_and_drop_what_is_missing():
    facts = dict(sheet._facts(FLAT))
    assert facts["Stavba"] == "Cihla"
    assert facts["Stav"] == "Velmi dobrý"
    assert facts["Vlastnictví"] == "Osobní"
    assert facts["Vybavení"] == "Částečně"
    assert facts["Podlaží"] == "1. patro z 6 podlaží"
    assert "Pozemek" not in facts and "Zahrada" not in facts


def test_a_plot_says_its_area_is_the_plot():
    facts = dict(sheet._facts({"category_main": "pozemek", "area_m2": 1200}))
    assert facts["Plocha pozemku"] == "1\u00a0200\u00a0m²"
    assert "Plocha" not in facts


def test_a_value_outside_the_registry_shows_as_the_portal_wrote_it():
    assert dict(sheet._facts({"condition": "k_nastehovani"}))["Stav"] == "K_nastehovani"


@pytest.mark.parametrize(
    ("floor", "total", "expected"),
    [
        (0, None, "přízemí"),
        (-1, None, "suterén"),
        (-2, None, "2. podzemní podlaží"),
        (3, 6, "3. patro z 6 podlaží"),
        (None, 6, None),
    ],
)
def test_floor_wording_matches_the_page(floor, total, expected):
    assert sheet._floor(floor, total) == expected


def test_amenities_drop_the_unknown_and_strike_the_absent():
    line = sheet._amenities(FLAT)
    assert "Sklep" not in line  # NULL: unknown is not absent
    assert "✗" in line and "Terasa" in line
    assert "Parkování (2\u00a0místa)" in line
    assert sheet._amenities({}) is None


def test_portal_text_loses_glyphs_the_font_lacks_and_is_escaped():
    assert sheet._clean("Byt 🏡 <b>&") == "Byt  &lt;b&gt;&amp;"


def test_a_masked_contact_says_hidden_never_none():
    assert "skrytý" in sheet._contact("Telefon", None, True, None)
    assert sheet._contact("Telefon", None, False, None) is None
    shown = sheet._contact("E-mail", "jana@example.cz", None, "mailto:jana@example.cz")
    assert 'href="mailto:jana@example.cz"' in shown


def test_phone_is_spaced_like_the_page():
    assert sheet._phone("420731404040") == "+420 731 404 040"
    assert sheet._phone("0049301234") == "0049301234"
