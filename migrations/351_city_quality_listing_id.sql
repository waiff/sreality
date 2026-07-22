-- 351_city_quality_listing_id.sql
-- R2 read cutover (runbook §4 "MUST precede flip", city-quality bullet) — Gate-2 blocker 3.6.
--
-- listings_with_city_quality() is the Browse city-quality allowlist RPC. It
-- returned the matching listings' `sreality_id`, and the SPA AND'd that set onto
-- the property grain via `.in('sreality_id', ...)` on browse_list / properties_map_mv.
--
-- Post-Gate-2 a new non-sreality listing inserts `sreality_id = NULL`, so the
-- representative child of such a property has a NULL sreality_id in the browse
-- read model. `col IN (…)` never matches NULL, so the city-quality Map/Table/
-- Cards/Count silently DROP those listings — while browse_stats_properties
-- (migration 080) re-implements the same predicate server-side and still COUNTS
-- them: a count-vs-list divergence.
--
-- Fix: return the surrogate `listing_id` (listings.id, NEVER NULL, the browse
-- read model's `listing_id` column = the repr child's listings.id) instead of
-- sreality_id. The SPA filters `.in('listing_id', ...)`, matching in the
-- surrogate id-space, null-safe. Same geo/quality body otherwise — only the
-- projected id changes.
--
-- The RETURN TYPE changes (sreality_id bigint -> listing_id bigint), which
-- CREATE OR REPLACE cannot do ("cannot change return type of existing
-- function"), so this DROPs + CREATEs. A dropped function LOSES its grants, so
-- the grants are re-applied to match the live ACL exactly: EXECUTE to
-- authenticated + service_role, PUBLIC/anon revoked (the public-release
-- hardening removed the original anon grant from migration 079/081).
--
-- Only caller is the SPA's resolveCityQualityPrefilter (frontend/src/lib/queries.ts).
-- browse_stats (migration 080) mirrors the predicate but does NOT call this
-- function, so nothing else depends on the return shape.

set local lock_timeout = '5s';

drop function if exists listings_with_city_quality(jsonb, int, int, jsonb);

create function listings_with_city_quality(
  p_index_rules jsonb default null,
  p_pop_min     int   default null,
  p_pop_max     int   default null,
  p_proximity   jsonb default null
)
returns table(listing_id bigint)
language sql
stable
as $$
  with
    rules as (
      select
        (r->>'index_name')::text       as index_name,
        (r->>'value')::numeric         as value,
        coalesce(r->>'op', '>=')       as op
      from jsonb_array_elements(coalesce(p_index_rules, '[]'::jsonb)) r
    ),
    prox_rules as (
      select
        (r->>'index_name')::text       as index_name,
        (r->>'value')::numeric         as value,
        coalesce(r->>'op', '>=')       as op
      from jsonb_array_elements(
        coalesce(p_proximity -> 'index_rules', '[]'::jsonb)
      ) r
    )
  select l.id as listing_id
  from listings l
  where
    l.geom is not null
    and (
      not (exists (select 1 from rules)
           or p_pop_min is not null
           or p_pop_max is not null)
      or exists (
        select 1
          from curated_cities_public c
          left join admin_boundaries_public b
            on b.id = c.admin_boundary_id
         where (
                 (c.admin_boundary_id is not null
                    and st_covers(b.geom, l.geom))
                 or (c.admin_boundary_id is null
                    and st_dwithin(
                          l.geom,
                          st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography,
                          c.default_radius_m))
               )
           and (p_pop_min is null or c.population >= p_pop_min)
           and (p_pop_max is null or c.population <= p_pop_max)
           and not exists (
             select 1 from rules r
             where not exists (
               select 1 from city_index_values_public v
               where v.city_id = c.city_id
                 and v.index_name = r.index_name
                 and case r.op
                       when '>=' then v.value >= r.value
                       when '<=' then v.value <= r.value
                       when '>'  then v.value >  r.value
                       when '<'  then v.value <  r.value
                       when '==' then v.value =  r.value
                       when '!=' then v.value <> r.value
                       else           v.value >= r.value
                     end
             )
           )
      )
    )
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
                 and case r.op
                       when '>=' then v.value >= r.value
                       when '<=' then v.value <= r.value
                       when '>'  then v.value >  r.value
                       when '<'  then v.value <  r.value
                       when '==' then v.value =  r.value
                       when '!=' then v.value <> r.value
                       else           v.value >= r.value
                     end
             )
           )
      )
    );
$$;

-- Match the live ACL: no PUBLIC/anon (the default execute grant is auto-added on
-- create, so revoke it), EXECUTE only for authenticated + service_role.
revoke execute on function listings_with_city_quality(jsonb, int, int, jsonb) from public;
grant  execute on function listings_with_city_quality(jsonb, int, int, jsonb) to authenticated, service_role;
