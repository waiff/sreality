-- 217_retire_portal_stage.sql
--
-- Retire the hand-set `portals.stage` presentation label (live/pilot/on_demand/
-- planned) and let the displayed portal posture be DERIVED from facts that are
-- already the source of truth: `kind` (scraper vs on-demand parser),
-- `is_enabled` (operator lifecycle), and `supports_complete_walk` (the real
-- operational maturity gate the runner already trusts — rule #3/#21). Live
-- health stays in the per-source `scraper_health_checks` rollup (the status dot),
-- so the badge never flickers with health.
--
-- WHY: `stage` was a 100% manual label, decoupled from everything measured.
-- Promoting a portal meant hand-writing a migration (163 did exactly that for
-- iDNES and left the rest "untouched on purpose"), so bazos + bezrealitky — both
-- supports_complete_walk=true with 0.0% stale-active, operationally identical to
-- the "live" pair — sat mislabeled "pilot" indefinitely, while remax + maxima
-- (supports_complete_walk=false) showed the same vague "pilot" that said nothing
-- about their real caveat (no index-absence delisting). The Health category-table
-- tooltip even tied "walks a partial index, delistings aren't inferred" to
-- stage='pilot', which was FALSE for the complete-walk pilots. Deriving the
-- posture kills the manual-promotion treadmill (a new portal shows correctly the
-- moment its config row lands) and the conflation bug in one move.
--
-- The ONLY readers of the column were portals_public (passthrough) and
-- portal_health_mv (display passthrough in the payload); nothing operational
-- ever keyed off it (verified: load_portal_config doesn't even SELECT it, the
-- mark_inactive gate is supports_complete_walk). admin.py's GET /portals SELECT
-- and the frontend badge are updated in the same PR.
--
-- DESTRUCTIVE (drops a column) — operator-approved ("fully derive, retire
-- stage"). Pre-drop stage values, for the record (reconstructable from
-- kind + supports_complete_walk anyway): sreality=live, idnes=live, bazos=pilot,
-- bezrealitky=pilot, maxima=pilot, remax=pilot, mmreality=pilot(disabled),
-- ceskereality=pilot(disabled), idnes_reality=on_demand(parser).

-- portal_health_mv reads portals_public, and portals_public reads portals.stage;
-- rebuild both (minus stage, plus supports_complete_walk) before dropping the
-- column so the drop needs no CASCADE. portal_health_mv has no matview
-- dependents; portals_public is read only by portal_health_mv.

drop materialized view if exists portal_health_mv;
drop view if exists portals_public;

-- portals_public: same passthrough as migration 114, minus `stage`.
create view portals_public as
  select source, label, kind, home_url, sort_order, is_enabled,
         supports_complete_walk, categories, split_threshold,
         scrape_cadence_minutes, operational_limits
  from portals;

grant select on portals_public to anon;

-- portal_health_mv: payload body copied from migration 169, with `stage`
-- replaced by `supports_complete_walk` (the frontend derives the posture badge
-- from it). Everything else is identical.
create materialized view portal_health_mv as
  select 1 as id,
    coalesce(jsonb_agg(jsonb_build_object(
      'source',                 p.source,
      'label',                  p.label,
      'kind',                   p.kind,
      'supports_complete_walk', p.supports_complete_walk,
      'home_url',               p.home_url,
      'listings_total',         coalesce(lc.listings_total, 0::bigint),
      'listings_active',        coalesce(lc.listings_active, 0::bigint),
      'listings_active_7d',     coalesce(lc.listings_active_7d, 0::bigint),
      'parses_total',           coalesce(pa.parses_total, 0::bigint),
      'parses_30d',             coalesce(pa.parses_30d, 0::bigint),
      'last_scrape_at',         sr.last_run_at,
      'runs_7d',                coalesce(sr.runs_7d, 0::bigint),
      'scraped_new_7d',         coalesce(fsn.first_seen_7d, 0::bigint),
      'inactive_7d',            coalesce(sr.inactive_7d, 0::bigint),
      'errors_7d',              coalesce(sr.errors_7d, 0::bigint),
      'last_parsed_at',         pa.last_parsed_at,
      'last_activity_at',       greatest(sr.last_run_at, pa.last_parsed_at, lc.last_seen_at)
    ) order by p.sort_order, p.label), '[]'::jsonb) as payload
  from portals_public p
    left join portal_listing_counts lc on lc.source = p.source
    left join parsed_url_activity   pa on pa.source = p.source
    left join (
      select scrape_runs_public.source,
             max(scrape_runs_public.ended_at) as last_run_at,
             count(*) filter (where scrape_runs_public.started_at > now() - '7 days'::interval) as runs_7d,
             coalesce(sum(scrape_runs_public.listings_inactive) filter (where scrape_runs_public.started_at > now() - '7 days'::interval), 0::bigint) as inactive_7d,
             coalesce(sum(scrape_runs_public.errors) filter (where scrape_runs_public.started_at > now() - '7 days'::interval), 0::bigint) as errors_7d
      from scrape_runs_public
      group by scrape_runs_public.source
    ) sr on sr.source = p.source
    left join (
      select listings_public.source, count(*) as first_seen_7d
      from listings_public
      where listings_public.first_seen_at > now() - '7 days'::interval
      group by listings_public.source
    ) fsn on fsn.source = p.source
  where p.is_enabled;

create unique index portal_health_mv_pk on portal_health_mv (id);
grant select on portal_health_mv to anon;

-- portal_health_summary() still reads portal_health_mv.payload (unchanged); the
-- refresh_health_matviews() pg_cron job still refreshes it by name. Re-assert the
-- function for clarity (idempotent).
create or replace function portal_health_summary()
returns jsonb
language sql
stable
as $$
  select payload from portal_health_mv;
$$;

grant execute on function portal_health_summary() to anon;

-- Now nothing references portals.stage — drop it.
alter table portals drop column stage;
