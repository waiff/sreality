-- 363_browse_projection_source_id_native.sql
-- NOTE ON NUMBERING: applied live via the Supabase MCP under the name
-- `362_browse_projection_source_id_native` (schema_migrations version
-- 20260723201941) before a concurrent branch merged `362_trial_at_signup` to
-- main. The recorded name is immutable, so the DISK file is renumbered to 363 to
-- clear the duplicate-number guard; prod is unaffected (already applied), and CI
-- replays this file on a fresh DB where the CREATE OR REPLACE is a clean forward.
--
-- Gate-2 canonical-URL tail: expose the repr listing's source_id_native on the
-- Browse read path so the SPA can build /listing/{source}/{native} links for a
-- representative listing that has no sreality_id (post-Gate-2 non-sreality rows
-- insert sreality_id = NULL). Migration 343 added source_id_native to
-- properties_public but NOT to browse_projection, and browse_projection has no
-- listings join at all (it reads only columns denormalized onto properties;
-- source_id_native is not one of them). So it is added here.
--
-- WHY a correlated scalar subquery, not a join: browse_projection's select list
-- is UNQUALIFIED, and listings shares many bare column names with properties
-- (source, street, first_seen_at, is_active, category_main, disposition, ...);
-- adding `left join listings l` would make every one of those ambiguous and fail
-- the replace. The scalar subquery probes listings by PK (l.id =
-- p.repr_listing_ref_id, at most one row, NULL when repr is null) -- identical
-- semantics to properties_public's PK left join, one index lookup per row, zero
-- ambiguity, and the existing column expressions stay verbatim.
--
-- CREATE OR REPLACE VIEW can only APPEND columns, so source_id_native goes last.
-- rebuild_browse_list()/rebuild_properties_map_mv() both materialize
-- `select * from browse_projection`, so the column auto-propagates with no
-- function change and no index change (a link column is never a filter/sort key).
-- Rebuilt inline to close the read-your-writes window: sync_browse_list()'s
-- positional `insert into browse_list select * from browse_projection` would
-- throw "INSERT has more expressions than target columns" until the next full
-- rebuild otherwise (caught in a savepoint, but degrades reads for up to 5 min).

begin;

set local lock_timeout = '5s';

create or replace view browse_projection as
select
    id as property_id,
    repr_listing_id as sreality_id,
    first_seen_at,
    last_seen_at,
    is_active,
    category_main,
    category_type,
    current_price_czk as price_czk,
    area_m2,
    disposition,
    locality,
    district,
    locality_district_id,
    locality_region_id,
    lat,
    lng,
    has_balcony,
    has_parking,
    has_lift,
    building_type,
    condition,
    energy_rating,
    estate_area,
    usable_area,
    garden_area,
    category_sub_cb,
    furnished,
    terrace,
    cellar,
    garage,
    parking_lots,
    ownership,
    case
        when is_active then greatest(0, floor(extract(epoch from now() - first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from last_seen_at - first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    case
        when area_m2 is not null and area_m2 > 0::numeric and current_price_czk is not null then round(current_price_czk::numeric / area_m2, 2)
        else null::numeric
    end as price_per_m2,
    building_condition_level,
    apartment_condition_level,
    source,
    street,
    mf_reference_rent_czk,
    mf_gross_yield_pct,
    obec,
    okres,
    region,
    home_obec_pop,
    near_pop_5km,
    near_pop_15km,
    near_jobs_5km,
    near_jobs_15km,
    near_youth_5km,
    near_youth_15km,
    near_overall_5km,
    near_overall_15km,
    subtype,
    last_change_at,
    obec_id,
    okres_id,
    region_id,
    price_change_count,
    price_change_count_30d,
    price_change_count_90d,
    price_change_count_365d,
    total_price_change_pct,
    concat_ws(', '::text, street, locality) as place_search_text,
    asset_id,
    repr_listing_ref_id as listing_id,
    (select l.source_id_native from listings l where l.id = p.repr_listing_ref_id) as source_id_native
from properties p
where status = 'active'::text
  and (not (select publication_gate_enabled()) or published_at is not null);

-- Rebuild both read models now (both do `select * from browse_projection`) so
-- the new column lands immediately and the positional-insert window is closed.
select rebuild_browse_list();
select rebuild_properties_map_mv();

-- rebuild_properties_map_mv DROP+CREATEs properties_map_mv, which inherits the
-- default ACL; assert it did not re-grant MAINTAIN to a browser role (mirrors
-- migration 342/343's guard). MAINTAIN is PG17+, so skip on the PG15 CI replay.
do $$
declare v_left integer;
begin
  if current_setting('server_version_num')::int < 170000 then
    return;
  end if;
  select count(*) into v_left
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public' and c.relkind in ('r', 'm', 'p')
     and (has_table_privilege('authenticated', c.oid, 'MAINTAIN')
       or has_table_privilege('anon', c.oid, 'MAINTAIN'));
  if v_left > 0 then
    raise exception
      'browse rebuild re-granted MAINTAIN to a browser role on % relation(s)', v_left;
  end if;
end $$;

commit;
