-- 384_location_w1_serving.sql
--
-- Location-data program, Wave W1, PR-A (5 of 5): layer (c) - the two serving
-- projections, the collision artefacts, the incremental queue, the
-- contradiction ledger, per-method enrichment state and the ops tables.
--
-- Design: design/final/01-schema.md sections 7.1 (listing_location_current),
-- 7.1.1 (the derived-value functions and the two predicates), 7.2
-- (property_location_current), 7.3 (pin_clusters + daily summary), 7.4
-- (dirty_locations), 8 (the contradiction ledger), 9 (enrichment state), 9.1
-- (location_jobs) and 9.2 (location_metrics_rollup).
-- design/final/00-shared-contracts.md sections 7.1/7.3/7.4 (the projection and
-- its predicates), 8 (disposition keying), 9 (dirty_locations) and 10
-- (pin-collision artefacts).
--
-- PROJECTION-WIDE RULES (01 section 7, all three load-bearing):
--   (a) NO GENERATED COLUMNS anywhere on either projection. Every derived value
--       is written by the projection builder from a NAMED function below, which
--       is the single definition. Three separate traps forced this:
--       ST_Transform, unaccent() and to_char() are all non-IMMUTABLE, and
--       Postgres does not recompute stored generated columns when an enum gains
--       a value.
--   (b) NO INDEX PREDICATE COMPARES THE GRANULARITY ENUM ORDINALLY. Precision
--       gates are partial indexes on stored booleans (WHERE geo_blockable), so
--       re-calibrating the collision threshold is a data change, not a
--       full-corpus GiST rebuild.
--   (c) NO CONSUMER READS geom WITHOUT THE FOUR AXES - they are NOT NULL on the
--       same row. A NULL reads as "no gate" and fails open.
--
-- These are caches, never truth. Truncating either is always legal.
--
-- Backend/service-role only: RLS on, Supabase's default anon/authenticated ACL
-- revoked at the foot of the file.

begin;

------------------------------------------------------------------
-- The derived-value functions (01 section 7.1.1). One definition each; the
-- builder writes the column, a CI parity test asserts builder_output =
-- function(row) for a golden set.
------------------------------------------------------------------

-- IMMUTABLE: round(numeric, int) and ST_X/ST_Y are IMMUTABLE. to_char() is NOT
-- (its output depends on LC_NUMERIC), which is why the rounded-numeric form is
-- the only legal spelling here - the to_char form fails at CREATE TABLE as a
-- generated column and would be a locale-dependent, silently cross-environment-
-- divergent blocking key even if it applied.
--
-- This is also the h3-pg FALLBACK (01 section 0.5 / OQ1): h3 is not available
-- on this instance, so the rounded 4-dp cell is the spatial blocking key and
-- cell equality MUST be expanded to the 3x3 neighbourhood at query time.
create function location_geo_cell_key(g geometry) returns text
language sql immutable as $fn$
  select case when g is null then null else
    'c:' || round(ST_Y(g)::numeric, 4)::text || ':' || round(ST_X(g)::numeric, 4)::text end
$fn$;

-- STABLE, not IMMUTABLE, because unaccent() is STABLE - which is exactly why
-- street_block_key is a builder-written column and never a generated one.
create function location_street_block_key(obec_kod bigint, street text, hn text) returns text
language sql stable as $fn$
  select case when obec_kod is null or street is null then null else
    obec_kod::text || ':' || lower(unaccent(street)) || ':' || coalesce(hn, '') end
$fn$;

-- Tier 0 blocking keys. 01 section 7.1 names both functions against the
-- projection columns but gives no body; the deterministic prefixed rendering
-- below is [PROPOSED] and matches location_geo_cell_key's 'c:' shape so the
-- four keys are visibly one family and never collide with each other.
create function location_addr_block_key(ruian_adm_kod bigint) returns text
language sql immutable as $fn$
  select case when ruian_adm_kod is null then null else 'a:' || ruian_adm_kod::text end
$fn$;

