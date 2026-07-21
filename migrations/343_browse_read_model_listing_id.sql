-- 343_browse_read_model_listing_id.sql
-- R2 read cutover (runbook §4 "MUST precede flip", browse-hydration bullet).
--
-- Exposes the surrogate on the Browse read path so the frontend can hydrate on
-- it, and re-points properties_public's repr join off the legacy handle. Without
-- this, post-Gate-2 EVERY browse card whose representative listing is a new
-- non-sreality row renders blank: the repr join misses, so price_unit / floor /
-- broker_* / description / the street fallback all go NULL ("repr goes NULL").
--
-- Purely additive to every consumer: new columns are appended (CREATE OR REPLACE
-- VIEW can only append), and the frontend selects explicit column lists, so
-- nothing reads them until the follow-up frontend PR does.
--
-- Why repr_listing_ref_id and not a migration of repr_listing_id: the surrogate
-- twin ALREADY exists (mig 323) and is written on every property write path
-- (recompute_property_stats attach + recompute, property_identity split,
-- scraper singleton). Verified live: 548,498/548,498 properties have it, and it
-- resolves to the same listing as repr_listing_id for every one. So this is a
-- pure read-side change — no writer change, no backfill, and the
-- property-singleton display mirror is untouched.

-- 1. browse_projection: append the surrogate. Both browse read models
--    (browse_list via rebuild_browse_list, properties_map_mv via
--    rebuild_properties_map_mv) materialize `select * from browse_projection`,
--    so they inherit the column with no function change.
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
    repr_listing_ref_id as listing_id
from properties p
where status = 'active'::text
  and (not (select publication_gate_enabled()) or published_at is not null);

-- 2. properties_public: re-point the repr join onto the surrogate and expose it
--    (+ source_id_native, free from the same join — it lets the SPA build a
--    canonical /listing/{source}/{native} link for a repr with no sreality_id).
create or replace view properties_public as
select
    p.id as property_id,
    p.repr_listing_id as sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk as price_czk,
    l.price_unit,
    p.area_m2,
    p.disposition,
    p.locality,
    p.district,
    p.locality_district_id,
    p.locality_region_id,
    p.lat,
    p.lng,
    l.floor,
    l.total_floors,
    p.has_balcony,
    p.has_parking,
    p.has_lift,
    p.building_type,
    p.condition,
    p.energy_rating,
    p.estate_area,
    p.usable_area,
    p.garden_area,
    p.category_sub_cb,
    p.furnished,
    p.terrace,
    p.cellar,
    p.garage,
    p.parking_lots,
    p.ownership,
    l.broker_name,
    l.broker_email,
    l.broker_phone,
    case
        when p.is_active then greatest(0, floor(extract(epoch from now() - p.first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    case
        when p.area_m2 is not null and p.area_m2 > 0::numeric and p.current_price_czk is not null then round(p.current_price_czk::numeric / p.area_m2, 2)
        else null::numeric
    end as price_per_m2,
    p.building_condition_level,
    p.apartment_condition_level,
    l.description,
    p.source_count,
    p.distinct_site_count,
    p.price_drop_count,
    p.price_rise_count,
    p.max_price_drop_pct,
    p.stats_computed_at,
    p.source,
    coalesce(p.street, l.street) as street,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    p.obec,
    p.okres,
    p.region,
    p.home_obec_pop,
    p.near_pop_5km,
    p.near_pop_15km,
    p.near_jobs_5km,
    p.near_jobs_15km,
    p.near_youth_5km,
    p.near_youth_15km,
    p.near_overall_5km,
    p.near_overall_15km,
    p.subtype,
    p.last_change_at,
    p.obec_id,
    p.okres_id,
    p.region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    concat_ws(', '::text, p.street, p.locality) as place_search_text,
    p.asset_id,
    p.mf_reference_rent,
    p.published_at,
    p.repr_listing_ref_id as listing_id,
    l.source_id_native
from properties p
     left join listings l on l.id = p.repr_listing_ref_id
where p.status = 'active'::text
  and (not (select publication_gate_enabled()) or p.published_at is not null);

-- 3. listing_broker_public: expose the surrogate so brokers.ts can key on it.
--    APPENDED, not inserted: CREATE OR REPLACE VIEW can only add columns at the
--    END — putting listing_id second reads to Postgres as renaming the existing
--    second column ("cannot change name of view column broker_id to listing_id").
create or replace view listing_broker_public as
select l.sreality_id,
    bi.broker_id,
    b.display_name as broker_display_name,
    coalesce(f.display_name, f.canonical_domain) as broker_firm_label,
    l.id as listing_id
   from listings l
     join broker_identities bi on bi.id = l.broker_identity_id
     join brokers b on b.id = bi.broker_id and b.status = 'active'::text
     left join firms f on f.id = b.primary_firm_id;

-- 4. Rebuild both browse read models NOW rather than waiting for pg_cron.
--    toolkit/browse_read_model.py's sync_browse_list does a POSITIONAL
--    `insert into browse_list select * from browse_projection` (no column
--    list), so between this migration and the next 5-minute tick every
--    merge/unmerge/split would hit "INSERT has more expressions than target
--    columns". It is caught inside a SAVEPOINT so merges still commit, but
--    read-your-writes would silently degrade for up to 5 minutes. Rebuilding
--    here closes that window immediately.
select rebuild_browse_list();
select rebuild_properties_map_mv();

-- Post-condition: rebuild_properties_map_mv DROP+CREATEs properties_map_mv, and a
-- freshly created relation inherits the DEFAULT ACL. Migration 342 revoked MAINTAIN
-- at the default ACL for exactly this reason (migration 331's one-time revoke was
-- undone by the next rebuild). Verified before writing this migration that the
-- postgres default ACL is now `authenticated=r` (SELECT only), so the rebuild above
-- cannot re-grant — but assert it rather than trust it, so this migration can never
-- silently undo 342's fix.
do $$
declare v_left integer;
begin
  if current_setting('server_version_num')::int < 170000 then
    return;  -- MAINTAIN is PG17+; naming it is a syntax error on the PG15 CI replay
  end if;
  select count(*) into v_left
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public' and c.relkind in ('r', 'm', 'p')
     and (has_table_privilege('authenticated', c.oid, 'MAINTAIN')
       or has_table_privilege('anon', c.oid, 'MAINTAIN'));
  if v_left > 0 then
    raise exception
      'browse rebuild re-granted MAINTAIN to a browser role on % relation(s) — migration 342 regressed',
      v_left;
  end if;
end $$;
