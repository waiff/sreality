-- 091_properties_foundation.sql
-- Slice 0 of the multi-portal dedup track (docs/design/multi-portal-dedup.md).
--
-- Adds the thin canonical `properties` parent over the existing per-source
-- `listings` table, plus the listings-side link + multi-source identity
-- columns. Purely additive: no existing column is altered, the listings
-- primary key (sreality_id) is untouched, and every new listings column is
-- nullable or defaulted so the production scraper -- which runs the OLD code
-- on `main` until the property-linking wrapper merges -- keeps inserting.
--
-- Operator decisions folded in (sign-off 2026-05-25):
--   * Derived filter aggregates live as COLUMNS on `properties` (not a
--     separate property_stats table); an async recompute job maintains them.
--   * The is_active rollup over children is the async job's responsibility,
--     not eager scraper logic.
--   * Each portal's native id is stored verbatim in listings.source_id_native;
--     `properties` gets its own surrogate id ("our own numbering"). For
--     sreality, source_id_native == sreality_id::text. The synthetic-id
--     scheme for non-sreality portals is deferred to Slice 3 (first non-
--     sreality scraper) -- no such rows exist today, so it is moot here.

create table properties (
  id                   bigserial primary key,

  -- Representative display columns, mirrored from the repr child listing.
  -- For a singleton property this is simply that one listing's data.
  repr_listing_id      bigint references listings(sreality_id) on delete set null,
  category_main        text,
  category_type        text,
  disposition          text,
  area_m2              numeric,
  district             text,
  geom                 geography(point, 4326),
  current_price_czk    integer,

  -- Lifecycle rollup over children. Singleton today; the async job
  -- recomputes bool_or(children.is_active) / min/max seen once a property
  -- has multiple children (Slice 3+).
  is_active            boolean     not null default true,
  first_seen_at        timestamptz not null default now(),
  last_seen_at         timestamptz not null default now(),

  -- Derived filter aggregates, maintained by the async recompute job (Slice 1).
  -- Defaults describe the singleton state every backfilled property starts in.
  source_count         integer     not null default 1,
  distinct_site_count  integer     not null default 1,
  price_drop_count     integer     not null default 0,
  price_rise_count     integer     not null default 0,
  max_price_drop_pct   numeric,
  stats_computed_at    timestamptz,

  created_at           timestamptz not null default now()
);

create index properties_geom_idx     on properties using gist (geom);
create index properties_active_idx   on properties (is_active);
create index properties_category_idx on properties (category_main, category_type);

alter table properties enable row level security;
-- No anon policy in Slice 0. The read surface (properties_public) lands in
-- migration 096 (Slice 1); the service-role scraper bypasses RLS to write.

-- listings-side link + multi-source identity. All nullable/defaulted so an
-- old-code INSERT (no property_id, no source_id_native) still succeeds until
-- the wrapper is live on main.
alter table listings
  add column property_id      bigint references properties(id),
  add column source           text not null default 'sreality',
  add column source_url       text,
  add column source_id_native text;

comment on column listings.source_id_native is
  'Native listing id from the source portal, stored verbatim. For sreality this equals sreality_id::text. The synthetic-id scheme for other portals is decided in Slice 3.';

-- Backfill the native id for every existing (sreality) row before building
-- the uniqueness guard.
update listings set source_id_native = sreality_id::text where source_id_native is null;

create index listings_property_id_idx on listings (property_id);

-- The real per-source identity. NULLs are distinct in a unique index, so
-- new old-code rows (source_id_native IS NULL) never collide until the
-- wrapper backfills them.
create unique index listings_source_native_uidx on listings (source, source_id_native);
