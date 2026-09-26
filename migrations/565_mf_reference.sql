-- 565_mf_reference.sql -- MF reference rent becomes ONE read-time SQL measure (PR-D, D-1).
--
-- ADDITIVE. Two new objects, nothing existing changes:
--
--   rent_map_cells   a materialized view: the latest rent-map revision flattened to one
--                    row per (level, code, vk, nov), with the six published adjustments
--                    as columns. Three levels:
--                      'ku'    the ministry's katastrální-území cell;
--                      'obec'  the ministry's municipality cell;
--                      'town'  per MUNICIPALITY whose price is published per KÚ: how
--                              many current KÚ it has (n_ku), how many of them are
--                              priced at this VK (n_priced), the lowest and highest
--                              price (lo/hi), and base_per_m2 = lo when every KÚ is
--                              priced and all prices are equal ("town-uniform"), else
--                              NULL. The KÚ -> obec edge is `ruian_admin_units.parent_id`
--                              over current units (Gate 0: 13,074 relations, 0 KÚ in two
--                              obce, 0 parent disagreements).
--                    Only priced cells are rows; a missing row IS "no cell".
--                    It replaces `rent_map_values_public` + `rent_map_adjustments_public`
--                    as MF's inputs (both are dropped in PR-F, with the 507 functions).
--                    Measured (Gate 0): precomputed cells cost 0.10 ms/row against 0.25
--                    for reading the two views per row.
--
--   mf_reference()   THE measure. Facts in, (rent, yield, detail jsonb) out. It reads
--                    ONLY rent_map_cells -- never geometry, never the registry. The six
--                    status codes and their Czech notes exist in this file and nowhere
--                    else in the repository (tests/test_mf_reference.py is the rail).
--
-- WHY THE MATVIEW IS GRANTED TO `authenticated`. An inlined SQL function's relations are
-- permission-checked as the CALLER, and `authenticated` reads properties_public directly.
-- The matview holds nothing but the ministry's published prices and RÚIAN codes.
--
-- WHY THE FUNCTION IS SHAPED THIS WAY. LANGUAGE sql, STABLE, SECURITY INVOKER, not STRICT,
-- no SET clause, one SELECT, RETURNS TABLE: the planner inlines it into the caller's
-- LEFT JOIN LATERAL (migration 425's lesson), so there is no per-row function call --
-- only index probes on rent_map_cells_key. It touches no PostGIS, so it needs no
-- search_path (545) and must not have one. Every relation is schema-qualified.
--
-- EVALUATION ORDER (plan_v2 §2.3 + the implementation addendum):
--   not a flat                          -> no row (the caller's LEFT JOIN reads NULLs)
--   country_status = 'foreign'          -> not_in_cz
--   obec_kod IS NULL                    -> location_unknown
--   no VK, area NULL or < 12 m²         -> inputs_missing
--   bound KÚ with a cell                -> ok (the KÚ's cell)
--   else the obec's own cell            -> ok (the ministry's town figure)
--   else a KÚ is bound                  -> no_rent_cell
--   else the town row, uniform          -> ok (territory.basis = 'town_uniform')
--   else the town row                   -> territory_coarse: NO value, the range + note
--   else                                -> no_rent_cell
--
-- VK is the leading integer of the disposition clamped to 1..4 ('10+1' -> 4; the 507
-- functions said 1). Novostavba is coalesce(condition = 'novostavba', false) (507 dropped
-- every adjustment on a NULL condition). Rent = round((base + adjustments) x area).
-- Yield = round(rent x 12 / price x 100, 2) only for category_type = 'prodej' and a
-- price of at least 100 000 (507's floor); otherwise the rent stands with no yield.
-- `katastr_kod` arrives with PR-B; until then every caller passes NULL, which is exactly
-- "the location is known to town level".
--
-- DETAIL SHAPE (numbers and the note only; clients format):
--   value  -> monthly_rent_czk present (+ territory, vk, is_novostavba, source_revision,
--             source_date, base_per_m2, adjustments[], adjustments_sum_per_m2,
--             total_per_m2, area_m2) -- the keys the stored breakdown always had;
--   range  -> range{per_m2_min, per_m2_max, rent_min_czk, rent_max_czk[, yield_min_pct,
--             yield_max_pct]} present, no monthly_rent_czk;
--   note   -> status + note only.
-- NULL-valued keys are stripped, so presence alone decides the shape.
--
-- APPLY with apply_migration.yml (or the MCP) BEFORE 566. Idempotent: every statement
-- is `if not exists`, `create or replace`, `on conflict do nothing` or a grant, so a
-- retried file resumes.

set lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1. The cells.
-- ---------------------------------------------------------------------------
create materialized view if not exists public.rent_map_cells as
with rev as (
  select r.source_revision, r.source_date
    from public.rent_map_revisions r
   order by r.source_revision desc
   limit 1
),
adj as (
  select a.vk::integer as vk, a.is_novostavba as nov,
         coalesce(max(a.czk_per_m2) filter (where a.attribute = 'balcony'), 0)        as adj_balcony,
         coalesce(max(a.czk_per_m2) filter (where a.attribute = 'terrace'), 0)        as adj_terrace,
         coalesce(max(a.czk_per_m2) filter (where a.attribute = 'furnished'), 0)      as adj_furnished,
         coalesce(max(a.czk_per_m2) filter (where a.attribute = 'garage'), 0)         as adj_garage,
         coalesce(max(a.czk_per_m2) filter (where a.attribute = 'elevator'), 0)       as adj_elevator,
         coalesce(max(a.czk_per_m2) filter (where a.attribute = 'other_material'), 0) as adj_other_material
    from public.rent_map_adjustments a
    join rev on rev.source_revision = a.source_revision
   group by a.vk, a.is_novostavba
),
priced as (
  select v.level, v.ruian_code as code, v.vk::integer as vk, n.nov,
         case when n.nov then v.ref_rent_novostavba_per_m2 else v.ref_rent_per_m2 end as base_per_m2,
         case v.level when 'ku' then v.ku_name else v.obec_name end as name,
         v.kraj
    from public.rent_map_values v
    join rev on rev.source_revision = v.source_revision
   cross join (values (false), (true)) n(nov)
),
ku_of_obec as (
  select o.code as obec_code, o.name as obec_name, k.code as ku_code,
         count(*) over (partition by o.code) as n_ku
    from public.ruian_admin_units o
    join public.ruian_admin_units k
      on k.parent_id = o.id and k.level = 'katastralni_uzemi' and k.valid_to is null
   where o.level = 'obec' and o.valid_to is null
),
town as (
  select t.obec_code as code, c.vk, c.nov,
         min(t.n_ku) as n_ku, count(*) as n_priced,
         min(c.base_per_m2) as lo_per_m2, max(c.base_per_m2) as hi_per_m2,
         min(t.obec_name) as name, min(c.kraj) as kraj
    from ku_of_obec t
    join priced c on c.level = 'ku' and c.code = t.ku_code and c.base_per_m2 is not null
   group by t.obec_code, c.vk, c.nov
),
cells as (
  select level, code, vk, nov, base_per_m2,
         null::bigint as n_ku, null::bigint as n_priced,
         null::integer as lo_per_m2, null::integer as hi_per_m2, name, kraj
    from priced
   where base_per_m2 is not null
  union all
  select 'town', code, vk, nov,
         case when n_priced = n_ku and lo_per_m2 = hi_per_m2 then lo_per_m2 end,
         n_ku, n_priced, lo_per_m2, hi_per_m2, name, kraj
    from town
)
select c.level, c.code, c.vk, c.nov, c.base_per_m2,
       c.n_ku, c.n_priced, c.lo_per_m2, c.hi_per_m2, c.name, c.kraj,
       coalesce(a.adj_balcony, 0)        as adj_balcony,
       coalesce(a.adj_terrace, 0)        as adj_terrace,
       coalesce(a.adj_furnished, 0)      as adj_furnished,
       coalesce(a.adj_garage, 0)         as adj_garage,
       coalesce(a.adj_elevator, 0)       as adj_elevator,
       coalesce(a.adj_other_material, 0) as adj_other_material,
       rev.source_revision, rev.source_date
  from cells c
 cross join rev
  left join adj a on a.vk = c.vk and a.nov = c.nov;

-- The probe key AND the key REFRESH ... CONCURRENTLY needs (api/rent_map.insert_revision).
create unique index if not exists rent_map_cells_key
  on public.rent_map_cells (level, code, vk, nov);

revoke all on public.rent_map_cells from public, anon, authenticated;
grant select on public.rent_map_cells to authenticated, service_role;

-- Corollary E: same producer, host and cadence as rent_map_choropleth -- they are
-- refreshed by the same ingest transaction.
insert into public.derived_artifacts
  (name, producer, host, cadence, staleness_budget, is_serving)
values ('rent_map_cells', 'api/rent_map.py + fetch_rent_map.yml', 'api-request',
        'on-demand + 0 3 5 * *', interval '40 days', true)
on conflict (name) do nothing;

-- ---------------------------------------------------------------------------
-- 2. The measure.
-- ---------------------------------------------------------------------------
create or replace function public.mf_reference(
  p_category_main  text,
  p_category_type  text,
  p_disposition    text,
  p_area_m2        numeric,
  p_price_czk      bigint,
  p_condition      text,
  p_has_balcony    boolean,
  p_terrace        boolean,
  p_furnished      text,
  p_garage         boolean,
  p_has_lift       boolean,
  p_building_type  text,
  p_obec_kod       bigint,
  p_katastr_kod    bigint,
  p_country_status public.country_status
)
returns table (mf_reference_rent_czk integer, mf_gross_yield_pct numeric, mf_reference_rent jsonb)
language sql
stable
parallel safe
as $fn$
select
  s.rent_czk,
  case when p_category_type = 'prodej' and p_price_czk >= 100000
       then round(s.rent_czk * 12.0 / p_price_czk * 100, 2) end,
  jsonb_strip_nulls(jsonb_build_object(
    'status', s.status,
    'note', case s.status
              when 'territory_coarse' then 'Konkrétní katastr není znám.'
              when 'no_rent_cell'     then 'Cenová mapa MF pro toto území a velikost bytu nájem neuvádí.'
              when 'not_in_cz'        then 'Cenová mapa MF pokrývá jen Českou republiku.'
              when 'location_unknown' then 'Poloha bytu zatím není určena.'
              when 'inputs_missing'   then 'MF nelze spočítat: chybí dispozice nebo plocha bytu.'
            end,
    'territory', case when s.priced then jsonb_build_object(
                   'ruian_code', c.code,
                   'level', case c.level when 'ku' then 'ku' else 'obec' end,
                   'name', c.name,
                   'kraj', c.kraj,
                   'basis', case when c.level = 'town' and s.status = 'ok'
                                 then 'town_uniform' end) end,
    'vk', case when s.priced then x.vk end,
    'is_novostavba', case when s.priced then x.nov end,
    'source_revision', case when s.priced then c.source_revision end,
    'source_date', case when s.priced then c.source_date end,
    'base_per_m2', case when s.status = 'ok' then c.base_per_m2 end,
    'adjustments', case when s.priced then jsonb_path_query_array(jsonb_build_array(
        case when a.balcony <> 0        then jsonb_build_object('attribute', 'balcony',        'czk_per_m2', a.balcony) end,
        case when a.elevator <> 0       then jsonb_build_object('attribute', 'elevator',       'czk_per_m2', a.elevator) end,
        case when a.furnished <> 0      then jsonb_build_object('attribute', 'furnished',      'czk_per_m2', a.furnished) end,
        case when a.garage <> 0         then jsonb_build_object('attribute', 'garage',         'czk_per_m2', a.garage) end,
        case when a.other_material <> 0 then jsonb_build_object('attribute', 'other_material', 'czk_per_m2', a.other_material) end,
        case when a.terrace <> 0        then jsonb_build_object('attribute', 'terrace',        'czk_per_m2', a.terrace) end),
      '$[*] ? (@ != null)') end,
    'adjustments_sum_per_m2', case when s.priced then a.total end,
    'total_per_m2', case when s.status = 'ok' then c.base_per_m2 + a.total end,
    'area_m2', case when s.priced then p_area_m2 end,
    'monthly_rent_czk', s.rent_czk,
    'range', case when s.status = 'territory_coarse' then jsonb_build_object(
        'per_m2_min', s.lo_per_m2,
        'per_m2_max', s.hi_per_m2,
        'rent_min_czk', s.rent_min_czk,
        'rent_max_czk', s.rent_max_czk,
        'yield_min_pct', case when p_category_type = 'prodej' and p_price_czk >= 100000
                              then round(s.rent_min_czk * 12.0 / p_price_czk * 100, 2) end,
        'yield_max_pct', case when p_category_type = 'prodej' and p_price_czk >= 100000
                              then round(s.rent_max_czk * 12.0 / p_price_czk * 100, 2) end) end))
-- greatest()/least() IGNORE a NULL argument, so the clamp alone would turn a missing or
-- non-numeric disposition into VK 1; the guard keeps it NULL (-> inputs_missing).
from (select case when p_disposition ~ '^\s*[0-9]'
                  then least(greatest(substring(p_disposition from '^\s*([0-9]{1,3})')::integer,
                                      1), 4)
             end as vk,
             coalesce(p_condition = 'novostavba', false) as nov) x
-- At most one of the three matches (each later join requires the earlier ones to miss),
-- so coalesce(ku.col, ob.col, tw.col) below reads exactly the chosen cell's row.
left join public.rent_map_cells ku
       on ku.level = 'ku' and ku.code = p_katastr_kod and ku.vk = x.vk and ku.nov = x.nov
left join public.rent_map_cells ob
       on ob.level = 'obec' and ob.code = p_obec_kod and ob.vk = x.vk and ob.nov = x.nov
      and ku.code is null
left join public.rent_map_cells tw
       on tw.level = 'town' and tw.code = p_obec_kod and tw.vk = x.vk and tw.nov = x.nov
      and ku.code is null and ob.code is null and p_katastr_kod is null
cross join lateral (
  select coalesce(ku.level, ob.level, tw.level) as level,
         coalesce(ku.code, ob.code, tw.code) as code,
         coalesce(ku.name, ob.name, tw.name) as name,
         coalesce(ku.kraj, ob.kraj, tw.kraj) as kraj,
         coalesce(ku.base_per_m2, ob.base_per_m2, tw.base_per_m2) as base_per_m2,
         tw.lo_per_m2, tw.hi_per_m2,
         coalesce(ku.adj_balcony, ob.adj_balcony, tw.adj_balcony) as adj_balcony,
         coalesce(ku.adj_terrace, ob.adj_terrace, tw.adj_terrace) as adj_terrace,
         coalesce(ku.adj_furnished, ob.adj_furnished, tw.adj_furnished) as adj_furnished,
         coalesce(ku.adj_garage, ob.adj_garage, tw.adj_garage) as adj_garage,
         coalesce(ku.adj_elevator, ob.adj_elevator, tw.adj_elevator) as adj_elevator,
         coalesce(ku.adj_other_material, ob.adj_other_material, tw.adj_other_material)
           as adj_other_material,
         coalesce(ku.source_revision, ob.source_revision, tw.source_revision) as source_revision,
         coalesce(ku.source_date, ob.source_date, tw.source_date) as source_date) c
cross join lateral (
  select case when p_has_balcony then c.adj_balcony else 0 end as balcony,
         case when p_terrace then c.adj_terrace else 0 end as terrace,
         case when p_furnished = 'ano' then c.adj_furnished else 0 end as furnished,
         case when p_garage then c.adj_garage else 0 end as garage,
         case when p_has_lift then c.adj_elevator else 0 end as elevator,
         case when x.nov and p_building_type not in ('panel', 'cihla')
              then c.adj_other_material else 0 end as other_material) a0
cross join lateral (
  select a0.*, a0.balcony + a0.terrace + a0.furnished + a0.garage + a0.elevator
               + a0.other_material as total) a
cross join lateral (
  select t.status, t.status in ('ok', 'territory_coarse') as priced,
         case when t.status = 'ok'
              then round((c.base_per_m2 + a.total) * p_area_m2)::integer end as rent_czk,
         c.lo_per_m2 + a.total as lo_per_m2,
         c.hi_per_m2 + a.total as hi_per_m2,
         case when t.status = 'territory_coarse'
              then round((c.lo_per_m2 + a.total) * p_area_m2)::integer end as rent_min_czk,
         case when t.status = 'territory_coarse'
              then round((c.hi_per_m2 + a.total) * p_area_m2)::integer end as rent_max_czk
    from (select case
                   when p_country_status = 'foreign' then 'not_in_cz'
                   when p_obec_kod is null then 'location_unknown'
                   when x.vk is null or p_area_m2 is null or p_area_m2 < 12 then 'inputs_missing'
                   when c.code is null then 'no_rent_cell'
                   when c.base_per_m2 is not null then 'ok'
                   else 'territory_coarse'
                 end as status) t) s
where p_category_main = 'byt'
$fn$;

comment on function public.mf_reference(text, text, text, numeric, bigint, text, boolean,
  boolean, text, boolean, boolean, text, bigint, bigint, public.country_status) is
  'THE MF reference rent (Cenová mapa nájemného): facts + stored obec/KÚ codes -> rent, '
  'yield and the detail jsonb (value | range + note | note). Reads only rent_map_cells. '
  'Inlined by the planner; call it through LEFT JOIN LATERAL. Migration 565.';

revoke execute on function public.mf_reference(text, text, text, numeric, bigint, text,
  boolean, boolean, text, boolean, boolean, text, bigint, bigint, public.country_status)
  from public, anon;
grant execute on function public.mf_reference(text, text, text, numeric, bigint, text,
  boolean, boolean, text, boolean, boolean, text, bigint, bigint, public.country_status)
  to authenticated, service_role;

reset lock_timeout;

-- ---------------------------------------------------------------------------
-- 3. The proof. What `\df` cannot show: that the function is still inlinable, that
--    the cells answer for the revision every reader will see, and that no browser role
--    beyond `authenticated` can read them.
-- ---------------------------------------------------------------------------
do $$
declare
  missing text[] := '{}';
  f record;
begin
  select p.prolang = (select oid from pg_language where lanname = 'sql') as is_sql,
         p.provolatile = 's' as is_stable, p.proisstrict as is_strict,
         p.prosecdef as is_definer, p.proconfig is null as no_set, p.proretset as is_set
    into f
    from pg_proc p
   where p.oid = 'public.mf_reference(text, text, text, numeric, bigint, text, boolean, '
                 'boolean, text, boolean, boolean, text, bigint, bigint, public.country_status)'
                 ::regprocedure;
  if not (f.is_sql and f.is_stable and not f.is_strict and not f.is_definer
          and f.no_set and f.is_set) then
    missing := missing || 'mf_reference is not in its inlinable shape';
  end if;
  if not exists (select 1 from pg_indexes
                  where schemaname = 'public' and indexname = 'rent_map_cells_key') then
    missing := missing || 'rent_map_cells_key missing';
  end if;
  if exists (select 1 from public.rent_map_revisions)
     and not exists (select 1 from public.rent_map_cells c
                      where c.source_revision = (select max(source_revision)
                                                   from public.rent_map_revisions)) then
    missing := missing || 'rent_map_cells does not answer for the latest revision';
  end if;
  if has_table_privilege('anon', 'public.rent_map_cells', 'SELECT') then
    missing := missing || 'anon can read rent_map_cells';
  end if;
  if array_length(missing, 1) is not null then
    raise exception '565 did not land: %', array_to_string(missing, '; ');
  end if;
end $$;
