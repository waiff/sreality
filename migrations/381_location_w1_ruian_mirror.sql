-- 381_location_w1_ruian_mirror.sql
--
-- Location-data program, Wave W1, PR-A (2 of 5): the RUIAN mirror - the
-- identity spine (D4).
--
-- Design: design/final/01-schema.md section 3 (3.1 registry_versions +
-- registry_load_discrepancies, 3.2 ruian_admin_units + relations, 3.3
-- ruian_admin_unit_geometries, 3.4 streets/building objects/parcels, 3.5
-- ruian_address_points + change log, 3.6 ruian_name_index).
--
-- Storage classes (01 section 0.2), stated per table below:
--   REGISTRY-VERSIONED     - low cardinality, every version held inline,
--                            UNIQUE (code, valid_from).
--   REGISTRY-CURRENT + LOG - ruian_address_points only: one row per Kod ADM
--                            plus an append-only change log. Versioning 3.02 M
--                            address points monthly would cost ~36 M rows/year
--                            while the measured daily delta is 0.1-1.8 MB.
--   APPEND-ONLY            - registry_versions, registry_load_discrepancies,
--                            ruian_address_point_changes.
--   REBUILDABLE            - ruian_name_index (derived from the tables above).
--
-- Nothing here is loaded by this migration; the CSV/SHP/VFR loaders are Section
-- 04's and land in a later W1 PR.
--
-- Backend/service-role only: RLS on, Supabase's default anon/authenticated ACL
-- revoked at the foot of the file.

begin;

------------------------------------------------------------------
-- ruian_name_norm - the ONE normalization definition.
--
-- 01 sections 3.2/3.4/3.6 declare name_norm columns as "written by
-- ruian_name_norm(name)" without giving a body; declared here so there is a
-- single definition rather than a per-loader reimplementation (01 section 0.4,
-- rule 2 - unaccent() is STABLE, not IMMUTABLE, so a normalized key can never
-- be a generated column). Body is [PROPOSED]: lower + unaccent + punctuation
-- folded to single spaces, which is exactly what 01 section 3.6 describes.
-- The loader lane owns any refinement.
------------------------------------------------------------------

create function ruian_name_norm(p_name text) returns text
language sql stable as $fn$
  select case
           when p_name is null then null
           else nullif(btrim(regexp_replace(lower(unaccent(p_name)), '[^a-z0-9]+', ' ', 'g')), '')
         end
$fn$;

------------------------------------------------------------------
-- registry_versions - APPEND-ONLY. Every resolution binds to one (D4).
--
-- ONE registry_version per LOAD EVENT, composed of several artefacts: the
-- baseline is two distinct CUZK products (OB_ADR_csv and strukt_ADR) with a
-- measured ~8h21m generation skew, so etag / bytes / sha256 / last-modified are
-- per-file jsonb maps and a Kod ADM present in one but absent in the other
-- becomes a registry_load_discrepancies row rather than a silent preference.
--
-- The chain columns are not decoration: the daily VFR change file carries
-- <vf:PredchoziSoubor> and monotonic TransakceOd/Do ids, so the mirror can
-- PROVE it has missed no day. VFR retention is ~90 days - past ~85 days behind,
-- a re-baseline from a monthly full is mandatory.
--
-- proj_version / proj_pipeline are recorded because PostGIS picks the ~1 m
-- Helmert path implicitly from the installed PROJ, so a PROJ upgrade can move
-- every coordinate.
------------------------------------------------------------------

create table registry_versions (
  id                     bigserial primary key,
  label                  text not null unique,
  kind                   text not null check (kind in ('baseline', 'delta')),
  source                 text not null default 'vdp',
  source_date            date not null,
  artifact_urls          jsonb not null,
  artifact_bytes         jsonb not null default '{}',
  artifact_sha256        jsonb not null default '{}',
  artifact_etag          jsonb not null default '{}',
  artifact_last_modified jsonb not null default '{}',
  proj_version           text not null,
  proj_pipeline          text not null,
  vfr_transaction_from   bigint,
  vfr_transaction_to     bigint,
  vfr_previous_file      text,
  parent_version_id      bigint references registry_versions(id),
  row_counts             jsonb not null default '{}',
  loaded_at              timestamptz not null default now(),
  is_current             boolean not null default false
);
alter table registry_versions enable row level security;

