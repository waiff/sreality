-- 494_listings_public_source_url.sql
--
-- Append `listings.source_url` to listings_public (docs/design/portal-listing-url.md, W2).
--
-- The listing's page on its own portal. Until now only property_sources_public
-- (migrations 093/334) exposed it, and only `where property_id is not null` — so a
-- listing inside the ~5-minute pre-attach window, and EVERY listings_public reader
-- (the comparable modal, the detail page's own row), could not see it and the SPA
-- RECONSTRUCTED a sreality URL from the category triple instead: wrong slug for 14
-- of sreality's 48 sub-category codes, both auction types 404, land/other never
-- linked, and a merged property's siblings structurally unreconstructable per row.
-- W0 (#1400) made the column a stored fact for all nine portals; this append lets
-- every surface READ it, so the SPA's builder can be deleted (the same PR).
--
-- Body copied VERBATIM from migration 425 (the newest listings_public definition —
-- it carries measure_price_per_m2 / price_per_m2_basis; a copy of 420 would ERROR,
-- `create or replace view` cannot drop a column). source_url is APPENDED LAST:
-- replace can only append. No positional consumer of listings_public exists
-- (no `select * from listings_public` anywhere in the repo), so nothing is rebuilt.
--
-- Widens source_url's browser-readable scope from the property-attached rows to
-- every listing row — SAME audience (authenticated only; anon has been dark since
-- migration 299) and SAME data class (a public portal page URL, not contact PII).
-- broker_email / broker_phone stay null::text (migration 398).
--
-- View-only, ONE short transaction, no rebuild inside it: `create or replace view`
-- takes ACCESS EXCLUSIVE on the view and holds it to commit, and five matviews
-- depend on it. lock_timeout so it fails fast behind a reader rather than queueing
-- (an operator window, not an autonomous retry loop).
--
-- DEPLOY ORDER: APPLY THIS BEFORE the SPA PR that selects `source_url` merges —
-- every listings_public detail read would otherwise 400 on an unknown column
-- (the migration-438 "merged is not applied" failure mode).
--
-- The column comment carries the one rule a schema reader must know: sreality's
-- stored URL is DISPLAY-ONLY (its SSR page 302s into a login.seznam.cz autologin
-- chain and an infinite `cwtkn=` redirect loop; the scraper reaches sreality by id
-- through /api/v1/estates, and scraper.db.detail_ref keeps it off every queue).

begin;
set local lock_timeout = '5s';

create or replace view listings_public as
select
  sreality_id,
  first_seen_at,
  last_seen_at,
  is_active,
  category_main,
  category_type,
  price_czk,
  price_unit,
  area_m2,
  disposition,
  locality,
  district,
  locality_district_id,
  locality_region_id,
  st_y(geom::geometry) as lat,
  st_x(geom::geometry) as lng,
  floor,
  total_floors,
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
  broker_name,
  null::text as broker_email,
  null::text as broker_phone,
  case
    when is_active then greatest(0, floor(extract(epoch from now() - first_seen_at) / 86400::numeric)::integer)
    else greatest(0, floor(extract(epoch from last_seen_at - first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  measure_price_per_m2(price_czk::numeric, area_m2::numeric, category_main, category_type) as price_per_m2,
  building_condition_level,
  apartment_condition_level,
  description,
  source,
  street,
  house_number,
  mf_reference_rent_czk,
  mf_gross_yield_pct,
  mf_reference_rent,
  obec,
  okres,
  region,
  subtype,
  obec_id,
  okres_id,
  region_id,
  id,
  source_id_native,
  property_id,
  measure_price_per_m2_basis(category_main, category_type) as price_per_m2_basis,
  source_url
from listings;

-- Re-assert the posture explicitly rather than trusting create or replace to keep
-- the ACL (migration 420's own rule). anon stays dark (migration 299 part B1).
revoke all on listings_public from anon;
grant select on listings_public to authenticated;

comment on column listings.source_url is
  'The listing''s page on its own portal, emitted by the portal''s parser at ingest '
  '(sreality: scraper/sreality_url.py, a closed sitemap-sourced codebook). A stored '
  'fact every surface reads and none reconstructs. Preserve-if-null at the write. '
  'DISPLAY-ONLY for sreality: its SSR page is a login-redirect loop — never a fetch '
  'target (scraper.db.detail_ref). Design: docs/design/portal-listing-url.md.';

-- Close the window in which a fresh `select=source_url` 400s on a stale schema cache
-- (migrations 277/439 precedent).
select pg_notify('pgrst', 'reload schema');

commit;
