-- 214_scraper_health_checks_matview.sql
--
-- Fix the Health dashboard's per-portal "SCRAPE HEALTH CHECKS" panel, which
-- shows "scraper_health_checks failed: canceling statement due to statement
-- timeout" for every large portal.
--
-- ROOT CAUSE. scraper_health_checks(p_source) is the ONE health RPC that never
-- got the migration-136 treatment the other two (health_summary,
-- portal_health_summary) received. It is still a LIVE multi-scan run by the
-- browser's anon role (statement_timeout = 3 s). Per source it scans listings
-- (5.4 GB), listing_snapshots, the detail queue, fetch-failures and images.
-- Measured against production: idnes 16.7 s cold / 10.4 s warm, sreality
-- 21.4 s / 9.8 s — the two national portals (>100k active rows each) have a
-- working set ~10x the 512 MB cache, so they never warm and ALWAYS exceed the
-- 3 s cap. The page polls every 60 s and fires one call per scraper source, so
-- every poll re-runs the doomed scan and times out. A side effect: on timeout
-- the collapsed portal's roll-up dot silently falls to grey "idle", hiding a
-- portal that actually has failing checks.
--
-- FIX. Bring scraper_health_checks under the exact same proven pattern
-- migration 136 established for the other two RPCs: precompute into a
-- materialized view refreshed by the existing refresh_health_matviews()
-- pg_cron loop (every 10 min), and repoint the RPC to read the precomputed row
-- (a sub-ms index lookup, comfortably under the 3 s anon cap). Zero frontend
-- change — the jsonb contract is preserved byte-for-byte; generated_at is
-- overlaid from health_mv_refresh_stamp exactly as health_summary() does, so
-- the card's "checked Xm ago" reflects true data age and the whole page tells
-- one staleness story. A monitor refreshed every 10 min is far finer-grained
-- than what it watches (sreality ~60 min cadence, pilots ~6 h), so the small
-- staleness is immaterial; and 10-min-stale-but-present beats a guaranteed
-- timeout that shows nothing.
--
-- SET-BASED, not a per-source loop. The matview body computes ALL sources in
-- ONE GROUP BY source pass per base relation rather than re-scanning the
-- bloated listings table once per portal. That makes the refresh O(tables) not
-- O(sources x tables) and keeps it ~flat as portals grow — the genuine
-- scalability fix, and it leaves no second-migration debt. The body is the
-- exact 16-check semantics of the live function (migration 176, carried
-- forward through helpers 170/179/180), transcribed grouped-by-source; a
-- per-source payload-equivalence check (old live RPC vs new RPC, minus the
-- time-relative jitter) gates the rollout.
--
-- FOLLOW-UP (separate, destructive migration — needs operator OK + backup):
-- the 4 SECURITY DEFINER stat helpers (delisting_latency_stat,
-- snapshot_churn_stat, field_null_drift_stat, unattached_listings_stat) exist
-- ONLY to let the live anon function read privileged aggregates across the
-- anon boundary. Their computation now happens here at owner-privileged
-- refresh time, and each helper has exactly one caller (the old function body)
-- and zero view/cron/API callers — so they become orphaned scaffolding and can
-- be dropped once this matview is verified in production.

------------------------------------------------------------------
-- 1. scraper_health_checks_mv — one row per scraper source, payload jsonb =
--    {source, checks:[16]} (generated_at overlaid at read time). Built
--    set-based: portals (kind='scraper') LEFT JOIN per-source aggregates, so
--    every scraper source gets a full 16-check payload even at zero listings.
------------------------------------------------------------------

