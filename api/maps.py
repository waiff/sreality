"""Mapy.cz proxy — autocomplete suggest + RÚIAN code resolve.

The frontend never holds the Mapy.cz API key; it talks to these proxy
routes instead. Suggest responses are cached in-process for a few
minutes since identical queries don't change on that timescale and the
free tier is rate-limited. Resolve responses are NOT cached because
they depend on registry state which can change between deploys.

W3 S3 — THE CHIP PRODUCER SPEAKS RÚIAN. A location chip is a level plus a
RÚIAN code (`api/location_filter.py`), so this module resolves against the ONE
store: the RÚIAN mirror (`ruian_admin_units` + `ruian_admin_unit_geometries`,
migration 381), not `admin_boundaries`. The codes are the same numbers —
`admin_boundaries.id` IS the RÚIAN code (migrations 083/141) — but the mirror
is what `listing_location` answers with, so chip and listing now come from one
registry version.

WHICH LEVELS A POINT CAN RESOLVE TO. The mirror loads polygons for stat,
region_soudrznosti, kraj, okres, orp, pou, obec, spravni_obvod,
katastralni_uzemi and zsj (`location_data/ruian_boundaries.LAYERS`). It draws
NONE for `cast_obce` or `momc` — RÚIAN publishes no part-of-municipality
boundary — so a point can never be PIP'd to a quarter. Therefore:

  * point  -> obec / okres / kraj, by point-in-polygon (the same two-branch
    pip-then-authoritative form the resolver uses, `resolve_db.py`);
  * cast_obce -> BY NAME, against the mirror's name index
    (`ruian_name_index`), narrowed to the obec the point PIP'd into, so
    "Žižkov" can only mean Praha's Žižkov when the pick sits in Praha.

`resolve_names` is the same name lookup without a point: the compatibility
reader for chips stored before codes existed (`upgrade_district_chips`).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from fastapi import HTTPException

from location_data.resolver.normalize import normalize_match_key
from scraper.geocoding import KEY_LEVEL_STATUS, mapy_api_keys

LOG = logging.getLogger(__name__)

# Mapy.cz suggestion `type` → the level we resolve the pick to. obec / okres /
# kraj resolve by point-in-polygon; `municipality_part` resolves to a cast_obce
# CODE by name inside its obec; street / address / POI have no code of their own
# and resolve to their CONTAINING obec. regional.country narrows nothing.
_TYPE_LEVEL: dict[str, str] = {
    "regional.region": "kraj",
    "regional.region.district": "okres",
    "regional.municipality": "obec",
    "regional.municipality_part": "cast_obce",
    "regional.street": "locality",
    "regional.address": "locality",
    "poi": "locality",
}

# The levels a chip may carry a code at, coarsest first (mirrors
# `api.location_filter.LEVEL_ORDER`).
_NAME_LEVELS: tuple[str, ...] = ("kraj", "okres", "obec", "cast_obce")
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


def _api_keys() -> list[str]:
    keys = mapy_api_keys()
    if not keys:
        raise HTTPException(status_code=503, detail="geocoding not configured")
    return keys


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
    keys = _api_keys()
    cache_key = (query, limit, lang)
    now = time.monotonic()
    hit = _suggest_cache.get(cache_key)
    if hit is not None and hit[0] > now:
        return hit[1]
    _cache_evict_expired(now)
    raw = _fetch_suggest(query, limit=limit, lang=lang, keys=keys)
    items = raw.get("items") or []
    payload = {"items": items}
    if len(_suggest_cache) >= _SUGGEST_CACHE_MAX:
        _suggest_cache.pop(next(iter(_suggest_cache)))
    _suggest_cache[cache_key] = (now + _SUGGEST_TTL_SECONDS, payload)
    return payload


def _fetch_suggest(
    query: str, *, limit: int, lang: str, keys: list[str]
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for i, key in enumerate(keys):
        try:
            return _http_get_json(
                MAPY_SUGGEST_URL,
                {"query": query, "limit": limit, "lang": lang, "apikey": key},
            )
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in KEY_LEVEL_STATUS and i < len(keys) - 1:
                LOG.warning(
                    "Mapy suggest key %d/%d rejected (status=%s); failing over to backup",
                    i + 1, len(keys), status,
                )
                continue
            # A rejected key / throttle / outage from Mapy must NOT surface as a raw
            # 500 — the frontend only degrades gracefully (fallback district pickers)
            # on 503, so a 500 leaves a silent empty dropdown. Log the real upstream
            # status for diagnosis and return 503.
            LOG.warning("Mapy suggest upstream failure status=%s: %s", status, exc)
            raise HTTPException(
                status_code=503, detail="geocoding temporarily unavailable",
            ) from exc
    # Unreachable: the last key never `continue`s.
    raise HTTPException(
        status_code=503, detail="geocoding temporarily unavailable",
    ) from last_exc


_CURRENT_VERSION_SQL = "SELECT id FROM registry_versions WHERE is_current LIMIT 1"

# One point, two branches under one LIMIT — the subdivided `pip` pieces first,
# the raw polygon as the partially-loaded-pack fallback. Copied in shape from
# `location_data/resolver/resolve_db._CONTAINING_OBEC_SQL`, whose header records
# why an `IN ('pip','authoritative')` list plans 800× worse than a UNION ALL.
_PIP_BRANCH = """
    SELECT u.id, u.code
      FROM ruian_admin_unit_geometries g
      JOIN ruian_admin_units u ON u.id = g.unit_id
     WHERE g.registry_version_id = %(version)s
       AND g.purpose = '{purpose}'
       AND u.level = 'obec'
       AND ST_Covers(g.geom, ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326))
     LIMIT 1
