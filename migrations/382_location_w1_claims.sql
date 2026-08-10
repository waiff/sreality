-- 382_location_w1_claims.sql
--
-- Location-data program, Wave W1, PR-A (3 of 5): layer (a) - the append-only
-- claim store (D2a), its evidence document store, and the portal contracts
-- registry (D9).
--
-- Design: design/final/01-schema.md sections 4.0 (portal_raw_payloads), 4.1
-- (vocabularies - the enums themselves shipped in migration 380), 4.2
-- (location_claims + fingerprint), 4.3 (observations), 4.4 (absences), 4.5
-- (links), 4.6 (retractions + location_claims_live), 4.7 (batches) and 5
-- (portal_contracts + portal_contract_entries).
--
-- FILE ORDER vs DESIGN ORDER. 01 declares location_claims (section 4.2) before
-- portal_contract_entries (section 5) while section 4.2's contract_entry_id and
-- section 4.7's contract_id both carry an inline REFERENCES to it, and section
-- 4.6's location_claims_live view joins both contract tables. Migrations must
-- apply in one pass, so the two contract tables are created FIRST here and
-- every FK stays inline as written. The only forward reference the design
-- itself resolves by ALTER (location_claims.batch_id / retractions.batch_id ->
-- location_claim_batches, section 4.7) keeps that shape.
--
-- Backend/service-role only: RLS on, Supabase's default anon/authenticated ACL
-- revoked at the foot of the file.

begin;

------------------------------------------------------------------
-- location_value_norm - the ONE claim-value normalization definition.
--
-- 01 section 4.2 declares location_claims.value_norm as "written by
-- location_value_norm(value_text)" without giving a body. Declared here so
-- there is a single definition: value_norm feeds both the trigram index and
-- claim_fingerprint (section 4.2.1), so two implementations would mean two
-- identity vocabularies. STABLE, not IMMUTABLE, because unaccent() is STABLE -
-- which is exactly why value_norm is a written column and not a generated one
-- (01 section 0.4). Body is [PROPOSED]; the extractor lane owns refinement.
------------------------------------------------------------------

create function location_value_norm(p_value text) returns text
language sql stable as $fn$
  select case
           when p_value is null then null
           else nullif(btrim(regexp_replace(lower(unaccent(p_value)), '[^a-z0-9]+', ' ', 'g')), '')
         end
$fn$;

------------------------------------------------------------------
-- portal_contracts / portal_contract_entries - APPEND-ONLY (loaded from git).
--
-- D9: git is the store of record (contracts/<portal>.yaml); these tables are a
-- deploy-time projection, existing so location_claims.contract_entry_id can be
-- a real FK and so an unloaded or renamed entry fails loudly. contract_sha256 +
-- git_ref make the two provably identical.
--
-- The header/entry SPLIT is load-bearing (00 section 13, review-coherence C6):
-- the earlier single-table design put the partial unique index on is_active on
-- a one-row-per-ENTRY table, which made activating any multi-entry version
-- fail. is_active applies to the (source, version) HEADER.
------------------------------------------------------------------

create table portal_contracts (
  id               bigserial primary key,
  source           text not null,
  version          integer not null,
  contract_sha256  bytea not null,
  git_ref          text not null,
  identity_ladder  text[] not null default '{}',
  exclusion_zones  jsonb not null default '[]',
  precision_priors jsonb not null default '{}',
  fetch_config     jsonb not null default '{}',
  loaded_at        timestamptz not null default now(),
  is_active        boolean not null default false,
  retired_at       timestamptz,
  unique (source, version)
);
alter table portal_contracts enable row level security;

create unique index portal_contracts_active on portal_contracts (source) where is_active;

-- Entries are IMMUTABLE once loaded; a change is a new contract version.
-- extraction_method is mandatory and NOT derivable from the surface (an
-- html_selector locator may be html_selector_parse, breadcrumb_parse or
-- map_widget_parse). There is deliberately no `claim_surface` key: DOM region
-- and payload sub-tree are neither licensing- nor provenance-bearing and their
-- precise addressing already lives in `locator` (00 section 3.2).
create table portal_contract_entries (
  id                      bigserial primary key,
  contract_id             bigint not null references portal_contracts(id),
  entry_id                text not null,
  surface                 location_claim_surface not null,
  page_kind               location_page_kind not null,
  locator                 jsonb not null,
  claim_type              location_claim_type not null,
  extraction_method       location_extraction_method not null,
  subject_scope           jsonb not null default '{}',
  transform               jsonb not null default '[]',
  precision_map           jsonb not null default '{}',
  default_granularity     location_granularity,
  default_position_source position_source,
  default_blur_evidence   blur_evidence not null default 'none',
  default_licence_class   licence_class not null default 'portal',
  cardinality             text not null default 'one' check (cardinality in ('one', 'many')),
  required                text not null default 'when_present'
                            check (required in ('always', 'when_present', 'best_effort')),
  on_conflict             text not null default 'emit_both',
  guards                  text[] not null default '{}',
  notes                   text,
  unique (contract_id, entry_id)
);
alter table portal_contract_entries enable row level security;

