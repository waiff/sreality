-- 079_listings_with_city_quality.sql
--
-- Phase QUAL — Browse pre-filter RPC. Same composition pattern as
-- `listings_with_tags` (migration 056-ish): returns the sreality_id
-- list of listings whose curated-city / proximity criteria match.
-- The frontend takes that list and AND's it with the existing
-- PostgREST listings_public query via `.in('sreality_id', ids)`.
--
-- Two filter shapes, both optional:
--   `p_index_rules`  : jsonb array of {index_name, value} (op '>=').
--                      A listing matches when there exists a curated
--                      city C such that l is within C.default_radius_m
--                      of C.centroid AND every rule in the array holds
--                      for C (i.e. the universal-quantification AND).
--   `p_pop_min/max`  : population bounds, applied to the same matching
--                      city.
--   `p_proximity`    : jsonb object {index_rules, population_min,
--                      radius_km} — same shape semantics as
--                      WatchdogFilterSpec.near_city_proximity. A
--                      listing matches when there exists a curated
--                      city C within `radius_km*1000` of l AND the
--                      inner rules all hold for C.
--
-- Both shapes can be set together — the result is the AND of the two
-- conditions (listing matches the per-city rule AND is near a matching
-- proximity city). Either or both may be NULL (no constraint).
--
-- SECURITY INVOKER + anon SELECT on `listings_public`, `curated_cities_public`,
-- and `city_index_values_public` mean no permission escalation.

create or replace function listings_with_city_quality(
  p_index_rules jsonb default null,
  p_pop_min int default null,
  p_pop_max int default null,
  p_proximity jsonb default null
)
returns table (sreality_id bigint)
language sql
stable
security invoker
as $$
  with
    rules as (
      select
        (r->>'index_name')::text as index_name,
        (r->>'value')::numeric   as value
      from jsonb_array_elements(coalesce(p_index_rules, '[]'::jsonb)) r
    ),
    prox_rules as (
      select
        (r->>'index_name')::text as index_name,
        (r->>'value')::numeric   as value
      from jsonb_array_elements(
        coalesce(p_proximity -> 'index_rules', '[]'::jsonb)
      ) r
    )
  select l.sreality_id
  from listings l
  where
    l.geom is not null
    -- Per-city block (city_index_rules + min/max population).
    and (
      not (exists (select 1 from rules)
           or p_pop_min is not null
           or p_pop_max is not null)
      or exists (
        select 1
          from curated_cities_public c
         where st_dwithin(
                 l.geom,
                 st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography,
                 c.default_radius_m
               )
           and (p_pop_min is null or c.population >= p_pop_min)
           and (p_pop_max is null or c.population <= p_pop_max)
           and not exists (
             select 1 from rules r
             where not exists (
               select 1 from city_index_values_public v
               where v.city_id = c.city_id
                 and v.index_name = r.index_name
                 and v.value >= r.value
             )
           )
      )
    )
    -- Proximity block (within radius_km of any matching city).
    and (
      p_proximity is null
      or exists (
        select 1
          from curated_cities_public c
         where st_dwithin(
                 l.geom,
                 st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography,
                 ((p_proximity ->> 'radius_km')::int * 1000)
               )
           and (
             (p_proximity ->> 'population_min')::int is null
             or c.population >= (p_proximity ->> 'population_min')::int
           )
           and not exists (
             select 1 from prox_rules r
             where not exists (
               select 1 from city_index_values_public v
               where v.city_id = c.city_id
                 and v.index_name = r.index_name
                 and v.value >= r.value
             )
           )
      )
    );
$$;

grant execute on function listings_with_city_quality(jsonb, int, int, jsonb) to anon, authenticated;
