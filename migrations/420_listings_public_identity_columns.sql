-- 420: listings_public exposes source_id_native + property_id.
--
-- Hydration sprint W9b — the listing-detail chain's server half (W9a was the
-- client half). Two facts the page needs about the row it is already holding
-- were reachable only through a SECOND relation:
--
--   * property_id     -> fetchPropertySources' first hop is
--                        `select property_id from property_sources_public
--                         where id = <this listing>` — and that view is a thin
--                        view over `listings` itself (`where property_id is not
--                        null`), so the hop re-reads the very heap tuple
--                        listings_public just returned, one PostgREST round trip
--                        later, purely to learn a column it could have carried.
--                        Everything property-grain then queues behind it:
--                        property MF, the status-event log the price chart's
--                        inactive-period gaps are built from, and the pipeline
--                        funnel.
--   * source_id_native -> the legacy -> canonical redirect
--                        (/listing/{sreality_id} -> /listing/{source}/{native})
--                        found THIS listing's native id by scanning the sibling
--                        source list for `s.id = listing.id` — i.e. it waited on
--                        the whole multi-portal read to learn one string off its
--                        own row, keeping the synthetic negative id in the URL
--                        bar a full round trip longer than necessary.
--
-- Measured live, EXPLAIN (ANALYZE, BUFFERS): the deleted hop is 7 execution
-- buffers (+672 planning). The win is NOT blocks — it is one fewer request and
-- one fewer waterfall level, which is corollary B's whole point: every Railway /
-- PostgREST call pays a fixed floor before any work, so a 7-block statement and
-- a 700-block statement cost nearly the same wall-clock from Prague.
--
-- Both columns are on `listings` already and agree exactly with what
-- property_sources_public reports for the same row — verified live across every
-- row the view exposes: 0 mismatches on property_id, 0 on source_id_native, and
-- 0 listings pointing at a non-active (merged_away) property, so the two paths
-- cannot disagree about which property survived a merge.
--
-- NO PII. This is the browser-readable view (`authenticated` SELECT, `anon`
-- dark), so standing constraint 3 is the governing rule here: neither column is
-- contact-shaped. source_id_native is the portal's own public advert id — the
-- one already printed in every canonical URL the SPA links to
-- (/listing/{source}/{source_id_native}) — and property_id is an internal
-- grouping surrogate.
--
-- Written against the LIVE pg_get_viewdef, NOT a migration file: 398 replaced
-- this view to close a contact-PII hole and the shape of that fix is load-bearing.
-- broker_email / broker_phone stay as `null::text` projections — they CANNOT be
-- dropped (CREATE OR REPLACE VIEW cannot remove a column, and matviews depend on
-- this view, so DROP ... CASCADE is not available), so the names remain while the
-- values do not. tests/test_tenant_isolation_live.py re-derives that exemption
-- from the deparsed view body on every run precisely so a careless CREATE OR
-- REPLACE that restores the source expression fails rather than being waved
-- through. Both are carried forward here verbatim. broker_name stays a real
-- column: a label, not a contact.
--
-- Definer view, no security_invoker — unchanged from live (reloptions IS NULL).
-- Additive: CREATE OR REPLACE VIEW can only append, so both columns go on the end
-- after `id` and no existing consumer's column order moves.

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
  case
    when area_m2 is not null and area_m2 > 0::numeric and price_czk is not null then price_czk::numeric / area_m2::numeric
    else null::numeric
  end as price_per_m2,
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
  property_id
from listings;

-- Re-assert the posture explicitly rather than trusting CREATE OR REPLACE to
-- preserve the ACL. `anon` has been dark on this view since 299 and must stay so;
-- `authenticated` SELECT is what the SPA reads it with.
revoke all on listings_public from anon;
grant select on listings_public to authenticated;
