"""idnes drain wiring of the shared coordinate resolver (scraper.location):
fetch_detail must route every parsed listing through CoordResolver.fill so page
coords win, an already-placed row's coords carry forward (never re-geocoded, geom
never wiped), and only a never-placed row falls back to Mapy. The resolver's own
behavior is unit-tested in tests/scraper/test_location.py."""

from __future__ import annotations

from types import SimpleNamespace

from scraper import idnes_main
from scraper.geocoding import GeocodeResult
from scraper.scraped_listing import ScrapedListing


def _listing(**over):
    base = dict(source="idnes", source_id_native="1", source_url="https://x/")
    base.update(over)
    return ScrapedListing(**base)


def _portal(have_geom=None, geocoder=None):
    cfg = SimpleNamespace(
        supports_complete_walk=True,
        categories=[],
        limits=SimpleNamespace(
            index_rate=3.0, price_change_min_pct=0.005, shared_rate_limiter=False,
        ),
    )
    portal = idnes_main.IdnesPortal(cfg)
    portal._coords._have_geom = have_geom
    portal._coords._geocoder = geocoder
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


def _boom(*a, **k):
    raise AssertionError("geocoder must not be called")


def _patch_fetch(monkeypatch, page_listing=None):
    monkeypatch.setattr(idnes_main, "detail_url", lambda ref: "https://reality.idnes.cz/x")
    monkeypatch.setattr(idnes_main, "category_from_url", lambda url: (None, None))
    monkeypatch.setattr(
        idnes_main, "parse_detail",
        lambda *a, **k: page_listing or _listing(locality="Praha 8 - Střížkov"),
    )


def test_fetch_detail_carries_stored_coords_forward(monkeypatch):
    # Already-placed row, page gives no coords: the stored coordinate is
    # injected (not merely the geocode skipped), so ingest can't wipe geom and
    # the content hash can't oscillate between the geocoded and wiped state.
    _patch_fetch(monkeypatch)
    portal = _portal(have_geom={"42": (50.1, 14.5)}, geocoder=_boom)
    item = portal.fetch_detail(_Client(), "42", None)
    assert item.kind == "ok"
    assert item.payload["listing"].lat == 50.1
    assert item.payload["listing"].lon == 14.5
    assert item.payload["listing"].raw["coords"] == {"source": "carry_forward"}


def test_fetch_detail_page_coords_win_over_stored(monkeypatch):
    _patch_fetch(monkeypatch,
                 page_listing=_listing(locality="Praha", lat=49.2, lon=16.6))
    portal = _portal(have_geom={"42": (50.1, 14.5)}, geocoder=_boom)
    item = portal.fetch_detail(_Client(), "42", None)
    assert item.kind == "ok"
    assert item.payload["listing"].lat == 49.2
    assert item.payload["listing"].lon == 16.6


def test_fetch_detail_geocodes_when_not_yet_placed(monkeypatch):
    # no stored coords AND no page coords -> the geocoder IS called
    _patch_fetch(monkeypatch)
    portal = _portal(have_geom={"999": (1.0, 2.0)}, geocoder=lambda q: _result())
    item = portal.fetch_detail(_Client(), "42", None)
    assert item.kind == "ok"
    assert item.payload["listing"].lat == 50.08
    assert item.payload["listing"].lon == 14.42


def test_fetch_detail_geocodes_when_map_never_preloaded(monkeypatch):
    # have_geom never preloaded (None) -> the old always-geocode behaviour
    _patch_fetch(monkeypatch)
    portal = _portal(have_geom=None, geocoder=lambda q: _result())
    item = portal.fetch_detail(_Client(), "42", None)
    assert item.kind == "ok"
    assert item.payload["listing"].lat == 50.08
