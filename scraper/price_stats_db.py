"""Database I/O for price-stats datasets (psycopg, transaction pooler).

Dataset/locality reads, locality resolution cache (with the obec PIP join),
run bookkeeping, per-month observation upserts, and the derived-metric
recompute that feeds the analysis tab + map choropleth. Pure SQL only — the
CAGR/yield math lives in `scraper.price_stats_metrics`.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from scraper.db import connect, database_url  # noqa: F401 (re-export connect)
from scraper.price_stats_metrics import compute_city_metrics

LOG = logging.getLogger(__name__)

_DATASET_COLS = (
    "id, slug, name, description, category_main_cb, building_condition, "
    "building_type, ownership, usable_area_from, usable_area_to, distance, "
    "is_active, start_ym, end_ym, obec_ids"
)


def load_active_datasets(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_DATASET_COLS} FROM price_stat_datasets "
            "WHERE is_active ORDER BY id"
        )
        return cur.fetchall()


def get_dataset(conn: psycopg.Connection, dataset_id: int) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_DATASET_COLS} FROM price_stat_datasets WHERE id = %s",
            (dataset_id,),
        )
        return cur.fetchone()


def upsert_locality(conn: psycopg.Connection, loc: dict[str, Any]) -> None:
    """Cache a resolved entity; set geom + PIP-resolve its RÚIAN obec_id.

    obec_id is found by point-in-polygon against admin_boundaries (level='obec')
    — sreality's municipality ids don't map to RÚIAN codes, but the coordinate
    does. NULL where the point falls outside any CZ obec polygon.
    """
    lat, lon = loc.get("lat"), loc.get("lon")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO price_stat_localities (
                entity_type, entity_id, name, municipality_id,
                municipality_seo_name, district, district_id, district_seo_name,
                region, region_id, region_seo_name, lat, lon, geom, obec_id,
                resolved_at
            ) VALUES (
                %(entity_type)s, %(entity_id)s, %(name)s, %(municipality_id)s,
                %(municipality_seo_name)s, %(district)s, %(district_id)s,
                %(district_seo_name)s, %(region)s, %(region_id)s,
                %(region_seo_name)s, %(lat)s, %(lon)s,
                CASE WHEN %(lon)s IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography
                END,
                CASE WHEN %(lon)s IS NULL THEN NULL ELSE (
                    SELECT b.id FROM admin_boundaries b
                     WHERE b.level = 'obec'
                       AND ST_Contains(b.geom::geometry,
                           ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326))
                     LIMIT 1
                ) END,
                now()
            )
            ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                name = EXCLUDED.name,
                municipality_id = EXCLUDED.municipality_id,
                municipality_seo_name = EXCLUDED.municipality_seo_name,
                district = EXCLUDED.district,
                district_id = EXCLUDED.district_id,
                district_seo_name = EXCLUDED.district_seo_name,
                region = EXCLUDED.region,
                region_id = EXCLUDED.region_id,
                region_seo_name = EXCLUDED.region_seo_name,
                lat = EXCLUDED.lat,
                lon = EXCLUDED.lon,
                geom = EXCLUDED.geom,
                obec_id = EXCLUDED.obec_id,
                resolved_at = now()
            """,
            {**loc, "lat": lat, "lon": lon},
        )


def list_localities(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT entity_type, entity_id, name, obec_id "
            "FROM price_stat_localities ORDER BY name"
        )
        return cur.fetchall()


