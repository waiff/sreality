"""Mapy.cz proxy — autocomplete suggest + admin-unit resolve.

The frontend never holds the Mapy.cz API key; it talks to these proxy
routes instead. Suggest responses are cached in-process for a few
minutes since identical queries don't change on that timescale and the
free tier is rate-limited. Resolve responses are NOT cached because
they depend on `admin_boundaries` state which can change between
deploys.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests
from fastapi import HTTPException

LOG = logging.getLogger(__name__)

# Mapy.cz suggestion `type` → the admin level we resolve the pick to. obec /
# okres / kraj resolve to that admin id; everything sub-municipal (street /
# address / POI / část obce) resolves to its CONTAINING obec and keeps the place
# name for a locality-text narrow. regional.country narrows nothing.
_TYPE_LEVEL: dict[str, str] = {
    "regional.region": "kraj",
    "regional.region.district": "okres",
    "regional.municipality": "obec",
    "regional.municipality_part": "locality",
    "regional.street": "locality",
    "regional.address": "locality",
    "poi": "locality",
}
# Default circle radius (metres) per type — only the point_with_radius fallback
# used when a point resolves to no admin polygon (foreign / off-grid points).
_TYPE_RADIUS: dict[str, int] = {
    "regional.country": 50000,
    "regional.region": 25000,
    "regional.region.district": 15000,
    "regional.municipality": 5000,
    "regional.municipality_part": 2000,
    "regional.street": 500,
    "regional.address": 300,
    "poi": 500,
}
_DEFAULT_RADIUS_M = 1500

MAPY_SUGGEST_URL = "https://api.mapy.cz/v1/suggest"
_SUGGEST_TTL_SECONDS = 300
_SUGGEST_CACHE_MAX = 256

_suggest_cache: dict[tuple[str, int, str], tuple[float, dict[str, Any]]] = {}


def _api_key() -> str:
    key = os.environ.get("MAPY_CZ_API_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="geocoding not configured")
    return key


def _http_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _cache_evict_expired(now: float) -> None:
    for k, (exp, _) in list(_suggest_cache.items()):
        if exp <= now:
            del _suggest_cache[k]


def clear_suggest_cache() -> None:
    _suggest_cache.clear()


def suggest(query: str, *, limit: int = 10, lang: str = "cs") -> dict[str, Any]:
    key = _api_key()
    cache_key = (query, limit, lang)
    now = time.monotonic()
    hit = _suggest_cache.get(cache_key)
    if hit is not None and hit[0] > now:
        return hit[1]
    _cache_evict_expired(now)
    try:
        raw = _http_get_json(
            MAPY_SUGGEST_URL,
            {"query": query, "limit": limit, "lang": lang, "apikey": key},
        )
    except (requests.RequestException, ValueError) as exc:
        # A rejected key / throttle / outage from Mapy must NOT surface as a raw
        # 500 — the frontend only degrades gracefully (fallback district pickers)
        # on 503, so a 500 leaves a silent empty dropdown. Log the real upstream
        # status for diagnosis and return 503.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        LOG.warning("Mapy suggest upstream failure status=%s: %s", status, exc)
        raise HTTPException(
            status_code=503, detail="geocoding temporarily unavailable",
        ) from exc
    items = raw.get("items") or []
    payload = {"items": items}
    if len(_suggest_cache) >= _SUGGEST_CACHE_MAX:
        _suggest_cache.pop(next(iter(_suggest_cache)))
    _suggest_cache[cache_key] = (now + _SUGGEST_TTL_SECONDS, payload)
    return payload


def _admin_boundaries_present(conn: Any) -> bool:
    """True iff the `public.admin_boundaries` table exists and has at least one row.

    Cached per-request via the `conn`; resolve is a low-frequency endpoint
    so we don't memoise across requests — `map-1` shipping changes the
    answer and we want it to light up immediately.
    """
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.admin_boundaries') IS NOT NULL")
            row = cur.fetchone()
            if not row or not row[0]:
                return False
            cur.execute("SELECT EXISTS (SELECT 1 FROM admin_boundaries LIMIT 1)")
            row = cur.fetchone()
            return bool(row and row[0])
    except Exception:
        return False


def _resolve_admin(
    conn: Any, *, lat: float, lng: float, level: str
) -> dict[str, Any] | None:
    """Resolve a picked point to its admin id at `level`.

    One st_covers PIP into admin_boundaries at the obec level + a parent_id walk
    to okres / kraj -- the SAME derivation the listings admin-geo trigger uses
    (migration 140), so the chip's id equals what a listing at this point gets
    and `id = id` matching is exact. obec polygons tile the country, so any CZ
    point resolves; a foreign point matches no obec and returns None.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ob.id, ob.name, ok.id, ok.name, kr.id, kr.name "
            "FROM admin_boundaries ob "
            "LEFT JOIN admin_boundaries ok ON ok.id = ob.parent_id AND ok.level = 'okres' "
            "LEFT JOIN admin_boundaries kr ON kr.id = ok.parent_id AND kr.level = 'kraj' "
            "WHERE ob.level = 'obec' "
            "AND st_covers(ob.geom, st_setsrid(st_makepoint(%s, %s), 4326)) "
            "LIMIT 1",
            (lng, lat),
        )
        row = cur.fetchone()
    if not row:
        return None
    obec_id, obec_name, okres_id, okres_name, kraj_id, kraj_name = row
    obec_id_i = int(obec_id) if obec_id is not None else None
    if level == "okres":
        if okres_id is None:
            return None
        return {"level": "okres", "id": int(okres_id), "obec_id": obec_id_i, "name": okres_name}
    if level == "kraj":
        if kraj_id is None:
            return None
        return {"level": "kraj", "id": int(kraj_id), "obec_id": obec_id_i, "name": kraj_name}
    if level == "locality":
        return {"level": "locality", "id": None, "obec_id": obec_id_i, "name": None}
    return {"level": "obec", "id": obec_id_i, "obec_id": obec_id_i, "name": obec_name}


def _radius_for_type(type_: str | None) -> int:
    return _TYPE_RADIUS.get(type_ or "", _DEFAULT_RADIUS_M)


def resolve(
    conn: Any,
    *,
    label: str,
    lat: float | None,
    lng: float | None,
    type_: str | None = None,
    regional_structure: list[dict[str, Any]] | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a picked Mapy.cz suggestion to a stable admin id at the picked level.

    `regional_structure` is accepted for request back-compat but unused -- the
    point + type are sufficient, and the PIP is more robust than name-walking
    (it can't confuse an obec with its same-named okres).
    """
    base = {
        "label": label,
        "lat": float(lat) if lat is not None else None,
        "lng": float(lng) if lng is not None else None,
        "level": None,
        "id": None,
        "obec_id": None,
        "name": None,
        "default_radius_m": _radius_for_type(type_),
        "raw": raw or {},
    }

    level = _TYPE_LEVEL.get(type_ or "")
    if lat is None or lng is None or level is None:
        # No position, or a country-level / unknown pick -> no admin narrowing.
        return {**base, "kind": "unresolved"}

    admin = None
    if _admin_boundaries_present(conn):
        admin = _resolve_admin(conn, lat=lat, lng=lng, level=level)

    if admin is None:
        # In-CZ point that matched no obec polygon (foreign / boundary gap):
        # fall back to a point + radius so the map can still focus the area.
        return {**base, "kind": "point_with_radius"}

    return {
        **base,
        "kind": "locality" if admin["level"] == "locality" else "admin",
        "level": admin["level"],
        "id": admin["id"],
        "obec_id": admin["obec_id"],
        "name": admin["name"],
    }
