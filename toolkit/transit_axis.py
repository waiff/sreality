"""find_comparables_along_axis: comparables in a corridor along transit.

Given an anchor point and a list of transport types (tram / subway /
bus), find route relations that pass near the anchor, then return
listings within `corridor_m` of any of those routes. Per-listing
output includes the closest line and the distance to it, so the
agent can reason about "which line is this comp on".

Reads/writes the OSM-mirror tables `transit_lines` +
`transit_line_fetches` introduced in migration 028, mirroring the
amenity cache discipline in `toolkit/amenities.py`. Cache key is the
canonicalised (bbox, transport_types) pair. On miss we hit Overpass,
write back to the cache, then serve from the cache. The fetch row's
`fetched_at` is what drives the TTL check.

This module is the second toolkit write-allowed exception alongside
`find_anchor_amenities`. The exception is justified by the same
externality rationale: transit-route facts live in OSM, not our
scrape, so caching them locally is necessary for repeated
low-latency lookups. See CLAUDE.md "Toolkit and API rules" #5 and
"Architectural rules" #10.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from scraper.overpass_client import OverpassClient
from toolkit.comparables import (
    ComparableFilters,
    TargetSpec,
    _filters_used,
    _lifecycle_where,
    _shared_filter_where,
)

if TYPE_CHECKING:
    import psycopg


_TRANSPORT_TYPES_ALLOWED: frozenset[str] = frozenset({"tram", "subway", "bus"})

# Hard cap on cohort size so a wide corridor in a dense city can't
# blow up the response.
_HARD_LIMIT = 500


def find_comparables_along_axis(
    conn: "psycopg.Connection",
    target: TargetSpec,
    filters: ComparableFilters,
    transport_types: list[str] | None = None,
    anchor_radius_m: int = 800,
    corridor_m: int = 300,
    cache_ttl_days: int = 30,
    overpass_client: OverpassClient | None = None,
) -> dict[str, Any]:
    """Return comparables inside a transit corridor near the anchor.

    Two-stage spatial filter:
      1. Identify route relations passing within `anchor_radius_m` of
         the anchor (lat/lng on `target`).
      2. From `listings`, return rows within `corridor_m` of any of
         those routes that also satisfy the shared attribute filters.

    `transport_types` defaults to all three (tram, subway, bus). The
    `radius_m` on `filters` is ignored for the spatial query — the
    corridor + anchor_radius pair replaces it — but `filters.radius_m`
    is still echoed in `metadata.filters_used` for traceability.
    """
    if transport_types is None:
        transport_types = ["tram", "subway", "bus"]
    _validate_transport_types(transport_types)
    transport_types = sorted(set(transport_types))

    from toolkit import _max_last_seen, _now_iso

    bbox = _bbox_around(target.lat, target.lng, anchor_radius_m)
    query_hash = _hash_query(bbox, transport_types)

    cache_hit = _cache_is_fresh(conn, query_hash, cache_ttl_days)
    fetched_lines = 0
    if not cache_hit:
        client = overpass_client if overpass_client is not None else OverpassClient()
        elements = client.fetch_routes(
            transport_types,
            bbox["minlat"], bbox["minlng"], bbox["maxlat"], bbox["maxlng"],
        )
        _write_cache(conn, bbox, transport_types, query_hash, elements)
        fetched_lines = len(elements)

    listings, lines_used, line_fetched_max = _query_corridor(
        conn, target, filters, transport_types,
        anchor_radius_m, corridor_m,
    )

    cohort_freshness = _max_last_seen(listings)
    data_freshness = _max_iso(cohort_freshness, line_fetched_max)

    metadata: dict[str, Any] = {
        "tool": "find_comparables_along_axis",
        "filters_used": {
            **_filters_used(target, filters),
            "transport_types":  transport_types,
            "anchor_radius_m":  anchor_radius_m,
            "corridor_m":       corridor_m,
            "cache_ttl_days":   cache_ttl_days,
        },
        "result_count": len(listings),
        "queried_at":   _now_iso(),
        "data_freshness": data_freshness,
        "from_cache":   cache_hit,
        "lines_considered": len(lines_used),
    }
    if not cache_hit:
        metadata["lines_fetched"] = fetched_lines

    return {
        "data": {
            "anchor":   {"lat": target.lat, "lng": target.lng},
            "bbox":     bbox,
            "lines":    lines_used,
            "listings": listings,
        },
        "metadata": metadata,
    }


def _validate_transport_types(transport_types: list[str]) -> None:
    if not transport_types:
        raise ValueError("transport_types must be non-empty")
    unknown = [t for t in transport_types if t not in _TRANSPORT_TYPES_ALLOWED]
    if unknown:
        valid = ", ".join(sorted(_TRANSPORT_TYPES_ALLOWED))
        raise ValueError(
            f"unknown transport_types: {unknown}. valid: {valid}",
        )


def _bbox_around(lat: float, lng: float, radius_m: int) -> dict[str, float]:
    """Latitude/longitude bbox big enough to enclose a circle of radius_m.

    Adds a small padding so a route relation that just clips the
    circle still falls inside the bbox we hand to Overpass.
    """
    pad = 1.05
    delta_lat = (radius_m / 111_000.0) * pad
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    delta_lng = (radius_m / (111_000.0 * cos_lat)) * pad
    return {
        "minlat": round(lat - delta_lat, 6),
        "minlng": round(lng - delta_lng, 6),
        "maxlat": round(lat + delta_lat, 6),
        "maxlng": round(lng + delta_lng, 6),
    }


def _hash_query(bbox: dict[str, float], transport_types: list[str]) -> str:
    """sha256 of the canonicalised (bbox, transport_types) tuple.

    Bbox values are rounded inside `_bbox_around` so two callers using
    the same anchor + radius land on the same hash and share the cache.
    """
    blob = "|".join([
        f"{bbox['minlat']:.6f}",
        f"{bbox['minlng']:.6f}",
        f"{bbox['maxlat']:.6f}",
        f"{bbox['maxlng']:.6f}",
        ",".join(sorted(transport_types)),
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_is_fresh(
    conn: "psycopg.Connection",
    query_hash: str,
    cache_ttl_days: int,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM transit_line_fetches
            WHERE query_hash = %(query_hash)s
              AND fetched_at > now() - make_interval(days => %(ttl)s)
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            {"query_hash": query_hash, "ttl": cache_ttl_days},
        )
        return cur.fetchone() is not None


def _write_cache(
    conn: "psycopg.Connection",
    bbox: dict[str, float],
    transport_types: list[str],
    query_hash: str,
    elements: list[dict[str, Any]],
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        for el in elements:
            coords = el["linestring"]
            wkt = "LINESTRING(" + ", ".join(
                f"{lng} {lat}" for lat, lng in coords
            ) + ")"
            cur.execute(
                """
                INSERT INTO transit_lines
                  (source, source_id, transport_type, route_ref, name,
                   geom, raw_json, fetched_at)
                VALUES (
                  'osm', %(source_id)s, %(transport_type)s,
                  %(route_ref)s, %(name)s,
                  ST_SetSRID(ST_GeomFromText(%(wkt)s), 4326)::geography,
                  %(raw_json)s, now()
                )
                ON CONFLICT (source, source_id) DO UPDATE SET
                  transport_type = EXCLUDED.transport_type,
                  route_ref      = EXCLUDED.route_ref,
                  name           = EXCLUDED.name,
                  geom           = EXCLUDED.geom,
                  raw_json       = EXCLUDED.raw_json,
                  fetched_at     = now()
                """,
                {
                    "source_id":      el["source_id"],
                    "transport_type": el["transport_type"],
                    "route_ref":      el.get("route_ref"),
                    "name":           el.get("name"),
                    "wkt":            wkt,
                    "raw_json":       Jsonb(el.get("tags") or {}),
                },
            )
        cur.execute(
            """
            INSERT INTO transit_line_fetches
              (query_hash, bbox_minlat, bbox_minlng, bbox_maxlat, bbox_maxlng,
               transport_types, source, line_count)
            VALUES (
              %(query_hash)s,
              %(minlat)s, %(minlng)s, %(maxlat)s, %(maxlng)s,
              %(transport_types)s, 'osm', %(count)s
            )
            """,
            {
                "query_hash":      query_hash,
                "minlat":          bbox["minlat"],
                "minlng":          bbox["minlng"],
                "maxlat":          bbox["maxlat"],
                "maxlng":          bbox["maxlng"],
                "transport_types": list(transport_types),
                "count":           len(elements),
            },
        )


def _query_corridor(
    conn: "psycopg.Connection",
    target: TargetSpec,
    filters: ComparableFilters,
    transport_types: list[str],
    anchor_radius_m: int,
    corridor_m: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Return (listings, lines_used, line_fetched_max_iso).

    `listings` rows include `nearest_line_source_id`,
    `nearest_line_transport_type`, `nearest_line_route_ref`, and
    `corridor_distance_m`.
    """
    shared_where, params = _shared_filter_where(target, filters)
    # _shared_filter_where adds a ST_DWithin(l.geom, anchor, radius_m)
    # clause we don't want here — strip it.
    listing_where = [
        w for w in shared_where if "ST_DWithin(l.geom" not in w
    ]
    params.pop("radius_m", None)

    life_where, life_params = _lifecycle_where(
        filters.lifecycle, filters.max_age_days,
    )
    listing_where.extend(life_where)
    params.update(life_params)

    params.update({
        "transport_types":  list(transport_types),
        "anchor_radius_m":  anchor_radius_m,
        "corridor_m":       corridor_m,
    })

    listing_where_sql = "\n  AND ".join(listing_where) if listing_where else "true"

    sql = (
        "WITH near_lines AS (\n"
        "  SELECT id, source_id, transport_type, route_ref, name, geom, fetched_at\n"
        "  FROM transit_lines\n"
        "  WHERE transport_type = ANY(%(transport_types)s)\n"
        "    AND ST_DWithin(\n"
        "      geom,\n"
        "      ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,\n"
        "      %(anchor_radius_m)s\n"
        "    )\n"
        "),\n"
        "candidate AS (\n"
        "  SELECT\n"
        "    l.id AS listing_id, l.sreality_id, l.price_czk, l.area_m2,\n"
        "    (l.price_czk::numeric / NULLIF(l.area_m2, 0)) AS price_per_m2,\n"
        "    l.disposition, l.district,\n"
        "    l.locality_district_id, l.locality_region_id,\n"
        "    l.floor, l.total_floors,\n"
        "    l.building_type, l.condition, l.energy_rating,\n"
        "    l.has_balcony, l.has_lift, l.has_parking,\n"
        "    ST_Distance(\n"
        "      l.geom,\n"
        "      ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography\n"
        "    ) AS distance_m,\n"
        "    l.first_seen_at, l.last_seen_at,\n"
        "    EXTRACT(DAY FROM (now() - l.last_seen_at))::int AS data_age_days,\n"
        "    nl.source_id      AS nearest_line_source_id,\n"
        "    nl.transport_type AS nearest_line_transport_type,\n"
        "    nl.route_ref      AS nearest_line_route_ref,\n"
        "    ST_Distance(l.geom, nl.geom) AS corridor_distance_m,\n"
        # Partition on the surrogate (R2): this window picks each listing's
        # nearest corridor line via `rn = 1`, so partitioning on a column that
        # goes NULL post-Gate-2 would collapse EVERY non-sreality listing into
        # one partition and keep exactly one of them for the whole cohort.
        "    ROW_NUMBER() OVER (\n"
        "      PARTITION BY l.id\n"
        "      ORDER BY ST_Distance(l.geom, nl.geom)\n"
        "    ) AS rn\n"
        "  FROM listings l\n"
        "  JOIN near_lines nl\n"
        "    ON ST_DWithin(l.geom, nl.geom, %(corridor_m)s)\n"
        f"  WHERE {listing_where_sql}\n"
        ")\n"
        "SELECT *\n"
        "FROM candidate\n"
        "WHERE rn = 1\n"
        "ORDER BY corridor_distance_m\n"
        f"LIMIT {_HARD_LIMIT}"
    )

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []

        cur.execute(
            """
            SELECT
              source_id, transport_type, route_ref, name,
              ST_Distance(
                geom,
                ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography
              ) AS distance_m,
              fetched_at
            FROM transit_lines
            WHERE transport_type = ANY(%(transport_types)s)
              AND ST_DWithin(
                geom,
                ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
                %(anchor_radius_m)s
              )
            ORDER BY distance_m
            """,
            {
                "lat":              target.lat,
                "lng":              target.lng,
                "transport_types":  list(transport_types),
                "anchor_radius_m":  anchor_radius_m,
            },
        )
        line_rows = cur.fetchall()
        line_cols = [d[0] for d in cur.description] if cur.description else []

    listings = [_row_to_listing_dict(cols, row) for row in rows]
    lines_used: list[dict[str, Any]] = []
    line_fetched_max: datetime | None = None
    for row in line_rows:
        d = dict(zip(line_cols, row))
        fetched_at = d.get("fetched_at")
        if isinstance(fetched_at, datetime):
            if line_fetched_max is None or fetched_at > line_fetched_max:
                line_fetched_max = fetched_at
            d["fetched_at"] = fetched_at.isoformat()
        lines_used.append({
            "source_id":      d["source_id"],
            "transport_type": d["transport_type"],
            "route_ref":      d.get("route_ref"),
            "name":           d.get("name"),
            "distance_m":     float(d["distance_m"]),
            "fetched_at":     d.get("fetched_at"),
        })

    return listings, lines_used, (
        line_fetched_max.isoformat() if line_fetched_max is not None else None
    )


_DATETIME_COLS = ("first_seen_at", "last_seen_at")


def _row_to_listing_dict(
    cols: list[str], row: tuple[Any, ...],
) -> dict[str, Any]:
    out = dict(zip(cols, row))
    out.pop("rn", None)
    for k in _DATETIME_COLS:
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    for k in ("area_m2", "price_per_m2", "distance_m", "corridor_distance_m"):
        v = out.get(k)
        if v is not None:
            out[k] = float(v)
    return out


def _max_iso(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b
