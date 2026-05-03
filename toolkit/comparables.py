"""find_comparables: spatial + attribute search over `listings` table.

Pure function over a psycopg connection. Builds parameterised SQL
dynamically based on which filters are set; never string-interpolates
user values into the query body.

How to add a new filter
-----------------------
1. Add a field to ComparableFilters with a None default and a clear
   type. None must mean "no filter applied".
2. Add a branch in build_query() that appends the WHERE clause and
   binds the value via params[name] = filters.<name>.
3. Add the field to _filters_used() so the metadata block echoes it.
4. Add a hermetic test in test_comparables.py asserting both presence
   when set and absence when None.

That's the entire change. SELECT projection, ORDER BY, LIMIT, and the
spatial / freshness clauses don't need to be touched.
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
    energy_rating_match: list[str] | None = None
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    category_main: str | None = "byt"
    category_type: str | None = "pronajem"
    locality_district_id: int | None = None
    locality_region_id: int | None = None
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
    if filters.energy_rating_match:
        where.append("l.energy_rating = ANY(%(energy_rating_match)s)")
        params["energy_rating_match"] = list(filters.energy_rating_match)

    if filters.has_balcony is not None:
        where.append("l.has_balcony = %(has_balcony)s")
        params["has_balcony"] = filters.has_balcony
    if filters.has_lift is not None:
        where.append("l.has_lift = %(has_lift)s")
        params["has_lift"] = filters.has_lift
    if filters.has_parking is not None:
        where.append("l.has_parking = %(has_parking)s")
        params["has_parking"] = filters.has_parking

    if filters.min_price_czk is not None:
        where.append("l.price_czk >= %(min_price_czk)s")
        params["min_price_czk"] = filters.min_price_czk
    if filters.max_price_czk is not None:
        where.append("l.price_czk <= %(max_price_czk)s")
        params["max_price_czk"] = filters.max_price_czk

    if filters.locality_district_id is not None:
        where.append("l.locality_district_id = %(locality_district_id)s")
        params["locality_district_id"] = filters.locality_district_id
    if filters.locality_region_id is not None:
        where.append("l.locality_region_id = %(locality_region_id)s")
        params["locality_region_id"] = filters.locality_region_id

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
        "  l.disposition, l.district,\n"
        "  l.locality_district_id, l.locality_region_id,\n"
        "  l.floor, l.total_floors,\n"
        "  l.building_type, l.condition, l.energy_rating,\n"
        "  l.has_balcony, l.has_lift, l.has_parking,\n"
        "  ST_Distance(\n"
        "    l.geom,\n"
        "    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography\n"
        "  ) AS distance_m,\n"
        "  l.first_seen_at, l.last_seen_at,\n"
        "  EXTRACT(DAY FROM (now() - l.last_seen_at))::int AS data_age_days,\n"
        "  latest_snap.id AS latest_snapshot_id,\n"
        "  latest_snap.scraped_at AS latest_snapshot_at,\n"
        "  latest_check.checked_at AS last_freshness_check_at\n"
        "FROM listings l\n"
        "LEFT JOIN LATERAL (\n"
        "  SELECT id, scraped_at FROM listing_snapshots\n"
        "  WHERE sreality_id = l.sreality_id\n"
        "  ORDER BY scraped_at DESC LIMIT 1\n"
        ") latest_snap ON true\n"
        "LEFT JOIN LATERAL (\n"
        "  SELECT checked_at FROM listing_freshness_checks\n"
        "  WHERE sreality_id = l.sreality_id\n"
        "  ORDER BY checked_at DESC LIMIT 1\n"
        ") latest_check ON true\n"
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
        "energy_rating_match": (
            list(filters.energy_rating_match)
            if filters.energy_rating_match else None
        ),
        "has_balcony": filters.has_balcony,
        "has_lift": filters.has_lift,
        "has_parking": filters.has_parking,
        "min_price_czk": filters.min_price_czk,
        "max_price_czk": filters.max_price_czk,
        "category_main": filters.category_main,
        "category_type": filters.category_type,
        "locality_district_id": filters.locality_district_id,
        "locality_region_id": filters.locality_region_id,
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
            **_cohort_freshness_stats(listings),
        },
    }


_DATETIME_COLS = (
    "first_seen_at", "last_seen_at",
    "latest_snapshot_at", "last_freshness_check_at",
)


def _row_to_dict(cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
    out = dict(zip(cols, row))
    for k in _DATETIME_COLS:
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    for k in ("area_m2", "price_per_m2", "distance_m"):
        v = out.get(k)
        if v is not None:
            out[k] = float(v)
    return out


def _cohort_freshness_stats(listings: list[dict[str, Any]]) -> dict[str, Any]:
    ages = [
        l["data_age_days"] for l in listings
        if isinstance(l.get("data_age_days"), int)
    ]
    unverified = sum(
        1 for l in listings if l.get("last_freshness_check_at") is None
    )
    if not ages:
        return {
            "oldest_data_age_days": None,
            "newest_data_age_days": None,
            "median_data_age_days": None,
            "unverified_count": unverified,
        }
    sorted_ages = sorted(ages)
    n = len(sorted_ages)
    if n % 2 == 1:
        median = float(sorted_ages[n // 2])
    else:
        median = (sorted_ages[n // 2 - 1] + sorted_ages[n // 2]) / 2.0
    return {
        "oldest_data_age_days": max(ages),
        "newest_data_age_days": min(ages),
        "median_data_age_days": median,
        "unverified_count": unverified,
    }
