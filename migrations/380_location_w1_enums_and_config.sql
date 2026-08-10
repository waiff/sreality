-- 380_location_w1_enums_and_config.sql
--
-- Location-data program, Wave W1, PR-A (1 of 5): extensions, every location
-- enum, and the four config/vocabulary tables that the rest of the subsystem
-- keys on.
--
-- Design: design/final/01-schema.md sections 0.5 (extensions), 2 (the shared
-- enums + location_granularity_rank), 2.1 (location_level_granularity), 2.2
-- (the ONE canonical CZ bbox in location_constants) and 4.1 (the claim
-- vocabularies + location_claim_type_meta). design/final/00-shared-contracts.md
-- is the tie-breaker and agrees with 01 on every label set below.
--
-- Two rules from 01 section 0.4 govern everything that follows and are restated
-- here because they are invisible in the DDL itself:
--   * location_granularity, match_confidence and the other ordinal enums may be
--     compared ordinally in a QUERY, never in an index predicate, a CHECK or a
--     stored generated column. Postgres does not re-evaluate either when an enum
--     gains a value, so a new rung would silently invalidate them. Persisted
--     comparisons go through location_granularity_rank.rank instead.
--   * Adding a location_granularity value requires, in the same PR, a new
--     location_granularity_rank row plus the projection rewrite/REINDEX
--     checklist of 01 section 0.4.
--
-- This is a backend/service-role-only subsystem. This Supabase project's default
-- privileges auto-GRANT anon/authenticated on new tables, sequences and
-- functions, so every object created here is RLS-enabled and explicitly revoked
-- at the foot of the file.

begin;