-- Exactly one current version, enforced by index (01 section 3.1).
create unique index registry_versions_one_current
  on registry_versions ((is_current)) where is_current;

-- APPEND-ONLY. There is no separate registry_load_failure table (00 section
-- 12.2): an aborted load is a row here with discrepancy='load_aborted', which
-- is what gives Section 04's consecutive_failed_baselines page a data source.
create table registry_load_discrepancies (
  registry_version_id bigint not null references registry_versions(id),
  entity_kind         ruian_level not null,
  entity_code         bigint not null,
  discrepancy         text not null,
  detail              jsonb,
  primary key (registry_version_id, entity_kind, entity_code, discrepancy)
);
alter table registry_load_discrepancies enable row level security;

------------------------------------------------------------------
-- ruian_admin_units - REGISTRY-VERSIONED. Adjacency list is the truth, the
-- ltree path a trigger-derived index.
--
-- ltree labels are LEVEL-PREFIXED NUMERIC CODES, never names or slugs
-- (01 section 3.2.1): ltree admits only [A-Za-z0-9_], so a label built from
-- "Krasny Les" or "Brno-stred" raises a syntax error at insert and
-- `admin_path <@ :path` - the primary filter index in this section AND in the
-- serving layer - would be undefined. Shape:
--   k{kraj_kod}.o{okres_kod}.b{obec_kod}.c{cast_obce_kod}
-- with .p{orp_kod}.u{pou_kod}.m{momc_kod} extending in that fixed order.
-- The human-readable rendering lives in display_path.
------------------------------------------------------------------

create table ruian_admin_units (
  id               bigserial primary key,
  level            ruian_level not null,
  code             bigint not null,
  name             text not null,
  name_norm        text not null,
  parent_id        bigint references ruian_admin_units(id),
  path             ltree not null,
  display_path     text not null,
  nuts_lau         text,
  definition_point geometry(Point, 4326),
  has_polygon      boolean not null default false,
  valid_from       date not null,
  valid_to         date,
  first_version_id bigint not null references registry_versions(id),
  last_version_id  bigint not null references registry_versions(id),
  retired_at       timestamptz,
  unique (level, code, valid_from)
);
alter table ruian_admin_units enable row level security;

create index ruian_admin_units_path_gist on ruian_admin_units using gist (path);
create index ruian_admin_units_parent    on ruian_admin_units (parent_id);
create index ruian_admin_units_code      on ruian_admin_units (level, code);
create index ruian_admin_units_name_trgm on ruian_admin_units using gin (name_norm gin_trgm_ops);
create index ruian_admin_units_defpoint  on ruian_admin_units using gist (definition_point)
  where definition_point is not null;

-- Non-tree edges: ORP does not nest cleanly under okres in all cases and a
-- katastralni uzemi can straddle obec boundaries (01 OQ6, unverified - the
-- loader must measure coverage_fraction on first import and fail loudly if the
-- tree assumption breaks).
create table ruian_admin_unit_relations (
  from_id             bigint not null references ruian_admin_units(id),
  to_id               bigint not null references ruian_admin_units(id),
  relation_type       text not null,
  coverage_fraction   numeric check (coverage_fraction between 0 and 1),
  registry_version_id bigint not null references registry_versions(id),
  primary key (from_id, to_id, relation_type, registry_version_id)
);
alter table ruian_admin_unit_relations enable row level security;

------------------------------------------------------------------
-- ruian_admin_unit_geometries - REGISTRY-VERSIONED. Three purposes: exactly one
-- 'authoritative' (unsimplified, the containment authority) and one 'render' row
-- per (unit, version), plus MANY 'pip' rows per unit - one per ST_Subdivide piece
-- of the authoritative geometry (04 C4.3; the partial unique below encodes this).
-- A simplified polygon is NEVER the containment authority.
--
-- representative_point and containment_radius_m are A PAIR (01 section 3.3.1):
-- an admin_centroid position uses the inscribed-circle CENTRE (always inside
-- the polygon) with the max centre-to-boundary distance. inscribed_radius_m is
-- kept for diagnostics only and must NEVER feed uncertainty_radius_m - for an
-- elongated obec it is far smaller than the true bound, which would let a
-- town-centroid row pass a certain-containment test and be asserted co-located
-- with an address point kilometres away.
------------------------------------------------------------------