create function location_building_block_key(stavebni_objekt_kod bigint) returns text
language sql immutable as $fn$
  select case when stavebni_objekt_kod is null then null else 'b:' || stavebni_objekt_kod::text end
$fn$;

------------------------------------------------------------------
-- listing_location_current - REBUILDABLE PROJECTION (a cache, never truth).
--
-- Registry identity carries BOTH currencies: the stable *_kod codes are the
-- cross-version keys that public predicates and API responses use, while the
-- *_unit_id surrogates are join handles valid ONLY for registry_version_id.
--
-- display_label replaces nine incompatible portal `locality` semantics.
-- postal_town is a SEPARATE column and is never folded into it: for bazos
-- neither value is wrong - they answer different questions - and 57.0 percent
-- of bazos rows disagree between the two.
--
-- THE TWO HONESTY PREDICATES (00 section 7.3/7.4, canonical combined form; the
-- builder computes them, they are not generated columns):
--
--   pin_collision_ok    <=> pin_collision_class in ('normal','building_1_to_many')
--                       AND cluster_heterogeneity_ok
--                       AND pin_shared_by_n <= threshold_from(
--                             location_collision_policy, source, obec_kod)
--
--   geo_blockable       <=> rank(granularity) >= rank('street_segment')
--                       AND position_source NOT IN ('admin_centroid',
--                             'portal_pin_blurred','carried_forward','none')
--                       AND pin_collision_ok
--
--   renderable_as_point <=> rank(granularity) >= rank('building')
--                       AND position_source IN ('registry_point','portal_pin')
--                       AND pin_collision_ok
--                       AND NOT location_disputed
--
-- `rank(...)` is location_granularity_rank.rank, NEVER the enum's ordinality.
-- The earlier `pin_collision_class IS NULL` form is a never-true test - the
-- vocabulary has no NULL member, its "everything is fine" value is the STRING
-- 'normal' - and it additionally excluded the two classes that ARE fine.
-- 01 section A.2 check 8 forbids the NULL test anywhere.
--
-- geo_cell_key is written ONLY when geo_blockable, NULL otherwise. That is what
-- keeps the granularity rung out of an IMMUTABLE expression.
------------------------------------------------------------------

