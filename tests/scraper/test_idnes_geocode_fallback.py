"""Tests for idnes_main coordinate filling: the stored-coord carry-forward in
_fill_coords and the Mapy.cz _geocode_fallback that fills lat/lon when an
iDNES detail page carried no embedded map config and the row was never placed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scraper import idnes_main
from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.scraped_listing import ScrapedListing


def _listing(**over):
    base = dict(source="idnes", source_id_native="1", source_url="https://x/")
    base.update(over)
    return ScrapedListing(**base)


def _portal(have_geom=None):
    cfg = SimpleNamespace(
        supports_complete_walk=True,
        categories=[],
        limits=SimpleNamespace(
            index_rate=3.0, price_change_min_pct=0.005, shared_rate_limiter=False,
        ),
    )
    portal = idnes_main.IdnesPortal(cfg)
    portal._have_geom = have_geom
    return portal


class _Client:
    def fetch_detail(self, ref):
        return ("<html></html>", 200)


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


def _patch_fetch(monkeypatch, geocoder, page_listing=None):
    monkeypatch.setattr(idnes_main, "detail_url", lambda ref: "https://reality.idnes.cz/x")
    monkeypatch.setattr(idnes_main, "category_from_url", lambda url: (None, None))
    monkeypatch.setattr(
        idnes_main, "parse_detail",
        lambda *a, **k: page_listing or _listing(locality="Praha 8 - Střížkov"),
    )
    monkeypatch.setattr(idnes_main, "geocode", geocoder)


def test_fetch_detail_carries_stored_coords_forward(monkeypatch):
    # Already-placed row, page gives no coords: the stored coordinate is
    # injected (not merely the geocode skipped), so ingest can't wipe geom and
    # the content hash can't oscillate between the geocoded and wiped state.
    def _boom(*a, **k):
        raise AssertionError("geocode must not be called for an already-placed row")
    _patch_fetch(monkeypatch, _boom)
    item = _portal(have_geom={"42": (50.1, 14.5)}).fetch_detail(_Client(), "42", None)
    assert item.kind == "ok"
    assert item.payload["listing"].lat == 50.1
    assert item.payload["listing"].lon == 14.5


def test_fetch_detail_page_coords_win_over_stored(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("geocode must not be called when the page has coords")
    _patch_fetch(monkeypatch, _boom,
                 page_listing=_listing(locality="Praha", lat=49.2, lon=16.6))
    item = _portal(have_geom={"42": (50.1, 14.5)}).fetch_detail(_Client(), "42", None)
    assert item.kind == "ok"
    assert item.payload["listing"].lat == 49.2
    assert item.payload["listing"].lon == 16.6


def test_fetch_detail_geocodes_when_not_yet_placed(monkeypatch):
    # no stored coords AND no page coords -> the geocoder IS called
    _patch_fetch(monkeypatch, lambda *a, **k: _result())
    item = _portal(have_geom={"999": (1.0, 2.0)}).fetch_detail(_Client(), "42", None)
    assert item.kind == "ok"
    assert item.payload["listing"].lat == 50.08
    assert item.payload["listing"].lon == 14.42


def test_fetch_detail_geocodes_when_map_never_preloaded(monkeypatch):
    # have_geom never preloaded (None) -> the old always-geocode behaviour
    _patch_fetch(monkeypatch, lambda *a, **k: _result())
    item = _portal(have_geom=None).fetch_detail(_Client(), "42", None)
    assert item.kind == "ok"
    assert item.payload["listing"].lat == 50.08
