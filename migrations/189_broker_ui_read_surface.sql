-- 189_broker_ui_read_surface.sql
--
-- Broker intelligence, phase 3 (Brokers UI): two anon-readable views the SPA needs.
-- Purely additive. Owner-privileged views (not security_invoker) so anon reads
-- public-safe columns without a grant on the base listings table — same posture as
-- listings_public.

-- The region/okres options for the leaderboard picker: only geo units that actually
-- have brokers, with names (from admin_boundaries) + broker counts. parent_id lets
-- the UI filter okresy under a chosen kraj. Obec level is excluded — too granular for
-- a "who dominates" picker (the matview keeps obec rows for direct queries).
create view broker_geo_options as
select s.geo_level, s.geo_id, ab.name, ab.parent_id,
       count(distinct s.broker_id) as broker_count
from broker_region_type_stats s
join admin_boundaries ab on ab.id = s.geo_id
where s.geo_level in ('region', 'okres')
group by s.geo_level, s.geo_id, ab.name, ab.parent_id;
grant select on broker_geo_options to anon;

-- A broker's own listings, public-safe columns only, keyed by canonical broker_id.
-- Bridges broker_identity_id (base listings, not on listings_public) to the public
-- listing fields the broker-detail page renders + links to /listing/:id. Always
-- queried filtered by broker_id (indexed via broker_identities.broker_id), so the
-- per-broker read (<=~250 rows) stays well under the anon statement timeout.
create view broker_listings_public as
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
  l.property_id
from listings l
join broker_identities bi on bi.id = l.broker_identity_id
where bi.broker_id is not null;
grant select on broker_listings_public to anon;
