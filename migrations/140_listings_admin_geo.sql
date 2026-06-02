-- 140_listings_admin_geo.sql
--
-- Normalized Czech administrative hierarchy (obec / okres / kraj), derived from
-- each listing's COORDINATE rather than its free-text address.
--
-- Why geom and not the address: ~95% of listings carry a precise per-listing
-- point straight from the portal's own map/GPS data (sreality + bezrealitky API
-- `gps`, idnes/mmreality embedded map centre, remax `data-gps`; only bazos
-- geocodes). The textual `locality` is portal-specific and inconsistent
-- (street+city+quarter for some, city-only for others, malformed or NULL for
-- bazos), so it is unusable for grouping. The point is the trustworthy anchor:
-- a single PIP into admin_boundaries + a parent_id walk yields obec -> okres ->
-- kraj for EVERY source uniformly.
--
-- Two new normalized columns + one display fill:
--   listings.obec    - municipality name                         [structured]
--   listings.okres   - administrative district (okres) name      [structured]
--   listings.region  - region (kraj) name                        [structured]
-- and the display `district` text is filled from okres (or obec for Prague)
-- ONLY when district is NULL, so sreality's richer "City - Quarter" labels are
-- preserved while portals/bazos (which never had a district) gain one.
--
-- Done INSTANTLY at write time via a BEFORE trigger (not a periodic recompute),
-- because new listings must show their district/region on the Browse card the
-- moment they are scraped. The single indexed PIP (admin_boundaries_geom_idx +
-- the okres / obec sets) is cheap; the trigger fires only on INSERT or when geom
-- itself changes, and early-returns on an unchanged, already-resolved point, so
-- re-detail-fetches don't re-PIP. Nothing on the runtime read path exact-matches
-- `district` (Browse uses ILIKE), so filling it can't break filters/stats.

alter table listings
  add column if not exists obec   text,
  add column if not exists okres  text,
  add column if not exists region text;

comment on column listings.obec is
  'Municipality (obec) name, derived from geom via admin_boundaries PIP. Source-agnostic.';
comment on column listings.okres is
  'Administrative district (okres) name, derived from geom. The Czech "district". Source-agnostic.';
comment on column listings.region is
  'Region (kraj) name, derived from geom. Source-agnostic.';


create or replace function public.listings_set_admin_geo()
returns trigger
language plpgsql
as $function$
declare
  v_obec  text;
  v_okres text;
  v_kraj  text;
begin
  if new.geom is null then
    return new;
  end if;

  -- Cheap path: an unchanged point that is already resolved (a re-detail-fetch
  -- writes geom = EXCLUDED.geom on every pass) needs no PIP.
  if tg_op = 'UPDATE'
     and new.geom is not distinct from old.geom
     and new.okres is not null then
    return new;
  end if;

  -- PIP to the containing obec (obec polygons tile the country), then walk the
  -- parent chain to okres + kraj. Same st_covers(admin.geom, listing.geom)
  -- orientation as recompute_mf_gross_yields(), which uses this index.
  select ob.name, ok.name, kr.name
    into v_obec, v_okres, v_kraj
  from admin_boundaries ob
  left join admin_boundaries ok on ok.id = ob.parent_id and ok.level = 'okres'
  left join admin_boundaries kr on kr.id = ok.parent_id and kr.level = 'kraj'
  where ob.level = 'obec'
    and st_covers(ob.geom, new.geom)
  limit 1;

  new.obec   := v_obec;
  new.okres  := v_okres;
  new.region := v_kraj;

  -- Display `district`: fill only when missing (preserve sreality labels).
  -- Prague's okres name ("území Hlavního města Prahy") is unusable as a label,
  -- so the capital falls back to its obec ("Praha") -- mirroring how sreality
  -- itself labels Prague by city, not okres.
  if new.district is null then
    if v_kraj = 'Hlavní město Praha' then
      new.district := v_obec;
    elsif v_okres is not null then
      new.district := 'okres ' || v_okres;
    end if;
  end if;

  return new;
end;
$function$;


drop trigger if exists trg_listings_admin_geo on listings;
create trigger trg_listings_admin_geo
  before insert or update of geom on listings
  for each row
  when (new.geom is not null)
  execute function public.listings_set_admin_geo();

-- One-time backfill of existing rows is run out-of-band (set-based, batched by
-- id range to stay under the statement timeout) right after this migration is
-- applied; the trigger maintains every write from here on. Points outside CZ
-- (foreign listings) match no obec polygon and correctly stay NULL.
