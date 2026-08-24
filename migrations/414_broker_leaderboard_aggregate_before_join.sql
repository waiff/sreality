-- W10a (hydration sprint): the broker leaderboard's default (unfiltered) call
-- joined ALL 88,762 region-grain broker_region_type_stats rows (one row per
-- broker x region x category_main x category_type, PK
-- (broker_id,geo_level,geo_id,category_main,category_type)) to brokers/firms
-- BEFORE aggregating, then grouped the ~89k joined rows down to the ~22,666
-- distinct brokers. Measured live: 244 hit + 6,104 read + 428 written buffers,
-- 1,690ms, mostly one Hash Join + HashAggregate over the un-aggregated join.
--
-- Three independent fixes:
--   1. Aggregate broker_region_type_stats DOWN TO ONE ROW PER BROKER inside the
--      CTE first, then join that (much smaller) summed set to brokers_public /
--      firms for display columns. The status='active' filter and firm_id
--      filter apply to the already-aggregated ~broker-count rows instead of
--      the ~89k raw fact rows.
--   2. A covering index on the (geo_level, geo_id) shape the WHERE clause's
--      geo predicate actually branches on, INCLUDEing every column the CTE
--      selects or filters on (broker_id, category_main, category_type, and
--      the four count columns).
--   3. Split the geo predicate's 4-way OR into a UNION ALL of two mutually
--      exclusive branches (no-geo-filter vs explicit-geo-filter). The original
--      single OR forces a BitmapOr, which — unlike a plain single-condition
--      index scan — always visits the heap (Bitmap Heap Scan), so the new
--      covering index never paid for itself on the by-far most common call
--      (the bare, unfiltered leaderboard). Isolated into its own branch, that
--      call becomes a genuine Index Only Scan: verified live, Heap Fetches: 0,
--      1,038 buffers / 56ms warm for the whole aggregation step (was 4,278
--      buffers via Bitmap Heap Scan on the un-split form).
--
-- broker_region_type_stats is a MATVIEW refreshed only by the daily full
-- broker sweep (resolve_brokers_full.yml), not a tight cron cadence, but the
-- index build still takes a lock briefly — fail fast and retry rather than
-- queue behind something.
set local lock_timeout = '5s';

create index if not exists broker_region_type_stats_geo_covering_idx
  on broker_region_type_stats (geo_level, geo_id)
  include (broker_id, category_main, category_type,
           listing_count, property_count, active_listing_count, active_property_count);

create or replace function broker_leaderboard(
  p_region_ids bigint[] default null,
  p_okres_ids bigint[] default null,
  p_obec_ids bigint[] default null,
  p_category_main text default null,
  p_category_type text default null,
  p_metric text default 'active_property_count',
  p_limit integer default 100,
  p_firm_ids bigint[] default null
)
returns table(
  broker_id bigint, display_name text, primary_email text, primary_phone text,
  firm_id bigint, firm_name text, firm_domain text,
  listing_count bigint, property_count bigint,
  active_listing_count bigint, active_property_count bigint
)
language sql stable as $function$
  with scoped as (
    -- Branch A: no geo filter at all (the bare /brokers leaderboard) — a
    -- single, non-OR condition the planner can answer with a plain Index
    -- Only Scan on broker_region_type_stats_geo_covering_idx.
    select s.broker_id,
           sum(s.listing_count)::bigint as listing_count,
           sum(s.property_count)::bigint as property_count,
           sum(s.active_listing_count)::bigint as active_listing_count,
           sum(s.active_property_count)::bigint as active_property_count
    from broker_region_type_stats s
    where coalesce(array_length(p_region_ids, 1), 0)
            + coalesce(array_length(p_okres_ids, 1), 0)
            + coalesce(array_length(p_obec_ids, 1), 0) = 0
      and s.geo_level = 'region'
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
    group by s.broker_id

    union all

    -- Branch B: an explicit region/okres/obec filter — mutually exclusive
    -- with A (guarded by the same array-length check), so no row is ever
    -- double-counted across the two branches.
    select s.broker_id,
           sum(s.listing_count)::bigint,
           sum(s.property_count)::bigint,
           sum(s.active_listing_count)::bigint,
           sum(s.active_property_count)::bigint
    from broker_region_type_stats s
    where coalesce(array_length(p_region_ids, 1), 0)
            + coalesce(array_length(p_okres_ids, 1), 0)
            + coalesce(array_length(p_obec_ids, 1), 0) > 0
      and (
        (s.geo_level = 'region' and s.geo_id = any(coalesce(p_region_ids, '{}'::bigint[])))
        or (s.geo_level = 'okres' and s.geo_id = any(coalesce(p_okres_ids, '{}'::bigint[])))
        or (s.geo_level = 'obec'  and s.geo_id = any(coalesce(p_obec_ids,  '{}'::bigint[])))
      )
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
    group by s.broker_id
  )
  select
    b.broker_id, b.display_name, b.primary_email, b.primary_phone,
    b.firm_id, b.firm_name, b.firm_domain,
    s.listing_count, s.property_count, s.active_listing_count, s.active_property_count
  from scoped s
  join brokers_public b on b.broker_id = s.broker_id
  where (p_firm_ids is null or b.firm_id = any(p_firm_ids))
  order by case p_metric
             when 'listing_count'        then s.listing_count
             when 'property_count'       then s.property_count
             when 'active_listing_count' then s.active_listing_count
             else                             s.active_property_count
           end desc
  limit greatest(1, least(p_limit, 2000));
$function$;
