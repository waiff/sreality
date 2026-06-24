-- 233_category_trends_matview.sql
--
-- Fix the Health page's per-portal "LISTINGS BY CATEGORY · RECONCILIATION"
-- panel, which fails with "category_trends failed: canceling statement due to
-- statement timeout".
--
-- ROOT CAUSE (same class as migrations 214/216). category_trends(p_source,...)
-- is a live SECURITY INVOKER RPC run by the browser's anon role (3s
-- statement_timeout). Per call it scans listings_public filtered by source +
-- joins fetch-failures + unnests 30 days of scrape_runs.by_category and builds
-- hourly/daily series. Measured 7,870 ms for bazos (2.6x the 3s cap) — it is
-- the LAST per-portal Health RPC that was still a live scan (health_summary,
-- portal_health_summary, scraper_health_checks, image_storage_overview,
-- images_failure_overview are already matview-backed; recent_scrape_runs 4.6ms
-- and workflow_failure_summary 3.7ms are genuinely cheap bounded-table reads
-- and stay live by design).
--
-- FIX. Precompute per source into category_trends_mv (one row per source,
-- payload = the same jsonb array the RPC returned), refreshed by the existing
-- every-10-min refresh_health_matviews() pg_cron loop. The RPC becomes a
-- sub-ms single-row lookup under the 3s cap. The p_hours/p_days params are
-- retained for signature compatibility but the Health UI only ever calls with
-- the defaults (72h/30d) — verified the sole caller is
-- frontend/src/lib/queries.ts fetchCategoryTrends(source), which passes only
-- p_source — so the matview materialises those default windows. (No DB-side
-- caller exists; verified across pg_proc/views/cron.)
--
-- DATA-QUALITY FIX (found while validating the assumption that one run has one
-- by_category entry per canonical category — FALSE for multi-scope portals).
-- bazos walks 14 nationwide scopes that collapse into 6 canonical (cm,ct):
-- komercni = restaurace+kancelar+prostory+sklad, dum includes chata. So a
-- single bazos run's by_category has up to 4 entries for one (cm,ct). The old
-- `distinct on (cm,ct) order by started_at desc` then picked ONE arbitrary
-- scope's reconciliation numbers (nondeterministic, and under-reported
-- portal_total/collected by ~3-4x; e.g. komercni/prodej showed ~102-535
-- instead of the true 1468), and the hourly/daily series carried duplicate
-- points per scope. The matview adds a runs_agg step that SUMS portal/
-- collected/db across scopes sharing a canonical (cm,ct) within a run, so the
-- reconciliation is deterministic and totals are correct. For single-scope
-- sources (sreality/idnes/bezrealitky/maxima/remax — verified max 1 entry per
-- category per run) this is sum-of-one-row = unchanged (verified idnes
-- byte-identical to the old RPC, including the series).

