"""Deep link into sreality's Cenová mapa (sold apartment prices) for one place.

The price map addresses places by SEZNAM's own locality ids, not RÚIAN codes:
`/cenova-mapa/hledani/byty/<kraj>-<id>/<okres>-<id>/<obec>-<id>`, with a street
as `?ulice=<street>-<id>` and a part of a town as one more path segment. The map
never writes a viewport into the URL, so a coordinate alone cannot address it.
The ids come from sreality's public locality suggest, which carries the whole
chain (region / district / municipality / street, seo names included) — but
sends no CORS header, so the browser cannot ask it and this proxy does.

HOW A LISTING BECOMES A QUERY. The SPA sends the listing's `display_label`
(`location_display_label`, migration 503: "Street 534/2, Obec", "Část, Obec" or
"Obec") and its point. The house number is cut first — with it the suggest finds
nothing. Then two asks, most specific first: "street-or-part, obec", then "obec".

WHY NEAREST. Suggest is a text search; "Nová Ves" alone returns five different
municipalities in five districts. Every candidate carries its own coordinate, so
the pick is the one nearest the listing's point, inside a cap per level — a hit
past the cap is somebody else's place, and no link beats a wrong one (the SPA
falls back to the bare national map).

Only `byty` exists as a path category (`domy`/`pozemky` 404, measured), so a
house or plot still opens the apartment map of its place.
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any

import requests
from fastapi import HTTPException

LOG = logging.getLogger(__name__)

SUGGEST_URL = "https://www.sreality.cz/api/v1/localities/suggest"
PRICE_MAP_BASE = "https://www.sreality.cz/cenova-mapa/hledani/byty"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# How far a candidate's own coordinate may sit from the listing and still be its
# place. A street's sits on the street (3 km covers a long one); a part of town's
# and a town's sit at their centre (5 km; 25 km covers Praha's edge).
_CAP_M: dict[str, float] = {"street": 3_000, "ward": 5_000, "municipality": 25_000}

# "534", "534/2", "12a", "410/32b" — a trailing house number, never a word, so
# "28. října" and "Třída 1. máje" keep their digits.
_HOUSE_NUMBER = re.compile(r"\s+\d+[a-zA-Z]?(?:/\d+[a-zA-Z]?)?$")

_CACHE_TTL_SECONDS = 24 * 3600
_CACHE_MAX = 2048
_cache: dict[tuple[str, float, float], tuple[float, dict[str, Any]]] = {}


def clear_cache() -> None:
    _cache.clear()


def _http_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def split_label(label: str) -> tuple[str | None, str | None]:
    """(street-or-part without house number, obec) from a display label."""
    head, sep, obec = label.strip().rpartition(",")
    if not sep:
        return None, label.strip() or None
    head = _HOUSE_NUMBER.sub("", head.strip()).strip()
    return head or None, obec.strip() or None


def _suggest(phrase: str, categories: str) -> list[dict[str, Any]]:
    try:
        raw = _http_get_json(
            SUGGEST_URL,
            {"phrase": phrase, "category": categories, "lang": "cs", "limit": 10},
        )
    except (requests.RequestException, ValueError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        LOG.warning("sreality suggest upstream failure status=%s: %s", status, exc)
        raise HTTPException(
            status_code=503, detail="sreality suggest temporarily unavailable",
        ) from exc
    return [r.get("userData") or {} for r in raw.get("results") or []]


def _slug_id(seo: Any, id_: Any) -> str | None:
    if not seo or not isinstance(id_, int) or id_ <= 0:
        return None
    return f"{seo}-{id_}"


def _municipality_path(u: dict[str, Any]) -> str | None:
    parts = [
        _slug_id(u.get("region_seo_name"), u.get("region_id")),
        _slug_id(u.get("district_seo_name"), u.get("district_id")),
        _slug_id(u.get("municipality_seo_name"), u.get("municipality_id")),
    ]
    if any(p is None for p in parts):
        return None
    return f"{PRICE_MAP_BASE}/{'/'.join(parts)}"  # type: ignore[arg-type]


def build_url(u: dict[str, Any]) -> str | None:
    """The price-map URL for one suggest result, or None if its chain is broken."""
    base = _municipality_path(u)
    if base is None:
        return None
    kind = u.get("entityType")
    if kind == "street":
        street = _slug_id(u.get("street_seo_name"), u.get("street_id"))
        return f"{base}?ulice={street}" if street else None
    if kind == "ward":
        ward = _slug_id(u.get("ward_seo_name"), u.get("ward_id"))
        return f"{base}/{ward}" if ward else None
    if kind == "municipality":
        return base
    return None


def _nearest(
    candidates: list[dict[str, Any]], lat: float, lng: float,
) -> tuple[dict[str, Any], str] | None:
    best: tuple[float, dict[str, Any], str] | None = None
    for u in candidates:
        cap_m = _CAP_M.get(u.get("entityType"))  # type: ignore[arg-type]
        if cap_m is None:
            continue
        c_lat, c_lng = u.get("latitude"), u.get("longitude")
        if not isinstance(c_lat, (int, float)) or not isinstance(c_lng, (int, float)):
            continue
        url = build_url(u)
        if url is None:
            continue
        d = _distance_m(lat, lng, c_lat, c_lng)
        if d <= cap_m and (best is None or d < best[0]):
            best = (d, u, url)
    return (best[1], best[2]) if best else None


def resolve(label: str, lat: float, lng: float) -> dict[str, Any]:
    """{url, level, name} for the most specific place near the point; url None if none."""
    key = (label.strip(), round(lat, 4), round(lng, 4))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]

    part, obec = split_label(label)
    result: dict[str, Any] = {"url": None, "level": None, "name": None}
    found = None
    if part and obec:
        found = _nearest(_suggest(f"{part}, {obec}", "street_cz,ward_cz"), lat, lng)
    if found is None and obec:
        found = _nearest(_suggest(obec, "municipality_cz"), lat, lng)
    if found is not None:
        u, url = found
        result = {
            "url": url,
            "level": u["entityType"],
            "name": u.get("suggestFirstRow") or u.get("municipality"),
        }

    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (now + _CACHE_TTL_SECONDS, result)
    return result
