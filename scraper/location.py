"""Portal-agnostic coordinate resolution for detail drains and backfills.

THE single home for the address→coords half of location enrichment (the
coords→hierarchy half is the in-DB admin-geo trigger, migrations 140/162/222;
the coords→street half is the RÚIAN resolver, scripts/backfill_address_point_streets).
Before this module the same logic lived as per-portal copies with drift: idnes and
realitymix each carried a `_geocode_fallback` + `_fill_coords` + have-geom preload
(only realitymix stamped provenance and bbox-guarded), bazos carried the memoised
geocoder, ceskereality had dead geocoder plumbing, and maxima/remax/mmreality had
nothing — their coords-less rows were unreachable forever.

Three layers, in resolution priority order (CoordResolver.fill):

  1. PAGE — coords the parser extracted win, always.
  2. CARRY-FORWARD — a refetched listing whose page carries no coords gets the
     STORED coordinate back (db.native_ids_with_geom preload). This is the
     2026-06 Mapy-credit-incident guard: without it every coords-less page
     re-geocoded on EVERY refetch, and ingesting lat=None wiped the stored geom
     and oscillated snapshots. A Mapy credit is spent at most once per listing.
  3. GEOCODE — Mapy.cz on the locality text, guarded: skip when the match is a
     region/country centroid (worse than NULL for map + radius filter) or lands
     outside the CZ bbox (a foreign mis-match for an ambiguous locality).

Carried/geocoded coords stamp raw['coords'] provenance ('carry_forward' /
'geocode') so Mapy-sourced rows stay auditable and the stamp is STABLE across
refetches (an unstamped carry would flip raw_json back and forth and churn
snapshots — the realitymix posture, now uniform).

The persistent side (geocode_cached + the listings.geocode_attempted_at ledger,
migration 288) serves single-threaded backfills; the drain path uses the in-run
memoised CachingGeocoder only (drain workers share no connection, and
carry-forward already caps drains at one geocode per listing ever).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg

from scraper import db, geocoding
from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.street import in_cz_bbox

LOG = logging.getLogger(__name__)

Geocoder = Callable[[str], GeocodeResult]

# Geocode tiers too coarse to store: a region/country centroid drops the pin in
# the middle of the country, worse than NULL for map display and the radius
# filter. A municipality (town) centroid is KEPT — it recovers the admin
# hierarchy (obec/okres/region, derived from geom by the listings trigger) and a
# rough map placement.
GEOCODE_SKIP_TYPES: frozenset[str] = frozenset({"regional.region", "regional.country"})

# A negative geocode_cache row older than this is retried (Mapy coverage
# improves; a transient miss shouldn't be permanent). Positive rows never expire.
NEGATIVE_CACHE_TTL = timedelta(days=30)


class CachingGeocoder:
    """Per-run memoised geocoder: collapses repeat street/locality queries to
    one Mapy.cz call each (a crawl's listings cluster by town). Caches misses
    too so a failing query isn't retried for every listing in that locality.

    Shared across the detail-drain worker pool. Dict get/set are atomic under
    the GIL and `geocoding.geocode` builds its own request session per call, so
    the worst concurrent case is a harmless duplicate lookup, never corruption."""

    def __init__(self, fn: Geocoder) -> None:
        self._fn = fn
        self._cache: dict[str, GeocodeResult | GeocodingError] = {}

    def __call__(self, query: str) -> GeocodeResult:
        key = normalize_query(query)
        cached = self._cache.get(key)
        if cached is not None:
            if isinstance(cached, GeocodingError):
                raise cached
            return cached
        try:
            result = self._fn(query)
        except GeocodingError as exc:
            self._cache[key] = exc
            raise
        self._cache[key] = result
        return result


def build_geocoder() -> Geocoder | None:
    """A run-memoised Mapy.cz geocoder, or None when no Mapy.cz key is set so
    the caller still runs (coords then come from whatever the page carried)."""
    if not geocoding.mapy_api_keys():
        LOG.info("GEOCODE skipped: no Mapy.cz API key set")
        return None
    return CachingGeocoder(geocoding.geocode)


def normalize_query(query: str) -> str:
    """The geocode cache key: lowercased, whitespace-collapsed. Python is the
    only writer and reader of geocode_cache keys, so no SQL twin exists."""
    return " ".join(query.lower().split())


class CoordResolver:
    """The drain-path seam: page coords win; else carry the stored coordinate
    forward; geocode the locality only when neither the page nor the DB has one.

    One instance per portal run. `preload(conn)` loads the stored coords once on
    the main thread (connect_drain); `fill(native_id, listing)` runs inside the
    worker pool. Geocoding — including an unset key — must never fail a fetch."""

    def __init__(self, source: str, *, geocoder: Geocoder | None = None) -> None:
        self.source = source
        self._have_geom: dict[str, tuple[float, float]] | None = None
        # False = build lazily on first use (so a keyless env stays a no-op);
        # an injected geocoder (tests, callers with their own cache) skips that.
        self._geocoder: Geocoder | None | bool = geocoder if geocoder is not None else False

    def preload(self, conn: psycopg.Connection) -> None:
        if self._have_geom is None:
            self._have_geom = db.native_ids_with_geom(conn, self.source)
            LOG.info(
                "GEOCODE preload source=%s have_geom=%d (carry stored coords forward on refetch)",
                self.source, len(self._have_geom),
            )

    def fill(self, native_id: str, listing: Any) -> Any:
        if listing.lat is not None and listing.lon is not None:
            return listing
        stored = (self._have_geom or {}).get(native_id)
        if stored is not None:
            # Stable provenance across refetches: without the stamp a geocoded
            # row's raw.coords would flip back on the next coords-less refetch,
            # flipping the content hash and churning snapshots.
            raw = {**listing.raw, "coords": {"source": "carry_forward"}}
            return replace(listing, lat=stored[0], lon=stored[1], raw=raw)
        return self._geocode_fallback(listing)

    def _geocode_fallback(self, listing: Any) -> Any:
        if not listing.locality:
            return listing
        if self._geocoder is False:
            self._geocoder = build_geocoder()
        if self._geocoder is None:
            return listing
        try:
            result = self._geocoder(listing.locality)
        except Exception:  # noqa: BLE001 - geocoding must never fail the fetch
            return listing
        if result.matched_type in GEOCODE_SKIP_TYPES or not in_cz_bbox(result.lat, result.lng):
            return listing
        raw = {**listing.raw, "coords": {"source": "geocode", "confidence": result.confidence,
                                         "matched_type": result.matched_type}}
        return replace(listing, lat=result.lat, lon=result.lng, raw=raw)


# --- persistent cache (backfill path; single-threaded callers only) -----------

_CACHE_GET_SQL = """
    SELECT lat, lng, matched_type, confidence, resolved_at
    FROM geocode_cache WHERE query_key = %(key)s
"""

_CACHE_PUT_SQL = """
    INSERT INTO geocode_cache (query_key, lat, lng, matched_type, confidence, resolved_at)
    VALUES (%(key)s, %(lat)s, %(lng)s, %(matched_type)s, %(confidence)s, now())
    ON CONFLICT (query_key) DO UPDATE SET
      lat = EXCLUDED.lat, lng = EXCLUDED.lng,
      matched_type = EXCLUDED.matched_type, confidence = EXCLUDED.confidence,
      resolved_at = EXCLUDED.resolved_at
"""


def geocode_cached(
    conn: psycopg.Connection, geocoder: Geocoder, query: str,
) -> GeocodeResult | None:
    """Geocode through the persistent geocode_cache (migration 288): a positive
    hit never re-spends a credit; a negative hit (lat NULL) suppresses retries
    for NEGATIVE_CACHE_TTL. Returns None on a miss/too-coarse/foreign result —
    the caller applies no further guards (the skip-type + bbox policy is applied
    HERE, before caching, so the cache stores only store-ready results)."""
    key = normalize_query(query)
    with conn.cursor() as cur:
        cur.execute(_CACHE_GET_SQL, {"key": key})
        row = cur.fetchone()
    if row is not None:
        lat, lng, matched_type, confidence, resolved_at = row
        if lat is not None and lng is not None:
            return GeocodeResult(lat=lat, lng=lng, confidence=confidence or "low",
                                 matched_address=query, matched_type=matched_type or "",
                                 bbox=None, raw={"cache": True})
        if datetime.now(timezone.utc) - resolved_at < NEGATIVE_CACHE_TTL:
            return None
    try:
        result: GeocodeResult | None = geocoder(query)
    except GeocodingError:
        result = None
    if result is not None and (
        result.matched_type in GEOCODE_SKIP_TYPES or not in_cz_bbox(result.lat, result.lng)
    ):
        result = None
    with conn.cursor() as cur:
        cur.execute(_CACHE_PUT_SQL, {
            "key": key,
            "lat": result.lat if result else None,
            "lng": result.lng if result else None,
            "matched_type": result.matched_type if result else None,
            "confidence": result.confidence if result else None,
        })
    return result
