-- 187_broker_public_read_surface.sql
--
-- Broker intelligence, phase 1 (part 3): the anon read surface. New objects only;
-- the hot listings_public/properties_public views are untouched.
--
-- The leaderboard ("which broker has the most listings of type T in region R")
-- is a heavy group-by over the whole listings corpus, so it is PRECOMPUTED into a
-- matview (the rent_map_choropleth / health-matview pattern) to stay under anon's
-- 3s statement timeout. The matview materializes EVERY geo level explicitly
-- (region / okres / obec) with distinct-property counts computed AT each level —
-- you cannot recover a correct distinct count by summing a finer level (review
-- fix). Refreshed only by the daily resolver full sweep (never the */10 tick),
-- so it lags <=24h, the same accepted tradeoff as property stats (rule #20).

-- One row per active canonical broker.
create view brokers_public as
select
  b.id                    as broker_id,
  b.display_name,
  b.primary_email,
  b.primary_phone,
  b.primary_firm_id       as firm_id,
  f.canonical_domain      as firm_domain,
  f.display_name          as firm_name,
  f.is_franchise          as firm_is_franchise,
  b.source_count,
  b.distinct_source_count,
  b.listing_count,
  b.property_count,
  b.active_listing_count,
  b.active_property_count,
  b.first_seen_at,
  b.last_seen_at
from brokers b
left join firms f on f.id = b.primary_firm_id
where b.status = 'active';
grant select on brokers_public to anon;

-- One row per canonical agency.
create view firms_public as
select
  f.id                  as firm_id,
  f.canonical_domain,
  f.display_name,
  f.is_franchise,
  f.broker_count,
  f.listing_count,
  f.active_listing_count,
  f.first_seen_at,
  f.last_seen_at
from firms f;
grant select on firms_public to anon;

-- Broker<->firm memberships. is_current derived at READ time from last_seen_at so
-- it never goes stale between resolver runs (review fix).
create view broker_firm_memberships_public as
select
  m.broker_id,
  m.firm_id,
  f.canonical_domain    as firm_domain,
  f.display_name        as firm_name,
  m.first_seen_at,
  m.last_seen_at,
  m.listing_count,
  (m.last_seen_at > now() - interval '90 days') as is_current
from broker_firm_memberships m
join brokers b on b.id = m.broker_id and b.status = 'active'
join firms   f on f.id = m.firm_id;
grant select on broker_firm_memberships_public to anon;

-- Multi-level leaderboard matview. geo_level in ('region','okres','obec');
-- category coalesced to '' so the CONCURRENTLY unique index has no NULLs; geo_id
-- filtered not-null. property_count uses a per-listing sentinel for NULL
-- property_id (a not-yet-attached straggler counts as its own property).
create materialized view broker_region_type_stats as
with attributed as (
  select
    b.id                                            as broker_id,
    l.region_id, l.okres_id, l.obec_id,
    coalesce(l.category_main, '')                   as category_main,
    coalesce(l.category_type, '')                   as category_type,
    coalesce(l.property_id, -l.sreality_id)         as property_key,
    (l.is_active and l.last_seen_at > now() - interval '7 days') as is_live
  from listings l
  join broker_identities bi on bi.id = l.broker_identity_id
  join brokers b on b.id = bi.broker_id and b.status = 'active'
),
per_level as (
  select 'region'::text as geo_level, region_id as geo_id,
         broker_id, category_main, category_type, property_key, is_live
  from attributed where region_id is not null
  union all
  select 'okres', okres_id, broker_id, category_main, category_type, property_key, is_live
  from attributed where okres_id is not null
  union all
  select 'obec', obec_id, broker_id, category_main, category_type, property_key, is_live
  from attributed where obec_id is not null
)
select
  broker_id, geo_level, geo_id, category_main, category_type,
  count(*)::bigint                                                  as listing_count,
  count(distinct property_key)::bigint                             as property_count,
  count(*) filter (where is_live)::bigint                          as active_listing_count,
  count(distinct property_key) filter (where is_live)::bigint      as active_property_count
from per_level
group by broker_id, geo_level, geo_id, category_main, category_type;

create unique index broker_region_type_stats_pk
  on broker_region_type_stats (broker_id, geo_level, geo_id, category_main, category_type);
create index broker_region_type_stats_rank_idx
  on broker_region_type_stats (geo_level, geo_id, category_main, category_type, active_property_count desc);
grant select on broker_region_type_stats to anon;

-- "Top brokers in region R of type T" + "broker X's footprint in R/T" (filter to
-- one broker). Sums per-category matview rows when a category is unspecified
-- (categories are disjoint per property, so the distinct counts sum correctly).
-- SECURITY INVOKER (default): runs on anon's grant on the matview + brokers_public.
create function broker_leaderboard(
  p_geo_level     text,
  p_geo_id        bigint,
  p_category_main text default null,
  p_category_type text default null,
  p_metric        text default 'active_property_count',
  p_limit         integer default 200
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
  select
    b.broker_id, b.display_name, b.primary_email, b.primary_phone,
    b.firm_name, b.firm_domain,
    sum(s.listing_count)::bigint,
    sum(s.property_count)::bigint,
    sum(s.active_listing_count)::bigint,
    sum(s.active_property_count)::bigint
  from broker_region_type_stats s
  join brokers_public b on b.broker_id = s.broker_id
  where s.geo_level = p_geo_level
    and s.geo_id = p_geo_id
    and (p_category_main is null or s.category_main = p_category_main)
    and (p_category_type is null or s.category_type = p_category_type)
  group by b.broker_id, b.display_name, b.primary_email, b.primary_phone,
           b.firm_name, b.firm_domain
  order by case p_metric
             when 'listing_count'        then sum(s.listing_count)
             when 'property_count'       then sum(s.property_count)
             when 'active_listing_count' then sum(s.active_listing_count)
             else                             sum(s.active_property_count)
           end desc
  limit greatest(1, least(p_limit, 1000));
$$;
grant execute on function broker_leaderboard(text, bigint, text, text, text, integer) to anon;