def resolve_obce(conn: psycopg.Connection, obec_ids: list[int]) -> int:
    """Cache localities for selected obce straight from admin_boundaries.

    admin_boundaries.sreality_id (obec level) IS the sreality municipality
    entity_id, so a selected obec maps to a sreality entity with no
    localities/suggest call. The entity coordinate is the obec centroid; obec_id
    is set directly (no PIP). Obce without a sreality_id are skipped (not
    scrapeable).
    """
    if not obec_ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO price_stat_localities (
                entity_type, entity_id, name, lat, lon, geom, obec_id, resolved_at
            )
            SELECT 'muni', b.sreality_id, b.name,
                   ST_Y(ST_Centroid(b.geom::geometry)),
                   ST_X(ST_Centroid(b.geom::geometry)),
                   ST_Centroid(b.geom::geometry)::geography, b.id, now()
              FROM admin_boundaries b
             WHERE b.id = ANY(%s) AND b.level = 'obec' AND b.sreality_id IS NOT NULL
            ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                name = EXCLUDED.name, obec_id = EXCLUDED.obec_id,
                lat = EXCLUDED.lat, lon = EXCLUDED.lon, geom = EXCLUDED.geom,
                resolved_at = now()
            """,
            (obec_ids,),
        )
        return cur.rowcount


def localities_for_obec_ids(
    conn: psycopg.Connection, obec_ids: list[int]
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT entity_type, entity_id, name, obec_id "
            "FROM price_stat_localities WHERE obec_id = ANY(%s) ORDER BY name",
            (obec_ids,),
        )
        return cur.fetchall()


def locality_exists(conn: psycopg.Connection, entity_type: str, entity_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM price_stat_localities "
            "WHERE entity_type = %s AND entity_id = %s",
            (entity_type, entity_id),
        )
        return cur.fetchone() is not None


def start_run(conn: psycopg.Connection, dataset_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO price_stat_runs (dataset_id) VALUES (%s) RETURNING id",
            (dataset_id,),
        )
        return cur.fetchone()[0]


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    *,
    status: str,
    localities: int = 0,
    observations: int = 0,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE price_stat_runs SET status = %s, localities = %s, "
            "observations = %s, error = %s, finished_at = now() WHERE id = %s",
            (status, localities, observations, error, run_id),
        )


def upsert_observations(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    entity_type: str,
    entity_id: int,
    category_type_cb: int,
    months: list[dict[str, Any]],
    run_id: int,
) -> int:
    """Latest-wins upsert of one (dataset, locality, category) monthly series."""
    if not months:
        return 0
    rows = [
        (
            dataset_id, entity_type, entity_id, category_type_cb,
            m["year"], m["month"], m.get("price"), m.get("active_count"),
            m.get("new_count"), m.get("deleted_count"), run_id,
        )
        for m in months
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO price_stat_observations (
                dataset_id, entity_type, entity_id, category_type_cb, year,
                month, price, active_count, new_count, deleted_count, run_id,
                fetched_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (dataset_id, entity_type, entity_id, category_type_cb,
                         year, month) DO UPDATE SET
                price = EXCLUDED.price,
                active_count = EXCLUDED.active_count,
                new_count = EXCLUDED.new_count,
                deleted_count = EXCLUDED.deleted_count,
                run_id = EXCLUDED.run_id,
                fetched_at = now()
            """,
            rows,
        )
    return len(rows)


def recompute_metrics(
    conn: psycopg.Connection, dataset_id: int, *, window_years: int = 5
) -> int:
    """Recompute price_stat_city_metrics for a dataset from its observations."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT o.entity_type, o.entity_id, l.obec_id, o.category_type_cb, "
            "o.year, o.month, o.price, o.active_count "
            "FROM price_stat_observations o "
            "JOIN price_stat_localities l "
            "  ON l.entity_type = o.entity_type AND l.entity_id = o.entity_id "
            "WHERE o.dataset_id = %s "
            "ORDER BY o.entity_type, o.entity_id, o.year, o.month",
            (dataset_id,),
        )
        obs = cur.fetchall()

    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for r in obs:
        key = (r["entity_type"], r["entity_id"])
        g = grouped.setdefault(
            key, {"obec_id": r["obec_id"], "sale": [], "rent": []}
        )
        bucket = "sale" if r["category_type_cb"] == 1 else "rent"
        g[bucket].append(r)

    rows: list[tuple[Any, ...]] = []
    for (entity_type, entity_id), g in grouped.items():
        m = compute_city_metrics(g["sale"], g["rent"], window_years=window_years)
        rows.append(
            (
                dataset_id, entity_type, entity_id, g["obec_id"], m["window_years"],
                m["sale_latest_price"], m["sale_latest_ym"], m["sale_cagr_pct"],
                m["sale_months"], m["sale_min_active"], m["rent_latest_price"],
                m["rent_latest_ym"], m["rent_cagr_pct"], m["rent_months"],
                m["rent_min_active"], m["gross_yield_pct"],
            )
        )

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM price_stat_city_metrics WHERE dataset_id = %s",
            (dataset_id,),
        )
        if rows:
            cur.executemany(
                """
                INSERT INTO price_stat_city_metrics (
                    dataset_id, entity_type, entity_id, obec_id, window_years,
                    sale_latest_price, sale_latest_ym, sale_cagr_pct,
                    sale_months, sale_min_active, rent_latest_price,
                    rent_latest_ym, rent_cagr_pct, rent_months, rent_min_active,
                    gross_yield_pct, computed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, now())
                """,
                rows,
            )
    return len(rows)


def refresh_choropleth(conn: psycopg.Connection) -> None:
    """Refresh the map matview (CONCURRENTLY needs the unique index, present)."""
    with conn.cursor() as cur:
        try:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY price_stat_choropleth")
        except psycopg.errors.ObjectNotInPrerequisiteState:
            # CONCURRENTLY requires a prior non-concurrent populate.
            cur.execute("REFRESH MATERIALIZED VIEW price_stat_choropleth")
