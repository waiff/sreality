-- 435_revert_broker_leaderboard_deferred_hydration.sql
--
-- REVERT for 435_broker_leaderboard_deferred_hydration.sql. SHIPPED UNAPPLIED.
--
-- Lives in migrations/reverts/ for the same two reasons as every revert in this build: the
-- CI replay globs migrations/*.sql non-recursively, and test_migration_numbers.py forbids a
-- duplicate number above 304.
--
-- This is migration 414's function body VERBATIM (lines 40-112 of
-- migrations/414_broker_leaderboard_aggregate_before_join.sql), copied rather than retyped.
-- Before 435 was applied, the live body was drift-checked against it:
--   pg_get_functiondef(...) ilike '%brokers_public%'  ->  true
--   md5(pg_get_functiondef(...))                      ->  f11d20a3163207584547f0ec8950c949
--
-- WHAT APPLYING THIS COSTS. It restores the pre-W4 plan: the display join runs against the
-- whole candidate set before the LIMIT (measured 3,140 / 22,952 / 38,405 / 4,227 blocks
-- across the four shapes), and it restores the UNDER-FILL hazard — the activity filter goes
-- back above the LIMIT, so a merged_away broker holding stats rows can consume a top-N slot
-- and be discarded, returning fewer than p_limit rows. That is a correctness regression, so
-- prefer fixing forward.
--
-- CREATE OR REPLACE, never DROP + CREATE: a DROP resets the ACL to the default
-- EXECUTE TO PUBLIC and would re-expose primary_email/primary_phone to anon.

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

-- Re-assert the perimeter, since the revert is exactly where it would be forgotten.
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[]) from public;
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[]) from anon;
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[]) from authenticated;
