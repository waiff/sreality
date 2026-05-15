"""describe_neighborhood: dispositional/price/condition profile of a radius.

One spatial query (CTE-driven, jsonb-aggregated) over `listings`, returning
aggregate counts, per-disposition price percentiles, and trend counts.
Pure read.

Active filter follows the established pattern: is_active = true AND
last_seen_at > now() - max_age_days. Trend counts use first_seen_at and
last_seen_at directly on the unfiltered base set; "becoming inactive"
means a listing flipped to is_active=false within the time window.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg


_HARD_CAP = 5000


def build_query(
    lat: float,
    lng: float,
    radius_m: int,
    max_age_days: int | None,
    category_main: str | None,
    category_type: str | None,
) -> tuple[str, dict[str, Any]]:
    """Render the SQL and parameter dict for the given inputs.

    Exposed so tests can assert on shape without a DB connection.
    max_age_days=None drops the freshness gate; `active` becomes
    "is_active=true" with no last_seen_at bound.
    """
    params: dict[str, Any] = {
        "lat": lat,
        "lng": lng,
        "radius_m": radius_m,
    }
    cat_clauses: list[str] = []
    if category_main is not None:
        cat_clauses.append("AND l.category_main = %(category_main)s")
        params["category_main"] = category_main
    if category_type is not None:
        cat_clauses.append("AND l.category_type = %(category_type)s")
        params["category_type"] = category_type
    cat_sql = "\n    ".join(cat_clauses)

    if max_age_days is not None:
        age_clause = "AND last_seen_at > now() - make_interval(days => %(max_age_days)s)"
        params["max_age_days"] = max_age_days
    else:
        age_clause = ""

    sql = f"""
WITH base AS (
  SELECT
    l.sreality_id, l.is_active, l.first_seen_at, l.last_seen_at,
    l.disposition, l.building_type, l.condition,
    l.price_czk, l.area_m2,
    EXTRACT(DAY FROM (now() - l.last_seen_at))::int AS data_age_days
  FROM listings l
  WHERE l.geom IS NOT NULL
    AND ST_DWithin(
      l.geom,
      ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
      %(radius_m)s
    )
    {cat_sql}
    AND NOT EXISTS (
      SELECT 1 FROM listing_fetch_failures lff
      WHERE lff.sreality_id = l.sreality_id AND lff.given_up = true
    )
),
active AS (
  SELECT * FROM base
  WHERE is_active = true
    {age_clause}
),
disposition_mix AS (
  SELECT coalesce(disposition, 'unknown') AS k, count(*) AS n
  FROM active GROUP BY 1
),
building_mix AS (
  SELECT coalesce(building_type, 'unknown') AS k, count(*) AS n
  FROM active GROUP BY 1
),
condition_mix AS (
  SELECT coalesce(condition, 'unknown') AS k, count(*) AS n
  FROM active GROUP BY 1
),
price_stats AS (
  SELECT
    disposition AS d,
    count(*) AS n,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY price_czk)::float AS median_price_czk,
    percentile_cont(0.5) WITHIN GROUP (
      ORDER BY (price_czk::numeric / NULLIF(area_m2, 0))
    )::float AS median_pp,
    percentile_cont(0.25) WITHIN GROUP (
      ORDER BY (price_czk::numeric / NULLIF(area_m2, 0))
    )::float AS p25_pp,
    percentile_cont(0.75) WITHIN GROUP (
      ORDER BY (price_czk::numeric / NULLIF(area_m2, 0))
    )::float AS p75_pp,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY area_m2)::float AS median_area
  FROM active
  WHERE disposition IS NOT NULL
    AND price_czk IS NOT NULL
    AND area_m2 IS NOT NULL
    AND area_m2 > 0
  GROUP BY disposition
  HAVING count(*) >= 5
)
SELECT
  (SELECT count(*) FROM active)::int AS active_count,
  coalesce((SELECT jsonb_object_agg(k, n) FROM disposition_mix), '{{}}'::jsonb)
    AS disposition_counts,
  coalesce((SELECT jsonb_object_agg(k, n) FROM building_mix), '{{}}'::jsonb)
    AS building_counts,
  coalesce((SELECT jsonb_object_agg(k, n) FROM condition_mix), '{{}}'::jsonb)
    AS condition_counts,
  coalesce((
    SELECT jsonb_agg(jsonb_build_object(
      'disposition', d,
      'n', n,
      'median_price_czk', median_price_czk,
      'median_price_per_m2', median_pp,
      'p25_price_per_m2', p25_pp,
      'p75_price_per_m2', p75_pp,
      'median_area_m2', median_area
    )) FROM price_stats
  ), '[]'::jsonb) AS price_stats_list,
  (SELECT count(*) FROM base
    WHERE first_seen_at > now() - interval '7 days')::int AS new_7d,
  (SELECT count(*) FROM base
    WHERE first_seen_at > now() - interval '30 days')::int AS new_30d,
  (SELECT count(*) FROM base
    WHERE NOT is_active
      AND last_seen_at > now() - interval '7 days')::int AS inactive_7d,
  (SELECT count(*) FROM base
    WHERE NOT is_active
      AND last_seen_at > now() - interval '30 days')::int AS inactive_30d,
  (SELECT max(data_age_days) FROM active)::int AS oldest_data_age_days,
  (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY data_age_days)::float
   FROM active) AS median_data_age_days,
  (SELECT max(last_seen_at) FROM active) AS max_last_seen
