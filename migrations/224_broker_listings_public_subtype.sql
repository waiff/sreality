-- 224_broker_listings_public_subtype.sql
--
-- Expose listings.subtype (migration 152) on broker_listings_public so the
-- broker inventory table can render the portal-agnostic kind (Kancelář,
-- Ubytování, …) instead of a bare disposition for commercial/house rows.
-- Reproduced verbatim from migration 189; the ONLY change is `l.subtype`
-- appended as the trailing column (create or replace requires additions at
-- the end, same order otherwise).

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
  l.subtype
from listings l
join broker_identities bi on bi.id = l.broker_identity_id
where bi.broker_id is not null;

grant select on broker_listings_public to anon;