create table listing_location_current (
  listing_id                bigint primary key,
  property_id               bigint,
  source                    text not null,
  resolution_id             bigint not null references location_resolutions(id),
  registry_version_id       bigint not null references registry_versions(id),
  registry_version          text not null,
  resolver_version          text not null,
  policy_version            text not null,
  built_at                  timestamptz not null default now(),

  -- D1 country
  country_code              char(2),
  country_status            country_status not null,
  country_method            country_determination_method not null,
  country_confidence        match_confidence not null,
  country_driving_claim_ids bigint[] not null default '{}',
  is_cz                     boolean not null,

  -- D3 four axes, mandatory next to every coordinate
  geom                      geometry(Point, 4326),
  granularity               location_granularity not null,
  position_source           position_source not null,
  blur_evidence             blur_evidence not null,
  match_confidence          match_confidence not null,
  match_components          jsonb not null default '{}',
  uncertainty_radius_m      numeric not null,
  radius_semantics          radius_semantics not null,
  position_licence_class    licence_class not null,

  -- registry identity (D4)
  ruian_adm_kod             bigint,
  stavebni_objekt_kod       bigint,
  parcela_id                bigint,
  ulice_kod                 bigint,
  obec_kod                  bigint,
  cast_obce_kod             bigint,
  momc_kod                  bigint,
  ku_kod                    bigint,
  pou_kod                   bigint,
  orp_kod                   bigint,
  okres_kod                 bigint,
  kraj_kod                  bigint,
  obec_unit_id              bigint,
  cast_obce_unit_id         bigint,
  okres_unit_id             bigint,
  kraj_unit_id              bigint,
  admin_path                ltree,
  admin_assignment_method   admin_assignment_method not null,
  admin_position_source     position_source not null,
  admin_sliver_distance_m   numeric,

  -- display / postal
  display_label             text not null,
  display_path              text,
  street_name               text,
  house_number_cp           text,
  house_number_co           text,
  evidencni                 text,
  psc                       char(5),
  postal_town               text,
  cast_obce_name            text,
  obec_name                 text,
  okres_name                text,
  kraj_name                 text,
  development_name          text,
  place_search_text         text,

  -- honesty signals (D10), all builder-written stored booleans
  pin_shared_by_n           integer not null default 1,
  pin_shared_by_n_25m       integer not null default 1,
  pin_shared_by_n_100m      integer not null default 1,
  pin_cluster_id            bigint,
  -- Carried VERBATIM from pin_clusters.classification - ONE vocabulary, and
  -- NEVER NULL: an unclustered listing is 'normal'.
  pin_collision_class       text not null default 'normal' check (pin_collision_class in
                              ('normal', 'legitimate_multiunit', 'building_1_to_many',
                               'town_centroid_suspect', 'parser_collapse_suspect',
                               'foreign_resort_centroid')),
  cluster_heterogeneity_ok  boolean not null default true,
  render_as                 text not null check (render_as in ('point', 'circle', 'area')),
  renderable_as_point       boolean not null,
  is_low_precision          boolean not null,
  geo_blockable             boolean not null,
  location_disputed         boolean not null default false,
  -- The membership verdict is a SCALAR comparison against this; without it the
  -- certain/possible decision degrades to per-row geometry in the hot filter
  -- path. Builder-written, NULL when no admin unit is assigned.
  distance_to_nearest_boundary_m numeric,
  history_completeness      text,

  -- field-level provenance (D2/D7)
  field_provenance          jsonb not null default '{}',
  geom_claim_id             bigint,
  street_claim_id           bigint,

  -- blocking keys (D6/D10), builder-written from the named functions above
  addr_block_key            text,
  building_block_key        text,
  street_block_key          text,
  geo_cell_key              text,
  h3_r10                    text,

  -- artifact 1 of the three-part structural licensing guard: a Mapy.cz-class
  -- coordinate physically cannot land in the store of record.
  constraint llc_licence check (position_licence_class <> 'ephemeral_display_only'),
  -- render_as is the 3-valued API rendering of renderable_as_point (the API
  -- needs 'area' as a third state, which a boolean cannot express); this makes
  -- the two provably consistent, satisfying the one-computation requirement.
  constraint llc_render check (renderable_as_point = (render_as = 'point'))
);
alter table listing_location_current enable row level security;

create index llc_geom_gist     on listing_location_current using gist (geom);
-- The ONLY index a geo-blocking query may use. The predicate is the stored
-- boolean, never an ordinal enum comparison.
create index llc_geom_blockable on listing_location_current using gist (geom) where geo_blockable;
create index llc_admin_path    on listing_location_current using gist (admin_path);
create index llc_obec          on listing_location_current (obec_kod, granularity);
create index llc_okres         on listing_location_current (okres_kod);
create index llc_addr_block    on listing_location_current (addr_block_key) where addr_block_key is not null;
create index llc_bldg_block    on listing_location_current (building_block_key) where building_block_key is not null;
create index llc_street_block  on listing_location_current (street_block_key) where street_block_key is not null;
create index llc_cell          on listing_location_current (geo_cell_key) where geo_cell_key is not null;
create index llc_psc           on listing_location_current (psc) where psc is not null;
create index llc_search_trgm   on listing_location_current using gin (place_search_text gin_trgm_ops);
create index llc_country       on listing_location_current (country_status, country_code);
create index llc_disputed      on listing_location_current (listing_id) where location_disputed;

------------------------------------------------------------------
-- property_location_current - REBUILDABLE PROJECTION. Reconciliation, not a
-- lottery.
--
-- Property-grain geography is a reconciliation OVER CHILDREN. Today a grouped
-- property's geom comes from one child, its street independently from a
-- possibly different child and its display row from a third, with no record
-- that the members disagreed. The disagreement columns are mandatory.
--
-- GRAIN HONESTY (01 section 7.2.1, normative): "how good is our location data?"
-- is answered at LISTING grain; "where is this property?" at PROPERTY grain.
-- "Highest-precision member wins" is a maximum order statistic, so
-- property-grain precision distributions are upward-biased and improve whenever
-- GROUPING improves, with no change in data quality. Any surface showing a
-- precision mix must label its grain.
------------------------------------------------------------------

