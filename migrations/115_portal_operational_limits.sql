-- 114_portal_operational_limits.sql
--
-- Make per-portal OPERATIONAL LIMITS (rate / workers / per-run caps / image
-- limits / completeness) operator-tunable from the DB + a future Scrapers
-- dashboard. Migration 107 deliberately kept these knobs OUT of the registry
-- ("per-run operational tuning set by the workflow CLI args, not portal
-- identity"). The operator now wants them editable per portal (limits vary a
-- lot between a 6 req/s JSON API and a 0.6 req/s HTML crawl) with a global
-- default layer "for the scraper as a whole" — so this migration reverses that
-- stance for the limit knobs, knowingly.
--
-- Precedence the loader resolves (scraper/portal.py): CLI override > per-portal
-- (portals.operational_limits) > global (app_settings.scraper_limits_global) >
-- baked-in code default. Production workflows still pass their CLI flags, so
-- CLI wins and this migration is ZERO behavior change; the seed below mirrors
-- today's production values so behavior also stays put when the CLI flags are
-- later dropped from the workflow YAML.
--
-- Purely additive: a new nullable jsonb column + its edit-attribution column, a
-- history table + before-update trigger (mirrors app_settings, migration 020;
-- 107 added no history — we add it because this surface is now operator-edited),
-- a global-defaults app_settings row, and the column appended to portals_public.

alter table portals add column operational_limits jsonb;
alter table portals add column operational_limits_updated_by text;

-- History: every change to operational_limits snapshots the OLD value, so a
-- dashboard edit is auditable / undoable. Same shape as app_settings_history.
create table portal_limits_history (
  id                 bigserial primary key,
  source             text not null,
  operational_limits jsonb,
  replaced_at        timestamptz not null default now(),
  replaced_by        text
);

create or replace function portals_limits_record_history()
returns trigger
language plpgsql
as $$
begin
  insert into portal_limits_history (source, operational_limits, replaced_at, replaced_by)
  values (old.source, old.operational_limits, now(), old.operational_limits_updated_by);
  return new;
end;
$$;

create trigger portal_limits_history_trigger
  before update on portals
  for each row
  when (old.operational_limits is distinct from new.operational_limits)
  execute function portals_limits_record_history();

-- Per-portal seed = today's PRODUCTION values (the workflow CLI args), so that
-- when the CLI flags are dropped the DB carries the same numbers. Keys omitted
-- by a portal fall through to the global defaults, then the code default.
update portals set operational_limits = '{
  "index_rate": 2.0,
  "detail_workers": 8,
  "detail_rate": 6.0,
  "max_detail_per_run": 12000,
  "max_detail_per_category": 700,
  "min_completeness": 0.9,
  "image_workers": 32,
  "max_image_downloads": 40000,
  "suspicious_stop_window": 100,
  "suspicious_stop_threshold": 0.30
}'::jsonb where source = 'sreality';

update portals set operational_limits = '{
  "index_rate": 0.5,
  "detail_workers": 2,
  "detail_rate": 0.6,
  "max_detail_per_run": 350,
  "min_completeness": 0.95
}'::jsonb where source = 'bazos';

update portals set operational_limits = '{
  "index_rate": 3.0,
  "detail_workers": 8,
  "detail_rate": 4.0,
  "max_detail_per_run": 6000,
  "min_completeness": 0.9
}'::jsonb where source = 'idnes';

update portals set operational_limits = '{
  "index_rate": 1.0,
  "detail_workers": 8,
  "detail_rate": 2.0,
  "max_detail_per_run": 2000,
  "min_completeness": 0.9
}'::jsonb where source = 'bezrealitky';

-- Global defaults layer ("the scraper as a whole"): a conservative baseline a
-- portal inherits for any key it omits. Reuses app_settings (history trigger +
-- admin GET/PUT plumbing already exist). Values mirror the baked-in code floor.
insert into app_settings (key, value, description, updated_by) values
  (
    'scraper_limits_global',
    '{
      "index_rate": 2.0,
      "detail_workers": 4,
      "detail_rate": 2.0,
      "max_detail_per_run": null,
      "max_detail_per_category": null,
      "min_completeness": 0.9,
      "image_workers": 32,
      "max_image_downloads": 1000,
      "suspicious_stop_window": 100,
      "suspicious_stop_threshold": 0.30
    }'::jsonb,
    'Global default scraper operational limits. A per-portal portals.operational_limits value overrides these; a portal that omits a key inherits it from here. CLI flags still override both.',
    'migration_114'
  )
  on conflict (key) do nothing;

-- Expose the new column on the anon-readable view (registry surface, like 107).
-- CREATE OR REPLACE must keep the existing columns in order and only append.
-- The column set has grown since 107 and differs across environments (prod
-- carries an extra scrape_cadence_minutes not in the committed sequence), so we
-- rebuild dynamically: preserve whatever portals_public currently selects and
-- append operational_limits. Robust to that drift + idempotent, and
-- portal_health_summary()'s dependency on the view is never broken.
do $$
declare existing_cols text;
begin
  select string_agg(quote_ident(column_name), ', ' order by ordinal_position)
    into existing_cols
  from information_schema.columns
  where table_name = 'portals_public' and column_name <> 'operational_limits';
  execute format(
    'create or replace view portals_public as select %s, operational_limits from portals',
    existing_cols
  );
end $$;
grant select on portals_public to anon;