create index pce_by_claim on portal_contract_entries (claim_type, surface);

------------------------------------------------------------------
-- portal_raw_payloads - APPEND-ON-CHANGE, content-addressed (01 section 4.0).
--
-- This is NOT the existing portal_raw_pages table. That one is latest-wins
-- (UNIQUE (source, source_id_native, page_kind) + ON CONFLICT DO UPDATE SET
-- html, 445,191 rows / 14 GB) and is the MIGRATION SOURCE - it is exactly why a
-- span into "the page" stops being resolvable after the next fetch. This is the
-- append-on-change TARGET that location_claims.payload_sha256 resolves against,
-- which is what keeps the D7 evidence check repeatable rather than one-shot.
------------------------------------------------------------------

create table portal_raw_payloads (
  id                bigserial primary key,
  source            text not null,
  source_id_native  text not null,
  listing_id        bigint,
  page_kind         location_page_kind not null,
  payload_sha256    bytea not null,
  content_type      text not null,
  body              bytea,
  body_r2_key       text,
  byte_size         integer not null,
  contract_version  integer,
  snapshot_id       bigint,
  first_observed_at timestamptz not null,
  last_observed_at  timestamptz not null,
  fetched_at        timestamptz not null default now(),
  unique (source, source_id_native, page_kind, payload_sha256),
  constraint prp_body_present check (body is not null or body_r2_key is not null)
);
alter table portal_raw_payloads enable row level security;

create index prp_sha     on portal_raw_payloads (payload_sha256);
create index prp_listing on portal_raw_payloads (listing_id, page_kind, first_observed_at desc)
  where listing_id is not null;
create index prp_native  on portal_raw_payloads (source, source_id_native, page_kind,
                                                 first_observed_at desc);

