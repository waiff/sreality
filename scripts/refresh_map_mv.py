"""Rebuild the Browse map's read-optimized materialized view, kept CLUSTERED.

`properties_map_mv` (migration 254) feeds the Browse map. On this instance
shared_buffers is small (512MB) for a 41GB DB, so the matview can't stay cached
and a scattered scan of up to 50k points is cold-slow (>3s, the anon timeout).
The fix is to keep the matview PHYSICALLY CLUSTERED by (category_main,
category_type, lat, lng): then the map's cohort lives in contiguous blocks and a
plain Index Scan reads them sequentially -- ~120ms even fully cold, robust
regardless of cache. Measured: scattered ~2.4-3.6s cold vs clustered ~0.12s.

REFRESH ... CONCURRENTLY can't maintain clustering (its diff relocates every row
whose last_seen_at changed -- i.e. most rows each scrape cycle), and a plain
REFRESH or CLUSTER takes a 30-70s ACCESS EXCLUSIVE lock (a map blackout). So we
BLUE-GREEN: build a fresh, clustered replacement off to the side (no lock on the
live matview), then atomically swap names in one short transaction. Readers see
the old copy until commit, then the new one -- a sub-second swap, no blackout.

    python -m scripts.refresh_map_mv

Required env: SUPABASE_DB_URL (same as every other job).
"""

from __future__ import annotations

import logging

from scraper.db import connect

LOG = logging.getLogger("refresh_map_mv")

# Mirrors migration 254's column list EXACTLY (so the frontend's columns are
# unchanged) plus the ORDER BY that makes the fresh build physically clustered.
_BUILD_SQL = """
create materialized view properties_map_mv_next as
select
  p.id                         as property_id,
  p.repr_listing_id            as sreality_id,
  p.first_seen_at, p.last_seen_at, p.is_active,
  p.category_main, p.category_type,
  p.current_price_czk          as price_czk,
  p.area_m2, p.disposition, p.locality, p.district,
  p.locality_district_id, p.locality_region_id,
  p.lat, p.lng,
  p.has_balcony, p.has_parking, p.has_lift, p.building_type, p.condition,
  p.energy_rating, p.estate_area, p.usable_area, p.garden_area, p.category_sub_cb,
  p.furnished, p.terrace, p.cellar, p.garage, p.parking_lots, p.ownership,
  case when p.is_active
       then greatest(0, floor(extract(epoch from now() - p.first_seen_at) / 86400::numeric)::integer)
       else greatest(0, floor(extract(epoch from p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  case when p.area_m2 is not null and p.area_m2 > 0::numeric and p.current_price_czk is not null
       then round(p.current_price_czk::numeric / p.area_m2, 2)
       else null::numeric end as price_per_m2,
  p.building_condition_level, p.apartment_condition_level,
  p.source, p.street,
  p.mf_reference_rent_czk, p.mf_gross_yield_pct,
  p.obec, p.okres, p.region,
  p.home_obec_pop, p.near_pop_5km, p.near_pop_15km, p.near_jobs_5km, p.near_jobs_15km,
  p.near_youth_5km, p.near_youth_15km, p.near_overall_5km, p.near_overall_15km,
  p.subtype, p.last_change_at,
  p.obec_id, p.okres_id, p.region_id,
  p.price_change_count, p.price_change_count_30d, p.price_change_count_90d,
  p.price_change_count_365d, p.total_price_change_pct,
  concat_ws(', '::text, p.street, p.locality) as place_search_text,
  p.asset_id
from properties p
where p.status = 'active' and p.lat is not null and p.lng is not null
  and (not (select publication_gate_enabled()) or p.published_at is not null)
order by p.category_main, p.category_type, p.lat, p.lng
"""
# The publication gate (migration 273) is wrapped in a scalar subquery so it
# evaluates ONCE per rebuild (an InitPlan) instead of once per row — the same
# fix migration 275 applies to properties_public's WHERE. Both anon-hot read
# surfaces must wrap the SECURITY DEFINER gate call, never call it bare (a bare
# SECURITY DEFINER qual can't be inlined -> per-row evaluation).


def refresh(conn) -> None:
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '10min'")
        # 1. Build the clustered replacement off to the side (no lock on live).
        cur.execute("DROP MATERIALIZED VIEW IF EXISTS properties_map_mv_next")
        cur.execute(_BUILD_SQL)
        cur.execute(
            "CREATE UNIQUE INDEX properties_map_mv_next_pk "
            "ON properties_map_mv_next (property_id)"
        )
        cur.execute(
            "CREATE INDEX properties_map_mv_next_cover ON properties_map_mv_next "
            "(category_main, category_type, lat, lng) "
            "INCLUDE (sreality_id, price_czk, disposition, subtype, area_m2, district, "
            "last_seen_at, first_seen_at, is_active)"
        )
        cur.execute("ANALYZE properties_map_mv_next")
        # 2. Atomic swap (sub-second ACCESS EXCLUSIVE -- readers see old until commit).
        with conn.transaction():
            cur.execute("DROP MATERIALIZED VIEW properties_map_mv")
            cur.execute("ALTER MATERIALIZED VIEW properties_map_mv_next RENAME TO properties_map_mv")
            cur.execute("ALTER INDEX properties_map_mv_next_pk RENAME TO properties_map_mv_pk")
            cur.execute("ALTER INDEX properties_map_mv_next_cover RENAME TO properties_map_mv_cover")
            cur.execute("GRANT SELECT ON properties_map_mv TO anon, authenticated")
        # 3. Let PostgREST pick up the new object.
        cur.execute("NOTIFY pgrst, 'reload schema'")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with connect() as conn:
        refresh(conn)
    LOG.info("properties_map_mv rebuilt (clustered) and swapped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
