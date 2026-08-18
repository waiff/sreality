-- 410: filter the broker leaderboard by company/firm.
--
-- WHY. The Brokers UI (Makléři) ranks brokers by inventory but has no way to
-- scope the list to one or more companies (e.g. "only mmreality.cz", "only
-- LEXXUS NORTON") — an operator scouting a specific franchise or competitor
-- has to eyeball the firm column and can't narrow to it. firms_public
-- already carries the id (migration 187) and is fully populated by the
-- resolver's existing rollup; only the leaderboard RPC is missing the filter.
--
-- broker_leaderboard is re-signed (DROP + CREATE — a body change can't be a
-- plain CREATE OR REPLACE). A new TRAILING parameter with a default and a
-- new TRAILING output column are both backward compatible with the old 7-arg
-- callers still deployed at apply time, so this is safe in either apply
-- order — apply-and-merge-promptly anyway, per the migration 190 discipline.
-- Filtering joins against brokers_public.firm_id, already present via the
-- existing `b` join — no new join, no matview change, no backfill.
--
-- brokers.primary_firm_id (surfaced as brokers_public.firm_id) is the
-- CURRENT firm only, matching exactly what the leaderboard row already
-- displays (firm_name/firm_domain) — filtering on anything else would
-- disagree with what's on screen. NULL firm_id (independent brokers) never
-- matches a firm filter, correctly: `NULL = any(p_firm_ids)` is NULL, not
-- true, same as every other optional predicate here.
--
-- Postgres grants EXECUTE on a new function to PUBLIC by default, and this
-- project's Supabase default ACL separately auto-grants anon/authenticated
-- (see the database skill) — migration 299's Amendment A6 revoked all three
-- from the OLD signature, and a DROP+CREATE starts a fresh ACL, so the
-- revoke must be re-stated here or the re-sign silently reopens broker PII
-- to any bundle holder (the same gotcha migration 396 hit re-creating
-- broker_geo_options). No grant is added back — the API's service-role
-- connection doesn't need one.

begin;

set local lock_timeout = '5s';

drop function if exists broker_leaderboard(bigint[], bigint[], bigint[], text, text, text, integer);

-- SECURITY INVOKER (default): runs on the caller's own grants (service-role
-- only, post-A6) on broker_region_type_stats + brokers_public.
create function broker_leaderboard(
  p_region_ids    bigint[] default null,
  p_okres_ids     bigint[] default null,
  p_obec_ids      bigint[] default null,
  p_category_main text default null,
  p_category_type text default null,
  p_metric        text default 'active_property_count',
  p_limit         integer default 100,
  p_firm_ids      bigint[] default null
)
returns table (
  broker_id             bigint,
  display_name          text,
  primary_email         text,
  primary_phone         text,
  firm_id               bigint,
  firm_name             text,
  firm_domain           text,
  listing_count         bigint,
  property_count        bigint,
  active_listing_count  bigint,
  active_property_count bigint
)
language sql
stable
as $$
  with scoped as (
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where (
      (coalesce(array_length(p_region_ids, 1), 0)
         + coalesce(array_length(p_okres_ids, 1), 0)
         + coalesce(array_length(p_obec_ids, 1), 0) = 0
       and s.geo_level = 'region')
      or (s.geo_level = 'region' and s.geo_id = any(coalesce(p_region_ids, '{}'::bigint[])))
      or (s.geo_level = 'okres'  and s.geo_id = any(coalesce(p_okres_ids,  '{}'::bigint[])))
      or (s.geo_level = 'obec'   and s.geo_id = any(coalesce(p_obec_ids,   '{}'::bigint[])))
    )
    and (p_category_main is null or s.category_main = p_category_main)
    and (p_category_type is null or s.category_type = p_category_type)
  )
  select
    b.broker_id, b.display_name, b.primary_email, b.primary_phone,
    b.firm_id, b.firm_name, b.firm_domain,
    sum(s.listing_count)::bigint,
    sum(s.property_count)::bigint,
    sum(s.active_listing_count)::bigint,
    sum(s.active_property_count)::bigint
  from scoped s
  join brokers_public b on b.broker_id = s.broker_id
  where (p_firm_ids is null or b.firm_id = any(p_firm_ids))
  group by b.broker_id, b.display_name, b.primary_email, b.primary_phone,
           b.firm_id, b.firm_name, b.firm_domain
  order by case p_metric
             when 'listing_count'        then sum(s.listing_count)
             when 'property_count'       then sum(s.property_count)
             when 'active_listing_count' then sum(s.active_listing_count)
             else                             sum(s.active_property_count)
           end desc
  limit greatest(1, least(p_limit, 2000));
$$;

revoke all on function
  broker_leaderboard(bigint[], bigint[], bigint[], text, text, text, integer, bigint[])
  from public, anon, authenticated;

commit;
