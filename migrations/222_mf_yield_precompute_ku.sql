-- 222_mf_yield_precompute_ku.sql
--
-- De-spatialise the hourly MF gross-yield recompute, and fix a coverage bug, by
-- precomputing each listing's cadastral (ku) territory at write time.
--
-- WHY
-- recompute_mf_gross_yields() (migrations 133/134) resolved every sale
-- apartment's MF rent-map territory with a per-run point-in-polygon lateral
-- (`st_covers(admin_boundaries.geom, listing.geom)`). That spatial scan ran in
-- full on every hourly run regardless of the `is distinct from` write guard, so
-- its cost grew with the multi-portal listing count until it crossed the pooler
-- statement timeout (~120s) and the job started failing every hour.
--
-- It also carried a latent COVERAGE bug. `ku` (cadastral) polygons tile the
-- whole country, so the lateral's `order by ku-first` always picked a `ku`, then
-- INNER-joined the rent map on that one code. When a listing's `ku` was not
-- individually priced by MF, the join dropped the listing to NULL even though
-- its `obec` had an MF rent row -- the `obec` fallback branch was dead. Measured
-- on production: ~69% of eligible apartments got a yield vs ~88% achievable.
--
-- THE FIX (mirrors migration 142 city-proximity + the 140/162 admin-geo trigger:
-- resolve geography ONCE at write time, keep the periodic job arithmetic)
--   1. listings.ku_id  -- the containing cadastral area (admin_boundaries.id,
--      level='ku'), an INTERNAL join key (NOT surfaced on public views; no
--      consumer filters on it, unlike obec_id which Browse growth uses).
--   2. listings_set_admin_geo() also captures ku_id from the same write-time PIP
--      (one extra index probe per scrape); its early-return guard gains
--      `new.ku_id is not null` so backfilled-but-missed rows self-heal on their
--      next geom-touch (the guard pattern migration 162 needed for obec_id).
--   3. recompute_mf_gross_yields() does NO per-run PIP -- it joins the precomputed
--      ku_id / obec_id to the rent map and takes coalesce(ku-rent, obec-rent),
--      ku-preferred. This reproduces the on-demand compute_reference_rent()
--      resolution EXACTLY (validated 1:1 over a 1,500-listing sample) and revives
--      the obec fallback, so both MF reference paths now agree.
--
-- Existing rows are backfilled out-of-band (batched, set-based) right after this
-- migration -- the same pattern migrations 140/162 used. New rows get ku_id from
-- the trigger. Until a row is backfilled its ku_id is NULL and the recompute
-- simply falls back to obec_id (or NULL -> no yield), so there is no broken
-- intermediate state.
--
-- Additive: new column + index, CREATE OR REPLACE trigger + function. No public
-- view or RPC signature changes (the three mf_* columns are untouched in name,
-- type and shape; the function still RETURNS integer).

set local lock_timeout = '5s';

-- 1. ku_id geographic join key (internal) + partial index ---------------------
alter table listings
  add column if not exists ku_id bigint;

comment on column listings.ku_id is
  'Cadastral area (katastrální území) RÚIAN code = admin_boundaries.id (level=ku), '
  'derived from geom via the same PIP that fills obec_id. Internal join key for '
  'the MF rent-map territory resolution (recompute_mf_gross_yields); the rent map '
  'prices big cities at ku granularity and the rest at obec. Not exposed publicly.';

create index if not exists listings_ku_id_idx
  on listings (ku_id) where ku_id is not null;


-- 2. admin-geo trigger: also capture ku_id -----------------------------------
-- Reproduced from the live definition with v_ku_id added (one extra st_covers
-- probe) and the early-return guard extended with `new.ku_id is not null`.
create or replace function public.listings_set_admin_geo()
returns trigger
language plpgsql
as $function$
declare
  v_obec     text;
  v_okres    text;
  v_kraj     text;
  v_obec_id  bigint;
  v_okres_id bigint;
  v_kraj_id  bigint;
  v_ku_id    bigint;