create materialized view if not exists scraper_health_checks_mv as
with
sources as (
  select source, coalesce(scrape_cadence_minutes, 60) as cad_mins
  from portals
  where kind = 'scraper'
),
runs_agg as (
  select
    source,
    max(started_at) filter (where index_pages > 0) as last_start,
    count(*) filter (where ended_at is null
                       and started_at < now() - interval '30 minutes'
                       and started_at > now() - interval '6 hours') as stuck,
    coalesce(sum(listings_scraped_new) filter (where started_at > now() - interval '24 hours'), 0) as scraped_new,
    coalesce(sum(listings_updated)     filter (where started_at > now() - interval '24 hours'), 0) as updated,
    coalesce(max(listings_inactive)    filter (where started_at > now() - interval '24 hours'), 0) as inactive_max,
    coalesce(sum(errors)               filter (where started_at > now() - interval '24 hours'), 0) as errors_sum
  from scrape_runs_public
  group by source
),
listings_agg as (
  select
    source,
    count(*) filter (where first_seen_at > now() - interval '24 hours') as new_listings_fs,
    count(*) filter (where is_active and last_seen_at < now() - interval '7 days') as stale_active,
    max(last_seen_at) filter (where is_active) as last_fresh
  from listings_public
  group by source
),
fails_agg as (
  select
    coalesce(l.source, 'sreality') as source,
    count(*) filter (where not f.given_up) as active_fail,
    count(*) filter (where f.given_up) as given_up
  from listing_fetch_failures_public f
  left join listings_public l on l.sreality_id = f.sreality_id
  group by coalesce(l.source, 'sreality')
),
queue_agg as (
  select
    source,
    count(*) filter (where claimed_at is null and not given_up) as claimable,
    count(*) filter (where claimed_at is null and not given_up and priority = 1) as changed,
    count(*) filter (where given_up) as q_given_up
  from listing_detail_queue_public
  group by source
),
lag_agg as (
  select
    q.source,
    coalesce(round((percentile_cont(0.5) within group (order by extract(epoch from now() - q.enqueued_at)/60.0))::numeric, 1), 0) as p50_min,
    coalesce(round((percentile_cont(0.9) within group (order by extract(epoch from now() - q.enqueued_at)/60.0))::numeric, 1), 0) as p90_min,
    count(*) filter (where q.enqueued_at < now() - make_interval(mins => (s.cad_mins * 3)::int))::int as unhealthy_n,
    count(*)::int as n
  from listing_detail_queue_public q
  join sources s on s.source = q.source
  where q.claimed_at is null and not q.given_up and q.priority <> 2
  group by q.source
),
attach_agg as (
  select
    source,
    count(*)::int as n,
    coalesce(round(extract(epoch from now() - min(first_seen_at))/60.0, 1), 0) as oldest_min
  from listings
  where is_active and property_id is null
  group by source
),
delist_agg as (
  select
    source,
    count(*)::int as n,
    coalesce(round((percentile_cont(0.5) within group (order by extract(epoch from inactive_at - last_seen_at)/60.0))::numeric, 1), 0) as p50_min,
    coalesce(round((percentile_cont(0.9) within group (order by extract(epoch from inactive_at - last_seen_at)/60.0))::numeric, 1), 0) as p90_min
  from listings
  where inactive_at is not null
    and inactive_at > now() - interval '7 days'
  group by source
),
churn_agg as (
  select source, snaps_24h, active_n,
         round(snaps_24h / nullif(active_n, 0)::numeric, 2) as ratio
  from snapshot_churn_24h_mv
),
drift_fresh as (
  select distinct on (source, field) source, field, pct_populated
  from data_quality_snapshots
  where field in ('price_czk', 'area_m2', 'geom', 'locality', 'disposition')
    and captured_at > now() - interval '20 hours'
  order by source, field, captured_at desc
),
drift_baseline as (
  select distinct on (source, field) source, field, pct_populated
  from data_quality_snapshots
  where field in ('price_czk', 'area_m2', 'geom', 'locality', 'disposition')
    and captured_at < now() - interval '20 hours'
    and captured_at > now() - interval '8 days'
  order by source, field, captured_at desc
),
drift_agg as (
  select
    f.source,
    count(*)::int as n_fields,
    coalesce(max(b.pct_populated - f.pct_populated), 0) as max_drift,
    (array_agg(f.field order by (b.pct_populated - f.pct_populated) desc))[1] as worst_field
  from drift_fresh f
  join drift_baseline b using (source, field)
  group by f.source
),
recon_agg as (
  select
    s.source,
    count(d.gap_pct) as n_with_data,
    max(d.gap_pct) as max_gap_pct
  from sources s
  left join lateral (
    select by_category
    from scrape_runs_public
    where ended_at is not null and index_pages > 0 and source = s.source
    order by started_at desc
    limit 1
  ) latest on true
  left join lateral (
    select abs((e->>'collected')::numeric - (e->>'sreality_result_size')::numeric)
             / nullif((e->>'sreality_result_size')::numeric, 0) * 100.0 as gap_pct
    from jsonb_array_elements(coalesce(latest.by_category, '[]'::jsonb)) e
    where (e->>'sreality_result_size') is not null and (e->>'collected') is not null
      and (e->>'sreality_result_size')::numeric > 0
  ) d on true
  group by s.source
),
calc as (
  select
    s.source,
    s.cad_mins,
    ra.last_start,
    extract(epoch from now() - ra.last_start)/60.0 as mins_since_start,
    coalesce(ra.stuck, 0)        as stuck,
    coalesce(ra.scraped_new, 0)  as scraped_new,
    coalesce(ra.updated, 0)      as updated,
    coalesce(ra.inactive_max, 0) as inactive_max,
    coalesce(ra.errors_sum, 0)   as errors_sum,
    round(100.0 * coalesce(ra.errors_sum, 0)
          / nullif(coalesce(ra.errors_sum, 0) + coalesce(ra.scraped_new, 0) + coalesce(ra.updated, 0), 0), 1) as err_pct,
    coalesce(la.new_listings_fs, 0) as new_listings_fs,
    coalesce(la.stale_active, 0)    as stale_active,
    extract(epoch from now() - la.last_fresh)/60.0 as mins_fresh,
    coalesce(fa.active_fail, 0) as active_fail,
    coalesce(fa.given_up, 0)    as given_up,
    coalesce(qa.claimable, 0)   as q_claimable,
    coalesce(qa.changed, 0)     as q_changed,
    coalesce(qa.q_given_up, 0)  as q_given_up,
    coalesce(lg.p50_min, 0)     as lag_p50,
    coalesce(lg.p90_min, 0)     as lag_p90,
    coalesce(lg.unhealthy_n, 0) as lag_unhealthy,
    coalesce(lg.n, 0)           as lag_n,
    coalesce(at.n, 0)           as attach_n,
    coalesce(at.oldest_min, 0)  as attach_oldest,
    coalesce(dl.n, 0)           as delist_n,
    coalesce(dl.p50_min, 0)     as delist_p50,
    coalesce(dl.p90_min, 0)     as delist_p90,
    coalesce(ch.snaps_24h, 0)   as churn_snaps,
    coalesce(ch.active_n, 0)    as churn_active,
    coalesce(ch.ratio, 0)       as churn_ratio,
    coalesce(dr.n_fields, 0)    as drift_nfields,
    coalesce(dr.max_drift, 0)   as drift_max,
    dr.worst_field             as drift_worst,
    coalesce(rc.n_with_data, 0) as recon_n,
    rc.max_gap_pct             as recon_gap
  from sources s
  left join runs_agg     ra on ra.source = s.source
  left join listings_agg la on la.source = s.source
  left join fails_agg    fa on fa.source = s.source
  left join queue_agg    qa on qa.source = s.source
  left join lag_agg      lg on lg.source = s.source
  left join attach_agg   at on at.source = s.source
  left join delist_agg   dl on dl.source = s.source
  left join churn_agg    ch on ch.source = s.source
  left join drift_agg    dr on dr.source = s.source
  left join recon_agg    rc on rc.source = s.source
)
select
  c.source,
  jsonb_build_object(
    'source', c.source,
    'checks', jsonb_build_array(
      jsonb_build_object(
        'key', 'liveness', 'label', 'Scraper running on schedule',
        'status', case when c.last_start is null then 'warn'
                       when c.mins_since_start < c.cad_mins * 1.5 then 'pass'
                       when c.mins_since_start < c.cad_mins * 3 then 'warn' else 'fail' end,
        'value', case when c.last_start is null then 'never'
                      else coalesce(round(c.mins_since_start::numeric, 0)::text, '–') || ' min ago' end,
        'detail', 'Last index walk started ' || coalesce(to_char(c.last_start, 'YYYY-MM-DD HH24:MI'), 'never')
                  || ' UTC. Expected cadence ~' || c.cad_mins::text || ' min (GitHub throttles short crons). '
                  || 'Warn >' || round(c.cad_mins * 1.5)::text || ' min, fail >' || round(c.cad_mins * 3)::text || ' min.'),
      jsonb_build_object('key', 'runs_completing', 'label', 'Runs finishing cleanly',
        'status', case when c.stuck = 0 then 'pass' when c.stuck = 1 then 'warn' else 'fail' end,
        'value', c.stuck::text || ' stuck',
        'detail', 'Index-walk or detail-drain runs started >30 min ago (last 6h) that never recorded an end timestamp — a crash or timeout before finalize. Expected 0.'),
      jsonb_build_object('key', 'new_listings', 'label', 'New listings flowing',
        'status', case when c.new_listings_fs > 0 then 'pass' else 'warn' end,
        'value', c.new_listings_fs::text || ' / 24h',
        'detail', 'New listings first seen in the last 24h (from listings.first_seen_at — immune to a crashed or SIGKILLed drain''s lost run counters). 0 over a full day suggests the index-walk enqueue or the detail-drain is blocked.'),
      jsonb_build_object('key', 'delisting_spike', 'label', 'No false mass-delisting',
        'status', case when c.inactive_max <= 500 then 'pass' when c.inactive_max <= 2000 then 'warn' else 'fail' end,
        'value', c.inactive_max::text || ' max/run',
        'detail', 'Largest single-run inactivation in 24h (the index-walk''s mark_inactive). A big spike usually means a truncated index walk falsely delisted live listings; the walk-completeness guard mitigates this. Warn >500, fail >2000.'),
      jsonb_build_object('key', 'delisting_latency', 'label', 'Delisting latency (gone → flipped)',
        'status', case when c.delist_n = 0 then 'pass'
                       when c.delist_p90 < 2160 then 'pass'
                       when c.delist_p90 < 4320 then 'warn' else 'fail' end,
        'value', case when c.delist_n = 0 then 'no flips recorded yet'
                      else 'p50 ' || c.delist_p50::text || 'm / p90 ' || c.delist_p90::text || 'm' end,
        'detail', 'How long a delisted listing stayed nominally active: inactive_at − last_seen_at over the '
                  || c.delist_n::text || ' listings flipped inactive in the last 7 days. Rows flipped before migration 175 carry no stamp and are ignored. Warn p90 >36h (2160 min), fail >72h (4320 min).'),
      jsonb_build_object('key', 'error_rate', 'label', 'Detail-fetch error rate',
        'status', case when coalesce(c.err_pct, 0) < 5 then 'pass' when coalesce(c.err_pct, 0) < 15 then 'warn' else 'fail' end,
        'value', coalesce(c.err_pct, 0)::text || '%',
        'detail', 'Errors as a share of detail work (errors + new + updated) over 24h. Elevated values usually mean the portal is rate-limiting. Warn >5%, fail >15%.'),
      jsonb_build_object('key', 'snapshot_churn', 'label', 'Snapshot churn (hash thrash)',
        'status', case when coalesce(c.churn_ratio, 0) < 0.5 then 'pass'
                       when coalesce(c.churn_ratio, 0) < 1.5 then 'warn' else 'fail' end,
        'value', coalesce(c.churn_ratio, 0)::text || '× / 24h',
        'detail', c.churn_snaps::text || ' snapshots written in the last 24h across ' || c.churn_active::text
                  || ' active listings. A ratio near 1 means the average listing re-snapshots DAILY — almost always a volatile field thrashing the content hash (the idnes A/B/A storm ran for weeks undetected), not real market churn. Warn ≥0.5, fail ≥1.5.'),
      jsonb_build_object('key', 'stale_active', 'label', 'No stale active listings',
        'status', case when c.stale_active < 50 then 'pass' when c.stale_active < 500 then 'warn' else 'fail' end,
        'value', c.stale_active::text,
        'detail', 'Listings still is_active=true but not seen in the index for >7 days — they should have been marked inactive. Warn >50, fail >500.'),
      jsonb_build_object('key', 'field_null_drift', 'label', 'Field completeness drift',
        'status', case when c.drift_nfields = 0 then 'pass'
                       when c.drift_max < 5 then 'pass'
                       when c.drift_max < 15 then 'warn' else 'fail' end,
        'value', case when c.drift_nfields = 0 then 'no baseline yet'
                      else c.drift_worst || ' −' || round(greatest(c.drift_max, 0), 1)::text || ' pts' end,
        'detail', 'Largest drop in field population (percentage points) vs the daily data-quality baseline (data_quality_snapshots, latest capture 20h–8d old), across price_czk / area_m2 / geom / locality / disposition. Catches a parser silently losing a field within a day — the bazos locality breakage took weeks to surface this way. Warn ≥5 pts, fail ≥15 pts.'),
      jsonb_build_object('key', 'fetch_failures', 'label', 'Fetch-failure backlog',
        'status', case when c.active_fail < 1000 then 'pass' when c.active_fail < 5000 then 'warn' else 'fail' end,
        'value', c.active_fail::text || ' active',
        'detail', c.given_up::text || ' listings given up after repeated failures. Active failures retry with priority next run. Warn >1000, fail >5000.'),
      jsonb_build_object('key', 'detail_queue_backlog', 'label', 'Detail-drain backlog',
        'status', case when c.q_claimable < 2000 then 'pass' when c.q_claimable < 10000 then 'warn' else 'fail' end,
        'value', c.q_claimable::text || ' queued',
        'detail', 'New + price-changed listings the index walk enqueued but the detail-drain has not fetched yet ('
                  || c.q_changed::text || ' price-changed). A new listing becomes an active row only once drained, so THIS backlog — not data loss — is what opens the gap in "Index walk completeness". The drain closes it; raise its cap/cadence if it grows. '
                  || c.q_given_up::text || ' given up. Warn >2k, fail >10k.'),
      jsonb_build_object('key', 'detail_queue_lag', 'label', 'Detail-drain lag (index→fetch)',
        'status', case when c.lag_n = 0 then 'pass'
                       when c.lag_p90 < c.cad_mins * 1.5 then 'pass'
                       when c.lag_p90 < c.cad_mins * 3 then 'warn' else 'fail' end,
        'value', case when c.lag_n = 0 then 'empty'
                      else 'p50 ' || c.lag_p50::text || 'm / p90 ' || c.lag_p90::text || 'm' end,
        'detail', 'Time between the index walk enqueueing a listing and the detail-drain fetching it, over listings still waiting (in-flight only — completed queue rows are deleted, so a caught-up drain reads empty). '
                  || c.lag_unhealthy::text || ' have waited >' || round(c.cad_mins * 3)::text || ' min (~3 missed cycles). '
                  || 'Fresh + price-changed rows only (excludes failure-retry). Warn p90 >' || round(c.cad_mins * 1.5)::text || ' min, fail p90 >' || round(c.cad_mins * 3)::text || ' min.'),
      jsonb_build_object('key', 'property_attach_lag', 'label', 'Property attach lag (Browse-visible)',
        'status', case when c.attach_n = 0 then 'pass'
                       when c.attach_oldest < 30 then 'pass'
                       when c.attach_oldest < 90 then 'warn' else 'fail' end,
        'value', case when c.attach_n = 0 then 'all attached'
                      else c.attach_n::text || ' waiting, oldest ' || c.attach_oldest::text || 'm' end,
        'detail', 'A scraped listing lands with no properties row and is invisible in Browse (which reads the property grain) until the async property-maintenance job (recompute_property_stats --incremental, ~every 5 min; daily full sweep as backstop) attaches it as a singleton. The remaining gap between "scraped into listings" and "Browse-visible" — pairs with the detail-drain lag above for end-to-end latency. Warn oldest >30 min, fail >90 min.'),
      jsonb_build_object('key', 'e2e_latency', 'label', 'End-to-end latency (portal → Browse)',
        'status', case when (c.lag_p90 + c.attach_oldest) < 90 then 'pass'
                       when (c.lag_p90 + c.attach_oldest) < 240 then 'warn' else 'fail' end,
        'value', round((c.lag_p90 + c.attach_oldest)::numeric, 0)::text || ' min',
        'detail', 'Composed pipeline latency: detail-drain p90 (' || c.lag_p90::text
                  || 'm, index-seen → fetched) + oldest unattached listing (' || c.attach_oldest::text
                  || 'm, fetched → Browse-visible). The two segment checks above are the components; this is the single "how far behind the portal is Browse" number. Warn ≥90 min, fail ≥240 min.'),
      jsonb_build_object('key', 'data_freshness', 'label', 'Data freshness',
        'status', case when c.mins_fresh is null then 'warn'
                       when c.mins_fresh < c.cad_mins then 'pass'
                       when c.mins_fresh < c.cad_mins * 3 then 'warn' else 'fail' end,
        'value', case when c.mins_fresh is null then '–'
                      else coalesce(round(c.mins_fresh::numeric, 0)::text, '–') || ' min' end,
        'detail', 'Time since the most recently seen active listing. Warn >' || c.cad_mins::text || ' min, fail >' || round(c.cad_mins * 3)::text || ' min.'),
      jsonb_build_object('key', 'index_completeness', 'label', 'Index walk completeness',
        'status', case when c.recon_n = 0 then 'warn'
                    when coalesce(c.recon_gap, 0) < 2 then 'pass'
                    when coalesce(c.recon_gap, 0) < 5 then 'warn' else 'fail' end,
        'value', case when c.recon_n = 0 then 'no data yet'
                      else round(coalesce(c.recon_gap, 0), 1)::text || '% max gap' end,
        'detail', 'Largest per-category gap between how many index entries we collected and the portal''s reported result_size on the latest completed index walk — i.e. did the walk SEE every listing. Whether we have FETCHED them is the separate detail-drain backlog. Populates once the walk records per-category result_size. Warn >2%, fail >5%.')
    )
  ) as payload
