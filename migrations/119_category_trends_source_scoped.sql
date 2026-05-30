-- 119_category_trends_source_scoped.sql
-- Supersede category_trends (migration 118) with a fully SOURCE-SCOPED per-category
-- health payload, so EVERY portal's "Listings by category" table — not just sreality —
-- gets the same columns + trend chart.
--
-- Fixes two things vs migration 118:
--   1. Trend was empty in the browser: 118 read `scrape_runs` directly, which anon
--      cannot SELECT (reads go through scrape_runs_public). Now reads scrape_runs_public.
--   2. The table's active/new/flipped came from the GLOBAL health_summary.by_category
--      (all sources), so the sreality card showed everyone's active count. Everything
--      here is scoped to p_source.
--
-- Spine = the WALKED categories (those a recent index run recorded a result_size for),
-- so unwalked edge categories (drazba/podil) drop out and pilots' real categories
-- (pozemek/ostatni/…) appear. Per category:
--   total_in_db, active_now, new_today/new_7d, flipped_today/flipped_7d,
--   failures_total/failures_given_up, portal_total + collected (latest run),
--   and hourly/daily series of portal (sreality_result_size) vs db (active_db).
-- SECURITY INVOKER + anon grant; all reads via the anon-exposed *_public views.

create or replace function category_trends(
  p_source text default 'sreality',
  p_hours  int  default 72,
  p_days   int  default 30
)
returns jsonb
language sql
stable
security invoker
as $$
  with listing_agg as (
    select
      category_main as cm,
      category_type as ct,
      count(*)::int                                                                          as total_in_db,
      count(*) filter (where is_active)::int                                                 as active_now,
      count(*) filter (where first_seen_at::date = now()::date)::int                         as new_today,
      count(*) filter (where first_seen_at >= (now() - interval '6 days')::date)::int        as new_7d,
      count(*) filter (where not is_active and last_seen_at::date = now()::date)::int         as flipped_today,
      count(*) filter (where not is_active
                         and last_seen_at >= (now() - interval '6 days')::date)::int          as flipped_7d
    from listings_public
    where source = p_source
      and category_main is not null
      and category_type is not null
    group by category_main, category_type
  ),
  failure_agg as (
    select
      l.category_main as cm,
      l.category_type as ct,
      count(*)::int                                  as total,
      count(*) filter (where f.given_up = true)::int as given_up
    from listing_fetch_failures_public f
    join listings_public l on l.sreality_id = f.sreality_id
    where l.source = p_source
    group by l.category_main, l.category_type
  ),
  runs as (
    select
      sr.started_at,
      (c.value->>'category_main')                       as cm,
      (c.value->>'category_type')                       as ct,
      nullif(c.value->>'sreality_result_size','')::int  as portal,
      nullif(c.value->>'collected','')::int             as collected,
      nullif(c.value->>'active_db','')::int             as db
    from scrape_runs_public sr
    cross join lateral jsonb_array_elements(sr.by_category) c(value)
    where sr.source = p_source
      and sr.index_pages > 0
      and (c.value->>'sreality_result_size') is not null
      and sr.started_at >= now() - make_interval(days => p_days)
  ),
  latest_run as (
    select distinct on (cm, ct) cm, ct, portal as portal_total, collected
    from runs
    order by cm, ct, started_at desc
  ),
  hourly as (
    select cm, ct,
           jsonb_agg(jsonb_build_object('t', started_at, 'portal', portal, 'db', db)
                     order by started_at) as series
    from runs
    where started_at >= now() - make_interval(hours => p_hours)
    group by cm, ct
  ),
  daily_pick as (
    select distinct on (cm, ct, date_trunc('day', started_at))
           cm, ct, date_trunc('day', started_at) as bucket, portal, db
    from runs
    order by cm, ct, date_trunc('day', started_at), started_at desc
  ),
  daily as (
    select cm, ct,
           jsonb_agg(jsonb_build_object('t', bucket, 'portal', portal, 'db', db)
                     order by bucket) as series
    from daily_pick
    group by cm, ct
  )
  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'category_main',     lr.cm,
        'category_type',     lr.ct,
        'total_in_db',       coalesce(la.total_in_db, 0),
        'active_now',        coalesce(la.active_now, 0),
        'new_today',         coalesce(la.new_today, 0),
        'new_7d',            coalesce(la.new_7d, 0),
        'flipped_today',     coalesce(la.flipped_today, 0),
        'flipped_7d',        coalesce(la.flipped_7d, 0),
        'failures_total',    coalesce(fa.total, 0),
        'failures_given_up', coalesce(fa.given_up, 0),
        'portal_total',      lr.portal_total,
        'collected',         lr.collected,
        'hourly',            coalesce(h.series, '[]'::jsonb),
        'daily',             coalesce(d.series, '[]'::jsonb)
      )
      order by coalesce(la.active_now, 0) desc
    ),
    '[]'::jsonb
  )
  from latest_run lr
  left join listing_agg la on la.cm = lr.cm and la.ct = lr.ct
  left join failure_agg fa on fa.cm = lr.cm and fa.ct = lr.ct
  left join hourly     h  on h.cm  = lr.cm and h.ct  = lr.ct
  left join daily      d  on d.cm  = lr.cm and d.ct  = lr.ct;
$$;

grant execute on function category_trends(text, int, int) to anon, authenticated;
