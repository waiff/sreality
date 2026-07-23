-- 358_broker_listings_public_listing_id.sql
--
-- Gate 2 flip-readiness (wave-5 audit): broker_listings_public exposed only
-- l.sreality_id, which is NULL for a post-Gate-2 (non-sreality) listing.
-- BrokerDetail.tsx used it as both the React list key (NULL key collisions
-- across every such row) and the listing-detail link (/listing/null). Add
-- the surrogate `l.id` as a trailing column (create or replace requires
-- additions at the end, same order otherwise — see migration 224) so the
-- frontend has an always-present key and can route through listingRowPath's
-- sreality_id-or-property fallback instead.
--
-- No re-GRANT here (Amendment A6, tests/test_migration_rls_grants.py):
-- CREATE OR REPLACE VIEW preserves the existing ACL on the same OID, so
-- migration 224's `grant select ... to anon` already covers this column
-- addition — restating it would trip the no-new-broker-regrant CI gate.

create or replace view broker_listings_public as
select
  bi.broker_id,
  l.sreality_id,
  l.source,
  l.source_url,
  l.locality,
  l.district,
  l.category_main,
  l.category_type,
  l.disposition,
  l.area_m2,
  l.price_czk,
  l.is_active,
  l.last_seen_at,
  l.property_id,
  l.subtype,
  l.id as listing_id
from listings l
join broker_identities bi on bi.id = l.broker_identity_id
where bi.broker_id is not null;
