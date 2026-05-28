-- 107_portal_operational_config.sql
--
-- Phase 4 of the scaling roadmap: the portal framework. Migration 100 created
-- the `portals` registry as a Health-page DISPLAY surface (label, kind, stage).
-- Phase 4 promotes it to the OPERATIONAL config the shared portal_runner reads,
-- so a portal is "config + a fetcher + a parser" with no per-portal branches in
-- the runner. This migration adds the three portal-defining knobs:
--
--   * supports_complete_walk — can this portal prove a near-complete index walk
--     (sreality: per-district union vs the API's result_size)? Gates
--     mark_inactive (architectural rule #3). HTML crawlers that only do partial
--     walks (bazos) stay false and never flip listings inactive.
--   * categories — the per-portal list of category descriptors the runner walks.
--     Shape is portal-specific (the Portal object interprets it): sreality uses
--     {category_main_cb, category_type_cb} code pairs; bazos uses
--     {sale_type, category} URL segments.
--   * split_threshold — the deep-pagination cap above which a category is walked
--     per-district and unioned (sreality only; NULL = the portal has no cap and
--     never splits).
--
-- Rate / worker / cadence knobs are deliberately NOT stored here — they are
-- per-run operational tuning set by the workflow CLI args, not portal identity.
--
-- Purely additive: new nullable columns + a data backfill of the two scraper
-- rows. The parser-only rows (bezrealitky / idnes / remax) keep the defaults
-- (not walked).

alter table portals add column supports_complete_walk boolean not null default false;
alter table portals add column categories jsonb;
alter table portals add column split_threshold integer;

-- sreality: all six category pairs, district-split over the 10k deep-pagination
-- cap, complete-walk capable (per-district union → mark_inactive runs).
update portals set
  supports_complete_walk = true,
  split_threshold = 10000,
  categories = '[
    {"category_main_cb": 1, "category_type_cb": 2},
    {"category_main_cb": 1, "category_type_cb": 1},
    {"category_main_cb": 2, "category_type_cb": 2},
    {"category_main_cb": 2, "category_type_cb": 1},
    {"category_main_cb": 4, "category_type_cb": 2},
    {"category_main_cb": 4, "category_type_cb": 1}
  ]'::jsonb
where source = 'sreality';

-- bazos: HTML crawler, partial walks only (never mark_inactive), no pagination
-- cap so no split. A single pilot category to start; expand by editing this row.
update portals set
  supports_complete_walk = false,
  split_threshold = null,
  categories = '[
    {"sale_type": "prodam", "category": "byt"}
  ]'::jsonb
where source = 'bazos';

-- Expose the new columns on the anon-readable view (registry, sans bookkeeping).
-- CREATE OR REPLACE (not DROP) — the new columns are appended after the existing
-- ones, which Postgres permits, so portal_health_summary()'s dependency on the
-- view is never broken.
create or replace view portals_public as
  select source, label, kind, stage, home_url, sort_order, is_enabled,
         supports_complete_walk, categories, split_threshold
  from portals;
grant select on portals_public to anon;
