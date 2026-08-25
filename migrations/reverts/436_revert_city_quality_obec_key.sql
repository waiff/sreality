-- 436_revert_city_quality_obec_key.sql
--
-- REVERT for 436_city_quality_obec_key.sql. SHIPPED UNAPPLIED.
--
-- Lives in migrations/reverts/ for the same two verified reasons as every revert in this
-- build: the CI schema replay applies `ls migrations/*.sql | sort` (NOT recursive), so a
-- revert beside its forward migration would be applied right after it and silently undo the
-- change in every replayed environment; and tests/test_migration_numbers.py globs the same
-- way and forbids a duplicate number above 304.
--
-- WHAT APPLYING THIS COSTS, stated plainly so it is never done casually:
--   * it restores the 1,778,259-block per-row SubPlan (the predicate evaluated 282,214
--     times instead of once);
--   * it restores the FetchAllOverflowError — the SPA path asks for 84k-262k listing ids
--     against an expectMax of 100,000, so the feature is BROKEN, not merely slow;
--   * it restores the GLOBAL-MAX revision bug, under which one partial city upload makes
--     city_index_values_public return 33 rows instead of 6,798 and every other city
--     silently fails every rule.
-- Prefer fixing forward.
--
-- It does NOT re-create near_city_proximity's Python path or its FilterDef; that retirement
-- is a code change, reverted with git. The `city_proximity` PARAMETER comes back live here.

begin;

set local lock_timeout = '5s';

-- 1. city_index_values_public back to migration 078's global-max spelling.
create or replace view city_index_values_public as
  select v.city_id, v.index_name, v.value, v.source_revision
    from city_index_values v
   where v.source_revision = (select max(source_revision) from city_index_values);

-- 2. browse_stats_properties back to migration 425's body, taken from the live catalog and
--    reverse-patched, so the 74-argument signature and the other ~15,700 characters cannot
--    drift. The two hunks are the exact inverse of the forward migration's.
do $rev$
declare def text;
begin
  select pg_get_functiondef(p.oid) into def
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public' and p.proname = 'browse_stats_properties';

  def := replace(def,
$new1$      and (city_index_rules is null or jsonb_array_length(city_index_rules) = 0
           or l.obec_id = any (array(select curated_cities_matching(city_index_rules))))$new1$,
$old1$      and ((city_index_rules is null or jsonb_array_length(city_index_rules) = 0)
        or (l.home_city_id is not null
            and not exists (select 1 from jsonb_array_elements(coalesce(city_index_rules, '[]'::jsonb)) r
                where not exists (select 1 from city_index_values_public v where v.city_id = l.home_city_id and v.index_name = r->>'index_name'
                  and case coalesce(r->>'op', '>=')
                        when '>=' then v.value >= (r->>'value')::numeric
                        when '<=' then v.value <= (r->>'value')::numeric
                        when '>'  then v.value >  (r->>'value')::numeric
                        when '<'  then v.value <  (r->>'value')::numeric
                        when '==' then v.value =  (r->>'value')::numeric
                        when '!=' then v.value <> (r->>'value')::numeric
                        else           v.value >= (r->>'value')::numeric
                      end))))$old1$);

  def := replace(def,
$new2$      and true$new2$,
$old2$      and (city_proximity is null or (l.lat is not null and l.lng is not null and exists (
            select 1 from curated_cities_public c
            where st_dwithin(st_setsrid(st_makepoint(l.lng, l.lat), 4326)::geography, st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography, ((city_proximity ->> 'radius_km')::int * 1000))
              and ((city_proximity ->> 'population_min')::int is null or c.population >= (city_proximity ->> 'population_min')::int)
              and not exists (select 1 from jsonb_array_elements(coalesce(city_proximity -> 'index_rules', '[]'::jsonb)) r
                where not exists (select 1 from city_index_values_public v where v.city_id = c.city_id and v.index_name = r->>'index_name'
                  and case coalesce(r->>'op', '>=')
                        when '>=' then v.value >= (r->>'value')::numeric
                        when '<=' then v.value <= (r->>'value')::numeric
                        when '>'  then v.value >  (r->>'value')::numeric
                        when '<'  then v.value <  (r->>'value')::numeric
                        when '==' then v.value =  (r->>'value')::numeric
                        when '!=' then v.value <> (r->>'value')::numeric
                        else           v.value >= (r->>'value')::numeric
                      end)))))$old2$);

  def := replace(def,
$newb$begin
  if city_proximity is not null then
    raise exception using errcode = '22023',
      message = 'browse_stats_properties: city_proximity is retired (W5, migration 436). '
                'Use the migration-142 near_*_min columns.';
  end if;$newb$,
$oldb$begin$oldb$);

  if def like '%curated_cities_matching%' then
    raise exception 'revert hunk 1 did not apply';
  end if;
  if def not like '%st_dwithin%' then
    raise exception 'revert hunk 2 did not apply';
  end if;
  execute def;
end $rev$;

-- 3. The function and its index go last: browse_stats_properties referenced the function
--    until the statement above replaced it.
drop function if exists public.curated_cities_matching(jsonb);
drop index if exists city_index_values_latest_idx;

commit;
