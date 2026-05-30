-- 118_category_trends.sql
-- Per-category trend data for the Health dashboard's "Listings by category" table:
--   · total_in_db  — every listings row we hold for that (source, category), active or not
--   · hourly/daily time series of "active on portal" (sreality_result_size) and
--     "active in DB" (active_db), read from the per-run by_category ledger on scrape_runs.
-- The index walk records both numbers per category on every run, so each index run is a
-- sample point; hourly = raw run points, daily = the last run of each calendar day.
-- SECURITY INVOKER + anon SELECT grant on the public views (same posture as health_summary).

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
  with totals as (
    select category_main, category_type, count(*)::int as total_in_db
    from listings_public
    where source = p_source
      and category_main is not null
      and category_type is not null
    group by category_main, category_type
  ),
  runs as (
    select sr.started_at,
           (c.value->>'category_main')          as cm,
           (c.value->>'category_type')          as ct,
           nullif(c.value->>'sreality_result_size','')::int as portal,
           nullif(c.value->>'active_db','')::int            as db
    from scrape_runs sr
    cross join lateral jsonb_array_elements(sr.by_category) c(value)
    where sr.source = p_source
      and sr.index_pages > 0
      and (c.value->>'sreality_result_size') is not null
      and sr.started_at >= now() - make_interval(days => p_days)
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
           cm, ct,
           date_trunc('day', started_at) as bucket,
           portal, db
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
        'category_main', t.category_main,
        'category_type', t.category_type,
        'total_in_db',   t.total_in_db,
        'hourly',        coalesce(h.series, '[]'::jsonb),
        'daily',         coalesce(d.series, '[]'::jsonb)
      )
      order by t.total_in_db desc
    ),
    '[]'::jsonb
  )
  from totals t
  left join hourly h on h.cm = t.category_main and h.ct = t.category_type
  left join daily  d on d.cm = t.category_main and d.ct = t.category_type;
$$;

grant execute on function category_trends(text, int, int) to anon, authenticated;