------------------------------------------------------------------
-- location_claims - APPEND-ONLY. The irreplaceable asset (D2a).
--
-- ONE table, not two: D2a states portal-structured signals AND text-mined
-- signals are both claims, and the resolver must rank a portal field against a
-- mined sentence under one comparable policy. The discriminator is
-- (claim_type, extraction_method).
--
-- listing_id carries no FK to listings(id) by design (01 section 4.2 states it
-- as a comment): claims are append-only history and must survive any future
-- listings-side lifecycle without an ON DELETE decision.
--
-- claim_fingerprint is TIME-FREE (01 section 4.2.1): not snapshot_id, not
-- first_observed_at, not payload_sha256, not the evidence span. Values dedupe;
-- occurrences are their own append-only series in location_claim_observations.
-- With snapshot identity inside the tuple, W3 would write one row per unchanged
-- value per snapshot - order 10 M+ rows for sreality alone.
--
-- The evidence columns are a SECURITY boundary, not metadata: every portal
-- ships an address-shaped decoy (sreality's premise.locality is the agency
-- OFFICE; remax's raw_json.address is mis-sourced from the "Podobne
-- nemovitosti" carousel and reached listings.street on 2 rows). The DB CHECKs
-- enforce PRESENCE plus the payload pointer; the deterministic validator
-- enforces that the quote is a substring of the SCOPED payload.
------------------------------------------------------------------

create table location_claims (
  id                        bigserial primary key,
  listing_id                bigint not null,
  source                    text not null,
  source_id_native          text not null,

  -- anchoring (D2a). Exactly one of these is the anchor; snapshot_anchor says
  -- which. 00 section 3.3: the four values below are canonical; a
  -- payload_sha256-addressed but not snapshot-bound body is
  -- 'unanchored_latest_fetch'.
  snapshot_id               bigint,
  snapshot_anchor           text not null default 'snapshot'
                              check (snapshot_anchor in
                                ('snapshot', 'unanchored_latest_fetch', 'unanchored_legacy', 'registry')),
  payload_id                bigint references portal_raw_payloads(id),
  payload_sha256            bytea,
  first_observed_at         timestamptz not null,
  extracted_at              timestamptz not null default now(),

  claim_type                location_claim_type not null,
  surface                   location_claim_surface not null,
  page_kind                 location_page_kind not null default 'none',
  extraction_method         location_extraction_method not null,
  extractor_id              text not null,
  extractor_version         text not null,
  contract_entry_id         bigint references portal_contract_entries(id),
  batch_id                  bigint,

  -- value, typed by claim_type (CHECKed below)
  value_text                text,
  value_norm                text,
  value_num                 numeric,
  value_geom                geometry(Point, 4326),
  value_shape               geometry(Geometry, 4326),
  value_jsonb               jsonb,

  -- relative_distance / poi_distance extras
  distance_m                integer,
  travel_mode               text check (travel_mode in ('walk', 'drive', 'transit', 'unspecified')),
  target_text               text,

  -- D7 evidence discipline (mandatory for text methods)
  evidence_quote            text,
  span_start                integer,
  span_end                  integer,
  payload_scope_version     text,
  subject_scoped            boolean,
  model                     text,
  prompt_version            text,

  -- self-declared precision riding on the claim
  declared_precision_label  text,
  declared_confidence       text,
  -- ERRATUM to 00 section 1.5. That section says uncertainty_radius_m +
  -- radius_semantics are "NOT NULL on the claim, the resolution, the candidate
  -- and both projections". 01 section 4.2 - which OWNS the DDL and therefore
  -- wins - carries only this NULLABLE declared_radius_m at claim grain, and no
  -- radius_semantics column on location_claims at all (the nullable one on
  -- location_claim_links belongs to the link, not the claim): a claim records
  -- what the portal DECLARED, and most portals declare nothing. The NOT NULL
  -- pair is real on the
  -- resolution, the candidate and both projections (383, 384), where it is the
  -- fail-open gate 05 P5 requires. Read 00 section 1.5's "on the claim" as
  -- corrected to those three projections.
  declared_radius_m         numeric,
  blur_evidence             blur_evidence not null default 'none',
  claim_confidence          match_confidence,

  -- licensing lineage (D6)
  licence_class             licence_class not null,

  -- migration provenance (Section 06)
  legacy_source_column      text,
  legacy_write_path_unknown boolean not null default false,
  history_completeness      text check (history_completeness in
                              ('full', 'payload_only', 'locality_text_only', 'none')),

  claim_fingerprint         bytea not null,
  created_at                timestamptz not null default now(),

  constraint loc_claim_text_evidence check (
    extraction_method not in ('llm_text', 'regex_text')
    or (evidence_quote is not null and span_start is not null and span_end is not null
        and span_end > span_start and payload_scope_version is not null
        and subject_scoped is not null)),
  -- D7: a span is meaningless without the document it indexes into.
  constraint loc_claim_evidence_payload check (
    evidence_quote is null or payload_sha256 is not null),
  constraint loc_claim_llm_model check (
    extraction_method <> 'llm_text' or (model is not null and prompt_version is not null)),
  constraint loc_claim_legacy check (
    extraction_method <> 'legacy_column' or legacy_source_column is not null),
  constraint loc_claim_anchor check (
    (snapshot_anchor = 'snapshot' and snapshot_id is not null)
    or (snapshot_anchor <> 'snapshot' and snapshot_id is null)),
  constraint loc_claim_value_present check (
    value_text is not null or value_num is not null or value_geom is not null
    or value_shape is not null or value_jsonb is not null),
  constraint loc_claim_coordinate_shape check (
    claim_type <> 'coordinate' or value_geom is not null),
  constraint loc_claim_distance_shape check (
    claim_type not in ('relative_distance', 'poi_distance')
    or (distance_m is not null and target_text is not null))
);
alter table location_claims enable row level security;

create unique index location_claims_fingerprint on location_claims (claim_fingerprint);
create index location_claims_listing   on location_claims (listing_id, claim_type, first_observed_at desc);
create index location_claims_snapshot  on location_claims (snapshot_id) where snapshot_id is not null;
create index location_claims_payload   on location_claims (payload_sha256) where payload_sha256 is not null;
create index location_claims_geom      on location_claims using gist (value_geom)
  where value_geom is not null;
create index location_claims_foreign   on location_claims (source, claim_type)
  where claim_type in ('foreign_indicator', 'country');
create index location_claims_norm_trgm on location_claims using gin (value_norm gin_trgm_ops)
  where value_norm is not null;
create index location_claims_by_method on location_claims (extraction_method, claim_type, first_observed_at desc);
-- The Mapy.cz remediation handle: the affected set is ONE indexed predicate
-- away, permanently (00 section 6.1, artifact 3 of the structural guard).
create index location_claims_ephemeral on location_claims (source, first_observed_at)
  where licence_class = 'ephemeral_display_only';

------------------------------------------------------------------
-- location_claim_observations - APPEND-ONLY. "Still present" is a fact.
--
-- The occurrence series that makes the time-free fingerprint safe. `seq` exists
-- because bulk backfills share one extracted_at and any ordered read must break
-- ties on a unique monotonic key, not on the timestamp alone. Deliberately
-- narrow (no evidence_quote, no value columns) - it is the highest-cardinality
-- table in the design.
------------------------------------------------------------------

create table location_claim_observations (
  claim_id          bigint not null references location_claims(id),
  observed_at       timestamptz not null,
  snapshot_id       bigint,
  payload_sha256    bytea,
  span_start        integer,
  span_end          integer,
  extractor_version text not null,
  extracted_at      timestamptz not null default now(),
  seq               bigserial,
  primary key (claim_id, observed_at, seq)
);
alter table location_claim_observations enable row level security;

create index lco_claim_time on location_claim_observations (claim_id, observed_at desc);
create index lco_snapshot   on location_claim_observations (snapshot_id) where snapshot_id is not null;

------------------------------------------------------------------
-- location_claim_absences - APPEND-ONLY. Negative assertions are data.
--
-- The unique key does NOT use snapshot_id directly: Postgres treats NULLs as
-- distinct, and snapshot_id is legitimately NULL for index-surface claims AND
-- for every archived_html claim - so the table would accumulate unbounded
-- duplicate absence rows for exactly the cohort it exists to throttle. The
-- generated snapshot_key = coalesce(snapshot_id, -1) is the portable form
-- (UNIQUE NULLS NOT DISTINCT is the PG15+ equivalent).
--
-- `surface` is in the key because "no street in the JSON payload" and "no
-- street in the archived HTML" are different facts.
------------------------------------------------------------------

create table location_claim_absences (
  id                bigserial primary key,
  listing_id        bigint not null,
  snapshot_id       bigint,
  snapshot_key      bigint generated always as (coalesce(snapshot_id, -1)) stored,
  surface           location_claim_surface not null,
  field             location_claim_type not null,
  reason            text not null check (reason in
                      ('not_stated', 'stated_but_ambiguous', 'only_in_excluded_block', 'not_attempted')),
  extraction_method location_extraction_method not null,
  extractor_version text not null,
  surfaces_seen     location_claim_surface[] not null default '{}',
  observed_at       timestamptz not null default now(),
  unique (listing_id, snapshot_key, surface, field, extractor_version)
);
alter table location_claim_absences enable row level security;

------------------------------------------------------------------
-- location_claim_links - REBUILDABLE. A claim resolved to a real entity.
--
-- linked_id is polymorphic over linked_kind, so it carries no FK. Amenity and
-- transit links resolve into the EXISTING OSM mirrors and are therefore always
-- licence_class='odbl' - which is what keeps a future CC BY export a query
-- rather than an archaeology project.
------------------------------------------------------------------

create table location_claim_links (
  claim_id            bigint not null references location_claims(id),
  registry_version_id bigint not null references registry_versions(id),
  linked_kind         text not null check (linked_kind in
                        ('admin_unit', 'street', 'address_point', 'building', 'parcel',
                         'amenity', 'transit_line')),
  linked_id           bigint not null,
  anchor_geom         geometry(Point, 4326),
  anchor_radius_m     numeric,
  radius_semantics    radius_semantics,
  licence_class       licence_class not null,
  score               numeric not null,
  primary key (claim_id, registry_version_id, linked_kind, linked_id)
);
alter table location_claim_links enable row level security;

------------------------------------------------------------------
-- location_claim_retractions - APPEND-ONLY. How an append-only table un-says
-- something.
--
-- A bad extractor version cannot be deleted, so a retraction is another append
-- naming the scope that is no longer to be believed. Claims stay on disk (the
-- record of what we once extracted is itself evidence); they simply stop being
-- resolver inputs. Retracting does NOT delete observations and does NOT rewrite
-- existing resolutions: it enqueues the affected listings into dirty_locations
-- and the next resolution is minted at the current version tuple over the
-- surviving claims.
------------------------------------------------------------------

create table location_claim_retractions (
  id               bigserial primary key,
  scope            text not null check (scope in
                     ('claim', 'batch', 'extractor_entry', 'contract_version')),
  claim_id         bigint references location_claims(id),
  batch_id         bigint,
  contract_source  text,
  contract_version integer,
  extractor_id     text,
  reason           text not null check (reason in
                     ('extractor_bug', 'contract_misread', 'fabrication', 'licence_withdrawal',
                      'superseded_backfill', 'operator_judgement')),
  note             text,
  retracted_by     text not null,
  retracted_at     timestamptz not null default now(),
  constraint lcr_scope check (
       (scope = 'claim'            and claim_id is not null)
    or (scope = 'batch'            and batch_id is not null)
    or (scope = 'extractor_entry'  and contract_source is not null
                                   and contract_version is not null and extractor_id is not null)
    or (scope = 'contract_version' and contract_source is not null
                                   and contract_version is not null))
);
alter table location_claim_retractions enable row level security;

create index lcr_claim on location_claim_retractions (claim_id) where claim_id is not null;
create index lcr_batch on location_claim_retractions (batch_id) where batch_id is not null;
create index lcr_entry on location_claim_retractions (contract_source, contract_version, extractor_id);

------------------------------------------------------------------
-- location_claim_batches - APPEND-ONLY. The unit of rollback.
--
-- batch_id is nullable on location_claims because a synchronous single-listing
-- write from the detail-drain is not a batch; bulk lanes (LLM miner, W2/W3
-- backfills, contract reloads) must set it.
------------------------------------------------------------------

create table location_claim_batches (
  id                bigserial primary key,
  lane              text not null,
  source            text,
  extractor_version text not null,
  contract_id       bigint references portal_contracts(id),
  wave              text,
  job_run_id        text,
  started_at        timestamptz not null default now(),
  finished_at       timestamptz,
  row_count         integer not null default 0,
  outcome           text check (outcome in ('running', 'ok', 'failed', 'retracted')),
  note              text
);
alter table location_claim_batches enable row level security;

create index lcb_version on location_claim_batches (extractor_version, started_at desc);

alter table location_claims
  add constraint location_claims_batch_fk
  foreign key (batch_id) references location_claim_batches(id);

alter table location_claim_retractions
  add constraint lcr_batch_fk
  foreign key (batch_id) references location_claim_batches(id);

------------------------------------------------------------------
-- location_claims_live - the relation the RESOLVER reads. Section 03 never
-- selects from location_claims directly, so a retraction cannot be silently
-- ignored (01 section A.2 check 9).
------------------------------------------------------------------

create view location_claims_live as
  select c.*
  from location_claims c
  where not exists (
    select 1 from location_claim_retractions r
    where (r.scope = 'claim'            and r.claim_id = c.id)
       or (r.scope = 'batch'            and r.batch_id = c.batch_id)
       or (r.scope = 'extractor_entry'  and r.contract_source = c.source
                                        and r.extractor_id   = c.extractor_id
                                        and r.contract_version is not distinct from
                                            (select pc.version from portal_contract_entries pce
                                               join portal_contracts pc on pc.id = pce.contract_id
                                              where pce.id = c.contract_entry_id))
       or (r.scope = 'contract_version' and r.contract_source = c.source
                                        and r.contract_version is not distinct from
                                            (select pc.version from portal_contract_entries pce
                                               join portal_contracts pc on pc.id = pce.contract_id
                                              where pce.id = c.contract_entry_id)));

------------------------------------------------------------------
-- Backend/service-role only. Tables, sequences, the view and the function all
-- carry Supabase's auto-GRANT; revoke each explicitly.
------------------------------------------------------------------

revoke all on portal_contracts             from anon, authenticated;
revoke all on portal_contract_entries      from anon, authenticated;
revoke all on portal_raw_payloads          from anon, authenticated;
revoke all on location_claims              from anon, authenticated;
revoke all on location_claim_observations  from anon, authenticated;
revoke all on location_claim_absences      from anon, authenticated;
revoke all on location_claim_links         from anon, authenticated;
revoke all on location_claim_retractions   from anon, authenticated;
revoke all on location_claim_batches       from anon, authenticated;
revoke all on location_claims_live         from anon, authenticated;

revoke all on sequence portal_contracts_id_seq            from anon, authenticated;
revoke all on sequence portal_contract_entries_id_seq     from anon, authenticated;
revoke all on sequence portal_raw_payloads_id_seq         from anon, authenticated;
revoke all on sequence location_claims_id_seq             from anon, authenticated;
revoke all on sequence location_claim_observations_seq_seq from anon, authenticated;
revoke all on sequence location_claim_absences_id_seq     from anon, authenticated;
revoke all on sequence location_claim_retractions_id_seq  from anon, authenticated;
revoke all on sequence location_claim_batches_id_seq      from anon, authenticated;

revoke execute on function location_value_norm(text) from public, anon, authenticated;

commit;