create table ruian_admin_unit_geometries (
  id                         bigserial primary key,
  unit_id                    bigint not null references ruian_admin_units(id),
  registry_version_id        bigint not null references registry_versions(id),
  purpose                    text not null check (purpose in ('authoritative', 'pip', 'render')),
  generalization_tolerance_m numeric not null,
  simplify_algorithm         text,
  geom                       geometry(MultiPolygon, 4326) not null,
  area_m2                    double precision,
  representative_point       geometry(Point, 4326) not null,
  inscribed_radius_m         double precision not null,
  centroid_point             geometry(Point, 4326) not null,
  containment_radius_m       double precision not null,
  max_radius_m               double precision
);
alter table ruian_admin_unit_geometries enable row level security;

-- 'pip' is deliberately outside the unique: one row per subdivided piece.
create unique index ruian_aug_unique_nonpip on ruian_admin_unit_geometries
  (unit_id, registry_version_id, purpose) where purpose <> 'pip';

create index ruian_aug_geom_gist on ruian_admin_unit_geometries using gist (geom);
create index ruian_aug_pip_gist  on ruian_admin_unit_geometries using gist (geom)
  where purpose = 'pip';
create index ruian_aug_auth      on ruian_admin_unit_geometries (unit_id)
  where purpose = 'authoritative';

------------------------------------------------------------------
-- ruian_streets / ruian_building_objects / ruian_parcels - REGISTRY-VERSIONED.
--
-- stavebni objekt is the grouping key that matters most for a flat-heavy
-- aggregator: N flats share one building but have different (or no) address
-- points. Parcels are needed because the corpus produces parcel-grade claims
-- (cadastral territory name, parcel number, ORP) that have nowhere to land
-- today.
------------------------------------------------------------------

create table ruian_streets (
  id               bigserial primary key,
  code             bigint not null,
  name             text not null,
  name_norm        text not null,
  obec_unit_id     bigint not null references ruian_admin_units(id),
  first_version_id bigint not null references registry_versions(id),
  last_version_id  bigint not null references registry_versions(id),
  valid_from       date not null,
  valid_to         date,
  unique (code, valid_from)
);
alter table ruian_streets enable row level security;

create index ruian_streets_obec_norm on ruian_streets (obec_unit_id, name_norm);
create index ruian_streets_norm_trgm on ruian_streets using gin (name_norm gin_trgm_ops);

create table ruian_building_objects (
  id                bigserial primary key,
  code              bigint not null,
  obec_unit_id      bigint references ruian_admin_units(id),
  cast_obce_unit_id bigint references ruian_admin_units(id),
  typ_so            text,
  definition_point  geometry(Point, 4326),
  footprint         geometry(MultiPolygon, 4326),
  dwelling_count    integer,
  valid_from        date not null,
  valid_to          date,
  first_version_id  bigint not null references registry_versions(id),
  last_version_id   bigint not null references registry_versions(id),
  unique (code, valid_from)
);
alter table ruian_building_objects enable row level security;

create index ruian_bo_point_gist on ruian_building_objects using gist (definition_point);
create index ruian_bo_fp_gist    on ruian_building_objects using gist (footprint)
  where footprint is not null;

create table ruian_parcels (
  id                bigserial primary key,
  code              bigint not null,
  katuz_unit_id     bigint not null references ruian_admin_units(id),
  parcel_label      text not null,
  parcel_label_norm text not null,
  druh_pozemku      text,
  definition_point  geometry(Point, 4326),
  boundary          geometry(MultiPolygon, 4326),
  valid_from        date not null,
  valid_to          date,
  first_version_id  bigint not null references registry_versions(id),
  last_version_id   bigint not null references registry_versions(id),
  unique (code, valid_from)
);
alter table ruian_parcels enable row level security;