begin
  if new.geom is null then
    return new;
  end if;

  if tg_op = 'UPDATE'
     and new.geom is not distinct from old.geom
     and new.okres is not null
     and new.obec_id is not null
     and new.okres_id is not null
     and new.ku_id is not null then
    return new;
  end if;

  select ob.id, ob.name, ok.id, ok.name, kr.id, kr.name
    into v_obec_id, v_obec, v_okres_id, v_okres, v_kraj_id, v_kraj
  from admin_boundaries ob
  left join admin_boundaries ok on ok.id = ob.parent_id and ok.level = 'okres'
  left join admin_boundaries kr on kr.id = ok.parent_id and kr.level = 'kraj'
  where ob.level = 'obec'
    and st_covers(ob.geom, new.geom)
  limit 1;

  select b.id
    into v_ku_id
  from admin_boundaries b
  where b.level = 'ku'
    and st_covers(b.geom, new.geom)
  limit 1;

  new.obec      := v_obec;
  new.okres     := v_okres;
  new.region    := v_kraj;
  new.obec_id   := v_obec_id;
  new.okres_id  := v_okres_id;
  new.region_id := v_kraj_id;
  new.ku_id     := v_ku_id;

  if new.district is null then
    if v_kraj = 'Hlavní město Praha' then
      new.district := v_obec;
    elsif v_okres is not null then
      new.district := 'okres ' || v_okres;
    end if;
  end if;

  return new;
end;
$function$;


-- 3. recompute_mf_gross_yields(): no per-run PIP -----------------------------
-- Reproduced from the live definition. ONLY the `cand` + `matched` CTEs change:
-- `cand` reads the precomputed ku_id / obec_id (was l.geom); `matched` replaces
-- the `join lateral (st_covers ...) terr` with two btree joins to the rent map on
-- ku_id and obec_id and a ku-preferred single-row pick (all territory fields,
-- incl. base rent, read from the ONE chosen side -- never mixed across
-- territories). Everything downstream (adj / computed / final / both UPDATEs) is
-- byte-identical to the prior definition.
create or replace function public.recompute_mf_gross_yields()
returns integer
language plpgsql
as $function$
declare
  n integer;
