-- 092_properties_backfill.sql
-- Slice 0 backfill: one singleton `properties` row per existing listing,
-- then link listings.property_id back to it. Data-only / additive, and
-- reversible before any dedup merge (truncate properties; null property_id).
--
-- property_id is intentionally LEFT NULLABLE here. The production scraper
-- runs OLD code on `main` until the property-linking wrapper merges; an
-- old-code INSERT supplies no property_id, so a NOT NULL constraint would
-- break live inserts the moment this migration applies. Nothing READS
-- property_id in Slice 0, so the transient gap (new old-code rows with
-- property_id IS NULL) is harmless and self-heals -- the wrapper attaches a
-- property on the next detail fetch, and the async recompute job (Slice 1)
-- backfills any stragglers. The NOT NULL constraint is tightened in a
-- follow-up migration once the wrapper is confirmed live on main.
--
-- Mirrors migration 089's reconciliation discipline: an assertion that every
-- listing ended up linked to exactly one property.

insert into properties (
  repr_listing_id, category_main, category_type, disposition,
  area_m2, district, geom, current_price_czk,
  is_active, first_seen_at, last_seen_at,
  source_count, distinct_site_count
)
select
  l.sreality_id, l.category_main, l.category_type, l.disposition,
  l.area_m2, l.district, l.geom, l.price_czk,
  l.is_active, l.first_seen_at, l.last_seen_at, 1, 1
from listings l
where l.property_id is null;

update listings l
set property_id = p.id
from properties p
where p.repr_listing_id = l.sreality_id
  and l.property_id is null;

do $$
declare
  n_listings   bigint;
  n_unlinked   bigint;
  n_properties bigint;
begin
  select count(*) into n_listings   from listings;
  select count(*) into n_unlinked   from listings where property_id is null;
  select count(*) into n_properties from properties;

  if n_unlinked <> 0 then
    raise exception 'properties backfill: % listings still unlinked', n_unlinked;
  end if;
  if n_properties <> n_listings then
    raise exception 'properties backfill: % properties != % listings',
      n_properties, n_listings;
  end if;

  raise notice 'properties backfill OK: % listings, % properties, 0 unlinked',
    n_listings, n_properties;
end $$;
