"""Mapy.cz proxy — autocomplete suggest + admin-unit resolve.

The frontend never holds the Mapy.cz API key; it talks to these proxy
routes instead. Suggest responses are cached in-process for a few
minutes since identical queries don't change on that timescale and the
free tier is rate-limited. Resolve responses are NOT cached because
they depend on `admin_boundaries` state which can change between
deploys.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests
from fastapi import HTTPException

# Mapy.cz suggestion `type` strings → (admin level if applicable, default radius in metres)
# for the point_with_radius fallback. Levels match `admin_boundaries.level`
# values planned for `map-1` (obec / okres / kraj / ku).
_TYPE_RULES: dict[str, tuple[str | None, int]] = {
    "regional.country": (None, 50000),
    "regional.region": ("kraj", 25000),
    "regional.municipality": ("obec", 5000),
    "regional.municipality_part": ("ku", 2000),
    "regional.street": (None, 500),
    "regional.address": (None, 300),
    "poi": (None, 500),
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
    raw = _http_get_json(
        MAPY_SUGGEST_URL,
        {"query": query, "limit": limit, "lang": lang, "apikey": key},
    )
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


def _match_polygon(
    conn: Any, regional_structure: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """Walk regionalStructure most-specific → least-specific, return first hit."""
    if not regional_structure:
        return None
    with conn.cursor() as cur:
        for item in regional_structure:
            type_ = item.get("type")
            name = item.get("name")
            if not type_ or not name:
                continue
            level = _TYPE_RULES.get(type_, (None, 0))[0]
            if level is None:
                continue
            cur.execute(
                "SELECT id, name FROM admin_boundaries "
                "WHERE level = %s AND name = %s LIMIT 1",
                (level, name),
            )
            row = cur.fetchone()
            if row:
                return {"level": level, "id": int(row[0]), "name": row[1]}
    return None


def _radius_for_type(type_: str | None) -> int:
    return _TYPE_RULES.get(type_ or "", (None, _DEFAULT_RADIUS_M))[1]


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
    """Resolve a picked Mapy.cz suggestion to an admin polygon or a point+radius."""
    if lat is None or lng is None:
        return {
            "kind": "unresolved",
            "label": label,
            "lat": None,
            "lng": None,
            "polygon": None,
            "default_radius_m": _DEFAULT_RADIUS_M,
            "raw": raw or {},
        }

    polygon = None
    if _admin_boundaries_present(conn):
        polygon = _match_polygon(conn, regional_structure)

    default_radius = _radius_for_type(type_)
    return {
        "kind": "admin_polygon" if polygon else "point_with_radius",
        "label": label,
        "lat": float(lat),
        "lng": float(lng),
        "polygon": polygon,
        "default_radius_m": default_radius,
        "raw": raw or {},
    }