begin
  with cand as (
    select
      l.sreality_id, l.category_main, l.ku_id, l.obec_id, l.price_czk, l.area_m2,
      l.has_balcony, l.terrace, l.furnished, l.garage, l.has_lift,
      l.building_type,
      (l.condition = 'novostavba') as is_nov,
      case
        when l.disposition ~ '^[[:space:]]*[01]' then 1
        when l.disposition ~ '^[[:space:]]*2'    then 2
        when l.disposition ~ '^[[:space:]]*3'    then 3
        when l.disposition ~ '^[[:space:]]*[4-9]' then 4
        else null
      end as vk
    from listings l
    where l.category_type = 'prodej'
  ),
  matched as (
    select
      c.sreality_id, c.price_czk, c.area_m2, c.vk, c.is_nov,
      c.has_balcony, c.terrace, c.furnished, c.garage, c.has_lift,
      c.building_type,
      coalesce(vku.ruian_code, vob.ruian_code)           as ruian_code,
      coalesce(vku.level, vob.level)                      as level,
      case when vku.ruian_code is not null
           then vku.ku_name else vob.obec_name end        as terr_name,
      coalesce(vku.kraj, vob.kraj)                        as kraj,
      coalesce(vku.source_revision, vob.source_revision)  as source_revision,
      case
        when vku.ruian_code is not null
          then case when c.is_nov then vku.ref_rent_novostavba_per_m2
                    else vku.ref_rent_per_m2 end
        else case when c.is_nov then vob.ref_rent_novostavba_per_m2
                  else vob.ref_rent_per_m2 end
      end                                                 as base
    from cand c
    left join rent_map_values_public vku
      on vku.vk = c.vk and vku.ruian_code = c.ku_id
    left join rent_map_values_public vob
      on vob.vk = c.vk and vob.ruian_code = c.obec_id
    where c.category_main = 'byt'
      and c.vk is not null
      -- Two symmetric plausibility floors so garbage source data can't emit an
      -- absurd reference rent / yield: no real apartment sells under 100k CZK
      -- (excludes "cena v RK" placeholders + rent-magnitude prices mis-tagged
      -- 'prodej'), and none is under 12 m² (excludes broken area parses like a
      -- "3+kk, 4.2 m²" -- p01 of genuine yield-bearing flats is ~22 m²).
      and c.price_czk >= 100000
      and c.area_m2 is not null and c.area_m2 >= 12
      and (vku.ruian_code is not null or vob.ruian_code is not null)
  ),
  adj as (
    select
      m.sreality_id,
      coalesce(sum(a.czk_per_m2) filter (where
           (a.attribute = 'balcony'   and m.has_balcony)
        or (a.attribute = 'terrace'   and m.terrace)
        or (a.attribute = 'furnished' and m.furnished = 'ano')
        or (a.attribute = 'garage'    and m.garage)
        or (a.attribute = 'elevator'  and m.has_lift)
        or (a.attribute = 'other_material' and m.is_nov
            and m.building_type is not null
            and m.building_type not in ('panel', 'cihla'))
      ), 0) as adj_sum,
      coalesce(jsonb_agg(
        jsonb_build_object('attribute', a.attribute, 'czk_per_m2', a.czk_per_m2)
        order by a.attribute
      ) filter (where
           (a.attribute = 'balcony'   and m.has_balcony)
        or (a.attribute = 'terrace'   and m.terrace)
        or (a.attribute = 'furnished' and m.furnished = 'ano')
        or (a.attribute = 'garage'    and m.garage)
        or (a.attribute = 'elevator'  and m.has_lift)
        or (a.attribute = 'other_material' and m.is_nov
            and m.building_type is not null
            and m.building_type not in ('panel', 'cihla'))
      ), '[]'::jsonb) as adj_items
    from matched m
    join rent_map_adjustments_public a
      on a.vk = m.vk and a.is_novostavba = m.is_nov
    group by m.sreality_id
  ),
  computed as (
    select
      m.sreality_id,
      round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2)::integer as rent_czk,
      round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2 * 12
            / m.price_czk * 100, 2) as yield_pct,
      jsonb_build_object(
        'territory', jsonb_build_object(
          'ruian_code', m.ruian_code, 'level', m.level,
          'name', m.terr_name, 'kraj', m.kraj),
        'vk', m.vk,
        'is_novostavba', m.is_nov,
        'source_revision', m.source_revision,
        'base_per_m2', m.base,
        'adjustments', coalesce(a.adj_items, '[]'::jsonb),
        'adjustments_sum_per_m2', coalesce(a.adj_sum, 0),
        'total_per_m2', m.base + coalesce(a.adj_sum, 0),
        'area_m2', m.area_m2,
        'monthly_rent_czk',
          round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2)::integer
      ) as detail
    from matched m
    left join adj a on a.sreality_id = m.sreality_id
    where m.base is not null
  ),
  final as (
    select c.sreality_id, comp.rent_czk, comp.yield_pct, comp.detail
    from cand c
    left join computed comp on comp.sreality_id = c.sreality_id
  )
  update listings l
    set mf_reference_rent_czk = f.rent_czk,
        mf_gross_yield_pct    = f.yield_pct,
        mf_reference_rent     = f.detail
  from final f
  where l.sreality_id = f.sreality_id
    and (l.mf_reference_rent_czk is distinct from f.rent_czk
         or l.mf_gross_yield_pct is distinct from f.yield_pct
         or l.mf_reference_rent is distinct from f.detail);

  get diagnostics n = row_count;

  update properties p
    set mf_reference_rent_czk = l.mf_reference_rent_czk,
        mf_gross_yield_pct    = l.mf_gross_yield_pct
  from listings l
  where l.sreality_id = p.repr_listing_id
    and (p.mf_reference_rent_czk is distinct from l.mf_reference_rent_czk
         or p.mf_gross_yield_pct is distinct from l.mf_gross_yield_pct);

  return n;
end;
$function$;
