-- 224_asset_groups.sql
-- "Same physical building" soft-link ABOVE properties. Purely additive.
--
-- The hierarchy today is listings (one portal posting) -> properties (the same
-- OFFER/category cohort across portals, rule #15). But a building listed as
-- both `dum` and `komercni` (or sale + rent) becomes TWO properties: the
-- merge guard in merge_properties correctly refuses to collapse different
-- category cohorts (it would corrupt the single denormalized
-- properties.category_main/category_type that Browse facets, Stats and MF-yield
-- key on). Production has ~1,800 such cross-category same-building pairs the
-- operator cannot express today.
--
-- An `asset` is the third grain: it groups properties that are the SAME
-- physical building across category cohorts, WITHOUT collapsing them -- both
-- property rows and both category facets stay intact. So per-category analytics
-- are untouched, and the operator (and a future engine MatchProfile) can still
-- assert "these are the same building". Unlike a merge, an asset link is
-- trivially reversible (clear asset_id); asset_membership_events is the
-- append-only audit, mirroring property_merge_events.
--
-- See toolkit/asset_identity.py for the link/unlink mechanics.

create table assets (
  id           bigserial   primary key,
  status       text        not null default 'active'
    check (status in ('active', 'dissolved')),
  note         text,
  created_by   text,
  created_at   timestamptz not null default now(),
  dissolved_at timestamptz
);

-- A property belongs to at most one asset; NULL = standalone. ON DELETE SET
-- NULL so a (never-expected) asset delete can never orphan a property.
alter table properties
  add column asset_id bigint references assets(id) on delete set null;

create index properties_asset_id_idx on properties (asset_id) where asset_id is not null;

-- Append-only link/unlink ledger. source='auto' is reserved for a future engine
-- MatchProfile that proposes asset links for non-apartment inventory (the dedup
-- engine is apartment-only and never reaches these); today every row is
-- source='operator'.
create table asset_membership_events (
  id          bigserial   primary key,
  asset_id    bigint      not null references assets(id),
  property_id bigint      not null references properties(id),
  action      text        not null check (action in ('linked', 'unlinked')),
  reason      text,
  source      text        not null default 'operator'
    check (source in ('operator', 'auto')),
  confidence  numeric,
  created_by  text,
  created_at  timestamptz not null default now()
);

create index asset_membership_events_asset_idx    on asset_membership_events (asset_id);
create index asset_membership_events_property_idx on asset_membership_events (property_id);

alter table assets                  enable row level security;
alter table asset_membership_events enable row level security;

-- Expose asset_id on the property grain the browser reads, so Browse can flag
-- "also listed as ..." siblings and (optionally) collapse same-building cards
-- without a write path or an extra round-trip. Append-only column add; the rest
-- of the body is the live definition verbatim.
create or replace view properties_public as
 SELECT p.id AS property_id,
    p.repr_listing_id AS sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk AS price_czk,
    l.price_unit,
    p.area_m2,
    p.disposition,
    p.locality,
    p.district,
    l.locality_district_id,
    l.locality_region_id,
    st_y(p.geom::geometry) AS lat,
    st_x(p.geom::geometry) AS lng,
    l.floor,
    l.total_floors,
    p.has_balcony,
    p.has_parking,
    p.has_lift,
    p.building_type,
    p.condition,
    l.energy_rating,
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
        CASE
            WHEN p.is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
            ELSE GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
        END AS tom_days,
        CASE
            WHEN p.area_m2 IS NOT NULL AND p.area_m2 > 0::numeric AND p.current_price_czk IS NOT NULL THEN round(p.current_price_czk::numeric / p.area_m2, 2)
            ELSE NULL::numeric
        END AS price_per_m2,
    l.building_condition_level,
    l.apartment_condition_level,
    l.description,
    p.source_count,
    p.distinct_site_count,
    p.price_drop_count,
    p.price_rise_count,
    p.max_price_drop_pct,
    p.stats_computed_at,
    l.source,
    COALESCE(p.street, l.street) AS street,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    l.obec,
    l.okres,
    l.region,
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
    l.obec_id,
    l.okres_id,
    l.region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    concat_ws(', '::text, COALESCE(p.street, l.street), p.locality) AS place_search_text,
    p.asset_id
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;
