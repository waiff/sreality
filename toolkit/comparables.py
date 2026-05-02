"""find_comparables: spatial + attribute search over `listings` table.

Pure function over a psycopg connection. Builds parameterised SQL
dynamically based on which filters are set; never string-interpolates
user values into the query body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import psycopg


@dataclass(frozen=True)
class TargetSpec:
    lat: float
    lng: float
    area_m2: float | None = None
    disposition: str | None = None
    floor: int | None = None
    exclude_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ComparableFilters:
    radius_m: int = 1000
    area_band_pct: float = 0.20
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    max_age_days: int = 7
    active_only: bool = True
    floor_band: int | None = None
    condition_match: list[str] | None = None
    building_type_match: list[str] | None = None
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    category_main: str | None = "byt"
    category_type: str | None = "pronajem"
    locality_district_id: int | None = None
    include_unreliable: bool = False


_DISPOSITION_LOOSE: dict[str, tuple[str, ...]] = {
    "1+kk": ("1+kk", "1+1"),
    "1+1":  ("1+kk", "1+1"),
    "2+kk": ("2+kk", "2+1"),
    "2+1":  ("2+kk", "2+1"),
    "3+kk": ("3+kk", "3+1"),
    "3+1":  ("3+kk", "3+1"),
    "4+kk": ("4+kk", "4+1"),
    "4+1":  ("4+kk", "4+1"),
    "5+kk": ("5+kk", "5+1"),
    "5+1":  ("5+kk", "5+1"),
}

_HARD_LIMIT = 500


def build_query(
    target: TargetSpec, filters: ComparableFilters
) -> tuple[str, dict[str, Any]]:
    """Render the SQL and parameter dict for the given target+filters.

    Exposed so tests can assert on shape without a DB connection.
    """
    params: dict[str, Any] = {
        "lat": target.lat,
        "lng": target.lng,
        "radius_m": filters.radius_m,
    }
    where: list[str] = [
        "l.geom IS NOT NULL",
        (
            "ST_DWithin("
            "l.geom, "
            "ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography, "
            "%(radius_m)s)"
        ),
    ]

    if filters.active_only:
        where.append("l.is_active = true")
        where.append(
            "l.last_seen_at > now() - make_interval(days => %(max_age_days)s)"
        )
        params["max_age_days"] = filters.max_age_days

    if filters.category_main is not None:
        where.append("l.category_main = %(category_main)s")
        params["category_main"] = filters.category_main
    if filters.category_type is not None:
        where.append("l.category_type = %(category_type)s")
        params["category_type"] = filters.category_type

    if target.disposition is not None:
        if filters.disposition_match == "exact":
            where.append("l.disposition = %(disposition)s")
            params["disposition"] = target.disposition
        elif filters.disposition_match == "loose":
            group = _DISPOSITION_LOOSE.get(
                target.disposition, (target.disposition,)
            )
            where.append("l.disposition = ANY(%(disposition_loose)s)")
            params["disposition_loose"] = list(group)
        # "any": no clause

    if target.area_m2 is not None:
        where.append("l.area_m2 BETWEEN %(area_min)s AND %(area_max)s")
        params["area_min"] = target.area_m2 * (1 - filters.area_band_pct)
        params["area_max"] = target.area_m2 * (1 + filters.area_band_pct)

    if filters.floor_band is not None and target.floor is not None:
        where.append("l.floor BETWEEN %(floor_min)s AND %(floor_max)s")
        params["floor_min"] = target.floor - filters.floor_band
        params["floor_max"] = target.floor + filters.floor_band

    if filters.condition_match:
        where.append("l.condition = ANY(%(condition_match)s)")
        params["condition_match"] = list(filters.condition_match)
    if filters.building_type_match:
        where.append("l.building_type = ANY(%(building_type_match)s)")
        params["building_type_match"] = list(filters.building_type_match)

    if filters.min_price_czk is not None:
        where.append("l.price_czk >= %(min_price_czk)s")
        params["min_price_czk"] = filters.min_price_czk
    if filters.max_price_czk is not None:
        where.append("l.price_czk <= %(max_price_czk)s")
        params["max_price_czk"] = filters.max_price_czk

    if filters.locality_district_id is not None:
        where.append("l.locality_district_id = %(locality_district_id)s")
        params["locality_district_id"] = filters.locality_district_id

    if not filters.include_unreliable:
        where.append(
            "NOT EXISTS ("
            "SELECT 1 FROM listing_fetch_failures lff "
            "WHERE lff.sreality_id = l.sreality_id AND lff.given_up = true"
            ")"
        )

    if target.exclude_ids:
        where.append("l.sreality_id <> ALL(%(exclude_ids)s)")
        params["exclude_ids"] = list(target.exclude_ids)

    sql = (
        "SELECT\n"
        "  l.sreality_id, l.price_czk, l.area_m2,\n"
        "  (l.price_czk::numeric / NULLIF(l.area_m2, 0)) AS price_per_m2,\n"
        "  l.disposition, l.district, l.locality_district_id,\n"
        "  l.floor, l.building_type, l.condition,\n"
        "  ST_Distance(\n"
        "    l.geom,\n"
        "    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography\n"
        "  ) AS distance_m,\n"
        "  l.first_seen_at, l.last_seen_at\n"
        "FROM listings l\n"
        "WHERE " + "\n  AND ".join(where) + "\n"
        "ORDER BY distance_m\n"
        f"LIMIT {_HARD_LIMIT}"
    )
    return sql, params


def _filters_used(target: TargetSpec, filters: ComparableFilters) -> dict[str, Any]:
    return {
        "target": {
            "lat": target.lat,
            "lng": target.lng,
            "area_m2": target.area_m2,
            "disposition": target.disposition,
            "floor": target.floor,
            "exclude_ids": list(target.exclude_ids),
        },
        "radius_m": filters.radius_m,
        "area_band_pct": filters.area_band_pct,
        "disposition_match": filters.disposition_match,
        "max_age_days": filters.max_age_days,
        "active_only": filters.active_only,
        "floor_band": filters.floor_band,
        "condition_match": (
            list(filters.condition_match)
            if filters.condition_match else None
        ),
        "building_type_match": (
            list(filters.building_type_match)
            if filters.building_type_match else None
        ),
        "min_price_czk": filters.min_price_czk,
        "max_price_czk": filters.max_price_czk,
        "category_main": filters.category_main,
        "category_type": filters.category_type,
        "locality_district_id": filters.locality_district_id,
        "include_unreliable": filters.include_unreliable,
    }


def find_comparables(
    conn: "psycopg.Connection",
    target: TargetSpec,
    filters: ComparableFilters,
) -> dict[str, Any]:
    from toolkit import _max_last_seen, _now_iso

    sql, params = build_query(target, filters)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []

    listings = [_row_to_dict(cols, row) for row in rows]
    return {
        "data": {"listings": listings},
        "metadata": {
            "tool": "find_comparables",
            "filters_used": _filters_used(target, filters),
            "result_count": len(listings),
            "queried_at": _now_iso(),
            "data_freshness": _max_last_seen(listings),
        },
    }


def _row_to_dict(cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
    out = dict(zip(cols, row))
    for k in ("first_seen_at", "last_seen_at"):
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    for k in ("area_m2", "price_per_m2", "distance_m"):
        v = out.get(k)
        if v is not None:
            out[k] = float(v)
    return out
