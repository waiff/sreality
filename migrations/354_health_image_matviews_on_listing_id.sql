-- 354_health_image_matviews_on_listing_id.sql
--
-- Listing-identity refactor, GATE 2 blocker 3.11. Four Health/image
-- materialized views still join `listings`/`images` (and group snapshots) on the
-- portal-native `sreality_id`. Post-Gate-2 a new non-sreality row inserts with
-- `sreality_id = NULL`, so every one of these keys silently breaks:
--
--   * snapshot_churn_24h_mv — INNER join listing_snapshots↔listings on sreality_id.
--     A NULL-sreality listing's snapshots never match, snaps_24h collapses toward 0,
--     the churn ratio → 0, and scraper_health_checks_mv grades a thrashing portal
--     'pass' (FAILS OPEN — the exact hash-thrash alarm this check exists to raise).
--   * images_failure_overview_mv — INNER join images↔listings on sreality_id. Failed
--     images on NULL-sreality listings vanish from the failure dashboard.
--   * image_storage_overview_mv — LEFT join listings_public↔images on sreality_id.
--     New listings report stored=0/total=0 (undercount; violates rule #6 coverage).
--   * health_summary_mv.snap_density — GROUP BY listing_snapshots_public.sreality_id
--     collapses ALL NULL-keyed snapshots into one bucket.
--
-- FIX: repoint each onto the surrogate identity — `listings.id` /
-- `listing_snapshots.listing_id` / `images.listing_id` (all present + 0-NULL today,
-- migrations 312/320/334). Behaviour is IDENTICAL for every current row: id and
-- sreality_id resolve to the same listing today (verified live — churn per-source,
-- images_failure 106309=106309, snap_density 570741=570741 groups, 0 image rows
-- where the two keys disagree); the repoint only starts to matter after the Gate-2
-- flip, when it becomes the CORRECT key.
--
-- These are MATERIALIZED views — the join key lives in a stored rule that
-- CREATE OR REPLACE cannot rewrite, so each is DROP + CREATE. snapshot_churn_24h_mv
-- feeds scraper_health_checks_mv, so the dependent is dropped first and recreated
-- last; scraper_health_checks_mv is otherwise reproduced VERBATIM (it is only
-- touched because the drop of its input forces it). NOT REPOINTED here and left
-- verbatim: the fetch-failure joins in scraper_health_checks_mv.fails_agg and
-- health_summary_mv.cat_failures/failures_top10 key on listing_fetch_failures,
-- which has NO listing_id column yet — a separate Gate-2 item must add it first.
--
-- ACL: a fresh matview inherits this project's default privileges, which grant
-- anon + authenticated the FULL relation privilege set (verified via
-- pg_default_acl). The live post-remediation posture (migrations 331/332/342) is
-- that these ops matviews are DARK to both browser roles — read only through the
-- SECURITY DEFINER health RPCs / owner-rights wrappers, never by name. So each
-- recreate re-issues `revoke all ... from anon, authenticated` to reproduce the
-- live ACL (postgres owner + service_role only). `revoke all` (not a named list)
-- is PG15/PG17-safe — it never names the version-specific MAINTAIN privilege.
-- Each `create` is annotated `-- ci-allow-ungated:` because
-- tests/test_migration_rls_grants.py flags any create that names an admin-only
-- relation (here, the matview's own name) without an embedded is_platform_admin()
-- gate — a matview cannot carry one; the ACL revoke above is its protection.
--
-- Refresh cadence and pg_cron are UNCHANGED: names are identical, so
-- refresh_health_matviews() (pg_cron, */10) and refresh_image_stats.py
-- (images.yml, 2-hourly) keep resolving them by name. Each keeps its UNIQUE index
-- so REFRESH ... CONCURRENTLY still applies.

begin;

set local lock_timeout = '5s';  -- fail fast rather than queue behind a live REFRESH

-- ===========================================================================
-- 1. images_failure_overview_mv (independent) — INNER join onto listing_id.
-- ===========================================================================
drop materialized view if exists images_failure_overview_mv;

-- ci-allow-ungated: images_failure_overview_mv admin-only ops matview; kept dark to browser roles by the revoke below, a matview cannot embed is_platform_admin().
create materialized view images_failure_overview_mv as
  select
    l.source,
    case
      when i.unavailable_reason is not null then 'unavailable'
      when i.download_attempts >= 5 then 'exhausted'
      else 'pending'
    end as bucket,
    case
      when i.unavailable_reason is not null then i.unavailable_reason
      when i.last_error is null then ''
      when i.last_error ~ '^[0-9]{3}' then 'HTTP ' || left(i.last_error, 3)
      else 'other'
    end as detail,
    count(*)::bigint as n
  from images i
  join listings l on l.id = i.listing_id
  where i.storage_path is null
  group by 1, 2, 3;

create unique index if not exists images_failure_overview_mv_key
  on images_failure_overview_mv (source, bucket, detail);

revoke all on images_failure_overview_mv from anon, authenticated;

-- ===========================================================================
-- 2. image_storage_overview_mv (independent) — LEFT join onto listing_id.
--    Body carried forward from migration 236 (reads listings_public + base
--    images), join key only changed.
-- ===========================================================================
drop materialized view if exists image_storage_overview_mv;

-- ci-allow-ungated: image_storage_overview_mv admin-only ops matview; kept dark to browser roles by the revoke below, a matview cannot embed is_platform_admin().
create materialized view image_storage_overview_mv as
  select
    l.category_main,
    l.category_type,
    count(i.id)                                       as total,
    count(i.storage_path)                             as stored,
    count(i.id) filter (where l.is_active)            as total_active,
    count(i.storage_path) filter (where l.is_active)  as stored_active
  from listings_public l
  left join images i on i.listing_id = l.id
  group by 1, 2;

create unique index if not exists image_storage_overview_mv_cat
  on image_storage_overview_mv (category_main, category_type);

revoke all on image_storage_overview_mv from anon, authenticated;

-- ===========================================================================
-- 3. Health churn chain. scraper_health_checks_mv depends on
--    snapshot_churn_24h_mv, so drop the dependent first, recreate it last.
-- ===========================================================================
drop materialized view if exists scraper_health_checks_mv;
drop materialized view if exists snapshot_churn_24h_mv;

-- 3a. snapshot_churn_24h_mv — INNER join listing_snapshots↔listings onto listing_id.
-- ci-allow-ungated: snapshot_churn_24h_mv admin-only ops matview; kept dark to browser roles by the revoke below, a matview cannot embed is_platform_admin().
create materialized view snapshot_churn_24h_mv as
  with snaps as (
    select l.source, count(*) as snaps_24h
    from listing_snapshots s
    join listings l on l.id = s.listing_id
    where s.scraped_at > now() - interval '24 hours'
    group by 1
  ),
  act as (
    select source, count(*) as active_n
    from listings
    where is_active
    group by 1
  )
  select a.source, coalesce(sn.snaps_24h, 0)::bigint as snaps_24h, a.active_n::bigint
  from act a
  left join snaps sn using (source);

create unique index if not exists snapshot_churn_24h_mv_pk
  on snapshot_churn_24h_mv (source);

revoke all on snapshot_churn_24h_mv from anon, authenticated;

-- 3b. scraper_health_checks_mv — reproduced VERBATIM from migration 214 (its input
--     snapshot_churn_24h_mv keeps the same output columns). fails_agg still joins on
--     listing_fetch_failures.sreality_id: that table has no listing_id column yet,
--     so it is out of scope for this item (see header).
-- ci-allow-ungated: scraper_health_checks_mv admin-only ops matview; kept dark to browser roles by the revoke below, a matview cannot embed is_platform_admin().
create materialized view scraper_health_checks_mv as
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

revoke all on scraper_health_checks_mv from anon, authenticated;

-- ===========================================================================
-- 4. health_summary_mv (independent) — snap_density grouped onto listing_id.
--    Body carried forward from migration 216; ONLY the snap_density counts CTE's
--    key changes (sreality_id → listing_id). cat_failures / failures_top10 read
--    listing_fetch_failures (no listing_id column) and stay verbatim (see header).
-- ===========================================================================
drop materialized view if exists health_summary_mv;

-- ci-allow-ungated: health_summary_mv admin-only ops matview; kept dark to browser roles by the revoke below, a matview cannot embed is_platform_admin().
create materialized view health_summary_mv as
with
category_pairs as (
  select * from (values
    ('byt',      'pronajem', 1),
    ('byt',      'prodej',   2),
    ('dum',      'pronajem', 3),
    ('dum',      'prodej',   4),
    ('komercni', 'pronajem', 5),
    ('komercni', 'prodej',   6)
  ) as t(category_main, category_type, sort_order)
),
series14 as (
  select generate_series((now() - interval '13 days')::date, now()::date, '1 day')::date as day
),
series7 as (
  select generate_series((now() - interval '6 days')::date, now()::date, '1 day')::date as day
),
-- ONE full scan: every count metric (national rollups + per-category cards).
cat_counts as materialized (
  select
    category_main,
    category_type,
    count(*) filter (where is_active = true)::int as active_now,
    count(*) filter (where is_active = false and last_seen_at >= now() - interval '7 days')::int as flipped_7d,
    count(*) filter (where first_seen_at <= now() - interval '7 days'
                       and (is_active = true or last_seen_at >= now() - interval '7 days'))::int as active_7d_ago,
    max(last_seen_at) as last_seen
  from listings_public
  group by category_main, category_type
),
-- ONE recent-slice scan: new listings per (cm, ct, day) over the 14-day window.
new_counts as materialized (
  select category_main, category_type,
    (date_trunc('day', first_seen_at))::date as day,
    count(*)::int as n
  from listings_public
  where first_seen_at >= (now() - interval '13 days')::date
  group by category_main, category_type, (date_trunc('day', first_seen_at))::date
),
-- ONE recent-slice scan: flipped-inactive per (cm, ct, day) over the 7-day window.
flip_counts as materialized (
  select category_main, category_type,
    (date_trunc('day', last_seen_at))::date as day,
    count(*)::int as n
  from listings_public
  where is_active = false and last_seen_at >= (now() - interval '6 days')::date
  group by category_main, category_type, (date_trunc('day', last_seen_at))::date
),
-- Non-listings CTEs carried forward verbatim from migration 136, EXCEPT snap_density,
-- whose per-listing snapshot count is now keyed on listing_id (Gate 2).
snap_density as (
  with counts as (
    select listing_id, count(*) as snap_count
    from listing_snapshots_public
    group by listing_id
  )
  select
    case when snap_count >= 4 then '4+' else snap_count::text end as bucket,
    count(*)::int as n
  from counts
  group by 1
),
freshness_24h as (
  select outcome, count(*)::int as n
  from listing_freshness_checks_public
  where checked_at >= now() - interval '24 hours'
  group by outcome
  order by n desc
),
failures_summary as (
  select
    count(*) filter (where given_up = true)::int as given_up,
    count(*)::int                                as total
  from listing_fetch_failures_public
),
failures_top10 as (
  select sreality_id, attempts, first_failure_at, last_failure_at, given_up
  from listing_fetch_failures_public
  order by attempts desc, last_failure_at desc nulls last
  limit 10
),
cat_failures as (
  select
    l.category_main,
    l.category_type,
    count(*)::int                                  as total,
    count(*) filter (where f.given_up = true)::int as given_up
  from listing_fetch_failures_public f
  join listings_public l on l.sreality_id = f.sreality_id
  group by l.category_main, l.category_type
),
-- Per-category series rebuilt from the pre-aggregated passes (no extra scans).
cat_new_per_day_14 as (
  select cp.category_main, cp.category_type,
    jsonb_agg(jsonb_build_object('day', s.day::text, 'n', coalesce(c.n, 0)) order by s.day) as series
  from category_pairs cp
  cross join series14 s
  left join new_counts c
    on c.category_main = cp.category_main
   and c.category_type = cp.category_type
   and c.day = s.day
  group by cp.category_main, cp.category_type
),
cat_flipped_per_day_7 as (
  select cp.category_main, cp.category_type,
    jsonb_agg(jsonb_build_object('day', s.day::text, 'n', coalesce(c.n, 0)) order by s.day) as series
  from category_pairs cp
  cross join series7 s
  left join flip_counts c
    on c.category_main = cp.category_main
   and c.category_type = cp.category_type
   and c.day = s.day
  group by cp.category_main, cp.category_type
),
by_category as (
  select
    cp.sort_order,
    jsonb_build_object(
      'category_main',       cp.category_main,
      'category_type',       cp.category_type,
      'active_now',          coalesce(cc.active_now, 0),
      'flipped_inactive_7d', coalesce(cc.flipped_7d, 0),
      'new_per_day_14d',     coalesce(npd.series, '[]'::jsonb),
      'flipped_per_day_7d',  coalesce(fpd.series, '[]'::jsonb),
      'failures_total',      coalesce(cf.total,    0),
      'failures_given_up',   coalesce(cf.given_up, 0)
    ) as obj
  from category_pairs cp
  left join cat_counts cc
    on cc.category_main = cp.category_main and cc.category_type = cp.category_type
  left join cat_new_per_day_14 npd
    on npd.category_main = cp.category_main and npd.category_type = cp.category_type
  left join cat_flipped_per_day_7 fpd
    on fpd.category_main = cp.category_main and fpd.category_type = cp.category_type
  left join cat_failures cf
    on cf.category_main = cp.category_main and cf.category_type = cp.category_type
)
select 1 as id, jsonb_build_object(
  'last_scrape_at',         (select max(last_seen) from cat_counts),
  'active_now',             (select coalesce(sum(active_now), 0)::int from cat_counts),
  'active_7d_ago',          (select coalesce(sum(active_7d_ago), 0)::int from cat_counts),
  'flipped_inactive_7d',    (select coalesce(sum(flipped_7d), 0)::int from cat_counts),
  'new_per_day_14d',        coalesce(
                              (select jsonb_agg(jsonb_build_object('day', s.day::text, 'n', coalesce(nd.n, 0)) order by s.day)
                               from series14 s
                               left join (select day, sum(n)::int as n from new_counts group by day) nd on nd.day = s.day),
                              '[]'::jsonb),
  'flipped_per_day_7d',     coalesce(
                              (select jsonb_agg(jsonb_build_object('day', s.day::text, 'n', coalesce(fd.n, 0)) order by s.day)
                               from series7 s
                               left join (select day, sum(n)::int as n from flip_counts group by day) fd on fd.day = s.day),
                              '[]'::jsonb),
  'snapshot_density',       coalesce(
                              (select jsonb_agg(
                                 jsonb_build_object('bucket', bucket, 'n', n)
                                 order by case when bucket = '4+' then 4 else bucket::int end
                               )
                               from snap_density),
                              '[]'::jsonb),
  'freshness_24h',          coalesce(
                              (select jsonb_agg(jsonb_build_object('outcome', outcome, 'n', n))
                               from freshness_24h),
                              '[]'::jsonb),
  'failures_given_up',      (select given_up from failures_summary),
  'failures_total',         (select total    from failures_summary),
  'failures_top10',         coalesce(
                              (select jsonb_agg(jsonb_build_object(
                                 'sreality_id',      sreality_id,
                                 'attempts',         attempts,
                                 'first_failure_at', first_failure_at,
                                 'last_failure_at',  last_failure_at,
                                 'given_up',         given_up
                               ))
                               from failures_top10),
                              '[]'::jsonb),
  'by_category',            coalesce(
                              (select jsonb_agg(obj order by sort_order)
                               from by_category),
                              '[]'::jsonb)
) as payload;

create unique index if not exists health_summary_mv_pk on health_summary_mv (id);

revoke all on health_summary_mv from anon, authenticated;

commit;
