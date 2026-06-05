-- 169_area_basis.sql
-- Make area_m2 a basis-aware, interior-only headline.
--
-- area_m2 was overloaded: a different physical measurement per portal (usable
-- on sreality/bezrealitky, total on mmreality, a usable->floor->total->title
-- fallback on idnes/maxima/remax, free-text regex on bazos) AND, for land
-- (pozemek), it held the PLOT size on idnes/maxima/remax — so the "Area" filter
-- silently compared apartment interiors against parcels. The parsers now derive
-- area_m2 through scraper.area.derive_headline_area (one usable->floor->total
-- precedence, NULL for pozemek) and record which measure it is in area_basis.
--
-- This migration adds the column and backfills existing rows. Backfill is
-- correctness-first; the precise per-row basis converges as the detail-drain
-- re-derives each listing on its next fetch.

alter table listings add column if not exists area_basis text;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'listings_area_basis_check'
  ) then
    alter table listings
      add constraint listings_area_basis_check
      check (area_basis is null or area_basis in ('usable','floor','total','unknown'));
  end if;
end $$;

-- 1. Land has no dwelling interior: clear the plot value that leaked into area_m2
--    (the plot stays in estate_area). This is the immediate correctness fix.
update listings
set area_m2 = null, area_basis = null
where category_main = 'pozemek'
  and (area_m2 is not null or area_basis is not null);

-- 2. Where a strict usable area exists, that is the headline (precedence picks it
--    first), so the basis is 'usable'. Covers the dominant case across portals.
update listings
set area_basis = 'usable'
where area_basis is null
  and category_main is distinct from 'pozemek'
  and usable_area is not null;

-- 3. Bazos contributes one unlabelled free-text number -> basis 'unknown'.
update listings
set area_basis = 'unknown'
where area_basis is null
  and source = 'bazos'
  and area_m2 is not null;

-- Remaining NULL area_basis (e.g. maxima/idnes floor-basis rows) is left for the
-- next detail-drain to re-derive precisely.

-- Mirror the land fix onto the properties rollup so Browse / properties_public
-- stop catching land on the "Area" filter before the async recompute runs.
update properties
set area_m2 = null
where category_main = 'pozemek'
  and area_m2 is not null;
