-- 273_property_publication_gate.sql
--
-- Dedup-aware publication gate (operator decision, FINAL — 2026-07).
--
-- "New listings appear in Browse / on the map / in Stats / to agents / in watchdog
-- notifications ONLY after the dedup engine has evaluated them." A HARD gate with NO
-- auto-publish timeout — the operator explicitly rejected a timeout ("that is
-- hiding-problems mindset"). The ONLY escape hatch is the kill-switch app_setting below.
--
-- Mechanism: a NEW property-grain column properties.published_at, NULL until the property
-- has been dedup-evaluated (or ruled ineligible / merged / split). Both singleton-insert
-- paths (scraper.db._create_singleton_property, recompute _attach_stragglers) INSERT
-- without this column, so every brand-new property starts NULL = hidden. The gate is
-- applied in ONE place per list surface: properties_public's WHERE (Browse table/cards/
-- counts + browse_stats + the watchdog & collection-monitor matchers) and the
-- properties_map_mv WHERE (the Browse map). Off -> passes everything (instant un-gate).
--
-- DEPLOY ORDER (critical — this is a hard gate with no self-heal): the setting is SEEDED
-- FALSE so merging this migration hides NOTHING. The operator deploys the stamping code,
-- watches published_at populate for ~24h (SELECT publish_reason, count(*) FROM properties
-- GROUP BY 1), THEN flips dedup_publication_gate_enabled to true. Flipping on before the
-- stamping code ships would hide every new listing.
--
-- NOT to be confused with listings.published_at (migration 266): that is a LISTING-grain,
-- portal-declared SLO timestamp (write-only instrumentation). This one is PROPERTY-grain
-- and drives visibility.

-- 1. The gate column + its provenance reason. Additive, nullable — no default, so the
--    singleton-insert paths that omit it leave NULL (= unpublished = hidden).
alter table properties
  add column if not exists published_at  timestamptz,
  add column if not exists publish_reason text;

comment on column properties.published_at is
  'Dedup-aware publication gate (migration 273): NULL until the dedup engine has evaluated '
  'this property (or it was ruled ineligible / merged / split). properties_public and '
  'properties_map_mv hide NULL rows while dedup_publication_gate_enabled is on. DISTINCT '
  'from listings.published_at (migration 266), a LISTING-grain portal-declared SLO '
  'timestamp — do not conflate the two.';
comment on column properties.publish_reason is
  'Why published_at was set: backfill | dedup_checked | merge_survivor | split | ineligible.';

-- 2. Backfill EVERY existing property (any status) — the gate is for NEW properties only,
--    so nothing already in the system is ever hidden by this migration.
update properties
   set published_at = now(), publish_reason = 'backfill'
 where published_at is null;

-- 3. Partial index over just the unpublished rows (empty right after the backfill; the
--    ineligible-sweep + health view scan it in microseconds as new NULLs trickle in).
create index if not exists properties_unpublished_idx
  on properties (first_seen_at)
  where published_at is null;

-- 4. The kill-switch. SEEDED FALSE (see DEPLOY ORDER above); the operator flips it true
--    after the stamping code has populated published_at. There is NO timeout setting.
insert into app_settings (key, value, description, updated_by)
values (
  'dedup_publication_gate_enabled',
  'false'::jsonb,
  'HARD dedup-aware publication gate (operator decision 2026-07). When true, a new '
  'property is hidden from Browse / map / Stats / agents / watchdog until the dedup engine '
  'has evaluated it (properties.published_at IS NOT NULL). NO auto-publish timeout — this '
  'switch is the only escape hatch. SEEDED false so merging the migration hides nothing; '
  'flip to true ONLY AFTER the stamping code has deployed and published_at has populated '
  '(watch: SELECT publish_reason, count(*) FROM properties GROUP BY 1). Read in-SQL by '
  'publication_gate_enabled(); its code fallback is true, but this seeded row wins.',
  'migration'
)
on conflict (key) do nothing;

