-- 423: `area_basis` — the provenance stamp for the polymorphic `area_m2`.
--
-- Measure sprint W1. The north star is "one measure, one definition, one label":
-- every per-m² figure must resolve from a single named measure that carries its
-- own numerator, denominator, unit and validity bounds, and no surface may render
-- the number without its basis label.
--
-- `listings.area_m2` is POLYMORPHIC and STAYS polymorphic: the interior area for
-- byt / dum / komercni, the PARCEL for pozemek. The measure resolves its basis
-- from (category_main, category_type) at read time; this column is the observed
-- provenance of what the writer actually put in `area_m2` on the last write —
-- which of the portal's labelled measures won (užitná / podlahová / celková), or
-- that nothing was labelled at all.
--
-- It NEVER changes `area_m2`'s value. NULLing land's area was considered and
-- rejected: bazos writes no estate_area at all, so for land that would be
-- deletion, and `area_m2` sits in the ScrapedListing content hash, so the rewrite
-- would churn ~24.8k snapshots for a non-event (architectural rule 2). For land
-- the stamp is 'plot' and the value is left exactly as it was.
--
-- The stamp itself is out of every content hash, so this column being populated
-- (now, or by a later backfill) appends not one snapshot row.
--
-- `properties` gets the same column so the multi-portal parent can carry the
-- survivor's basis without a second migration when the rollup starts writing it.

alter table listings add column if not exists area_basis text;
alter table properties add column if not exists area_basis text;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'listings_area_basis_check'
  ) then
    alter table listings add constraint listings_area_basis_check
      check (area_basis is null or area_basis in ('usable','floor','total','plot','unknown'));
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'properties_area_basis_check'
  ) then
    alter table properties add constraint properties_area_basis_check
      check (area_basis is null or area_basis in ('usable','floor','total','plot','unknown'));
  end if;
end $$;

comment on column listings.area_basis is
  'Which physical area listings.area_m2 holds, as observed by the parser that '
  'wrote it: usable (užitná) | floor (podlahová) | total (celková) | plot '
  '(pozemek — area_m2 IS the parcel) | unknown (scraped from free text, no '
  'label). Provenance only: it never changes area_m2 and it is out of every '
  'content hash. Written by scraper.area.derive_headline_area.';

comment on column properties.area_basis is
  'Basis of the property-grain area rollup; same vocabulary as '
  'listings.area_basis.';

-- The single most dangerous column in any per-m² arithmetic, documented in place
-- so the next reader cannot repeat the mistake.
comment on column listings.price_unit is
  'The portal''s own price-period label, kept verbatim for provenance. Four live '
  'spellings for TWO concepts: ''za nemovitost''/''celkem'' = a total, '
  '''za mesic''/''měsíc'' = monthly rent. It therefore duplicates category_type '
  '(prodej vs pronajem), which is the authoritative field. It is NOT an area '
  'unit and MUST NEVER be used to decide a per-m² basis — a per-area price has '
  'no representation in price_czk at all (the parsers refuse such a cell and '
  'write NULL), so a value here can never mean "per m²". Use area_basis for the '
  'denominator and category_type for the numerator''s period.';