"""
    return sql, params


def describe_neighborhood(
    conn: "psycopg.Connection",
    lat: float,
    lng: float,
    radius_m: int = 1000,
    max_age_days: int | None = None,
    category_main: str | None = "byt",
    category_type: str | None = "pronajem",
) -> dict[str, Any]:
    from toolkit import _now_iso

    sql, params = build_query(
        lat, lng, radius_m, max_age_days, category_main, category_type,
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if cur.description else []

    rec: dict[str, Any] = dict(zip(cols, row)) if row else {}

    active_count = int(rec.get("active_count") or 0)
    radius_km = radius_m / 1000.0
    area_km2 = math.pi * radius_km * radius_km
    density = active_count / area_km2 if area_km2 > 0 else 0.0

    disposition_mix = _to_fractions(
        rec.get("disposition_counts") or {}, active_count
    )
    building_type_mix = _to_fractions(
        rec.get("building_counts") or {}, active_count
    )
    condition_mix = _to_fractions(
        rec.get("condition_counts") or {}, active_count
    )

    price_stats = {
        s["disposition"]: {
            "n": int(s["n"]),
            "median_price_czk": (
                int(round(s["median_price_czk"]))
                if s.get("median_price_czk") is not None else None
            ),
            "median_price_per_m2": _to_float(s.get("median_price_per_m2")),
            "p25_price_per_m2": _to_float(s.get("p25_price_per_m2")),
            "p75_price_per_m2": _to_float(s.get("p75_price_per_m2")),
            "median_area_m2": _to_float(s.get("median_area_m2")),
        }
        for s in (rec.get("price_stats_list") or [])
    }

    data: dict[str, Any] = {
        "center": {"lat": lat, "lng": lng},
        "radius_m": radius_m,
        "active_listing_count": active_count,
        "active_listings_per_km2": round(density, 2),
        "disposition_mix": disposition_mix,
        "building_type_mix": building_type_mix,
        "condition_mix": condition_mix,
        "price_stats_by_disposition": price_stats,
        "trend": {
            "new_listings_last_7_days": int(rec.get("new_7d") or 0),
            "new_listings_last_30_days": int(rec.get("new_30d") or 0),
            "becoming_inactive_last_7_days": int(rec.get("inactive_7d") or 0),
            "becoming_inactive_last_30_days": int(rec.get("inactive_30d") or 0),
        },
        "data_age": {
            "oldest_data_age_days": (
                int(rec["oldest_data_age_days"])
                if rec.get("oldest_data_age_days") is not None else None
            ),
            "median_data_age_days": (
                float(rec["median_data_age_days"])
                if rec.get("median_data_age_days") is not None else None
            ),
        },
    }

    max_last_seen = rec.get("max_last_seen")
    data_freshness_iso = (
        max_last_seen.isoformat()
        if isinstance(max_last_seen, datetime) else None
    )

    metadata: dict[str, Any] = {
        "tool": "describe_neighborhood",
        "filters_used": {
            "lat": lat, "lng": lng,
            "radius_m": radius_m,
            "max_age_days": max_age_days,
            "category_main": category_main,
            "category_type": category_type,
        },
        "result_count": active_count,
        "queried_at": _now_iso(),
        "data_freshness": data_freshness_iso,
    }
    if active_count > _HARD_CAP:
        metadata["notes"] = [
            f"active_listing_count={active_count} exceeds soft cap "
            f"{_HARD_CAP}; results returned in full but may be noisier"
        ]

    return {"data": data, "metadata": metadata}


def _to_fractions(counts: dict[str, Any], total: int) -> dict[str, float]:
    if total <= 0 or not counts:
        return {}
    return {k: round(int(v) / total, 4) for k, v in counts.items()}


def _to_float(v: Any) -> float | None:
    return float(v) if v is not None else None