create table property_location_current (
  property_id             bigint primary key,
  built_at                timestamptz not null default now(),
  member_count            integer not null,
  winner_listing_id       bigint not null,
  winner_rule             text not null,
  winner_source           text not null,
  geom                    geometry(Point, 4326),
  granularity             location_granularity not null,
  position_source         position_source not null,
  blur_evidence           blur_evidence not null,
  match_confidence        match_confidence not null,
  uncertainty_radius_m    numeric not null,
  radius_semantics        radius_semantics not null,
  position_licence_class  licence_class not null,
  ruian_adm_kod           bigint,
  stavebni_objekt_kod     bigint,
  obec_kod                bigint,
  cast_obce_kod           bigint,
  okres_kod               bigint,
  kraj_kod                bigint,
  admin_path              ltree,
  admin_assignment_method admin_assignment_method not null,
  street_name             text,
  psc                     char(5),
  display_label           text not null,
  place_search_text       text,
  country_code            char(2),
  country_status          country_status not null,
  -- disagreement is a first-class quality signal (D10)
  member_spread_m         numeric,
  members_with_geom       integer not null default 0,
  distinct_street_names   integer not null default 0,
  distinct_obec_kods      integer not null default 0,
  disagreement_flags      text[] not null default '{}',
  pin_shared_by_n         integer not null default 1,
  geo_blockable           boolean not null,
  render_as               text not null check (render_as in ('point', 'circle', 'area')),
  constraint plc_licence check (position_licence_class <> 'ephemeral_display_only')
);
alter table property_location_current enable row level security;

create index plc_geom_gist      on property_location_current using gist (geom);
create index plc_geom_blockable on property_location_current using gist (geom) where geo_blockable;
create index plc_admin_path     on property_location_current using gist (admin_path);
create index plc_obec           on property_location_current (obec_kod, granularity);
create index plc_disagree       on property_location_current (property_id)
  where array_length(disagreement_flags, 1) > 0;

------------------------------------------------------------------
-- pin_clusters - REBUILDABLE, CURRENT-STATE PER EPOCH.
--
-- Clusters are PER-PORTAL because portals collapse differently: bezrealitky's
-- collapse is real-world 1-to-many, idnes's apparent 14 percent is an artifact
-- of foreign listings and drops to 1.5 percent on CZ-only coordinates, bazos at
-- 5.56 listings/point is genuine centroid collapse. The classification enum is
-- therefore not cosmetic - the same COUNT means different things per portal.
--
-- Epoch-keyed with a retention rule, not append-per-day-forever: a naive
-- (source, cell_key, computed_at) key with a daily recompute would run to
-- ~110 M rows/year in a table whose serving consumer only ever wants the
-- current value.
--
-- Decimal precision is explicitly NOT a usable collapse detector (bazos's
-- uniform 5-6 decimals with 5.56 listings/point is high stated precision with
-- town-centroid reality; 7+ decimals are reprojection artifacts).
------------------------------------------------------------------

create table pin_clusters (
  id                           bigserial primary key,
  epoch_id                     bigint not null references pin_cluster_epochs(id),
  source                       text not null,
  cell_key                     text not null,
  geom                         geometry(Point, 4326) not null,
  listing_count                integer not null,
  distinct_streets             integer not null,
  distinct_obec_kods           integer not null,
  nearest_admin_unit_id        bigint references ruian_admin_units(id),
  distance_to_admin_centroid_m numeric,
  declared_blur_share          numeric,
  classification               text not null check (classification in
                                 ('normal', 'legitimate_multiunit', 'building_1_to_many',
                                  'town_centroid_suspect', 'parser_collapse_suspect',
                                  'foreign_resort_centroid')),
  first_seen_at                timestamptz not null default now(),
  computed_at                  timestamptz not null default now(),
  registry_version_id          bigint not null references registry_versions(id),
  policy_version               text not null,
  unique (epoch_id, source, cell_key)
);
alter table pin_clusters enable row level security;