create materialized view if not exists category_trends_mv as
with
runs as (
  select sr.source, sr.started_at,
    (c.value->>'category_main') as cm,
    (c.value->>'category_type') as ct,
    nullif(c.value->>'sreality_result_size','')::int as portal,
    nullif(c.value->>'collected','')::int            as collected,
    nullif(c.value->>'active_db','')::int            as db
  from scrape_runs_public sr
  cross join lateral jsonb_array_elements(sr.by_category) c(value)
  where sr.index_pages > 0
    and (c.value->>'sreality_result_size') is not null
    and sr.started_at >= now() - interval '30 days'
),
runs_agg as (
  select source, started_at, cm, ct,
    sum(portal)::int    as portal,
    sum(collected)::int as collected,
    sum(db)::int        as db
  from runs
  group by source, started_at, cm, ct
),
listing_agg as (
  select source, category_main as cm, category_type as ct,
    count(*)::int                                                                   as total_in_db,
    count(*) filter (where is_active)::int                                          as active_now,
    count(*) filter (where first_seen_at::date = now()::date)::int                  as new_today,
    count(*) filter (where first_seen_at >= (now() - interval '6 days')::date)::int as new_7d,
    count(*) filter (where not is_active and last_seen_at::date = now()::date)::int  as flipped_today,
    count(*) filter (where not is_active and last_seen_at >= (now() - interval '6 days')::date)::int as flipped_7d
  from listings_public
  where category_main is not null and category_type is not null
  group by source, category_main, category_type
),
failure_agg as (
  select l.source, l.category_main as cm, l.category_type as ct,
    count(*)::int                                  as total,
    count(*) filter (where f.given_up = true)::int as given_up
  from listing_fetch_failures_public f
  join listings_public l on l.sreality_id = f.sreality_id
  group by l.source, l.category_main, l.category_type
),
latest_run as (
  select distinct on (source, cm, ct) source, cm, ct, portal as portal_total, collected
  from runs_agg
  order by source, cm, ct, started_at desc
),
hourly as (
  select source, cm, ct,
    jsonb_agg(jsonb_build_object('t', started_at, 'portal', portal, 'db', db) order by started_at) as series
  from runs_agg
  where started_at >= now() - interval '72 hours'
  group by source, cm, ct
),
daily_pick as (
  select distinct on (source, cm, ct, date_trunc('day', started_at))
    source, cm, ct, date_trunc('day', started_at) as bucket, portal, db
  from runs_agg
  order by source, cm, ct, date_trunc('day', started_at), started_at desc
),
daily as (
  select source, cm, ct,
    jsonb_agg(jsonb_build_object('t', bucket, 'portal', portal, 'db', db) order by bucket) as series
  from daily_pick
  group by source, cm, ct
)
select lr.source,
  coalesce(jsonb_agg(jsonb_build_object(
    'category_main', lr.cm, 'category_type', lr.ct,
    'total_in_db', coalesce(la.total_in_db, 0),
    'active_now', coalesce(la.active_now, 0),
    'new_today', coalesce(la.new_today, 0),
    'new_7d', coalesce(la.new_7d, 0),
    'flipped_today', coalesce(la.flipped_today, 0),
    'flipped_7d', coalesce(la.flipped_7d, 0),
    'failures_total', coalesce(fa.total, 0),
    'failures_given_up', coalesce(fa.given_up, 0),
    'portal_total', lr.portal_total,
    'collected', lr.collected,
    'hourly', coalesce(h.series, '[]'::jsonb),
    'daily', coalesce(d.series, '[]'::jsonb)
  ) order by coalesce(la.active_now, 0) desc), '[]'::jsonb) as payload
from latest_run lr
left join listing_agg la on la.source = lr.source and la.cm = lr.cm and la.ct = lr.ct
left join failure_agg fa on fa.source = lr.source and fa.cm = lr.cm and fa.ct = lr.ct
left join hourly h on h.source = lr.source and h.cm = lr.cm and h.ct = lr.ct
left join daily d  on d.source = lr.source and d.cm = lr.cm and d.ct = lr.ct
group by lr.source;

create unique index if not exists category_trends_mv_source_idx on category_trends_mv (source);
grant select on category_trends_mv to anon, authenticated;

-- Repoint the RPC to the precomputed row. Sub-ms under the 3s anon cap. Returns
-- '[]' for a source with no row (matching the old function's empty-array result
-- for a source with no reconciliation runs). Signature unchanged (p_hours/p_days
-- retained for compat; the matview serves the default 72h/30d windows the UI uses).
create or replace function public.category_trends(
  p_source text default 'sreality',
  p_hours  integer default 72,
  p_days   integer default 30
)
returns jsonb
language sql
stable
as $function$
  select coalesce(
    (select payload from category_trends_mv where source = p_source),
    '[]'::jsonb
  );
$function$;

grant execute on function public.category_trends(text, integer, integer) to anon, authenticated;

-- Wire into the existing every-10-min refresh loop. Whole body redefined (it
-- has been create-or-replace'd across 136/176/180/214). category_trends_mv has
-- no dependency on other matviews; placed before the refresh stamp.
create or replace function public.refresh_health_matviews()
returns void
language plpgsql
security definer
set search_path = public
as $function$
begin
  refresh materialized view concurrently health_summary_mv;
  refresh materialized view concurrently portal_health_mv;
  refresh materialized view concurrently snapshot_churn_24h_mv;
  refresh materialized view concurrently scraper_health_checks_mv;
  refresh materialized view concurrently category_trends_mv;
  refresh materialized view concurrently health_mv_refresh_stamp;
end;
$function$;
