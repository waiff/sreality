-- 383_location_w1_resolutions.sql
--
-- Location-data program, Wave W1, PR-A (4 of 5): layer (b) - the resolution as
-- a pure versioned function (D2b), plus the policy tables it reads (D8/D3/D10).
--
-- Design: design/final/01-schema.md sections 6.1 (resolutions + candidates),
-- 6.2 (field / uncertainty / collision policy), 6.3 (verifications) and 7.3
-- (pin_cluster_epochs). design/final/00-shared-contracts.md sections 6.1
-- (position_licence_class + the loc_res_licence guard) and 10.3
-- (collision_epoch_id inside the resolution's unique key).
--
-- pin_cluster_epochs is created HERE, ahead of location_resolutions, rather than
-- with pin_clusters in migration 384: 01 declares it in section 7.3 and resolves
-- the backward reference with an ALTER, but collision_epoch_id is NOT NULL and
-- part of the resolution's identity, so the FK must precede the table it
-- constrains for the migration to apply in one pass.
--
-- THE FIFTH VERSION INPUT. Pin-collision evidence is corpus-wide: it is an input
-- the resolver consumes but no single listing's claims contain. Without
-- collision_epoch_id inside the UNIQUE, a recompute that reclassifies a cluster
-- cannot invalidate the resolutions that consumed the old classification - stale
-- precision keeps serving map pins and dedup blocks, and the four-version
-- campaign gate has only three of its four inputs.
--
-- Backend/service-role only: RLS on, Supabase's default anon/authenticated ACL
-- revoked at the foot of the file.

begin;

------------------------------------------------------------------
-- pin_cluster_epochs - APPEND-ONLY (immutable). One epoch per scheduled
-- collision recompute.
--
-- Retention (normative, 01 section 7.3): an epoch may be deleted once no
-- location_resolutions row still references it as collision_epoch_id and it is
-- not the current epoch. A recompute that reclassifies nothing still mints a
-- row (~1 row) but re-resolves nothing - only listings whose bucket changed are
-- enqueued into dirty_locations.
------------------------------------------------------------------

create table pin_cluster_epochs (
  id                  bigserial primary key,
  computed_at         timestamptz not null default now(),
  policy_version      text not null,
  registry_version_id bigint not null references registry_versions(id),
  sources             text[] not null default '{}',
  cluster_count       integer,
  reclassified_count  integer,
  parent_epoch_id     bigint references pin_cluster_epochs(id),
  note                text
);
alter table pin_cluster_epochs enable row level security;

create index pin_cluster_epochs_recent on pin_cluster_epochs (computed_at desc);

------------------------------------------------------------------
-- location_resolutions - APPEND-ONLY (semantically a versioned cache).
--
-- The UNIQUE key IS the resolver's signature, so re-running is a no-op and
-- bumping any version produces a new row without touching the old one. FIVE
-- version inputs, not four (00 section 10.3).
--
-- ALL FOUR D3 AXES ARE NOT NULL. `unknown` is a value; NULL is not permitted. A
-- NULL uncertainty_radius_m makes both branches of the three-valued containment
-- test evaluate to NULL, so the row falls out of `certain` AND `possible` alike
-- - silently invisible to every radius and containment filter rather than
-- badged. The policy-derived default for an unresolved row is the coarsest
-- defensible bound.
--
-- position_licence_class, not licence_class: on a position-bearing relation the
-- value describes the winning COORDINATE, not the row (00 section 6.1).
-- loc_res_licence is artifact 2 of the three-part structural licensing guard -
-- a non-storable (Mapy.cz-class) coordinate can never become a resolution
-- winner.
------------------------------------------------------------------

create table location_resolutions (
  id                        bigserial primary key,
  listing_id                bigint not null,
  claim_set_hash            bytea not null,
  resolver_version          text not null,
  registry_version_id       bigint not null references registry_versions(id),
  policy_version            text not null,
  collision_epoch_id        bigint not null references pin_cluster_epochs(id),
  resolved_at               timestamptz not null default now(),

  status                    resolution_status not null,
  chosen_candidate_id       bigint,
  chosen_rule               text,
  candidate_count           integer not null default 0,
  runner_up_score_gap       numeric,

  -- D1 country determination
  country_code              char(2),
  country_status            country_status not null default 'undetermined',
  country_method            country_determination_method not null default 'unknown',
  country_confidence        match_confidence not null default 'low',
  country_driving_claim_ids bigint[] not null default '{}',
  country_conflicting       jsonb,

  -- D3 four axes of the WINNER, denormalized so the row is self-describing
  granularity               location_granularity not null default 'unknown',
  position_source           position_source not null default 'none',
  blur_evidence             blur_evidence not null default 'none',
  match_confidence          match_confidence not null default 'low',
  uncertainty_radius_m      numeric not null,
  radius_semantics          radius_semantics not null,
  geom                      geometry(Point, 4326),
  position_licence_class    licence_class not null default 'portal',

  input_claim_ids           bigint[] not null,
  unique (listing_id, claim_set_hash, resolver_version, registry_version_id, policy_version,
          collision_epoch_id),
  constraint loc_res_licence check (position_licence_class <> 'ephemeral_display_only')
);
alter table location_resolutions enable row level security;

create index location_resolutions_listing on location_resolutions (listing_id, resolved_at desc);
create index location_resolutions_status  on location_resolutions (status)
  where status in ('ambiguous', 'unmatched');

------------------------------------------------------------------
-- location_resolution_candidates - APPEND-ONLY.
--
-- Storing ALL candidates, not just the winner, is what preserves ambiguity
-- detection, operator arbitration and later re-ranking; every reviewed geocoder
-- returns a ranked list and collapsing to one row at ingest destroys the
-- information. It is also where non-winning POSITIONS live: there is no
-- `place_position` table (00 section 11.2) - a non-winner is a candidate row
-- with its own geom, its own four axes and distance_to_pin_m.
--
-- distance_to_pin_m implements the D8 cross-check: registry wins for geom when
-- the address resolves at building granularity, but a registry candidate beyond
-- registry_pin_conflict_m (300 m) from the portal pin FLAGS rather than
-- silently picking - that flag becomes a location_contradictions row.
------------------------------------------------------------------

create table location_resolution_candidates (
  id                   bigserial primary key,
  resolution_id        bigint not null references location_resolutions(id) on delete cascade,
  rank                 integer not null,
  score                numeric not null,
  target_kind          text not null check (target_kind in
                         ('address_point', 'building', 'parcel', 'street', 'admin_unit',
                          'coordinate_only')),
  ruian_adm_kod        bigint references ruian_address_points(kod_adm),
  stavebni_objekt_kod  bigint,
  parcela_id           bigint references ruian_parcels(id),
  ulice_id             bigint references ruian_streets(id),
  admin_unit_id        bigint references ruian_admin_units(id),
  geom                 geometry(Point, 4326),
  granularity          location_granularity not null,
  position_source      position_source not null,
  blur_evidence        blur_evidence not null default 'none',
  match_confidence     match_confidence not null,
  uncertainty_radius_m numeric not null,
  radius_semantics     radius_semantics not null,
  licence_class        licence_class not null,
  component_match      jsonb not null default '{}',
  distance_to_pin_m    numeric,
  rejected_reason      text,
  unique (resolution_id, rank)
);
alter table location_resolution_candidates enable row level security;

alter table location_resolutions
  add constraint location_resolutions_chosen_fk
  foreign key (chosen_candidate_id) references location_resolution_candidates(id) deferrable;

create index lrc_by_addr   on location_resolution_candidates (ruian_adm_kod)
  where ruian_adm_kod is not null;
create index lrc_ephemeral on location_resolution_candidates (resolution_id)
  where licence_class = 'ephemeral_display_only';

------------------------------------------------------------------
-- location_resolution_verifications - APPEND-ONLY. "Still current" without
-- minting a resolution.
--
-- When a new registry_version lands and none of the entities a resolution
-- depended on changed, the resolution is still correct - but its
-- registry_version_id names the old version, so every staleness query counts it
-- as behind. Minting a resolution per listing per registry version to say
-- "nothing changed" would multiply the table by the registry cadence for zero
-- information. Written by the registry_verification_advance job.
------------------------------------------------------------------

create table location_resolution_verifications (
  resolution_id       bigint not null references location_resolutions(id),
  registry_version_id bigint not null references registry_versions(id),
  verified_at         timestamptz not null default now(),
  verifier_version    text not null,
  unchanged_inputs    jsonb not null default '{}',
  primary key (resolution_id, registry_version_id)
);
alter table location_resolution_verifications enable row level security;

create index lrv_by_version on location_resolution_verifications (registry_version_id, verified_at);

------------------------------------------------------------------
-- location_field_policy - MUTABLE (config, versioned). Survivorship as data.
--
-- may_overwrite_non_null and requires_independent_agreement ARE D7's graded
-- write-back guard; a shorter policy table (the retired
-- field_survivorship_policy) silently drops it. Normative: every llm_text row
-- has may_overwrite_non_null = false, and requires_independent_agreement = true
-- even to fill a NULL - a claim that would change a non-NULL column never
-- auto-writes, it opens a contradiction row.
------------------------------------------------------------------

create table location_field_policy (
  id                             bigserial primary key,
  policy_version                 text not null,
  field                          location_claim_type not null,
  source_pattern                 text not null,
  method_pattern                 text not null,
  rank                           integer not null,
  min_granularity                location_granularity,
  min_confidence                 match_confidence,
  max_age_days                   integer,
  may_fill_null                  boolean not null default true,
  may_overwrite_non_null         boolean not null default false,
  requires_independent_agreement boolean not null default false,
  tie_breaker                    text not null default 'granularity_then_rank_then_recency',
  unique (policy_version, field, source_pattern, method_pattern)
);
alter table location_field_policy enable row level security;

-- Seed policy_version 'v1'. 01 section 6.2 states the four non-negotiable
-- policy FACTS rather than a literal row set, so the ladder below is the
-- minimal encoding of them: registry beats portal beats mined text (lower rank
-- wins), and the llm_text lane is graded exactly as D7 requires. It covers the
-- fields with genuine survivorship contention; adding a field or a per-portal
-- override is a data change, never a migration.
insert into location_field_policy
  (policy_version, field, source_pattern, method_pattern, rank,
   min_confidence, may_fill_null, may_overwrite_non_null, requires_independent_agreement)
select 'v1', f.field, l.source_pattern, l.method_pattern, l.rank,
       l.min_confidence, l.may_fill_null, l.may_overwrite_non_null, l.requires_independent_agreement
from unnest(array[
       'coordinate', 'address_point_id', 'street_name', 'house_number_cp', 'house_number_co',
       'psc', 'obec_name', 'cast_obce_name', 'okres_name', 'kraj_name'
     ]::location_claim_type[]) as f(field)
cross join (values
  ('ruian',     'registry_derived',        100, null::match_confidence, true, true,  false),
  ('portal:*',  'portal_structured_field', 300, null::match_confidence, true, true,  false),
  ('portal:*',  'html_selector_parse',     400, null::match_confidence, true, true,  false),
  ('llm_text',  'llm_text',                900, 'high'::match_confidence, true, false, true)
) as l(source_pattern, method_pattern, rank, min_confidence,
       may_fill_null, may_overwrite_non_null, requires_independent_agreement);

------------------------------------------------------------------
-- location_uncertainty_policy - MUTABLE (config, versioned).
--
-- r95_m NULL means "derive per `derivation`". The area-centroid rows use
-- derivation='admin_containment_radius', which reads
-- ruian_admin_unit_geometries.containment_radius_m paired with
-- representative_point - NEVER inscribed_radius_m (01 section 3.3.1).
--
-- radius_semantics is 'geometric_bound' or 'declared' throughout, NOT
-- 'r95_empirical': 01 OQ4 is explicit that the seed radii are engineering
-- judgement rather than measurement, and calling an uncalibrated number a
-- 95-percent containment radius would be a false probability statement.
-- bezrealitky is the stated calibration set; the pass that fixes this writes a
-- new policy_version, which re-resolves through the campaign runner.
------------------------------------------------------------------

create table location_uncertainty_policy (
  policy_version   text not null,
  position_source  position_source not null,
  granularity      location_granularity not null,
  source           text not null default '*',
  r95_m            numeric,
  radius_semantics radius_semantics not null,
  derivation       text not null default 'constant' check (derivation in
                     ('constant', 'admin_containment_radius', 'declared_shape', 'max_of_inputs')),
  note             text,
  primary key (policy_version, position_source, granularity, source)
);
alter table location_uncertainty_policy enable row level security;

insert into location_uncertainty_policy
  (policy_version, position_source, granularity, source, r95_m, radius_semantics, derivation, note) values
  -- the ext-modeling-practices section B3 ladder, transcribed onto
  -- (position_source, granularity) pairs. UNCALIBRATED (01 OQ4).
  ('v1', 'registry_point',     'address_point',        '*', 10,   'geometric_bound', 'constant', 'registry address point'),
  ('v1', 'registry_point',     'building',             '*', 15,   'geometric_bound', 'constant', 'building entrance'),
  ('v1', 'registry_point',     'parcel',               '*', 25,   'geometric_bound', 'constant', 'rooftop / parcel'),
  ('v1', 'portal_pin',         'address_point',        '*', 15,   'geometric_bound', 'constant', null),
  ('v1', 'portal_pin',         'building',             '*', 30,   'geometric_bound', 'constant', 'building centroid'),
  ('v1', 'portal_pin',         'street_segment',       '*', 100,  'geometric_bound', 'constant', 'interpolated along a segment'),
  ('v1', 'portal_pin',         'street',               '*', 300,  'geometric_bound', 'constant', 'street centroid'),
  ('v1', 'derived_geocode',    'address_point',        '*', 100,  'geometric_bound', 'constant', 'interpolated'),
  ('v1', 'derived_geocode',    'street_segment',       '*', 100,  'geometric_bound', 'constant', 'interpolated'),
  ('v1', 'derived_geocode',    'street',               '*', 300,  'geometric_bound', 'constant', 'street centroid'),
  -- portal-declared obfuscation. TWO different statements, never one row: 01
  -- section 3.3.1 says a geometric_bound, an r95_empirical and a declared value
  -- are incompatible, and a row cannot simultaneously carry an invented
  -- constant AND tell the builder to derive from a published shape.
  --   (a) the '*' fallback is the 500-1000 m band section B3 states for a blur
  --       nobody published a shape for - engineering judgement, so it is a
  --       geometric_bound produced by derivation='constant';
  ('v1', 'portal_pin_blurred', 'obec',                 '*', 1000, 'geometric_bound', 'constant', 'blur fallback, obec-level; no shape published'),
  ('v1', 'portal_pin_blurred', 'cast_obce_or_quarter', '*', 750,  'geometric_bound', 'constant', 'blur fallback, quarter-level; no shape published'),
  ('v1', 'portal_pin_blurred', 'street',               '*', 500,  'geometric_bound', 'constant', 'blur fallback, street-level; no shape published'),
  --   (b) a portal that genuinely PUBLISHES a shape gets its own source rows,
  --       r95_m NULL + derivation='declared_shape' (read the claim's
  --       uncertainty_geometry) + radius_semantics='declared'. maxima ships
  --       Circle.radius (1.36 / 2.26 km observed; unit unresolved, 01 OQ7 -
  --       'declared' keeps it honest either way); sreality ships the
  --       locality.geometry bbox, which is an empty stub on
  --       entity_type='address', hence no address_point row here.
  ('v1', 'portal_pin_blurred', 'obec',                 'maxima',   null, 'declared', 'declared_shape', 'maxima Circle.radius'),
  ('v1', 'portal_pin_blurred', 'cast_obce_or_quarter', 'maxima',   null, 'declared', 'declared_shape', 'maxima Circle.radius'),
  ('v1', 'portal_pin_blurred', 'street',               'maxima',   null, 'declared', 'declared_shape', 'maxima Circle.radius'),
  ('v1', 'portal_pin',         'obec',                 'sreality', null, 'declared', 'declared_shape', 'sreality locality.geometry bbox'),
  ('v1', 'portal_pin',         'cast_obce_or_quarter', 'sreality', null, 'declared', 'declared_shape', 'sreality locality.geometry bbox'),
  ('v1', 'portal_pin',         'street',               'sreality', null, 'declared', 'declared_shape', 'sreality locality.geometry bbox'),
  -- area centroid: derived from the polygon, never a constant.
  ('v1', 'admin_centroid',     'country',              '*', null, 'geometric_bound', 'admin_containment_radius', null),
  ('v1', 'admin_centroid',     'kraj',                 '*', null, 'geometric_bound', 'admin_containment_radius', null),
  ('v1', 'admin_centroid',     'okres',                '*', null, 'geometric_bound', 'admin_containment_radius', null),
  ('v1', 'admin_centroid',     'obec',                 '*', null, 'geometric_bound', 'admin_containment_radius', null),
  ('v1', 'admin_centroid',     'cast_obce_or_quarter', '*', null, 'geometric_bound', 'admin_containment_radius', null),
  -- a carried-forward position inherits no fresh measurement, so its bound is
  -- the max of whatever produced it; no invented constant.
  ('v1', 'carried_forward',    'address_point',        '*', null, 'geometric_bound', 'max_of_inputs', null),
  ('v1', 'carried_forward',    'building',             '*', null, 'geometric_bound', 'max_of_inputs', null),
  ('v1', 'carried_forward',    'street',               '*', null, 'geometric_bound', 'max_of_inputs', null),
  ('v1', 'carried_forward',    'obec',                 '*', null, 'geometric_bound', 'max_of_inputs', null),
  ('v1', 'carried_forward',    'unknown',              '*', null, 'geometric_bound', 'max_of_inputs', null),
  -- the unresolved sentinel: the coarsest defensible bound (01 section 6.1).
  ('v1', 'none',               'unknown',              '*', 250000, 'geometric_bound', 'constant', 'CZ-scale sentinel for a row with no usable position');

------------------------------------------------------------------
-- location_collision_policy - MUTABLE (config, versioned).
--
-- The pin-collision threshold is POLICY, never a constant in code and never an
-- index predicate: the serving index is WHERE geo_blockable (a stored boolean),
-- so re-calibrating is a data change rather than a full-corpus GiST rebuild.
--
-- This table has EXACTLY ONE live reader (00 section 10.4). The per-portal
-- pin_collision_semantics in the contract YAML is a DEPLOY-TIME SEED loaded
-- into this table; the resolver and the projection builder read the policy row
-- and never the YAML. Two live readers of one config is the antipattern the
-- design exists to remove.
--
-- threshold_n is UNCALIBRATED (01 OQ3): no number appearing in prose is
-- calibrated, and the column is NOT NULL, so the seed below is provisional and
-- must be replaced by the declared-vs-detected confusion matrix pass.
------------------------------------------------------------------

create table location_collision_policy (
  policy_version          text not null,
  source                  text not null default '*',
  obec_kod                bigint,
  obec_key                bigint generated always as (coalesce(obec_kod, -1)) stored,
  threshold_n             integer not null,
  radius_m                integer not null default 0,
  min_distinct_streets    integer not null default 2,
  pin_collision_semantics text not null default 'suspect'
                            check (pin_collision_semantics in ('suspect', 'legitimate_multiunit')),
  primary key (policy_version, source, obec_key)
);
alter table location_collision_policy enable row level security;

insert into location_collision_policy
  (policy_version, source, obec_kod, threshold_n, radius_m, min_distinct_streets, pin_collision_semantics) values
  ('v1', '*',           null, 4,  0, 2, 'suspect'),
  -- bezrealitky's 1-to-many collapse is genuinely real-world (Rezidence
  -- Veletrzni 42, River Garden, Signature Prague; max cluster 12, 1.09
  -- listings/point) and that portal is simultaneously the address-point-tier
  -- outlier and the ground-truth anchor for calibrating every other portal's
  -- R95. A global threshold would make it permanently ineligible for geometric
  -- blocking - which is precisely the failure 01 section 6.2 names.
  ('v1', 'bezrealitky', null, 12, 0, 2, 'legitimate_multiunit');

------------------------------------------------------------------
-- Backend/service-role only.
------------------------------------------------------------------

revoke all on pin_cluster_epochs                from anon, authenticated;
revoke all on location_resolutions              from anon, authenticated;
revoke all on location_resolution_candidates    from anon, authenticated;
revoke all on location_resolution_verifications from anon, authenticated;
revoke all on location_field_policy             from anon, authenticated;
revoke all on location_uncertainty_policy       from anon, authenticated;
revoke all on location_collision_policy         from anon, authenticated;

revoke all on sequence pin_cluster_epochs_id_seq             from anon, authenticated;
revoke all on sequence location_resolutions_id_seq           from anon, authenticated;
revoke all on sequence location_resolution_candidates_id_seq from anon, authenticated;
revoke all on sequence location_field_policy_id_seq          from anon, authenticated;

commit;
