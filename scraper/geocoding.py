"""Mapy.cz geocoding wrapper.

Used by the source-kind dispatcher when a non-sreality listing's HTML
gives a locality string but no coordinates. Uses Mapy.cz over Google /
OSM for first-party Czech address coverage.

# Assumed schema (NEEDS LIVE CONFIRMATION)

The api.mapy.com developer portal blocks server-side fetches from this
sandbox, so the response shape below is reconstructed from public
references. The first call to `geocode()` against a real key will
either match this shape or raise GeocodingError("malformed response").
Run `python -m scraper.geocoding "Václavské náměstí 1, Praha 1"` to
verify; the helper prints the parsed result and the raw response.

Endpoint  : GET https://api.mapy.com/v1/geocode
Auth      : apikey query param
Params    : query (str, required), apikey, lang ("cs" default), limit (int, default 5)
Response  : {
              "items": [
                {
                  "name":    "Václavské náměstí",
                  "label":   "Václavské náměstí, 110 00 Praha 1",
                  "position": {"lon": 14.4283, "lat": 50.0810},
                  "type":     "regional.address" |
                              "regional.street" |
                              "regional.municipality_part" |
                              "regional.municipality" |
                              "regional.region" |
                              "poi",
                  "regionalStructure": [...],
                  "zip":      "110 00"
                },
                ...
              ],
              "locality": "..."
            }

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

_HIGH_TYPES = frozenset({"regional.address"})
_MEDIUM_TYPES = frozenset({
    "regional.street",
    "regional.municipality_part",
    "regional.municipality",
})


class GeocodingError(RuntimeError):
    """Raised when Mapy.cz returns no usable result for a query."""


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lng: float
    confidence: GeocodeConfidence
    matched_label: str
    matched_type: str
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
    item = items[0]
    if not isinstance(item, dict):
        raise GeocodingError("Mapy.cz returned malformed item")
    position = item.get("position") or {}
    lat = position.get("lat")
    lng = position.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        raise GeocodingError("Mapy.cz item missing lat/lon")
    matched_type = str(item.get("type") or "")
    confidence = _confidence_for_type(matched_type)
    label = str(item.get("label") or item.get("name") or "")
    return GeocodeResult(
        lat=float(lat),
        lng=float(lng),
        confidence=confidence,
        matched_label=label,
        matched_type=matched_type,
        raw=item,
    )


def _confidence_for_type(matched_type: str) -> GeocodeConfidence:
    if matched_type in _HIGH_TYPES:
        return "high"
    if matched_type in _MEDIUM_TYPES:
        return "medium"
    return "low"


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
        "matched_label": result.matched_label,
        "raw_item_keys": sorted(result.raw.keys()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
