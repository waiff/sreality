-- 059_filter_visibility.sql
--
-- Agenda × filter visibility matrix for the unified filter registry.
--
-- The canonical filter list lives in `toolkit/filter_registry.py`. Each
-- registry entry declares which agendas it can apply to (Browse,
-- Watchdog, Comparables, Estimation, Velocity, Neighborhood, Defaults).
-- This table lets the operator toggle individual (agenda, filter)
-- pairs from the Settings UI without a redeploy.
--
-- A missing row is treated as `enabled = true` so a fresh deploy
-- behaves like a deploy without the table — no surprise side-effects
-- if the operator never visits Settings. The registry seeds one row
-- per declared (agenda, filter) pair at deploy time below, mostly so
-- the Settings matrix can render every cell from a single SELECT.
--
-- Append-only per CLAUDE.md: never modify this migration. Future
-- changes to the registry's declared agendas get their own migrations
-- (or rely on the seed-on-startup INSERT … ON CONFLICT DO NOTHING in
-- api/dependencies.py).

begin;

create table if not exists filter_visibility (
    agenda      text        not null,
    filter_id   text        not null,
    enabled     boolean     not null default true,
    updated_at  timestamptz not null default now(),
    updated_by  text,
    primary key (agenda, filter_id)
);

create index if not exists filter_visibility_agenda_idx
    on filter_visibility (agenda);

-- One row per (agenda, filter) pair the registry declares at deploy
-- time. Subsequent registry additions either ship their own seed
-- migration or fall back to the "missing row = enabled" default until
-- the operator visits Settings.
--
-- The (agenda, filter_id) pairs below mirror `filter_registry.REGISTRY`
-- as of this commit. ON CONFLICT DO NOTHING means re-running the
-- migration is idempotent and any operator edits already in the table
-- survive.