create index pin_clusters_geom  on pin_clusters using gist (geom);
create index pin_clusters_hot   on pin_clusters (source, listing_count desc)
  where classification <> 'normal';
create index pin_clusters_epoch on pin_clusters (epoch_id, source);

-- Trend history without a full daily snapshot of every cell (~3.3k rows/year).
-- declared_vs_detected is the standing confusion matrix that calibrates the
-- collision threshold - the only reader blur_evidence has.
create table pin_cluster_daily_summary (
  source                text not null,
  day                   date not null,
  listings_per_point    numeric not null,
  pct_in_clusters_ge_20 numeric not null,
  max_cluster           integer not null,
  distinct_cells        integer not null,
  declared_vs_detected  jsonb not null default '{}',
  primary key (source, day)
);
alter table pin_cluster_daily_summary enable row level security;

------------------------------------------------------------------
-- dirty_locations - MUTABLE queue (drained, not history).
--
-- Three behavioural facts, all load-bearing:
--   1. The enqueue happens INSIDE the claim-insert / resolution transaction -
--      the only coupling between intake and resolution, and what makes an
--      operator merge or correction visible on the next Browse/map read.
--   2. The drain claims bounded slices with FOR UPDATE SKIP LOCKED and rebuilds
--      the projection row in the same transaction.
--   3. Judge the queue by OLDEST-ROW AGE, not length.
--
-- Shape follows the repo's existing dirty_properties precedent (rule 20).
------------------------------------------------------------------

create table dirty_locations (
  listing_id       bigint primary key,
  enqueued_at      timestamptz not null default now(),
  reason           text not null check (reason in
                     ('claim_insert', 'resolution_written', 'registry_version', 'policy_version',
                      'collision_recompute', 'property_grouping', 'operator_edit', 'full_sweep')),
  attempts         integer not null default 0,
  last_error       text,
  next_eligible_at timestamptz not null default now()
);
alter table dirty_locations enable row level security;

create index dirty_locations_due on dirty_locations (next_eligible_at, enqueued_at);

------------------------------------------------------------------
-- The contradiction ledger (D7): APPEND-ONLY detection + MUTABLE disposition +
-- a computed "open" view. There is no `status` column on the detection table
-- and nothing there is ever UPDATEd; a re-run at a new reconciler version
-- appends a new row.
--
-- auto_action records what the resolver ALREADY DID; it is an observation, not
-- a workflow state.
--
-- location_disputed on the projection is a CACHE derived from this ledger on
-- rebuild; the reconciler never writes the projection.
------------------------------------------------------------------

create table location_contradictions (
  id                  bigserial primary key,
  listing_id          bigint not null,
  property_id         bigint,
  snapshot_id         bigint,
  detected_at         timestamptz not null default now(),
  reconciler_version  text not null,
  resolver_version    text,
  registry_version_id bigint not null references registry_versions(id),
  field               location_claim_type not null,
  rule                text not null,
  severity            text not null check (severity in ('major', 'minor', 'info')),
  stored              jsonb,
  claimed             jsonb,
  served_claim_id     bigint references location_claims(id),
  claimed_claim_id    bigint references location_claims(id),
  evidence_claim_ids  bigint[] not null default '{}',
  distance_m          numeric,
  evidence_quote      text,
  auto_action         text not null check (auto_action in
                        ('none', 'blocked_write', 'downgraded_precision')),
  -- VERSION-FREE IDENTITY: a stable hash over
  --   (listing_id, rule, field, normalized_claimed_value, normalized_served_value)
  -- and DELIBERATELY NOT over reconciler_version, registry_version_id or
  -- snapshot_id. listing_id is INSIDE the hashed key - that is what lets the
  -- disposition table key on it standalone, because it has no other column to
  -- scope a judgement to a listing.
  dedupe_key          bytea not null,
  unique (dedupe_key, listing_id, reconciler_version, registry_version_id)
);
alter table location_contradictions enable row level security;