"""

_CONTAINING_CHAIN_SQL = f"""
WITH RECURSIVE hit AS (
  ({_PIP_BRANCH.format(purpose="pip")})
  UNION ALL
  ({_PIP_BRANCH.format(purpose="authoritative")})
  LIMIT 1
), chain AS (
  SELECT u.id, u.parent_id, u.level::text AS level, u.code, u.name
    FROM ruian_admin_units u JOIN hit h ON h.id = u.id
  UNION ALL
  SELECT p.id, p.parent_id, p.level::text, p.code, p.name
    FROM ruian_admin_units p JOIN chain c ON p.id = c.parent_id
)
SELECT id, level, code, name FROM chain
"""

# The name index, optionally narrowed to one obec. `parent_obec_unit_id` is what
# makes a část obce unambiguous without a polygon. Several rows can come back on
# purpose: "Jihlava" is an obec AND an okres, and a chip that means both is the
# closest code-equality has to the ILIKE-across-four-columns it replaced.
_NAME_LOOKUP_SQL = """
SELECT u.level::text, u.code, u.name
  FROM ruian_name_index n
  JOIN ruian_admin_units u ON u.id = n.entity_id AND u.level = n.entity_kind
 WHERE n.registry_version_id = %(version)s
   AND n.name_norm = %(name_norm)s
   AND u.level::text = ANY(%(levels)s)
   AND u.valid_to IS NULL
   AND (%(obec_unit_id)s::bigint IS NULL OR n.parent_obec_unit_id = %(obec_unit_id)s)
 ORDER BY u.level, u.code
 LIMIT 8