------------------------------------------------------------------
-- Extensions (01 section 0.5).
--
-- postgis is already installed. ltree carries the admin code path, pg_trgm the
-- typo-tolerant gazetteer, unaccent the normalization functions. Installed
-- unqualified, matching migration 001's `create extension if not exists
-- postgis;` precedent, so `ltree`, `gin_trgm_ops` and `unaccent()` resolve
-- without a schema qualifier in the DDL below.
--
-- h3-pg is NOT available on this instance; 01 section 0.5 / OQ1 anticipates
-- that and the fallback ships instead: the rounded spatial cell key
-- (location_geo_cell_key, migration 384) with a MANDATORY 3x3 neighbourhood
-- expansion at query time. listing_location_current.h3_r10 stays a nullable,
-- unused, additive upgrade slot.
------------------------------------------------------------------

create extension if not exists ltree;
create extension if not exists pg_trgm;
create extension if not exists unaccent;

------------------------------------------------------------------
-- D3 axis 1: granularity. ORDINAL - declared coarse to fine so
-- `granularity >= 'street'` is a legal QUERY predicate (01 section 2).
------------------------------------------------------------------

create type location_granularity as enum (
  'unknown',
  'country',
  'kraj',
  'okres',
  'obec',
  'cast_obce_or_quarter',
  'street',
  'street_segment',
  'parcel',
  'building',
  'address_point'
);

-- D3 axis 2: what the coordinate physically IS.
create type position_source as enum (
  'none',
  'admin_centroid',
  'derived_geocode',
  'carried_forward',
  'portal_pin_blurred',
  'portal_pin',
  'registry_point'
);

-- Blur PROVENANCE is a SEPARATE axis from the coordinate's identity: a portal
-- that DECLARES its pin approximate and a pin we DETECTED as collapsed are
-- different facts. The declared flags are the only evaluation set available for
-- calibrating the collision detector (00 section 1.3).
create type blur_evidence as enum ('none', 'declared', 'detected', 'both');

-- D3 axis 3: how well the text matched. Ambiguity is NOT a confidence value -
-- it is a resolution_status.
create type match_confidence as enum ('low', 'medium', 'high', 'exact');

-- D3 axis 4 lives on the row as `uncertainty_radius_m numeric not null`, always
-- paired with its semantics. Mixing semantics in arithmetic is forbidden
-- (01 section 3.3.1): take the max, never the mean.
create type radius_semantics as enum ('r95_empirical', 'geometric_bound', 'declared');

create type resolution_status as enum (
  'resolved', 'ambiguous', 'unmatched', 'no_input', 'skipped_foreign'
);

-- D1: country determination is a first-class, confidence-bearing decision with
-- a status. 'disputed' flags and routes to arbitration; it never suppresses.
create type country_status as enum ('cz', 'foreign', 'disputed', 'undetermined');

create type country_determination_method as enum (
  'portal_field',
  'registry_containment',
  'portal_bucket',
  'text_claim',
  'classifier',
  'assumed_default',
  'unknown'
);

-- D5: how an admin level got onto a row. ONE enum, used verbatim by 01/03/05.
-- Note the spelling `pip_nearest_within_n_m` (00 section 4.2 retires the
-- capital-N form) and `claimed` (00 section 4.2 retires `claimed_text`).
create type admin_assignment_method as enum (
  'registry',
  'pip_containment',
  'pip_nearest_within_n_m',
  'unresolved_sliver',
  'outside_country',
  'claimed',
  'unresolved'
);

-- Licensing lineage (D6). SIX values, and this is the ONLY licence vocabulary
-- in the program: 00 section 6.2 retires portal_payload / portal_first_party /
-- first_party -> 'portal'; ruian / ruian_ccby -> 'cc_by_ruian'; osm /
-- portal_osm_derived -> 'odbl' (ODbL follows the geometry, not the republisher).
create type licence_class as enum (
  'portal',
  'cc_by_ruian',
  'odbl',
  'commercial_permanent',
  'ephemeral_display_only',
  'operator'
);

-- Registry admin levels (superset of D3 granularity; mapped by
-- location_level_granularity below).
create type ruian_level as enum (
  'stat', 'region_soudrznosti', 'kraj', 'okres', 'orp', 'pou', 'obec',
  'spravni_obvod', 'momc', 'cast_obce', 'katastralni_uzemi', 'zsj', 'ulice',
  'adresni_misto', 'stavebni_objekt', 'parcela'
);

------------------------------------------------------------------
-- The claim vocabularies (01 section 4.1). The claim_type spellings are
-- Section 02's, folded with 03/05/06's additions. Nothing else in the program
-- may declare a parallel vocabulary.
--
-- `blur_hint` is a DISTINCT claim type, not a flavour of precision_declaration
-- (00 section 2.2, normative): a blur hint is a binary presence signal carrying
-- no declared value (bazos "Priblizna lokalita"), whereas precision_declaration
-- carries a declared label into declared_precision_label.
------------------------------------------------------------------

create type location_claim_type as enum (
  -- geometry / precision
  'coordinate',
  'uncertainty_geometry',
  'precision_declaration',
  'blur_hint',
  'map_zoom',
  'geohash',
  'admin_polygon',
  -- identity keys
  'address_point_id',
  'building_id',
  'obec_code',
  'portal_admin_id',
  'portal_street_id',
  'osm_relation_id',
  'cadastral_territory_name',
  'cadastral_territory_code',
  'parcel_number',
  -- address components
  'street_name',
  'house_number_cp',
  'house_number_co',
  'evidencni',
  'house_unit',
  'psc',
  'postal_town',
  'obec_name',
  'cast_obce_name',
  'quarter_name',
  'mestsky_obvod_name',
  'okres_name',
  'orp_name',
  'kraj_name',
  'country',
  'homonym_qualifier',
  'address_line_verbatim',
  -- context / weak signals
  'development_name',
  'landmark',
  'relative_distance',
  'poi_distance',
  'micro_position',
  'neighbour_listing_ref',
  'foreign_indicator'
);

-- WHERE the claim came from (the artefact + addressing technique).
-- `portal_json` is deliberately NOT a member: it is ambiguous across the three
-- JSON-bearing surfaces and resolves to the specific one (sreality -> api_json,
-- bezrealitky -> graphql, mmreality -> embedded_json). 01 section A.2 check 4
-- forbids the literal anywhere in the codebase.
create type location_claim_surface as enum (
  'api_json', 'graphql', 'embedded_json', 'html_selector', 'map_config',
  'og_meta', 'jsonld', 'url_slug',
  'description', 'archived_html', 'legacy_column', 'registry', 'operator_input'
);

create type location_page_kind as enum (
  'index', 'detail', 'map', 'gazetteer', 'snapshot', 'archive', 'none'
);

-- `graphql` is a SURFACE, not an extraction method: a GraphQL field read is
-- surface='graphql' + extraction_method='portal_structured_field'.
create type location_extraction_method as enum (
  'portal_structured_field',
  'portal_declared_quality',
  'html_selector_parse',
  'url_slug_parse',
  'breadcrumb_parse',
  'jsonld_parse',
  'map_widget_parse',
  'regex_text',
  'llm_text',
  'legacy_column',
  'registry_derived',
  'operator_manual'
);

------------------------------------------------------------------
-- location_granularity_rank - MUTABLE (config), 11 rows.
--
-- The explicit smallint rank next to each enum label is what keeps arithmetic,
-- sorting and "insert a rung" data changes rather than schema rewrites, and it
-- is the ONLY legal way to persist a granularity comparison (01 section 0.4).
------------------------------------------------------------------

create table location_granularity_rank (
  granularity      location_granularity primary key,
  rank             smallint not null unique,
  is_address_grain boolean not null default false,
  note             text
);
alter table location_granularity_rank enable row level security;

insert into location_granularity_rank (granularity, rank, is_address_grain, note) values
  ('unknown',              0,   false, 'no usable location signal at all'),
  ('country',              10,  false, 'country only'),
  ('kraj',                 20,  false, 'region'),
  ('okres',                30,  false, 'district; also the slot for ORP and POU'),
  ('obec',                 40,  false, 'municipality'),
  ('cast_obce_or_quarter', 50,  false, 'part of municipality / quarter / MOMC / ZSJ / cadastral territory'),
  ('street',               60,  false, 'street, no segment or house number'),
  ('street_segment',       70,  false, 'lowest rung admitted to geometric dedup blocking (01 section 7.1.1)'),
  ('parcel',               80,  false, 'cadastral parcel'),
  ('building',             90,  true,  'stavebni objekt - the same-building group key'),
  ('address_point',        100, true,  'RUIAN Kod ADM - the CZ UPRN');

------------------------------------------------------------------
-- location_level_granularity - MUTABLE (config), 16 rows.
--
-- Level-to-granularity mapping is DATA, not code (01 section 2.1): ORP, POU,
-- MOMC, ZSJ and katastralni uzemi have no D3 slot of their own and each is
-- mapped to the nearest rung. 01 OQ5 keeps "does this need an extra rung?" open;
-- answering it is a data change here plus the section 0.4 checklist.
------------------------------------------------------------------

create table location_level_granularity (
  ruian_level ruian_level primary key,
  granularity location_granularity not null,
  note        text
);
alter table location_level_granularity enable row level security;

insert into location_level_granularity (ruian_level, granularity, note) values
  ('stat',               'country',              'CZ state polygon'),
  ('region_soudrznosti', 'kraj',                 'NUTS2 cohesion region - groups kraje; nearest rung is kraj'),
  ('kraj',               'kraj',                 null),
  ('okres',              'okres',                null),
  ('orp',                'okres',                '01 section 2.1 seed: ORP has no D3 slot'),
  ('pou',                'okres',                '01 section 2.1 seed: POU has no D3 slot'),
  ('obec',               'obec',                 null),
  ('spravni_obvod',      'cast_obce_or_quarter', '01 section 2.1 seed'),
  ('momc',               'cast_obce_or_quarter', '01 section 2.1 seed'),
  ('cast_obce',          'cast_obce_or_quarter', '01 section 2.1 seed'),
  ('katastralni_uzemi',  'cast_obce_or_quarter', '01 section 2.1 seed'),
  ('zsj',                'cast_obce_or_quarter', '01 section 2.1 seed'),
  ('ulice',              'street',               null),
  ('adresni_misto',      'address_point',        null),
  ('stavebni_objekt',    'building',             null),
  ('parcela',            'parcel',               null);

------------------------------------------------------------------
-- location_constants - MUTABLE (config), 01 section 2.2 / 6.2.
--
-- The CZ bounding box is ONE row here, consumed by ingest, resolver, API and
-- analytics alike. The repo currently carries six independent copies of it
-- (scraper/street.py plus seven per-portal parsers, lat 48.0-51.5, lon
-- 12.0-19.0); this row is the canonical one they collapse onto, and the 0.05
-- degree buffer is a TRIGGER for review, never a determination (the Wisla hotel
-- sits 0.008 degrees outside the box).
------------------------------------------------------------------

create table location_constants (
  name       text primary key,
  value_num  numeric,
  value_geom geometry(Geometry, 4326),
  note       text not null
);
alter table location_constants enable row level security;

insert into location_constants (name, value_num, value_geom, note) values
  ('cz_bbox', null, ST_MakeEnvelope(12.0, 48.0, 19.0, 51.5, 4326),
   'The ONE canonical CZ bounding box (01 section 2.2). Same envelope the repo already uses in scraper/street.py and the per-portal parsers. A trigger for review, never a country determination - D1 owns that.'),
  ('cz_bbox_trigger_buffer_deg', 0.05, null,
   'Buffer applied to cz_bbox before flagging a row for review (mining-synthesis section 2b).'),
  ('pip_sliver_tolerance_m', 250, null,
   'The nearest-obec sliver fallback distance; migration 289 chose this value. Recorded as admin_assignment_method = pip_nearest_within_n_m.'),
  ('registry_pin_conflict_m', 300, null,
   'A registry candidate further than this from the portal pin opens a contradiction rather than silently winning (ext-modeling-practices section C2).');

------------------------------------------------------------------
-- location_claim_type_meta - MUTABLE (config), one row per enum label.
--
-- Makes 02 section 2.1.2's rules checkable in SQL rather than by convention
-- ("no entry may emit a coordinate without a precision block").
--
-- Two seed facts are normative and must not be "fixed" later (00 section 2.1):
--   * postal_town is is_admin_bearing = FALSE. It is a Czech Post town and
--     differs from the obec on 57.0 percent of bazos rows.
--   * development_name, landmark and micro_position are is_admin_bearing = FALSE
--     PERMANENTLY. "Nova zlata mile" is an urbanisation, not an admin unit.
------------------------------------------------------------------

create table location_claim_type_meta (
  claim_type           location_claim_type primary key,
  is_precision_bearing boolean not null default false,
  is_admin_bearing     boolean not null default false,
  is_position_bearing  boolean not null default false,
  note                 text
);
alter table location_claim_type_meta enable row level security;

-- One row per label, generated from the enum itself so the table cannot drift
-- out of step with the type.
insert into location_claim_type_meta (claim_type)
select unnest(enum_range(null::location_claim_type));

update location_claim_type_meta set is_precision_bearing = true
 where claim_type in (
   'precision_declaration', 'uncertainty_geometry', 'blur_hint', 'map_zoom',
   'geohash', 'coordinate', 'address_point_id', 'building_id');

update location_claim_type_meta set is_admin_bearing = true
 where claim_type in (
   'obec_name', 'obec_code', 'cast_obce_name', 'quarter_name',
   'mestsky_obvod_name', 'okres_name', 'orp_name', 'kraj_name', 'country',
   'cadastral_territory_name', 'cadastral_territory_code');

-- 01 section 4.1 states the precision-bearing and admin-bearing seed sets
-- explicitly but leaves is_position_bearing ("carries or implies a coordinate")
-- unenumerated. Seeded here as the claim types that resolve DIRECTLY to a
-- geometry without an admin-centroid fallback - reading it any wider would make
-- the flag true for every admin name and therefore useless.
update location_claim_type_meta set is_position_bearing = true
 where claim_type in (
   'coordinate', 'uncertainty_geometry', 'geohash', 'admin_polygon',
   'address_point_id', 'building_id', 'parcel_number');

update location_claim_type_meta
   set note = 'Czech Post town, not an admin unit - differs from the obec on 57.0 percent of bazos rows. is_admin_bearing stays FALSE.'
 where claim_type = 'postal_town';

update location_claim_type_meta
   set note = 'An urbanisation or landmark, never an admin unit. is_admin_bearing is FALSE permanently.'
 where claim_type in ('development_name', 'landmark', 'micro_position');

------------------------------------------------------------------
-- Backend/service-role only. Supabase default privileges auto-GRANT
-- anon/authenticated on every new table and sequence; revoke explicitly.
-- No table above uses a serial/bigserial, so there is no sequence to revoke.
------------------------------------------------------------------

revoke all on location_granularity_rank   from anon, authenticated;
revoke all on location_level_granularity  from anon, authenticated;
revoke all on location_constants          from anon, authenticated;
revoke all on location_claim_type_meta    from anon, authenticated;

commit;