create index loc_contra_open    on location_contradictions (severity, detected_at desc);
create index loc_contra_listing on location_contradictions (listing_id, field);
create index loc_contra_dedupe  on location_contradictions (dedupe_key);

-- KEYED ON THE VERSION-FREE IDENTITY, NOT ON contradiction_id. Bumping
-- reconciler_version is ROUTINE (one per shipped rule) and every bump re-detects
-- every still-true finding as a new row with a new id; with the disposition
-- keyed on contradiction_id, every operator judgement would orphan and the
-- arbitration queue would refill with findings already ruled on - thousands of
-- duplicate cards per bump at the measured contradiction rate.
--
-- status is LIFECYCLE and disposition is JUDGEMENT: two columns, not one list.
-- A flattened list cannot express "acknowledged, judgement deferred", which is
-- the most common state of a live arbitration queue.
--
-- decided_by and auto_closed_reason are BOTH required: operator_id cannot
-- express a machine actor, and "who" and "why" are different facts. Auto-close
-- is an APPENDED disposition, never an edit, and only fires when the predicate
-- stops firing AND the inputs changed (new snapshot, new registry version, new
-- collision epoch) - a re-run that merely happens again closes nothing.
create table location_contradiction_dispositions (
  dedupe_key         bytea primary key,
  contradiction_id   bigint references location_contradictions(id),
  status             text not null check (status in
                       ('open', 'acknowledged', 'resolved_operator', 'resolved_upstream', 'wontfix')),
  disposition        text check (disposition in
                       ('accepted_claim', 'rejected_claim', 'both_wrong', 'not_a_conflict', 'deferred')),
  operator_note      text,
  operator_id        text,
  decided_by         text not null default 'operator',
  decided_at         timestamptz not null default now(),
  auto_closed_reason text
);
alter table location_contradiction_dispositions enable row level security;

create index lcd_contradiction on location_contradiction_dispositions (contradiction_id)
  where contradiction_id is not null;

-- Latest-wins with history retained: the disposition row above is the head,
-- every decision is appended here.
create table location_contradiction_disposition_log (
  id                 bigserial primary key,
  dedupe_key         bytea not null,
  contradiction_id   bigint references location_contradictions(id),
  status             text not null,
  disposition        text,
  operator_note      text,
  operator_id        text,
  decided_by         text not null,
  auto_closed_reason text,
  decided_at         timestamptz not null default now()
);
alter table location_contradiction_disposition_log enable row level security;

create index lcdl_key on location_contradiction_disposition_log (dedupe_key, decided_at desc);

-- "Open" is a COMPUTED state, never a column.
create view location_contradictions_open as
  select c.*, coalesce(d.status, 'open') as status
  from location_contradictions c
  left join location_contradiction_dispositions d on d.dedupe_key = c.dedupe_key
  where coalesce(d.status, 'open') in ('open', 'acknowledged');

------------------------------------------------------------------
-- location_enrichment_state - MUTABLE (latest-wins per method). Attempt is not
-- outcome.
--
-- Today's ledgers are per-pipeline and non-uniform (an integer attempt version,
-- a bare timestamp where success and failure are indistinguishable, two
-- refetch-destructible raw_json markers) with no outcome dimension on any of
-- them. input_hash is the cost gate the LLM lane needs: gate every re-run on the
-- content hash changing, or an hourly-to-monthly re-scrape multiplies the bill
-- by the re-scrape factor for zero new information.
--
-- Scheduling/throttling ONLY. The queue is dirty_locations; the history is
-- location_claims + location_claim_observations.
------------------------------------------------------------------

create table location_enrichment_state (
  listing_id          bigint not null,
  method              location_extraction_method not null,
  lane                text not null,
  attempts            integer not null default 0,
  last_attempt_at     timestamptz,
  last_outcome        text check (last_outcome in
                        ('placed', 'ambiguous', 'unmatched', 'skipped', 'error', 'not_applicable')),
  last_error          text,
  input_hash          bytea,
  registry_version_id bigint references registry_versions(id),
  extractor_version   text,
  given_up            boolean not null default false,
  next_eligible_at    timestamptz,
  primary key (listing_id, method, lane)
);
alter table location_enrichment_state enable row level security;

