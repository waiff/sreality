"""Tests for idnes_main._geocode_fallback — the Mapy.cz fallback that fills
lat/lon when an iDNES detail page carried no embedded map config."""

from __future__ import annotations

import pytest

from scraper import idnes_main
from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.scraped_listing import ScrapedListing


def _listing(**over):
    base = dict(source="idnes", source_id_native="1", source_url="https://x/")
    base.update(over)
    return ScrapedListing(**base)


def _result(lat=50.08, lng=14.42, matched_type="regional.municipality_part"):
    return GeocodeResult(
        lat=lat, lng=lng, confidence="low",
        matched_address="Praha 8 - Střížkov", matched_type=matched_type,
        bbox=None, raw={},
    )


def test_fills_missing_coords(monkeypatch):
    monkeypatch.setattr(idnes_main, "geocode", lambda *a, **k: _result())
    out = idnes_main._geocode_fallback(_listing(locality="Praha 8 - Střížkov"))
    assert out.lat == 50.08 and out.lon == 14.42


def test_noop_when_coords_already_present(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("geocode must not be called when coords exist")
    monkeypatch.setattr(idnes_main, "geocode", _boom)
    out = idnes_main._geocode_fallback(_listing(locality="Praha", lat=1.0, lon=2.0))
    assert out.lat == 1.0 and out.lon == 2.0


def test_noop_without_locality(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("geocode must not be called without a locality")
    monkeypatch.setattr(idnes_main, "geocode", _boom)
    out = idnes_main._geocode_fallback(_listing(locality=None))
    assert out.lat is None and out.lon is None


def test_skips_too_coarse_country_or_region(monkeypatch):
    monkeypatch.setattr(idnes_main, "geocode",
                        lambda *a, **k: _result(matched_type="regional.country"))
    out = idnes_main._geocode_fallback(_listing(locality="Česko"))
    assert out.lat is None and out.lon is None


def test_swallows_geocoding_error(monkeypatch):
    def _raise(*a, **k):
        raise GeocodingError("MAPY_CZ_API_KEY is not set")
    monkeypatch.setattr(idnes_main, "geocode", _raise)
    out = idnes_main._geocode_fallback(_listing(locality="Praha"))
    assert out.lat is None and out.lon is None