create index ruian_parcels_lookup on ruian_parcels (katuz_unit_id, parcel_label_norm);
create index ruian_parcels_geom   on ruian_parcels using gist (boundary) where boundary is not null;

------------------------------------------------------------------
-- ruian_address_points - REGISTRY-CURRENT (one row per Kod ADM), full width.
--
-- Two traps are encoded as constraints:
--   1. The SIGN trap. The CSV publishes POSITIVE Krovak (Y, X) while the VFR
--      XML publishes NEGATIVE EPSG:5514. Negating then transforming puts the
--      reference point inside Prague Castle; feeding the raw positives as
--      EPSG:5513 lands it in GERMANY - a valid-looking European coordinate.
--      geom_5514 is the single audited conversion (generated, therefore
--      immutable and unbypassable) and ruian_ap_krovak_envelope makes a sign
--      error fail at write time. geom (4326) is LOADER-written because
--      ST_Transform is not IMMUTABLE.
--   2. Czech double numbering. cislo_domovni (cislo popisne / evidencni) and
--      cislo_orientacni are stored SEPARATELY, never merged, because portals
--      differ in which they publish and matching the wrong one produces
--      confident false merges.
--
-- Full width is not a luxury: PSC is 100 percent populated across 3,020,222
-- rows in OB_ADR_csv, so the CSV alone removes any need for a Czech Post
-- dataset. cast_obce_unit_id IS the membership relation for "is this listing in
-- Vinohrady?" - there is no CastObce polygon in RUIAN at all.
------------------------------------------------------------------

create table ruian_address_points (
  kod_adm              bigint primary key,
  obec_unit_id         bigint not null references ruian_admin_units(id),
  obec_kod             bigint not null,
  momc_unit_id         bigint references ruian_admin_units(id),
  praha_obvod_unit_id  bigint references ruian_admin_units(id),
  cast_obce_unit_id    bigint references ruian_admin_units(id),
  cast_obce_kod        bigint,
  street_id            bigint references ruian_streets(id),
  ulice_kod            bigint,
  stavebni_objekt_code bigint,
  typ_so               text,
  cislo_domovni        integer,
  cislo_orientacni     integer,
  znak_orientacniho    text,
  psc                  char(5) not null,
  krovak_y_positive    double precision,
  krovak_x_positive    double precision,
  geom_5514            geometry(Point, 5514)
    generated always as (
      case when krovak_y_positive is not null and krovak_x_positive is not null
           then ST_SetSRID(ST_MakePoint(-krovak_y_positive, -krovak_x_positive), 5514) end) stored,
  geom                 geometry(Point, 4326),
  plati_od             date not null,
  valid_to             date,
  first_version_id     bigint not null references registry_versions(id),
  last_version_id      bigint not null references registry_versions(id),
  house_number_key     text generated always as (
      coalesce(cislo_domovni::text, '') ||
      case when cislo_orientacni is not null
           then '/' || cislo_orientacni::text || coalesce(znak_orientacniho, '') else '' end) stored,
  constraint ruian_ap_krovak_envelope check (
      krovak_y_positive is null or
      (krovak_y_positive between 400000 and 950000 and krovak_x_positive between 900000 and 1250000))
);
alter table ruian_address_points enable row level security;

create index ruian_ap_geom_gist on ruian_address_points using gist (geom);
create index ruian_ap_street_hn on ruian_address_points (street_id, cislo_domovni, cislo_orientacni);
create index ruian_ap_obec_hn   on ruian_address_points (obec_unit_id, cislo_domovni);
create index ruian_ap_psc       on ruian_address_points (psc);
create index ruian_ap_cast_obce on ruian_address_points (cast_obce_unit_id)
  where cast_obce_unit_id is not null;
create index ruian_ap_so        on ruian_address_points (stavebni_objekt_code)
  where stavebni_objekt_code is not null;

