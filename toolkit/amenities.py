"""find_anchor_amenities: enumerate POIs near a point, by category.

Reads the OSM cache (`amenities` + `amenity_fetches`) when fresh; falls
back to a live Overpass call and writes the result back into the cache
on miss. Per-category lookup so a partial cache (one category fresh,
another stale) only re-fetches the stale categories.

This is the second toolkit function permitted to write, after
`verify_listing_freshness`. The exception is justified by data
externality: amenity facts live in OSM, not our scrape, so caching
them locally is necessary for repeated low-latency lookup. The cache
is purely an OSM mirror — no derived analytical state.

`data_freshness` in the returned envelope reflects the maximum of
`amenities.fetched_at` across returned rows (POIs have no
`last_seen_at`).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from scraper.overpass_client import OverpassClient

if TYPE_CHECKING:
    import psycopg


# OR across the list, AND within each dict. value=True means "key
# present, any value". See CLAUDE.md "Toolkit and API rules" for the
# decision log behind each entry.
CATEGORY_TAGS: dict[str, list[dict[str, str | bool]]] = {
    "tram_stop": [
        {"railway": "tram_stop"},
        {"public_transport": "stop_position", "tram": "yes"},
    ],
    "metro_station": [
        {"railway": "station", "station": "subway"},
        {"station": "subway"},
    ],
    "bus_stop": [
        {"highway": "bus_stop"},
    ],
    "supermarket": [
        {"shop": "supermarket"},
    ],
    "convenience": [
        {"shop": "convenience"},
    ],
    "pharmacy": [
        {"amenity": "pharmacy"},
    ],
    "school_primary": [
        {"amenity": "school", "isced:level": "1"},
        {"amenity": "school", "isced:level": "1;2"},
        {"amenity": "school", "isced:level": "1,2"},
        {"amenity": "school", "school:CZ": "zakladni"},
    ],
    "kindergarten": [
        {"amenity": "kindergarten"},
    ],
    "park": [
        {"leisure": "park"},
    ],
    "restaurant": [
        {"amenity": "restaurant"},
    ],
}

# Categories whose density makes them better as a count-signal than an
# itemized list. Triggers a notes[] entry when count exceeds threshold.
_DENSITY_WARN_THRESHOLD = 200


def find_anchor_amenities(
    conn: "psycopg.Connection",
    lat: float,
    lng: float,
    radius_m: int = 1000,
    categories: list[str] | None = None,
    cache_ttl_days: int = 30,
    overpass_client: OverpassClient | None = None,
) -> dict[str, Any]:
    from toolkit import _now_iso

    requested = categories if categories is not None else list(CATEGORY_TAGS)
    unknown = [c for c in requested if c not in CATEGORY_TAGS]
    if unknown:
        valid = ", ".join(sorted(CATEGORY_TAGS))
        raise ValueError(
            f"unknown categories: {unknown}. valid: {valid}",
        )

    client = overpass_client  # may be None; lazily instantiated on miss
    by_category: dict[str, dict[str, Any]] = {}
    from_cache: dict[str, bool] = {}
    notes: list[str] = []
    fetched_at_max: datetime | None = None
    total_rows = 0

    for category in requested:
        is_hit = _cache_is_fresh(conn, lat, lng, radius_m, category, cache_ttl_days)
        if not is_hit:
            if client is None:
                client = OverpassClient()
            elements = client.fetch(
                CATEGORY_TAGS[category], lat, lng, radius_m,
            )
            _write_cache(conn, lat, lng, radius_m, category, elements)

        rows = _read_amenities(conn, lat, lng, radius_m, category)
        from_cache[category] = is_hit
        for r in rows:
            if r["fetched_at"] is not None and (
                fetched_at_max is None or r["fetched_at"] > fetched_at_max
            ):
                fetched_at_max = r["fetched_at"]

        nearest = rows[0]["distance_m"] if rows else None
        items = [
            {
                "name": r["name"],
                "lat": r["lat"],
                "lng": r["lng"],
                "distance_m": round(r["distance_m"], 1),
                "source_id": r["source_id"],
                "fetched_at": (
                    r["fetched_at"].isoformat()
                    if r["fetched_at"] is not None else None
                ),
            }
            for r in rows
        ]
        by_category[category] = {
            "count": len(rows),
            "nearest_distance_m": (
                round(nearest, 1) if nearest is not None else None
            ),
            "items": items,
        }
        total_rows += len(rows)
        if len(rows) > _DENSITY_WARN_THRESHOLD:
            notes.append(
                f"category '{category}' returned {len(rows)} items; "
                f"treat as density signal, not single-anchor",
            )

    metadata: dict[str, Any] = {
        "tool": "find_anchor_amenities",
        "filters_used": {
            "lat": lat,
            "lng": lng,
            "radius_m": radius_m,
            "categories": requested,
            "cache_ttl_days": cache_ttl_days,
        },
        "result_count": total_rows,
        "queried_at": _now_iso(),
        "data_freshness": (
            fetched_at_max.isoformat() if fetched_at_max is not None else None
        ),
    }
    if notes:
        metadata["notes"] = notes

    return {
        "data": {
            "center": {"lat": lat, "lng": lng},
            "radius_m": radius_m,
            "categories": by_category,
            "from_cache": from_cache,
        },
        "metadata": metadata,
    }


def _cache_is_fresh(
    conn: "psycopg.Connection",
    lat: float,
    lng: float,
    radius_m: int,
    category: str,
    cache_ttl_days: int,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM amenity_fetches
            WHERE category = %(category)s
              AND radius_m = %(radius_m)s
              AND ST_DWithin(
                center_geom,
                ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
                1.0
              )
              AND fetched_at > now() - make_interval(days => %(ttl)s)
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            {
                "category": category, "radius_m": radius_m,
                "lat": lat, "lng": lng, "ttl": cache_ttl_days,
            },
        )
        return cur.fetchone() is not None


def _write_cache(
    conn: "psycopg.Connection",
    lat: float,
    lng: float,
    radius_m: int,
    category: str,
    elements: list[dict[str, Any]],
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        for el in elements:
            cur.execute(
                """
                INSERT INTO amenities
                  (source, source_id, category, name, geom, raw_json, fetched_at)
                VALUES (
                  'osm', %(source_id)s, %(category)s, %(name)s,
                  ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
                  %(raw_json)s, now()
                )
                ON CONFLICT (source, source_id) DO UPDATE SET
                  category = EXCLUDED.category,
                  name     = EXCLUDED.name,
                  geom     = EXCLUDED.geom,
                  raw_json = EXCLUDED.raw_json,
                  fetched_at = now()
                """,
                {
                    "source_id": el["source_id"],
                    "category": category,
                    "name": el["name"],
                    "lat": el["lat"],
                    "lng": el["lng"],
                    "raw_json": Jsonb(el["tags"]),
                },
            )
        cur.execute(
            """
            INSERT INTO amenity_fetches
              (center_geom, radius_m, category, source, amenity_count)
            VALUES (
              ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
              %(radius_m)s, %(category)s, 'osm', %(count)s
            )
            """,
            {
                "lat": lat, "lng": lng,
                "radius_m": radius_m, "category": category,
                "count": len(elements),
            },
        )


def _read_amenities(
    conn: "psycopg.Connection",
    lat: float,
    lng: float,
    radius_m: int,
    category: str,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              source_id,
              name,
              ST_Y(geom::geometry) AS lat,
              ST_X(geom::geometry) AS lng,
              ST_Distance(
                geom,
                ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography
              ) AS distance_m,
              fetched_at
            FROM amenities
            WHERE category = %(category)s
              AND ST_DWithin(
                geom,
                ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
                %(radius_m)s
              )
            ORDER BY distance_m
            """,
            {
                "category": category,
                "lat": lat, "lng": lng,
                "radius_m": radius_m,
            },
        )
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]