-- 5. A tiny STABLE SECURITY DEFINER reader so the views evaluate the switch as a one-time
--    InitPlan and anon needs no grant on app_settings. Fallback true only if the row is
--    somehow absent (the seeded row above is false, and it wins).
create or replace function publication_gate_enabled()
 returns boolean
 language sql
 stable
 security definer
 set search_path = public
as $$
  select coalesce(
    (select (value #>> '{}')::boolean
       from app_settings where key = 'dedup_publication_gate_enabled'),
    true
  );
$$;

grant execute on function publication_gate_enabled() to anon, authenticated;

-- 6. properties_public: migration 252's body VERBATIM, plus a trailing p.published_at
--    output column, and the gate added to the final WHERE. Off -> passes everything.
CREATE OR REPLACE VIEW properties_public AS
 SELECT p.id AS property_id,
    p.repr_listing_id AS sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk AS price_czk,
    l.price_unit,
    p.area_m2,
    p.disposition,
    p.locality,
    p.district,
    p.locality_district_id,
    p.locality_region_id,
    p.lat,
    p.lng,
    l.floor,
    l.total_floors,
    p.has_balcony,
    p.has_parking,
    p.has_lift,
    p.building_type,
    p.condition,
    p.energy_rating,
    p.estate_area,
    p.usable_area,
    p.garden_area,
    p.category_sub_cb,
    p.furnished,
    p.terrace,
    p.cellar,
    p.garage,
    p.parking_lots,
    p.ownership,
    l.broker_name,
    l.broker_email,
    l.broker_phone,
        CASE
            WHEN p.is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
            ELSE GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
        END AS tom_days,
        CASE
            WHEN p.area_m2 IS NOT NULL AND p.area_m2 > 0::numeric AND p.current_price_czk IS NOT NULL THEN round(p.current_price_czk::numeric / p.area_m2, 2)
            ELSE NULL::numeric
        END AS price_per_m2,
    p.building_condition_level,
    p.apartment_condition_level,
    l.description,
    p.source_count,
    p.distinct_site_count,
    p.price_drop_count,
    p.price_rise_count,
    p.max_price_drop_pct,
    p.stats_computed_at,
    p.source,
    COALESCE(p.street, l.street) AS street,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    p.obec,
    p.okres,
    p.region,
    p.home_obec_pop,
    p.near_pop_5km,
    p.near_pop_15km,
    p.near_jobs_5km,
    p.near_jobs_15km,
    p.near_youth_5km,
    p.near_youth_15km,
    p.near_overall_5km,
    p.near_overall_15km,
    p.subtype,
    p.last_change_at,
    p.obec_id,
    p.okres_id,
    p.region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    concat_ws(', '::text, p.street, p.locality) AS place_search_text,
    p.asset_id,
    p.mf_reference_rent,
    p.published_at
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text
    AND (NOT publication_gate_enabled() OR p.published_at IS NOT NULL);

-- 7. properties_map_mv: matviews can't CREATE OR REPLACE, so mirror migration 254's
--    DROP/CREATE + indexes + grant, adding the same gate to its WHERE. (scripts/
--    refresh_map_mv.py's blue-green _BUILD_SQL carries the identical predicate.) Seeded
--    false, so the gate passes everything and the fresh matview is fully populated.
drop materialized view if exists properties_map_mv;

create materialized view properties_map_mv as
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
  and (not publication_gate_enabled() or p.published_at is not null);

create unique index if not exists properties_map_mv_pk on properties_map_mv (property_id);

create index if not exists properties_map_mv_cover on properties_map_mv
  (category_main, category_type, lat, lng)
  include (sreality_id, price_czk, disposition, subtype, area_m2, district,
           last_seen_at, first_seen_at, is_active);

grant select on properties_map_mv to anon, authenticated;

-- 8. Stall-observability read surface for the SEPARATE dashboard PR (no UI built here).
--    Partial-index-backed; the count/min over unpublished rows is microseconds.
create or replace view publication_gate_health_public as
select
  count(*)               as unpublished,
  min(first_seen_at)     as oldest_unpublished_at,
  (select count(*) from properties where status = 'active') as active_total
from properties
where published_at is null and status = 'active';

grant select on publication_gate_health_public to anon;