from calc c;

create unique index if not exists scraper_health_checks_mv_source_idx
  on scraper_health_checks_mv (source);

grant select on scraper_health_checks_mv to anon, authenticated;

------------------------------------------------------------------
-- 2. Repoint the RPC to read the precomputed row. Sub-ms index lookup under
--    the 3 s anon cap. payload is the LEFT operand of || so a source absent
--    from the matview yields no row -> SQL NULL (the frontend renders "No
--    checks available." + an idle dot), never a partial object. generated_at
--    is overlaid from the shared refresh stamp, mirroring health_summary().
------------------------------------------------------------------

create or replace function public.scraper_health_checks(p_source text default 'sreality')
returns jsonb
language sql
stable
as $function$
  select payload || jsonb_build_object(
    'generated_at', (select refreshed_at from health_mv_refresh_stamp)
  )
  from scraper_health_checks_mv
  where source = p_source;
$function$;

grant execute on function public.scraper_health_checks(text) to anon, authenticated;

------------------------------------------------------------------
-- 3. Wire the new matview into the existing every-10-min refresh loop. Whole
--    body redefined (it has been create-or-replace'd in 136/176/180 — do not
--    assume incrementality). scraper_health_checks_mv reads snapshot_churn_24h_mv
--    so it is refreshed AFTER it and BEFORE the refresh stamp. CONCURRENTLY
--    (the unique index above enables it) so anon readers are never blocked.
------------------------------------------------------------------

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
  refresh materialized view concurrently health_mv_refresh_stamp;
end;
$function$;
