-- 216_health_summary_single_pass.sql
--
-- health_summary_mv was the dominant cost of the every-10-min health refresh
-- loop: measured ~18.8s for just 4 of its components, ~30-45s full. The cause
-- is ~10 separate scans of the 5.4 GB listings table per refresh (national
-- counts + per-category counts + per-day series, each its own subquery scan),
-- which grows with the table.
--
-- This collapses those ~10 listings scans into THREE materialized passes:
--   * cat_counts  — one GROUP BY (category_main, category_type) full scan whose
--     FILTER aggregates yield every count metric; the national scalars
--     (active_now / active_7d_ago / flipped_inactive_7d / last_scrape_at) are
--     rollups (sum / max) of it, and the per-category card numbers read it
--     directly. Replaces active_now, active_7d_ago, flipped_inactive_7d,
--     last_scrape, cat_active_now, cat_flipped_7d (6 scans -> 1).
--   * new_counts  — one recent-slice scan (first_seen index) grouped by
--     (cm, ct, day); national new_per_day_14d = per-day rollup, per-category
--     series read it directly. Replaces new_per_day_14 + cat_new_per_day_14.
--   * flip_counts — same shape for flipped-per-day. Replaces flipped_per_day_7
--     + cat_flipped_per_day_7.
-- The non-listings CTEs (snap_density over listing_snapshots, freshness_24h,
-- failures_summary/top10, cat_failures) are carried forward VERBATIM.
--
-- The payload is byte-for-byte identical to the previous body (verified per
-- top-level key against the live matview before cutover) — the frontend
-- contract and health_summary() / refresh_health_matviews() are untouched.
--
-- Cutover is blip-free + replay-safe: build the new body under a temp name,
-- then a fast drop-old + rename swap (no populate under the swap lock). On a
-- fresh DB (CI replay) the old matview from migration 136 exists, so the swap
-- applies identically. health_summary() and refresh_health_matviews() resolve
-- health_summary_mv by name at runtime, so they keep working across the rename.

drop materialized view if exists health_summary_mv_next;

create materialized view health_summary_mv_next as
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
-- Non-listings CTEs carried forward verbatim from migration 136.
snap_density as (
  with counts as (
    select sreality_id, count(*) as snap_count
    from listing_snapshots_public
    group by sreality_id
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

create unique index health_summary_mv_next_pk on health_summary_mv_next (id);

-- Fast swap: drop old, rename new in (no populate under the lock).
drop materialized view health_summary_mv;
alter materialized view health_summary_mv_next rename to health_summary_mv;
alter index health_summary_mv_next_pk rename to health_summary_mv_pk;
grant select on health_summary_mv to anon;
