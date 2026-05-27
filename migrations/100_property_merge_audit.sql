-- 100_property_merge_audit.sql
-- PR2 of the multi-portal dedup completion: the merge/unmerge core the Tier-2
-- sweep (PR3) and the operator review UI call. Purely additive.
--
-- A merge is mechanically `UPDATE listings SET property_id = survivor` + a
-- recompute: the ~9 FK child tables key on listings.sreality_id, never
-- property_id, so listing history never moves. The loser property is
-- SOFT-RETIRED (status='merged_away'), never deleted -- property_identity_
-- candidates FKs cascade on delete and properties.repr_listing_id is SET NULL,
-- so a delete would wipe audit trails. property_merge_events is the per-child
-- ledger that makes unmerge a deterministic replay regardless of later
-- recomputes or re-merges.
--
-- Because the operator chose AUTO-merge for high-confidence Tier-2 matches
-- (PR3), reversibility is mandatory: every merge (auto or manual) is one
-- mergegroup of events, undoable via unmerge_group.

alter table properties
  add column status      text not null default 'active'
    check (status in ('active', 'merged_away')),
  add column merged_into bigint references properties(id),
  add column merged_at   timestamptz;

create index properties_status_idx      on properties (status);
create index properties_merged_into_idx on properties (merged_into);

-- The reversibility ledger: one row per re-pointed child listing. prev_property_id
-- is captured explicitly (== retired_property_id) so unmerge is a deterministic
-- replay even after the survivor later absorbs a third property.
create table property_merge_events (
  id                   bigserial primary key,
  merge_group_id       uuid        not null,
  survivor_property_id bigint      not null references properties(id),
  retired_property_id  bigint      not null references properties(id),
  listing_id           bigint      not null,
  prev_property_id     bigint      not null,
  reason               text        not null,
  confidence           numeric,
  markers              jsonb,
  source               text        not null default 'auto'
    check (source in ('auto', 'operator')),
  undone_at            timestamptz,
  undone_by            text,
  created_at           timestamptz not null default now()
);

create index property_merge_events_group_idx    on property_merge_events (merge_group_id);
create index property_merge_events_survivor_idx on property_merge_events (survivor_property_id);
create index property_merge_events_active_idx
  on property_merge_events (merge_group_id) where undone_at is null;

alter table property_merge_events enable row level security;

-- The candidate queue gains a link to the merge action it produced, plus a flag
-- so the review UI distinguishes auto-merges (which need a one-click Undo) from
-- operator-confirmed ones.
alter table property_identity_candidates
  add column auto_merged    boolean not null default false,
  add column merge_group_id uuid;

-- properties_public must exclude merged-away parents, else a merge leaves a
-- duplicate ghost in Browse (the survivor + the inactive retired row). Same
-- column list / order as migration 095 (so CREATE OR REPLACE is valid); only a
-- WHERE clause is added.
create or replace view properties_public as
select
  p.id                          as property_id,
  p.repr_listing_id             as sreality_id,
  p.first_seen_at,
  p.last_seen_at,
  p.is_active,
  p.category_main,
  p.category_type,
  p.current_price_czk           as price_czk,
  l.price_unit,
  p.area_m2,
  p.disposition,
  p.locality,
  p.district,
  l.locality_district_id,
  l.locality_region_id,
  ST_Y(p.geom::geometry)        as lat,
  ST_X(p.geom::geometry)        as lng,
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
  case
    when p.is_active then GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
    else GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
  end                           as tom_days,
  case
    when p.area_m2 is not null and p.area_m2 > 0::numeric and p.current_price_czk is not null
      then p.current_price_czk::numeric / p.area_m2::numeric
    else null::numeric
  end                           as price_per_m2,
  l.building_condition_level,
  l.apartment_condition_level,
  l.description,
  p.source_count,
  p.distinct_site_count,
  p.price_drop_count,
  p.price_rise_count,
  p.max_price_drop_pct,
  p.stats_computed_at
from properties p
left join listings l on l.sreality_id = p.repr_listing_id
where p.status = 'active';
