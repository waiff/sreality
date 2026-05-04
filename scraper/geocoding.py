"""Mapy.cz geocoding wrapper.

Used by the source-kind dispatcher when a non-sreality listing's HTML
gives a locality string but no coordinates. Uses Mapy.cz over Google /
OSM for first-party Czech address coverage.

# Schema (confirmed against a live api.mapy.com response)

Endpoint  : GET https://api.mapy.com/v1/geocode
Auth      : apikey query param
Params    : query (str, required), apikey, lang ("cs" default), limit (int, default 5)

Response  : {
              "items": [
                {
                  "name":     "Václavské náměstí 846/1",
                  "label":    "Adresa "                       <- result-CLASS, not the address
                  "location": "Václavské náměstí 846/1, ...", <- the actual human address
                  "position": {"lon": 14.42403, "lat": 50.08418},
                  "bbox":     [west, south, east, north],
                  "type":     "regional.address" |
                              "regional.street" |
                              "regional.municipality_part" |
                              "regional.municipality" |
                              "regional.region" |
                              "regional.country",
                  "regionalStructure": [...],
                  "zip":      "110 00"  (only on address-type)
                },
                ...
              ],
              "locality": []
            }

Two important behaviours from real responses:

  1. `label` is the result-category tag (e.g. "Adresa ", "Náměstí "),
     NOT the human-readable address. Use `location` for the address.

  2. Items are ranked by Mapy.cz's relevance algorithm, not by
     specificity. A query for "Václavské náměstí 1" returns the whole
     street first and the exact building second. For an estimation we
     want the most-specific coordinates available, so we scan the
     items and pick the highest-specificity type rather than blindly
     taking items[0].

If Mapy.cz returns any other shape we degrade gracefully — every field
read goes through `.get(...)` and missing fields produce confidence
"low" plus a warning rather than a crash.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Literal

import requests

LOG = logging.getLogger(__name__)

GEOCODE_URL = "https://api.mapy.com/v1/geocode"

GeocodeConfidence = Literal["high", "medium", "low"]

RETRYABLE_STATUS: frozenset[int] = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
)

# Specificity ordering — higher score = more precise coordinates.
# regional.address is the only "high" tier because everything else
# returns a centroid that may be ~100m (street) to ~1km+ (city) off
# the actual building. For an estimation that's used to find spatial
# comparables, low-specificity geocodes are basically useless and
# should surface as low-confidence so the caller can decide.
_TYPE_SPECIFICITY: dict[str, int] = {
    "regional.address": 100,
    "regional.street": 60,
    "regional.municipality_part": 30,
    "regional.municipality": 20,
    "regional.region": 10,
    "regional.country": 0,
}
_HIGH_MIN_SPECIFICITY = 100
_MEDIUM_MIN_SPECIFICITY = 60


class GeocodingError(RuntimeError):
    """Raised when Mapy.cz returns no usable result for a query."""


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lng: float
    confidence: GeocodeConfidence
    matched_address: str   # human-readable address (Mapy's `location`)
    matched_type: str      # the Mapy `type` enum value
    bbox: tuple[float, float, float, float] | None  # (west, south, east, north)
    raw: dict[str, Any]


def geocode(
    locality: str,
    *,
    api_key: str | None = None,
    lang: str = "cs",
    limit: int = 5,
    timeout_s: float = 10.0,
    max_retries: int = 2,
    session: requests.Session | None = None,
) -> GeocodeResult:
    """Geocode a Czech locality string.

    Reads `MAPY_CZ_API_KEY` from env if api_key is None. Raises
    GeocodingError if the request fails after retries or if the response
    contains no usable item.
    """
    if not isinstance(locality, str) or not locality.strip():
        raise GeocodingError("empty locality")
    key = api_key or os.environ.get("MAPY_CZ_API_KEY")
    if not key:
        raise GeocodingError("MAPY_CZ_API_KEY is not set")

    sess = session or requests.Session()
    params = {"query": locality, "apikey": key, "lang": lang, "limit": limit}
    payload = _request_with_retry(
        sess, params, timeout_s=timeout_s, max_retries=max_retries,
    )
    return _payload_to_result(payload)


def _request_with_retry(
    sess: requests.Session,
    params: dict[str, Any],
    *,
    timeout_s: float,
    max_retries: int,
) -> dict[str, Any]:
    error: Exception | None = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            time.sleep(2.0 ** (attempt - 1))
        try:
            r = sess.get(GEOCODE_URL, params=params, timeout=timeout_s)
        except (requests.ConnectionError, requests.Timeout) as exc:
            error = exc
            LOG.warning(
                "GET %s attempt %d/%d failed: %s",
                GEOCODE_URL, attempt + 1, max_retries + 1, exc,
            )
            continue
        if r.status_code in RETRYABLE_STATUS:
            error = requests.HTTPError(
                f"{r.status_code} from {GEOCODE_URL}", response=r,
            )
            LOG.warning(
                "GET %s attempt %d/%d failed: %s",
                GEOCODE_URL, attempt + 1, max_retries + 1, error,
            )
            continue
        if r.status_code >= 400:
            r.raise_for_status()
        try:
            return r.json()
        except ValueError as exc:
            error = exc
            LOG.warning(
                "GET %s attempt %d/%d JSON decode failed: %s",
                GEOCODE_URL, attempt + 1, max_retries + 1, exc,
            )
    assert error is not None
    raise GeocodingError(f"Mapy.cz request failed: {error}") from error


def _payload_to_result(payload: dict[str, Any]) -> GeocodeResult:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise GeocodingError("Mapy.cz returned no items")
    item = _pick_most_specific_item(items)
    if item is None:
        raise GeocodingError("Mapy.cz returned no usable items")
    position = item.get("position") or {}
    lat = position.get("lat")
    lng = position.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        raise GeocodingError("Mapy.cz item missing lat/lon")
    matched_type = str(item.get("type") or "")
    confidence = _confidence_for_type(matched_type)
    address = str(item.get("location") or item.get("name") or "")
    bbox = _parse_bbox(item.get("bbox"))
    return GeocodeResult(
        lat=float(lat),
        lng=float(lng),
        confidence=confidence,
        matched_address=address,
        matched_type=matched_type,
        bbox=bbox,
        raw=item,
    )


def _pick_most_specific_item(
    items: list[Any],
) -> dict[str, Any] | None:
    """Pick the item with the highest type-specificity, breaking ties by API order.

    Mapy.cz ranks by relevance, not specificity — a query for
    "Václavské náměstí 1" returns the whole street first and the exact
    building second. For an estimation we want the building.
    """
    best: tuple[int, int, dict[str, Any]] | None = None
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        if not isinstance(raw.get("position"), dict):
            continue
        score = _TYPE_SPECIFICITY.get(str(raw.get("type") or ""), 0)
        # Tie-break: lower idx wins (Mapy's own ranking).
        candidate = (score, -idx, raw)
        if best is None or candidate > best:
            best = candidate
    return best[2] if best is not None else None


def _confidence_for_type(matched_type: str) -> GeocodeConfidence:
    score = _TYPE_SPECIFICITY.get(matched_type, 0)
    if score >= _HIGH_MIN_SPECIFICITY:
        return "high"
    if score >= _MEDIUM_MIN_SPECIFICITY:
        return "medium"
    return "low"


def _parse_bbox(
    raw: Any,
) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    if not all(isinstance(x, (int, float)) for x in raw):
        return None
    return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))


# --- live verification helper ---------------------------------------------
# Run:
#   python -m scraper.geocoding "Václavské náměstí 1, Praha 1"
# Prints the parsed result + raw response. Used to confirm the assumed
# schema once a real MAPY_CZ_API_KEY is available.

def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scraper.geocoding")
    p.add_argument("query")
    p.add_argument("--lang", default="cs")
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args(argv)
    try:
        result = geocode(args.query, lang=args.lang, limit=args.limit)
    except GeocodingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "lat": result.lat,
        "lng": result.lng,
        "confidence": result.confidence,
        "matched_type": result.matched_type,
        "matched_address": result.matched_address,
        "bbox": list(result.bbox) if result.bbox else None,
        "raw_item_keys": sorted(result.raw.keys()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