-- The bitemporal half: only CHANGED rows, one entry per (kod_adm,
-- registry_version). Reverse-replaying this over the current table reconstructs
-- any prior vintage, which is what keeps "re-run last March's resolution
-- against ruian:2026-03" a real operation.
create table ruian_address_point_changes (
  kod_adm             bigint not null,
  registry_version_id bigint not null references registry_versions(id),
  change_kind         text not null check (change_kind in ('insert', 'update', 'retire', 'reinstate')),
  changed_fields      text[] not null default '{}',
  before_row          jsonb,
  after_row           jsonb,
  observed_at         timestamptz not null default now(),
  primary key (kod_adm, registry_version_id)
);
alter table ruian_address_point_changes enable row level security;

create index ruian_apc_version on ruian_address_point_changes (registry_version_id, change_kind);

------------------------------------------------------------------
-- ruian_name_index - REBUILDABLE. The in-house typo-tolerant gazetteer (D6).
--
-- There is no verified official fuzzy geocoder for Czech addresses: CUZK's
-- attribute search will not tolerate misspellings and its GeocodeSOE batch
-- endpoint is 403. Matching is our problem.
--
-- homonym_count and qualifier are the schema encoding of the two regression
-- cases D6 names: "Krasny Les u Frydlantu" vs the other Krasny Les ~100 km
-- away, and the de-accented "Bilovec" that fuzzy-matched a street in western
-- Slovakia. name_kind='deaccented' is first-class because ceskereality stores
-- streets ~98 percent de-accented, so any exact-string join across sources
-- silently fails on them.
------------------------------------------------------------------

create table ruian_name_index (
  id                   bigserial primary key,
  entity_kind          ruian_level not null,
  entity_id            bigint not null,
  registry_version_id  bigint not null references registry_versions(id),
  name                 text not null,
  name_norm            text not null,
  name_kind            text not null check (name_kind in
                         ('official', 'deaccented', 'qualifier_stripped', 'alias',
                          'historical', 'portal_alias')),
  qualifier            text,
  parent_obec_unit_id  bigint references ruian_admin_units(id),
  parent_okres_unit_id bigint references ruian_admin_units(id),
  psc_set              char(5)[],
  homonym_count        integer not null default 1,
  unique (entity_kind, entity_id, name_norm, name_kind, registry_version_id)
);
alter table ruian_name_index enable row level security;

create index ruian_name_norm_trgm on ruian_name_index using gin (name_norm gin_trgm_ops);
create index ruian_name_homonym   on ruian_name_index (name_norm) where homonym_count > 1;

------------------------------------------------------------------
-- Backend/service-role only. Supabase default privileges auto-GRANT
-- anon/authenticated on new tables, SEQUENCES and FUNCTIONS; revoke each
-- explicitly (a `revoke ... from public` alone is a no-op against an explicit
-- ACL entry).
------------------------------------------------------------------

revoke all on registry_versions            from anon, authenticated;
revoke all on registry_load_discrepancies  from anon, authenticated;
revoke all on ruian_admin_units            from anon, authenticated;
revoke all on ruian_admin_unit_relations   from anon, authenticated;
revoke all on ruian_admin_unit_geometries  from anon, authenticated;
revoke all on ruian_streets                from anon, authenticated;
revoke all on ruian_building_objects       from anon, authenticated;
revoke all on ruian_parcels                from anon, authenticated;
revoke all on ruian_address_points         from anon, authenticated;
revoke all on ruian_address_point_changes  from anon, authenticated;
revoke all on ruian_name_index             from anon, authenticated;

revoke all on sequence registry_versions_id_seq           from anon, authenticated;
revoke all on sequence ruian_admin_units_id_seq           from anon, authenticated;
revoke all on sequence ruian_admin_unit_geometries_id_seq from anon, authenticated;
revoke all on sequence ruian_streets_id_seq               from anon, authenticated;
revoke all on sequence ruian_building_objects_id_seq      from anon, authenticated;
revoke all on sequence ruian_parcels_id_seq               from anon, authenticated;
revoke all on sequence ruian_name_index_id_seq            from anon, authenticated;

revoke execute on function ruian_name_norm(text) from public, anon, authenticated;

commit;