insert into filter_visibility (agenda, filter_id, enabled, updated_by) values
    -- location (composite — Browse + Watchdog)
    ('browse',   'location', true, 'migration_059'),
    ('watchdog', 'location', true, 'migration_059'),
    ('browse',   'districts', true, 'migration_059'),
    ('watchdog', 'districts', true, 'migration_059'),

    -- cohort tuning
    ('comparables',  'radius_m', true, 'migration_059'),
    ('estimation',   'radius_m', true, 'migration_059'),
    ('velocity',     'radius_m', true, 'migration_059'),
    ('neighborhood', 'radius_m', true, 'migration_059'),
    ('defaults',     'radius_m', true, 'migration_059'),
    ('comparables', 'area_band_pct', true, 'migration_059'),
    ('estimation',  'area_band_pct', true, 'migration_059'),
    ('velocity',    'area_band_pct', true, 'migration_059'),
    ('defaults',    'area_band_pct', true, 'migration_059'),
    ('comparables', 'disposition_match', true, 'migration_059'),
    ('estimation',  'disposition_match', true, 'migration_059'),
    ('velocity',    'disposition_match', true, 'migration_059'),
    ('defaults',    'disposition_match', true, 'migration_059'),
    ('comparables', 'floor_band', true, 'migration_059'),
    ('estimation',  'floor_band', true, 'migration_059'),
    ('velocity',    'floor_band', true, 'migration_059'),
    ('comparables',  'max_age_days', true, 'migration_059'),
    ('estimation',   'max_age_days', true, 'migration_059'),
    ('velocity',     'max_age_days', true, 'migration_059'),
    ('neighborhood', 'max_age_days', true, 'migration_059'),
    ('defaults',     'max_age_days', true, 'migration_059'),
    ('comparables', 'active_only', true, 'migration_059'),
    ('estimation',  'active_only', true, 'migration_059'),
    ('defaults',    'active_only', true, 'migration_059'),
    ('comparables', 'population', true, 'migration_059'),
    ('estimation',  'population', true, 'migration_059'),
    ('velocity',    'population', true, 'migration_059'),

    -- listing status (Browse-only)
    ('browse', 'status', true, 'migration_059'),

    -- velocity bands
    ('browse',      'tom_days_min', true, 'migration_059'),
    ('comparables', 'tom_days_min', true, 'migration_059'),
    ('estimation',  'tom_days_min', true, 'migration_059'),
    ('browse',      'tom_days_max', true, 'migration_059'),
    ('comparables', 'tom_days_max', true, 'migration_059'),
    ('estimation',  'tom_days_max', true, 'migration_059'),
    ('browse',      'last_seen_min_days', true, 'migration_059'),
    ('comparables', 'last_seen_min_days', true, 'migration_059'),
    ('estimation',  'last_seen_min_days', true, 'migration_059'),
    ('browse',      'last_seen_max_days', true, 'migration_059'),
    ('comparables', 'last_seen_max_days', true, 'migration_059'),
    ('estimation',  'last_seen_max_days', true, 'migration_059'),
    ('browse',      'first_seen_min_days', true, 'migration_059'),
    ('comparables', 'first_seen_min_days', true, 'migration_059'),
    ('estimation',  'first_seen_min_days', true, 'migration_059'),
    ('browse',      'first_seen_max_days', true, 'migration_059'),
    ('comparables', 'first_seen_max_days', true, 'migration_059'),
    ('estimation',  'first_seen_max_days', true, 'migration_059'),

    -- category + disposition
    ('browse',       'category_main', true, 'migration_059'),
    ('watchdog',     'category_main', true, 'migration_059'),
    ('comparables',  'category_main', true, 'migration_059'),
    ('estimation',   'category_main', true, 'migration_059'),
    ('velocity',     'category_main', true, 'migration_059'),
    ('neighborhood', 'category_main', true, 'migration_059'),
    ('defaults',     'category_main', true, 'migration_059'),
    ('browse',       'category_type', true, 'migration_059'),
    ('watchdog',     'category_type', true, 'migration_059'),
    ('comparables',  'category_type', true, 'migration_059'),
    ('estimation',   'category_type', true, 'migration_059'),
    ('velocity',     'category_type', true, 'migration_059'),
    ('neighborhood', 'category_type', true, 'migration_059'),
    ('defaults',     'category_type', true, 'migration_059'),
    ('browse',       'category_sub_cb', true, 'migration_059'),
    ('watchdog',     'category_sub_cb', true, 'migration_059'),
    ('comparables',  'category_sub_cb', true, 'migration_059'),
    ('estimation',   'category_sub_cb', true, 'migration_059'),
    ('velocity',     'category_sub_cb', true, 'migration_059'),
    ('neighborhood', 'category_sub_cb', true, 'migration_059'),
    ('defaults',     'category_sub_cb', true, 'migration_059'),
    ('browse',   'dispositions', true, 'migration_059'),
    ('watchdog', 'dispositions', true, 'migration_059'),
    ('comparables', 'condition_match', true, 'migration_059'),
    ('estimation',  'condition_match', true, 'migration_059'),
    ('velocity',    'condition_match', true, 'migration_059'),
    ('comparables', 'building_type_match', true, 'migration_059'),
    ('estimation',  'building_type_match', true, 'migration_059'),
    ('velocity',    'building_type_match', true, 'migration_059'),
    ('browse',   'building_material', true, 'migration_059'),
    ('watchdog', 'building_material', true, 'migration_059'),
    ('comparables', 'energy_rating_match', true, 'migration_059'),
    ('estimation',  'energy_rating_match', true, 'migration_059'),
    ('velocity',    'energy_rating_match', true, 'migration_059'),

    -- furnished + ownership (universal)
    ('browse',       'furnished', true, 'migration_059'),
    ('watchdog',     'furnished', true, 'migration_059'),
    ('comparables',  'furnished', true, 'migration_059'),
    ('estimation',   'furnished', true, 'migration_059'),
    ('velocity',     'furnished', true, 'migration_059'),
    ('neighborhood', 'furnished', true, 'migration_059'),
    ('defaults',     'furnished', true, 'migration_059'),
    ('browse',       'ownership', true, 'migration_059'),
    ('watchdog',     'ownership', true, 'migration_059'),
    ('comparables',  'ownership', true, 'migration_059'),
    ('estimation',   'ownership', true, 'migration_059'),
    ('velocity',     'ownership', true, 'migration_059'),
    ('neighborhood', 'ownership', true, 'migration_059'),
    ('defaults',     'ownership', true, 'migration_059'),

    -- amenities (tri-state; universal)
    ('browse',       'has_balcony', true, 'migration_059'),
    ('watchdog',     'has_balcony', true, 'migration_059'),
    ('comparables',  'has_balcony', true, 'migration_059'),
    ('estimation',   'has_balcony', true, 'migration_059'),
    ('velocity',     'has_balcony', true, 'migration_059'),
    ('neighborhood', 'has_balcony', true, 'migration_059'),
    ('defaults',     'has_balcony', true, 'migration_059'),
    ('browse',       'has_lift', true, 'migration_059'),
    ('watchdog',     'has_lift', true, 'migration_059'),
    ('comparables',  'has_lift', true, 'migration_059'),
    ('estimation',   'has_lift', true, 'migration_059'),
    ('velocity',     'has_lift', true, 'migration_059'),
    ('neighborhood', 'has_lift', true, 'migration_059'),
    ('defaults',     'has_lift', true, 'migration_059'),
    ('browse',       'has_parking', true, 'migration_059'),
    ('watchdog',     'has_parking', true, 'migration_059'),
    ('comparables',  'has_parking', true, 'migration_059'),
    ('estimation',   'has_parking', true, 'migration_059'),
    ('velocity',     'has_parking', true, 'migration_059'),
    ('neighborhood', 'has_parking', true, 'migration_059'),
    ('defaults',     'has_parking', true, 'migration_059'),
    ('browse',       'terrace', true, 'migration_059'),
    ('watchdog',     'terrace', true, 'migration_059'),
    ('comparables',  'terrace', true, 'migration_059'),
    ('estimation',   'terrace', true, 'migration_059'),
    ('velocity',     'terrace', true, 'migration_059'),
    ('neighborhood', 'terrace', true, 'migration_059'),
    ('defaults',     'terrace', true, 'migration_059'),
    ('browse',       'cellar', true, 'migration_059'),
    ('watchdog',     'cellar', true, 'migration_059'),
    ('comparables',  'cellar', true, 'migration_059'),
    ('estimation',   'cellar', true, 'migration_059'),
    ('velocity',     'cellar', true, 'migration_059'),
    ('neighborhood', 'cellar', true, 'migration_059'),
    ('defaults',     'cellar', true, 'migration_059'),
    ('browse',       'garage', true, 'migration_059'),
    ('watchdog',     'garage', true, 'migration_059'),
    ('comparables',  'garage', true, 'migration_059'),
    ('estimation',   'garage', true, 'migration_059'),
    ('velocity',     'garage', true, 'migration_059'),
    ('neighborhood', 'garage', true, 'migration_059'),
    ('defaults',     'garage', true, 'migration_059'),
    ('browse',       'min_parking_lots', true, 'migration_059'),
    ('watchdog',     'min_parking_lots', true, 'migration_059'),
    ('comparables',  'min_parking_lots', true, 'migration_059'),
    ('estimation',   'min_parking_lots', true, 'migration_059'),
    ('velocity',     'min_parking_lots', true, 'migration_059'),
    ('neighborhood', 'min_parking_lots', true, 'migration_059'),
    ('defaults',     'min_parking_lots', true, 'migration_059'),

    -- price + area + estate + usable + garden (universal)
    ('browse',       'min_price_czk', true, 'migration_059'),
    ('watchdog',     'min_price_czk', true, 'migration_059'),
    ('comparables',  'min_price_czk', true, 'migration_059'),
    ('estimation',   'min_price_czk', true, 'migration_059'),
    ('velocity',     'min_price_czk', true, 'migration_059'),
    ('neighborhood', 'min_price_czk', true, 'migration_059'),
    ('defaults',     'min_price_czk', true, 'migration_059'),
    ('browse',       'max_price_czk', true, 'migration_059'),
    ('watchdog',     'max_price_czk', true, 'migration_059'),
    ('comparables',  'max_price_czk', true, 'migration_059'),
    ('estimation',   'max_price_czk', true, 'migration_059'),
    ('velocity',     'max_price_czk', true, 'migration_059'),
    ('neighborhood', 'max_price_czk', true, 'migration_059'),
    ('defaults',     'max_price_czk', true, 'migration_059'),
    ('browse',   'min_area_m2', true, 'migration_059'),
    ('watchdog', 'min_area_m2', true, 'migration_059'),
    ('browse',   'max_area_m2', true, 'migration_059'),
    ('watchdog', 'max_area_m2', true, 'migration_059'),
    ('browse',       'min_estate_area', true, 'migration_059'),
    ('watchdog',     'min_estate_area', true, 'migration_059'),
    ('comparables',  'min_estate_area', true, 'migration_059'),
    ('estimation',   'min_estate_area', true, 'migration_059'),
    ('velocity',     'min_estate_area', true, 'migration_059'),
    ('neighborhood', 'min_estate_area', true, 'migration_059'),
    ('defaults',     'min_estate_area', true, 'migration_059'),
    ('browse',       'max_estate_area', true, 'migration_059'),
    ('watchdog',     'max_estate_area', true, 'migration_059'),
    ('comparables',  'max_estate_area', true, 'migration_059'),
    ('estimation',   'max_estate_area', true, 'migration_059'),
    ('velocity',     'max_estate_area', true, 'migration_059'),
    ('neighborhood', 'max_estate_area', true, 'migration_059'),
    ('defaults',     'max_estate_area', true, 'migration_059'),
    ('browse',       'min_usable_area', true, 'migration_059'),
    ('watchdog',     'min_usable_area', true, 'migration_059'),
    ('comparables',  'min_usable_area', true, 'migration_059'),
    ('estimation',   'min_usable_area', true, 'migration_059'),
    ('velocity',     'min_usable_area', true, 'migration_059'),
    ('neighborhood', 'min_usable_area', true, 'migration_059'),
    ('defaults',     'min_usable_area', true, 'migration_059'),
    ('browse',       'max_usable_area', true, 'migration_059'),
    ('watchdog',     'max_usable_area', true, 'migration_059'),
    ('comparables',  'max_usable_area', true, 'migration_059'),
    ('estimation',   'max_usable_area', true, 'migration_059'),
    ('velocity',     'max_usable_area', true, 'migration_059'),
    ('neighborhood', 'max_usable_area', true, 'migration_059'),
    ('defaults',     'max_usable_area', true, 'migration_059'),
    ('browse',       'min_garden_area', true, 'migration_059'),
    ('watchdog',     'min_garden_area', true, 'migration_059'),
    ('comparables',  'min_garden_area', true, 'migration_059'),
    ('estimation',   'min_garden_area', true, 'migration_059'),
    ('velocity',     'min_garden_area', true, 'migration_059'),
    ('neighborhood', 'min_garden_area', true, 'migration_059'),
    ('defaults',     'min_garden_area', true, 'migration_059'),
    ('browse',       'max_garden_area', true, 'migration_059'),
    ('watchdog',     'max_garden_area', true, 'migration_059'),
    ('comparables',  'max_garden_area', true, 'migration_059'),
    ('estimation',   'max_garden_area', true, 'migration_059'),
    ('velocity',     'max_garden_area', true, 'migration_059'),
    ('neighborhood', 'max_garden_area', true, 'migration_059'),
    ('defaults',     'max_garden_area', true, 'migration_059'),

    -- locality ids
    ('comparables', 'locality_district_id', true, 'migration_059'),
    ('estimation',  'locality_district_id', true, 'migration_059'),
    ('velocity',    'locality_district_id', true, 'migration_059'),
    ('watchdog',    'locality_district_id', true, 'migration_059'),
    ('comparables', 'locality_region_id', true, 'migration_059'),
    ('estimation',  'locality_region_id', true, 'migration_059'),
    ('velocity',    'locality_region_id', true, 'migration_059'),
    ('watchdog',    'locality_region_id', true, 'migration_059'),

    -- curation
    ('browse',   'tags', true, 'migration_059'),
    ('watchdog', 'tags', true, 'migration_059'),

    -- reliability
    ('comparables', 'include_unreliable', true, 'migration_059'),
    ('estimation',  'include_unreliable', true, 'migration_059'),
    ('velocity',    'include_unreliable', true, 'migration_059')
on conflict (agenda, filter_id) do nothing;

commit;
