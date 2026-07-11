"""Tests for scraper.location — the portal-agnostic coordinate resolution seam:
CoordResolver (page > carry-forward > geocode + provenance stamps), the memoised
CachingGeocoder, and the persistent geocode_cache wrapper (migration 288)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scraper import location
from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.location import CachingGeocoder, CoordResolver, build_geocoder, geocode_cached
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


def _boom(*a, **k):
    raise AssertionError("geocoder must not be called")


# --- CoordResolver.fill: resolution order + stamps ---------------------------

def test_page_coords_win_over_stored_and_geocode():
    r = CoordResolver("idnes", geocoder=_boom)
    r._have_geom = {"1": (50.1, 14.5)}
    out = r.fill("1", _listing(lat=49.2, lon=16.6, locality="Brno"))
    assert (out.lat, out.lon) == (49.2, 16.6)
    assert "coords" not in out.raw  # page path leaves raw alone


def test_carry_forward_injects_stored_and_stamps_provenance():
    r = CoordResolver("idnes", geocoder=_boom)
    r._have_geom = {"1": (50.1, 14.5)}
    out = r.fill("1", _listing(locality="Praha"))
    assert (out.lat, out.lon) == (50.1, 14.5)
    assert out.raw["coords"] == {"source": "carry_forward"}


def test_geocode_fills_never_placed_and_stamps_provenance():
    r = CoordResolver("idnes", geocoder=lambda q: _result())
    r._have_geom = {"999": (1.0, 2.0)}
    out = r.fill("1", _listing(locality="Praha 8 - Střížkov"))
    assert (out.lat, out.lon) == (50.08, 14.42)
    assert out.raw["coords"]["source"] == "geocode"
    assert out.raw["coords"]["matched_type"] == "regional.municipality_part"


def test_geocode_skips_too_coarse_region_country():
    r = CoordResolver("idnes", geocoder=lambda q: _result(matched_type="regional.country"))
    out = r.fill("1", _listing(locality="Česko"))
    assert out.lat is None and out.lon is None


def test_geocode_rejects_foreign_point_outside_cz_bbox():
    r = CoordResolver("idnes", geocoder=lambda q: _result(lat=36.5, lng=-4.9))
    out = r.fill("1", _listing(locality="Marbella"))
    assert out.lat is None and out.lon is None


def test_geocode_noop_without_locality():
    r = CoordResolver("idnes", geocoder=_boom)
    out = r.fill("1", _listing(locality=None))
    assert out.lat is None and out.lon is None


def test_geocode_swallows_errors():
    def _raise(q):
        raise GeocodingError("boom")
    r = CoordResolver("idnes", geocoder=_raise)
    out = r.fill("1", _listing(locality="Praha"))
    assert out.lat is None and out.lon is None


def test_no_key_env_is_a_noop(monkeypatch):
    monkeypatch.delenv("MAPY_CZ_API_KEY", raising=False)
    monkeypatch.delenv("MAPY2_CZ_API_KEY", raising=False)
    r = CoordResolver("idnes")  # lazy build finds no key -> None -> no-op
    out = r.fill("1", _listing(locality="Praha"))
    assert out.lat is None and out.lon is None


# --- CachingGeocoder / build_geocoder ----------------------------------------

def test_build_geocoder_none_without_key(monkeypatch):
    monkeypatch.delenv("MAPY_CZ_API_KEY", raising=False)
    monkeypatch.delenv("MAPY2_CZ_API_KEY", raising=False)
    assert build_geocoder() is None


def test_build_geocoder_cached_with_key(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "k")
    assert isinstance(build_geocoder(), CachingGeocoder)


def test_caching_geocoder_memoises_hits_and_misses():
    calls: list[str] = []

    def fn(q):
        calls.append(q)
        if q == "bad":
            raise GeocodingError("no result")
        return _result()

    g = CachingGeocoder(fn)
    assert g("Praha  8").lat == 50.08
    assert g("praha 8").lat == 50.08          # normalized key -> one upstream call
    with pytest.raises(GeocodingError):
        g("bad")
    with pytest.raises(GeocodingError):
        g("bad")                               # miss cached too
    assert calls == ["Praha  8", "bad"]


def test_normalize_query():
    assert location.normalize_query("  Praha   8  ") == "praha 8"


# --- geocode_cached (persistent cache; fake conn) ----------------------------

class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._row = None

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        if sql.lstrip().startswith("SELECT"):
            self._row = self._conn.cache.get(params["key"])
        else:
            self._conn.cache[params["key"]] = (
                params["lat"], params["lng"], params["matched_type"],
                params["confidence"], datetime.now(timezone.utc),
            )

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.cache: dict[str, tuple] = {}
        self.executed: list = []

    def cursor(self):
        return _FakeCursor(self)


def test_geocode_cached_positive_hit_skips_geocoder():
    conn = _FakeConn()
    conn.cache["praha 8"] = (50.08, 14.42, "regional.address", "high",
                             datetime.now(timezone.utc))
    out = geocode_cached(conn, _boom, "Praha  8")
    assert out is not None and out.lat == 50.08


def test_geocode_cached_negative_hit_within_ttl_skips_geocoder():
    conn = _FakeConn()
    conn.cache["nowhere"] = (None, None, None, None, datetime.now(timezone.utc))
    assert geocode_cached(conn, _boom, "nowhere") is None


def test_geocode_cached_negative_hit_past_ttl_retries():
    conn = _FakeConn()
    stale = datetime.now(timezone.utc) - location.NEGATIVE_CACHE_TTL - timedelta(days=1)
    conn.cache["nowhere"] = (None, None, None, None, stale)
    out = geocode_cached(conn, lambda q: _result(), "nowhere")
    assert out is not None and out.lat == 50.08
    assert conn.cache["nowhere"][0] == 50.08  # cache refreshed positive


def test_geocode_cached_applies_policy_before_caching():
    conn = _FakeConn()
    out = geocode_cached(conn, lambda q: _result(matched_type="regional.country"), "Česko")
    assert out is None
    assert conn.cache["česko"][0] is None  # stored as NEGATIVE, not a coarse positive


def test_geocode_cached_miss_writes_negative():
    conn = _FakeConn()

    def _raise(q):
        raise GeocodingError("no result")

    assert geocode_cached(conn, _raise, "Neznámo") is None
    assert conn.cache["neznámo"][0] is None