create index les_due on location_enrichment_state (lane, next_eligible_at) where not given_up;

------------------------------------------------------------------
-- location_jobs - MUTABLE, one row per recurring lane. The ops calendar is a
-- table, not a wiki page, so the "now() - last_success_at > 3 x cadence" page
-- has a real data source. Section 04 owns the CONTENTS (which lanes exist,
-- their cadence, runner, lease and alert route); this is only the shape, so no
-- rows are seeded here.
--
-- lease_holder / lease_expires_at implement lease-row CAS, NOT a session
-- advisory lock: every service-role path uses the transaction-mode pooler,
-- where a lock taken on one backend and released on another silently strands.
--
-- Canonical lane names (00 section 14): location_resolve_incremental,
-- location_resolve_sweep, pin_collision_recompute, resolution_campaign_runner,
-- registry_verification_advance, llm_claims_lane. Concurrency groups:
-- location-resolve, location-collision, location-campaign, location-llm.
------------------------------------------------------------------

create table location_jobs (
  job_name             text primary key,
  cadence              interval not null,
  concurrency_group    text not null,
  runner               text not null,
  enabled              boolean not null default true,
  lease_holder         text,
  lease_expires_at     timestamptz,
  last_started_at      timestamptz,
  last_success_at      timestamptz,
  last_outcome         text check (last_outcome in ('ok', 'failed', 'cancelled', 'skipped', 'running')),
  last_error           text,
  consecutive_failures integer not null default 0,
  alert_route          text,
  note                 text
);
alter table location_jobs enable row level security;

create index location_jobs_stale on location_jobs (last_success_at) where enabled;

------------------------------------------------------------------
-- location_metrics_rollup - APPEND-ONLY, low volume. The weekly digest.
--
-- `grain` is NOT optional: labelling the grain is a correctness requirement for
-- any precision statistic (section 7.2.1), and a rollup that omits it
-- reproduces exactly the upward-biased comparison it warns about.
--
-- registry_oracle_audit is a JOB, not a table; its findings land here as
-- metric='registry_oracle_mismatches' rows.
------------------------------------------------------------------

create table location_metrics_rollup (
  period_start date not null,
  period_end   date not null,
  metric       text not null,
  grain        text not null check (grain in ('listing', 'property', 'cluster', 'corpus')),
  source       text not null default '*',
  value_num    numeric,
  detail       jsonb not null default '{}',
  computed_at  timestamptz not null default now(),
  primary key (period_start, metric, grain, source)
);
alter table location_metrics_rollup enable row level security;

------------------------------------------------------------------
-- Backend/service-role only.
------------------------------------------------------------------

revoke all on listing_location_current                 from anon, authenticated;
revoke all on property_location_current                from anon, authenticated;
revoke all on pin_clusters                             from anon, authenticated;
revoke all on pin_cluster_daily_summary                from anon, authenticated;
revoke all on dirty_locations                          from anon, authenticated;
revoke all on location_contradictions                  from anon, authenticated;
revoke all on location_contradiction_dispositions      from anon, authenticated;
revoke all on location_contradiction_disposition_log   from anon, authenticated;
revoke all on location_contradictions_open             from anon, authenticated;
revoke all on location_enrichment_state                from anon, authenticated;
revoke all on location_jobs                            from anon, authenticated;
revoke all on location_metrics_rollup                  from anon, authenticated;

revoke all on sequence pin_clusters_id_seq             from anon, authenticated;
revoke all on sequence location_contradictions_id_seq  from anon, authenticated;
revoke all on sequence location_contradiction_disposition_log_id_seq from anon, authenticated;

revoke execute on function location_geo_cell_key(geometry)                from public, anon, authenticated;
revoke execute on function location_street_block_key(bigint, text, text)  from public, anon, authenticated;
revoke execute on function location_addr_block_key(bigint)                from public, anon, authenticated;
revoke execute on function location_building_block_key(bigint)            from public, anon, authenticated;

commit;