"""


def _registry_version(conn: Any) -> int | None:
    """The current registry version, or None when the mirror is not loaded.

    Not memoised across requests: resolve is a low-frequency endpoint and a
    fresh registry load must light up immediately."""
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.ruian_admin_units') IS NOT NULL")
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            cur.execute(_CURRENT_VERSION_SQL)
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def _containing_chain(
    conn: Any, *, version: int, lat: float, lng: float
) -> dict[str, tuple[int, int, str]]:
    """{level: (unit_id, code, name)} for the obec covering the point and each
    of its ancestors. Empty when the point is outside every obec polygon."""
    with conn.cursor() as cur:
        cur.execute(_CONTAINING_CHAIN_SQL, {"version": version, "lat": lat, "lng": lng})
        rows = cur.fetchall()
    return {
        str(level): (int(unit_id), int(code), name)
        for unit_id, level, code, name in rows
    }


def _lookup_name(
    conn: Any,
    *,
    version: int,
    name: str,
    levels: tuple[str, ...],
    obec_unit_id: int | None = None,
) -> list[tuple[str, int, str]]:
    key = normalize_match_key(name)
    if not key:
        return []
    with conn.cursor() as cur:
        cur.execute(
            _NAME_LOOKUP_SQL,
            {
                "version": version,
                "name_norm": key,
                "levels": list(levels),
                "obec_unit_id": obec_unit_id,
            },
        )
        return [(str(lvl), int(code), nm) for lvl, code, nm in cur.fetchall()]


def _resolve_admin(
    conn: Any, *, version: int, lat: float, lng: float, level: str, name: str | None
) -> dict[str, Any] | None:
    """Resolve a picked point to its RÚIAN code at `level`.

    obec polygons tile the country, so any CZ point resolves and a foreign point
    matches nothing. `cast_obce` has no polygon at any registry version, so it is
    answered by NAME inside the PIP'd obec and falls back to that obec when the
    name is not one of its parts."""
    chain = _containing_chain(conn, version=version, lat=lat, lng=lng)
    obec = chain.get("obec")
    if obec is None:
        return None
    obec_unit_id, obec_code, obec_name = obec
    if level in ("okres", "kraj"):
        hit = chain.get(level)
        if hit is None:
            return None
        return {"level": level, "id": hit[1], "obec_id": obec_code, "name": hit[2]}
    if level == "cast_obce":
        # `cast_obce` ONLY, never `momc`: the answer table stores `cast_obce_kod`
        # and has no městská-část column, so a momc code would be a chip that
        # matches nothing. A Prague quarter picked in Mapy resolves through its
        # část obce or falls through to the obec below.
        matches = (
            _lookup_name(
                conn, version=version, name=name, levels=("cast_obce",),
                obec_unit_id=obec_unit_id,
            )
            if name
            else []
        )
        if matches:
            _lvl, code, nm = matches[0]
            return {"level": "cast_obce", "id": code, "obec_id": obec_code, "name": nm}
        # A quarter the registry does not know as a part of this obec (a Mapy
        # neighbourhood, a colloquial name): narrow to the town rather than
        # inventing a code.
        return {"level": "locality", "id": None, "obec_id": obec_code, "name": None}
    if level == "locality":
        return {"level": "locality", "id": None, "obec_id": obec_code, "name": None}
    return {"level": "obec", "id": obec_code, "obec_id": obec_code, "name": obec_name}


def resolve_names(
    conn: Any, queries: list[tuple[str, str | None]]
) -> dict[tuple[str, str | None], list[tuple[str, int]]]:
    """Resolve `(name, context)` pairs to the (level, code) pairs they name.

    The compatibility reader for chips stored before codes existed. A name can
    legitimately answer at several levels ("Jihlava" is an obec AND an okres) and
    all of them are returned — the ILIKE this replaces matched both. `context`
    (the chip's parent municipality, e.g. "Modřany" in "Praha") narrows the
    answer to parts of that obec when it resolves to one. Unknown names simply
    get no entry; the caller decides what an unresolvable chip means."""
    out: dict[tuple[str, str | None], list[tuple[str, int]]] = {}
    version = _registry_version(conn)
    if version is None or not queries:
        return out
    for name, context in queries:
        if (name, context) in out:
            continue
        obec_unit_id: int | None = None
        if context:
            parents = _lookup_name(
                conn, version=version, name=context, levels=("obec",),
            )
            if parents:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM ruian_admin_units WHERE level = 'obec' "
                        "AND code = %s AND valid_to IS NULL ORDER BY valid_from DESC "
                        "LIMIT 1",
                        (parents[0][1],),
                    )
                    row = cur.fetchone()
                obec_unit_id = int(row[0]) if row else None
        matches = _lookup_name(
            conn, version=version, name=name, levels=_NAME_LEVELS,
            obec_unit_id=obec_unit_id,
        )
        if not matches and obec_unit_id is not None:
            matches = _lookup_name(
                conn, version=version, name=name, levels=_NAME_LEVELS,
            )
        out[(name, context)] = [(lvl, code) for lvl, code, _nm in matches]
    return out


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
    name: str | None = None,
) -> dict[str, Any]:
    """Resolve a picked Mapy.cz suggestion to its RÚIAN code at the picked level.

    `regional_structure` is accepted for request back-compat but unused -- the
    point + type are sufficient, and the PIP is more robust than name-walking
    (it can't confuse an obec with its same-named okres). `name` is the pick's
    own name and is read for ONE level, `cast_obce`, which RÚIAN draws no
    polygon for: the point places the obec, the name places the part inside it.
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
    version = _registry_version(conn)
    if version is not None:
        admin = _resolve_admin(
            conn, version=version, lat=lat, lng=lng, level=level, name=name,
        )

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
