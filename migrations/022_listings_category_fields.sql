-- 022_listings_category_fields.sql
-- Promote ten more `recommendations_data` fields from raw_json to typed
-- columns. Motivated by the six-category scrape expansion (PR #30): the
-- listings table was originally apartment-rental-shaped, so it lacks
-- columns that are first-class for houses (estate_area = lot/plot m²)
-- and commercial properties (category_sub_cb = office vs retail vs
-- warehouse, etc). Without these columns 4,000 commercial listings are
-- one undifferentiated bucket, and house comparables can't be filtered
-- by lot size — both regressions of the recent scope expansion.
--
-- Boolean amenities `terrace`, `cellar`, `garage` were previously merged
-- into the existing `has_balcony` and `has_parking` columns inside the
-- parser (see scraper.parser._has_balcony / _has_parking). Splitting
-- them out as their own columns lets a downstream filter like
-- "garage required" actually be precise instead of the looser
-- "any parking, including a single street space". The legacy combined
-- columns stay untouched for backward compatibility.
--
-- `parking_lots` records the COUNT of parking spaces (an integer in
-- raw_json) — distinct from the existing `has_parking` boolean.
-- `furnished` and `ownership` are sreality enum codes; we store the
-- text label (no diacritics) per the project's existing convention
-- for `category_main` / `category_type`. Mapping:
--   furnished: 1=ano, 2=ne, 3=castecne, 0/missing=NULL
--   ownership: 1=osobni, 2=druzstevni, 3=statni, 0/missing=NULL
--
-- All columns nullable; a row with an older raw_json that doesn't have
-- the source field stays NULL until the next refetch.
--
-- Backfill from raw_json in this same migration so existing rows are
-- queryable immediately. Pattern mirrors migration 016. The CASE
-- statements for furnished / ownership are inlined here rather than
-- introducing a helper SQL function — they're three rows each and a
-- function would be overkill.
--
-- One partial index on category_sub_cb (the active-listings hot path
-- that 4k commercial listings will exercise once the UI surfaces a
-- subtype filter). Boolean amenities deliberately get no index —
-- their cardinality is too low for an index to beat a sequential scan
-- with a hash filter.

alter table listings
  add column estate_area      numeric(9,1),
  add column usable_area      numeric(9,1),
  add column garden_area      numeric(9,1),
  add column category_sub_cb  integer,
  add column furnished        text,
  add column terrace          boolean,
  add column cellar           boolean,
  add column garage           boolean,
  add column parking_lots     integer,
  add column ownership        text;

create index listings_category_sub_cb_active_idx
  on listings (category_sub_cb)
  where is_active = true;

update listings
set
  estate_area      = (raw_json -> 'recommendations_data' ->> 'estate_area')::numeric,
  usable_area      = (raw_json -> 'recommendations_data' ->> 'usable_area')::numeric,
  garden_area      = (raw_json -> 'recommendations_data' ->> 'garden_area')::numeric,
  category_sub_cb  = (raw_json -> 'recommendations_data' ->> 'category_sub_cb')::int,
  furnished        = case (raw_json -> 'recommendations_data' ->> 'furnished')::int
                       when 1 then 'ano'
                       when 2 then 'ne'
                       when 3 then 'castecne'
                       else null
                     end,
  terrace          = (raw_json -> 'recommendations_data' ->> 'terrace')::int::boolean,
  cellar           = (raw_json -> 'recommendations_data' ->> 'cellar')::int::boolean,
  garage           = (raw_json -> 'recommendations_data' ->> 'garage')::int::boolean,
  parking_lots     = (raw_json -> 'recommendations_data' ->> 'parking_lots')::int,
  ownership        = case (raw_json -> 'recommendations_data' ->> 'ownership')::int
                       when 1 then 'osobni'
                       when 2 then 'druzstevni'
                       when 3 then 'statni'
                       else null
                     end
where raw_json -> 'recommendations_data' is not null;
