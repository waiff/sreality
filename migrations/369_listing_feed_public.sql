-- 369_listing_feed_public.sql
--
-- DEPENDS ON migration 368 (listings.discovery_seq — PR #945, portal-order-fidelity Phase 1).
-- That PR was cut from main independently and may not be merged/applied yet — apply 368
-- BEFORE this one, or this migration's `discovery_seq`/`listings_feed_sort_idx` references
-- will fail against a database that doesn't have the column yet.
--
-- Phase 2+3 of the portal-order-fidelity program (docs/design/portal-order-fidelity.md):
-- a listing-grain, single-portal read surface for Browse's "filter to one portal, get an
-- exact mirror of that portal's own page" mode. Deliberately NOT built on `browse_list`
-- (migration 276) or `properties`/`properties_public` — those are the property-grain,
-- multi-portal-deduped market view rule #15 exists for, and a filtered-to-one-portal
-- request needs every displayed field to genuinely be that portal's own listing's data,
-- not a trust-blended golden record (the confirmed leakage bug in
-- scripts/recompute_property_stats.py's `golden`/`best_geo`/`best_street` CTEs, which
-- rank source trust ahead of activity — see the design doc's Root cause 3). A listing-grain
-- view never touches those CTEs, so every field is unambiguously the filtered listing's own.
--
-- Sort key: `portal_date` prefers the portal's own declared date (`listings.published_at`)
-- ONLY for the two sources where it is a reliable first-publish-or-activity signal today
-- (ceskereality = genuine "Datum vložení"; bazos = last-bump/TOP-renewal date, still a
-- legitimate recency signal for that portal's own page). Every other source — including
-- sreality, whose `published_at` is a weak ~40%-populated day-granular fallback that would
-- otherwise wrongly rank a stale-dated row above a same-day discovery — falls through to
-- NULL here, so `discovery_seq` (migration 368: a true relative-discovery-order sequence,
-- assigned once at index-walk enqueue time, immune to the batching/concurrency reordering
-- documented in the design doc's Root cause 2) becomes the EFFECTIVE sort key. This is safe
-- specifically because the intended query always filters to exactly one `source` — within a
-- single portal's result set only one of the two columns ever has real spread, so a plain
-- `ORDER BY portal_date DESC NULLS LAST, discovery_seq DESC NULLS LAST, id DESC` self-selects
-- the right effective key with no per-portal branching in the reader. The day bezrealitky's
-- anon API starts actually returning `timeActivated` (it's wired but NULL today, migration
-- 266), this view can add it to the trusted-source list with a one-line `create or replace`
-- — no reader change needed.
--
-- Identity: `id` (surrogate PK) + `source_id_native` are the natural key going forward
-- (rule: R2/Gate-2 identity work) — `sreality_id` is carried for legacy compat only (NULL
-- for non-sreality rows post-Gate-2 flip) and must not be used as the row identity here.
--
-- NOTE: this migration ships the read contract only. Frontend wiring (detecting a
-- single-portal filter, switching Browse's query source, and — the part that needs live
-- browser testing this session couldn't do — extending keyset.ts's 2-column cursor pattern
-- to this view's 3-column sort) is tracked as a follow-up in the design doc, not in this
-- migration.

create view listing_feed_public as
select
  l.id,
  l.source,
  l.source_id_native,
  l.sreality_id,                          -- legacy compat only; NULL post-Gate-2 for non-sreality
  l.category_main,
  l.category_type,
  l.category_sub_cb,
  l.subtype,
  l.disposition,
  l.price_czk,
  l.price_unit,
  l.area_m2,
  l.locality,
  l.district,
  l.obec,
  l.okres,
  l.region,
  l.obec_id,
  l.okres_id,
  l.region_id,
  st_y(l.geom::geometry) as lat,
  st_x(l.geom::geometry) as lng,
  l.floor,
  l.total_floors,
  l.has_balcony,
  l.has_parking,
  l.has_lift,
  l.building_type,
  l.condition,
  l.energy_rating,
  l.estate_area,
  l.usable_area,
  l.garden_area,
  l.furnished,
  l.terrace,
  l.cellar,
  l.garage,
  l.parking_lots,
  l.ownership,
  l.building_condition_level,
  l.apartment_condition_level,
  l.description,
  l.street,
  l.house_number,
  l.is_active,
  l.first_seen_at,
  l.last_seen_at,
  l.published_at,
  l.discovery_seq,
  case when l.source in ('bazos', 'ceskereality') then l.published_at end as portal_date,
  case when area_m2 is not null and area_m2 > 0::numeric and price_czk is not null
       then price_czk::numeric / area_m2::numeric
       else null::numeric end as price_per_m2
from listings l;

-- anon holds NO relation grants (migration 299's settled Phase 0 posture — the
-- SPA is fully login-gated and reads as authenticated; CI's
-- test_anon_holds_no_relation_grants enforces this on every new view). Unlike
-- listings_public/properties_public (grandfathered pre-299 anon grants, left
-- untouched rather than risk breaking their existing consumers), this is a
-- brand-new view with no such history — it follows the current posture from
-- day one.
grant select on listing_feed_public to authenticated;

-- Covering index for the intended query shape: filter (source, is_active[, category_main,
-- category_type]), sort (portal_date desc nulls last, discovery_seq desc nulls last, id desc)
-- for the keyset tiebreak. Mirrors browse_list's proven covering-index pattern (migration
-- 276) rather than inventing a new one. Expression columns match the view's `portal_date`
-- CASE exactly, so the planner can use this index to satisfy that ORDER BY without a sort.
create index listings_feed_sort_idx on listings (
  source, is_active, category_main, category_type,
  (case when source in ('bazos', 'ceskereality') then published_at end) desc nulls last,
  discovery_seq desc nulls last,
  id desc
);
