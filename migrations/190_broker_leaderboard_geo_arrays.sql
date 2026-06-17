-- 190_broker_leaderboard_geo_arrays.sql
--
-- Brokers UI v2: drive the leaderboard from the Browse location picker (DistrictChips
-- → region/okres/obec admin-id arrays) instead of a single kraj/okres pair, and add a
-- listing→broker lookup view for the "show broker" button on listing/property detail.
--
-- broker_leaderboard is re-signed (DROP + CREATE — the body+signature change can't be a
-- plain CREATE OR REPLACE). The deployed SPA calls the OLD single-unit signature, so
-- this is apply-and-merge-promptly (the browse_stats re-sign discipline). Purely
-- additive otherwise.

drop function if exists broker_leaderboard(text, bigint, text, text, text, integer);

-- Ranked brokers across the picked admin units (any mix of region/okres/obec). When
-- all three arrays are empty it falls back to a NATIONAL leaderboard (region-level
-- rows only, so each property is counted once). Categories disjoint per property, so
-- summing the matview's per-(geo,category) rows yields correct distinct counts.
-- SECURITY INVOKER (default): runs on anon's grant on the matview + brokers_public.
create function broker_leaderboard(
  p_region_ids    bigint[] default null,
  p_okres_ids     bigint[] default null,
  p_obec_ids      bigint[] default null,
  p_category_main text default null,
  p_category_type text default null,
  p_metric        text default 'active_property_count',
  p_limit         integer default 100
)
returns table (
  broker_id             bigint,
  display_name          text,
  primary_email         text,
  primary_phone         text,
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
    b.firm_name, b.firm_domain,
    sum(s.listing_count)::bigint,
    sum(s.property_count)::bigint,
    sum(s.active_listing_count)::bigint,
    sum(s.active_property_count)::bigint
  from scoped s
  join brokers_public b on b.broker_id = s.broker_id
  group by b.broker_id, b.display_name, b.primary_email, b.primary_phone,
           b.firm_name, b.firm_domain
  order by case p_metric
             when 'listing_count'        then sum(s.listing_count)
             when 'property_count'       then sum(s.property_count)
             when 'active_listing_count' then sum(s.active_listing_count)
             else                             sum(s.active_property_count)
           end desc
  limit greatest(1, least(p_limit, 2000));
$$;
grant execute on function broker_leaderboard(bigint[], bigint[], bigint[], text, text, text, integer) to anon;

-- Listing -> its resolved canonical broker (for the "show broker" button on
-- listing/property detail). One row per listing that has a resolved broker;
-- always queried by sreality_id. Owner-privileged view, anon-readable.
create view listing_broker_public as
select
  l.sreality_id,
  bi.broker_id,
  b.display_name                              as broker_display_name,
  coalesce(f.display_name, f.canonical_domain) as broker_firm_label
from listings l
join broker_identities bi on bi.id = l.broker_identity_id
join brokers b on b.id = bi.broker_id and b.status = 'active'
left join firms f on f.id = b.primary_firm_id;
grant select on listing_broker_public to anon;
