-- 219_portal_data_coverage.sql
--
-- Surface per-portal DATA-QUALITY coverage on the Health dashboard. Today the
-- only per-source field signal is `field_null_drift` — a same-DAY delta on 5 of
-- 29 fields — so a portal that has shipped (say) 0% parsed street since day one
-- never "drifts" and sits invisibly green, even though low street coverage
-- materially gates cross-portal dedup (rule #15, which needs BOTH street AND
-- disposition). This adds the ABSOLUTE coverage numbers as informational metrics
-- (not pass/warn/fail checks — condition/scoring scope would make most portals
-- falsely warn; these describe data quality, they don't gate the rollup dot).
--
-- Cheap: `portal_listing_counts` already scans `listings` grouped by source for
-- the catalogue counts; these are three more FILTERed aggregates over the SAME
-- scan. Exposed through the existing portal_health_mv payload (no new fetch on
-- the frontend), refreshed by the same pg_cron refresh_health_matviews().
--
-- Coverage chosen for SCRAPER/PARSER data quality (active listings only):
--   geo_pct           — coordinate extraction (the admin-hierarchy anchor, mig 140)
--   street_pct        — parsed street (the harder field; gates dedup)
--   dedup_eligible_pct — street AND disposition (THE dedup gate, rule #15)
-- (condition/image coverage are downstream-pipeline metrics with their own
-- scoping — deferred, not scraper coverage.)

-- portal_listing_counts: add the three coverage columns AT THE END (CREATE OR
-- REPLACE VIEW permits appending columns, so portal_health_mv's dependency on it
-- is untouched until we rebuild the matview below).
create or replace view portal_listing_counts as
  select
    source,
    count(*)                                                            as listings_total,
    count(*) filter (where is_active)                                   as listings_active,
    count(*) filter (where is_active and last_seen_at > now() - interval '7 days')
                                                                        as listings_active_7d,
    max(last_seen_at)                                                   as last_seen_at,
    round(100.0 * count(*) filter (where is_active and geom is not null)
          / nullif(count(*) filter (where is_active), 0), 1)            as geo_pct,
    round(100.0 * count(*) filter (where is_active and street is not null)
          / nullif(count(*) filter (where is_active), 0), 1)            as street_pct,
    round(100.0 * count(*) filter (where is_active and street is not null and disposition is not null)
          / nullif(count(*) filter (where is_active), 0), 1)            as dedup_eligible_pct
  from listings
  group by source;

grant select on portal_listing_counts to anon;

-- portal_health_mv: same payload as migration 217, plus the three coverage
-- numbers from lc.
drop materialized view if exists portal_health_mv;

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
      'geo_pct',                lc.geo_pct,
      'street_pct',             lc.street_pct,
      'dedup_eligible_pct',     lc.dedup_eligible_pct,
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

create or replace function portal_health_summary()
returns jsonb
language sql
stable
as $$
  select payload from portal_health_mv;
$$;

grant execute on function portal_health_summary() to anon;
