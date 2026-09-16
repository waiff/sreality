-- 528_autodedup_foundation.sql
--
-- The autonomous cross-portal deduplication engine's own store.
-- Design: docs/design/autodedup/PROGRAM.md.
--
-- WHY A SCHEMA OF ITS OWN. Purely ADDITIVE and fully ISOLATED: no foreign key into any
-- production table, nothing shared with `dedup_sim.*` (the parallel operator-guided
-- simulation program, migrations 372/373/489/492) and nothing read from the removed
-- legacy engine (CLAUDE.md rule 15). Teardown is one `drop schema autodedup cascade`
-- and production never notices. Two programs, two schemas: a shared table would make
-- either one's recalibration the other's regression.
--
-- NO FK INTO listings/properties/images, mirroring migration 492's reasoning: a FK on an
-- 822k-row hot table adds lock surface for no benefit here, and the whole schema must
-- stay droppable without touching production. `listing_id` columns are listings.id
-- (bigint) by convention, enforced in code, not by the catalog.
--
-- WHAT WRITES WHAT (all of it backend-only, all of it through the admin-gated API or the
-- GitHub Actions lane — there is no browser path into this schema):
--   settings                     operator, through the admin API
--   runs, iterations             the lane (`python -m autodedup.lane`), one row per pass
--   listing_fp, image_band,
--   phash_pop, exploded_blocks   the fingerprint/blocking build pass
--   pairs, judgements,
--   must_not_link                the resolve pass + the LLM judge
--   clusters, cluster_members,
--   cluster_conflicts            the clustering pass
--   verdicts                     the operator, through the validation UI
--   labels, models, eval_samples the training/calibration passes
--   resolve_queue, scan_cursor,
--   judge_queue                  the realtime producers + drains
--   merges                       NOBODY, in this program. See below.
--
-- SHADOW MODE (program ruling D4). The entire trial decides, stores and reports; it
-- writes NOTHING into production — no `merge_properties` call, no `listings.property_id`
-- move, no operator state carried. `autodedup.merges` is created here as the EMPTY
-- forward ledger a later, separately-approved write path would fill, so that path is a
-- code change and not a migration; `pairs.applied_merge_group` and `clusters.property_id`
-- are the matching reserved columns. All three stay NULL/empty for the life of this
-- program.
--
-- CLEAN SLATE (program ruling D7). The engine reads NOTHING from `property_merge_events`
-- — not decisions, not unmerge/split history, not even counts — so nothing here mirrors
-- or reconciles against its contents, and no decision anywhere in the engine takes an
-- existing row in that table as an input. `merges.stamped` is not a counter-example: it
-- is a boolean about work THIS engine did (the future write path's non-atomic seam), read
-- only from `autodedup.merges` itself, and it stays false on the zero rows this program
-- writes.
--
-- SIZING. Trial (D1's four cohort blocks, ~3,500-6,500 listings; ~5,000 expected):
-- well under 60 MB total.
-- A later corpus-wide build (822,599 all-time listings; no retention window, because a
-- 24-month window would structurally make pre-2024 re-listings unretrievable) would be:
--     listing_fp      822k rows  x ~300 B  ~=  250 MB
--     image_band      ~8.2M rows x ~80  B  ~=  660 MB   <- the largest line
--     phash_pop       only phashes on >= 3 listings     ~=   30 MB
--     pairs           only score >= the store floor     ~=  200 MB
--     judgements / clusters / verdicts / the rest       ~=  100 MB
--     TOTAL ~1.2 GB, with ad_anchor_images (3 -> 2, ruling D8) as the shipped knob if
--     that ever needs to come down. Corpus backfill is OUT of this program's scope
--     (ruling D9); these numbers are here so the schema is not re-litigated later.
-- Rejected pairs below the store floor are counted, never stored: they are re-derivable
-- from the block index, and a multi-GB pair table would buy nothing.
--
-- POSTURE. Every base table RLS-on with anon/authenticated revoked (this project's
-- default privileges auto-GRANT otherwise — migrations 299/492 — and
-- tests/test_migration_rls_grants.py rule 3 fails a migration without the RLS line).
-- WHAT ACTUALLY HOLDS THE POSTURE is the per-table `revoke all … from anon,
-- authenticated` sweep at the bottom, the sequence sweep beside it, and the fact that
-- this schema grants no USAGE to either browser role. The three `alter default
-- privileges … in schema autodedup` statements below are belt-and-braces for a future
-- global default grant: a brand-new schema inherits no per-schema default ACL to revoke,
-- so today they store no catalog row and change nothing.
-- There is NO `_public` view and none planned: the browser Supabase client is pinned to
-- schema `public`, so this schema is unreachable from the browser by construction, and
-- the progress page + validation UI read it through the admin-gated API. Every relation
-- below is registered in tests/test_migration_rls_grants.py::_ADMIN_ONLY_RELATIONS.
--
-- APPLY. `set lock_timeout` is PLAIN, never SET LOCAL — the apply path
-- (.github/workflows/apply_migration.yml) runs statements in autocommit, where SET LOCAL
-- evaporates; every statement is idempotent so a lock-timeout retry re-runs the file
-- safely.
-- RECEIPT: an index name can never be schema-qualified in CREATE INDEX, so the apply
-- receipt (scripts/migration_objects.py) qualifies each index with the schema of its ON
-- target and probes `autodedup.<index>` via to_regclass — a change shipped in this same PR.
-- Index names are kept because IF NOT EXISTS requires a name and the lock-timeout retry
-- re-runs the whole file. Manual check:
--   select indexname from pg_indexes where schemaname = 'autodedup' order by 1;

set lock_timeout = '5s';

create schema if not exists autodedup;

revoke all on schema autodedup from anon, authenticated;
alter default privileges for role postgres in schema autodedup
  revoke all on tables from anon, authenticated;
alter default privileges for role postgres in schema autodedup
  revoke all on sequences from anon, authenticated;
alter default privileges for role postgres in schema autodedup
  revoke all on functions from anon, authenticated;

------------------------------------------------------------------
-- control plane
------------------------------------------------------------------

-- Engine knobs. A new knob needs no migration: the Python registry
-- (autodedup/settings.py) holds defaults, types and the plain-language blurb the same
-- way toolkit/dedup_sim_settings.py does, and a missing row here means "use the default".
-- Kept HERE rather than in public.app_settings so this engine's kill switches can never
-- be confused with the parallel program's keys.
create table if not exists autodedup.settings (
  key         text primary key,
  value       jsonb       not null,
  updated_at  timestamptz not null default now(),
  updated_by  text
);

-- One row per lane pass (census, export, build, resolve, judge, cluster, eval).
-- `fingerprint` is the hash of the inputs that produced it, so a pass is reproducible
-- after the settings move; `cohort` pins WHICH blocks the pass covered (ruling D1).
create table if not exists autodedup.runs (
  id           bigserial primary key,
  mode         text        not null,
  status       text        not null check (status in ('running','success','failed','cancelled')),
  fingerprint  text,
  cohort       jsonb,
  params       jsonb,
  progress     jsonb,
  stats        jsonb,
  error        text,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz
);
create index if not exists autodedup_runs_started_idx on autodedup.runs (started_at desc);

-- The operator-facing program log, and the only table behind the SPA's top-line Progress
-- page (W1, before the validation views). One row per ITERATION — a unit of work the
-- operator can read as a story: what was tried, on what sample, with which tools, what it
-- measured and what it cost. `runs` is machine bookkeeping; this is the narrative, and
-- the two are deliberately separate so a wave that took five lane runs is still one line
-- on the page. `artifacts` maps a name to a URL (an Actions artifact, a report); `run_id`
-- is the GitHub Actions run id, not a FK to `runs`.
create table if not exists autodedup.iterations (
  id           bigserial primary key,
  wave         text not null,
  title        text not null,
  status       text not null check (status in ('running','done','failed','skipped')),
  approach     text,
  tools        text[],
  sample_stats jsonb,
  metrics      jsonb,
  cost_usd     numeric(10,4) not null default 0,
  artifacts    jsonb,
  run_id       bigint,
  notes        text,
  started_at   timestamptz,
  finished_at  timestamptz,
  created_at   timestamptz not null default now()
);
create index if not exists autodedup_iterations_created_idx
  on autodedup.iterations (created_at desc);

------------------------------------------------------------------
-- blocking substrate
------------------------------------------------------------------

-- One precomputed row per listing. Every blocking probe is an index lookup on THIS table;
-- nothing in the engine is a set-based join over the corpus. Location columns come only
-- from public.listing_location (migration 508 dropped the listing-level ones);
-- `granularity_rank` comes from the ranking function, never from the enum's declaration
-- order. `attrs` carries the remaining listing columns value-or-absent, so a new feature
-- needs no migration.
-- `numerals` is jsonb, not the design's `integer[]`: the discriminators that consume it
-- (`numeric_fact_overlap` / `numeral_conflict`) compare (value, unit) PAIRS, and a flat
-- int4 array carries no unit (so "3. NP" and "3+kk" collide in one slot), truncates
-- 68.5 m² and 68.4 m² to the same 68 in exactly the near-miss case the discriminator
-- exists for, and would reject — not clamp — a >int4 numeral scraped out of free text
-- (this repo has aborted whole insert batches on that class twice; see
-- scraper.db.sane_price_czk). Shape: `[{"v": 68.5, "u": "m2"}, …]`, `u` from a closed
-- vocabulary owned by the extractor.
create table if not exists autodedup.listing_fp (
  listing_id            bigint  primary key,
  source                text    not null,
  category_main         text,
  category_type         text,
  cat_group             text,
  block_key             bigint,
  obec_kod              bigint,
  cast_obce_kod         bigint,
  country_code          text,
  country_status        text,
  lat                   double precision,
  lon                   double precision,
  uncertainty_radius_m  numeric,
  granularity_rank      smallint,
  is_address_grain      boolean,
  pin_pop               integer,
  street_key            text,
  house_number_cp       text,
  psc                   text,
  ruian_adm_kod         bigint,
  area_m2               numeric,
  area_band             integer,
  disposition           text,
  floor                 integer,
  total_floors          integer,
  price_first           integer,
  price_last            integer,
  price_min             integer,
  price_max             integer,
  price_events          smallint,
  broker_key            text,
  broker_identity_id    bigint,
  broker_firm_id        bigint,
  desc_simhash          bigint,
  desc_tokens           integer,
  numerals              jsonb,
  n_images              smallint,
  n_anchor_images       smallint,
  attrs                 jsonb,
  first_seen_at         timestamptz,
  last_seen_at          timestamptz,
  inactive_at           timestamptz,
  is_active             boolean,
  fp_version            smallint not null,
  built_at              timestamptz not null default now()
);
create index if not exists autodedup_fp_k1_dispo_idx on autodedup.listing_fp
  (block_key, cat_group, category_type, disposition);
create index if not exists autodedup_fp_k1_area_idx on autodedup.listing_fp
  (block_key, cat_group, category_type, area_band);
create index if not exists autodedup_fp_k2_addr_idx on autodedup.listing_fp
  (obec_kod, street_key, house_number_cp) where street_key is not null;
create index if not exists autodedup_fp_k3_broker_idx on autodedup.listing_fp
  (broker_key, cat_group, category_type) where broker_key is not null;
create index if not exists autodedup_fp_k6_foreign_idx on autodedup.listing_fp
  (country_code, cat_group, category_type, area_band) where country_status = 'foreign';
create index if not exists autodedup_fp_stale_idx on autodedup.listing_fp (fp_version, built_at);

-- dHash LSH. Four 16-bit bands per hash: by pigeonhole two hashes within Hamming <= 3 are
-- GUARANTEED to share at least one exact band, and <= 6 shares one with high probability.
-- Two 32-bit bands would halve the storage and silently guarantee only Hamming <= 1 — a
-- recall trade, not a byte trade. Rows are per LISTING and deduplicated, not per image,
-- because the probe only ever asks "which listings".
create table if not exists autodedup.image_band (
  band_no    smallint not null,
  band_val   integer  not null,
  listing_id bigint   not null,
  primary key (band_no, band_val, listing_id)
);
create index if not exists autodedup_image_band_listing_idx on autodedup.image_band (listing_id);

-- Corpus-wide shared-photo popularity, materialized ONLY for hashes seen on >= 3 distinct
-- listings, so the table stays tiny while the signal stays corpus-wide: a developer
-- catalogue spans blocks, so a per-block scope would miss exactly the case it is for.
create table if not exists autodedup.phash_pop (
  phash       bigint primary key,
  n_listings  integer not null,
  computed_at timestamptz not null default now()
);
create index if not exists autodedup_phash_pop_n_idx on autodedup.phash_pop (n_listings desc);

-- A (probe, key) whose membership exceeds the max-block-size knob is never expanded.
-- An exploded PHOTO block is not merely dropped: membership in one is the
-- developer-catalogue detector and becomes a negative feature.
create table if not exists autodedup.exploded_blocks (
  probe         text    not null,
  block_hash    bigint  not null,
  member_count  integer not null,
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  primary key (probe, block_hash)
);

------------------------------------------------------------------
-- pair grain
------------------------------------------------------------------

-- Latest-wins, one row per unordered pair. `feature_version` / `model_version` are
-- COLUMNS, not part of the key: a row per generation would multiply the largest table in
-- the schema by the number of recalibrations, and every generation is re-derivable from
-- the fingerprints anyway. `applied_merge_group` is the reserved write-path column and
-- stays NULL for this whole program (ruling D4).
create table if not exists autodedup.pairs (
  listing_lo          bigint   not null,
  listing_hi          bigint   not null,
  probes              text[]   not null,
  families            smallint not null default 0,
  features            jsonb,
  score               real,
  zone                text     check (zone in ('merge','band','reject')),
  decision            text,
  guard_veto          text,
  cluster_key         bigint,
  feature_version     smallint,
  model_version       text,
  applied_merge_group uuid,
  decided_at          timestamptz not null default now(),
  primary key (listing_lo, listing_hi),
  constraint autodedup_pairs_order_ck check (listing_lo < listing_hi)
);
create index if not exists autodedup_pairs_band_idx on autodedup.pairs (score desc)
  where zone = 'band';
create index if not exists autodedup_pairs_hi_idx on autodedup.pairs (listing_hi);
create index if not exists autodedup_pairs_cluster_idx on autodedup.pairs (cluster_key)
  where cluster_key is not null;
create index if not exists autodedup_pairs_decided_idx on autodedup.pairs (decided_at desc);

-- One row per (pair, judge version, tier). `llm_call_id` points at public.llm_calls by
-- value only — no FK, per the no-FK rule above — so the judge's spend is reconcilable
-- against the three `called_for` values migration 527 adds.
create table if not exists autodedup.judgements (
  listing_lo             bigint not null,
  listing_hi             bigint not null,
  judge_version          text   not null,
  tier                   text   not null check (tier in ('text','vision','gold')),
  model                  text   not null,
  verdict                text   not null check (verdict in
                           ('same_property','different_property',
                            'same_building_different_unit','insufficient_evidence')),
  confidence             real,
  unit_discriminator     text,
  key_evidence           text[],
  contradicting_evidence text[],
  developer_project_suspected boolean,
  llm_call_id            bigint,
  cost_usd               numeric(10,6),
  created_at             timestamptz not null default now(),
  primary key (listing_lo, listing_hi, judge_version, tier),
  constraint autodedup_judgements_order_ck check (listing_lo < listing_hi)
);
create index if not exists autodedup_judgements_verdict_idx
  on autodedup.judgements (verdict, created_at desc);

-- PERMANENT. Survives every generation bump, recalibration and model swap. Written by a
-- hard-guard veto, by an LLM `different_property` / `same_building_different_unit`
-- verdict, and by an operator "not the same" — the last of which a machine never
-- overwrites.
create table if not exists autodedup.must_not_link (
  listing_lo bigint not null,
  listing_hi bigint not null,
  source     text   not null check (source in ('guard','model','llm','operator')),
  reason     text,
  created_at timestamptz not null default now(),
  primary key (listing_lo, listing_hi),
  constraint autodedup_mnl_order_ck check (listing_lo < listing_hi)
);

------------------------------------------------------------------
-- cluster grain
------------------------------------------------------------------

-- `cluster_key` is the smallest listings.id ever admitted and is immutable, so a cluster
-- keeps its identity across rebuilds. `property_id` is reserved for the future write path
-- and stays NULL in this program.
create table if not exists autodedup.clusters (
  cluster_key         bigint primary key,
  generation          text    not null,
  size                integer not null,
  block_key           bigint,
  cat_group           text,
  category_main       text,
  category_type       text,
  area_min            numeric,
  area_max            numeric,
  sources             text[],
  medoid_listing_id   bigint,
  min_edge_score      real,
  mean_edge_score     real,
  n_judged_edges      integer not null default 0,
  n_certificate_edges integer not null default 0,
  evidence_families   smallint,
  max_gap_days        integer,
  shared_photo_warning boolean not null default false,
  status              text not null check (status in
                        ('proposed','applied','conflict','oversize','reverted','rejected')),
  property_id         bigint,
  model_version       text,
  feature_version     smallint,
  first_built_at      timestamptz not null default now(),
  last_changed_at     timestamptz not null default now()
);
create index if not exists autodedup_clusters_review_idx
  on autodedup.clusters (min_edge_score, cluster_key desc);
create index if not exists autodedup_clusters_status_idx
  on autodedup.clusters (status, size desc);

create table if not exists autodedup.cluster_members (
  cluster_key   bigint not null,
  listing_id    bigint not null,
  joined_at     timestamptz not null default now(),
  joined_via_lo bigint,
  joined_via_hi bigint,
  primary key (cluster_key, listing_id)
);
create index if not exists autodedup_cluster_members_listing_idx
  on autodedup.cluster_members (listing_id);

-- A union the invariants refused, or a bridge between two existing clusters (deliberately
-- outside the autonomous envelope). These are the highest-value rows in the validation
-- UI: a conflict means strong evidence in both directions.
create table if not exists autodedup.cluster_conflicts (
  id            bigserial primary key,
  kind          text not null check (kind in ('invariant','must_not_link','oversize','bridge')),
  cluster_key_a bigint,
  cluster_key_b bigint,
  listing_lo    bigint,
  listing_hi    bigint,
  invariant     text,
  detail        jsonb,
  created_at    timestamptz not null default now(),
  constraint autodedup_cluster_conflicts_order_ck check (listing_lo < listing_hi)
);
create index if not exists autodedup_cluster_conflicts_kind_idx
  on autodedup.cluster_conflicts (kind, created_at desc);

------------------------------------------------------------------
-- reserved write ledger + learning
------------------------------------------------------------------

-- EMPTY FOR THIS PROGRAM (ruling D4: the trial is shadow mode end to end). Created now so
-- that a later, separately-approved write path is a code change and not a migration, and
-- so the shape is agreed while nobody is under deployment pressure: exactly ONE retired
-- property per group, so reversing one is exact and reverting a whole generation is
-- linear. `stamped` is the future write path's own bookkeeping — did the engine finish
-- the non-atomic seam after `merge_properties` returned — and is ENGINE-LOCAL: ruling D7
-- forbids READING `property_merge_events` (no decisions, no unmerge/split history, not
-- even counts), and nothing here mirrors or reconciles against its contents. Dropping the
-- flag would only guarantee the future write path needs a migration after all; it stays
-- false on the zero rows this program writes.
create table if not exists autodedup.merges (
  merge_group_id       uuid primary key,
  cluster_key          bigint,
  listing_lo           bigint,
  listing_hi           bigint,
  survivor_property_id bigint,
  retired_property_id  bigint,
  score                real,
  generation           text not null,
  model_version        text,
  feature_version      smallint,
  written_at           timestamptz not null default now(),
  reverted_at          timestamptz,
  reverted_reason      text,
  plausibility_flag    text,
  stamped              boolean not null default false,
  constraint autodedup_merges_order_ck check (listing_lo < listing_hi)
);
create index if not exists autodedup_merges_written_idx on autodedup.merges (written_at desc);
create index if not exists autodedup_merges_unstamped_idx on autodedup.merges (merge_group_id)
  where not stamped;
create index if not exists autodedup_merges_flagged_idx on autodedup.merges (plausibility_flag)
  where plausibility_flag is not null;

-- Operator decisions from the validation UI. One per (pair | cluster, decider), enforced
-- by the two partial unique indexes: re-deciding UPSERTs rather than stacking.
create table if not exists autodedup.verdicts (
  id          bigserial primary key,
  kind        text not null check (kind in ('pair','cluster')),
  cluster_key bigint,
  listing_lo  bigint,
  listing_hi  bigint,
  verdict     text not null check (verdict in
                ('same','different','same_building_different_unit','unsure')),
  weight      real not null default 1.0,
  note        text,
  decided_by  text not null,
  decided_at  timestamptz not null default now(),
  constraint autodedup_verdicts_order_ck check (listing_lo < listing_hi)
);
create index if not exists autodedup_verdicts_at_idx on autodedup.verdicts (decided_at desc);
create unique index if not exists autodedup_verdicts_pair_uidx on autodedup.verdicts
  (kind, listing_lo, listing_hi, decided_by) where kind = 'pair';
create unique index if not exists autodedup_verdicts_cluster_uidx on autodedup.verdicts
  (kind, cluster_key, decided_by) where kind = 'cluster';

-- Training labels. operator 1.0 > gold_llm 0.6 > cheap_llm 0.3 > rule_certain (bootstrap).
-- Nothing here is seeded from the removed legacy engine's frozen sets (CLAUDE.md rule 15,
-- ruling D7). The train/calibrate/test split is BY CLUSTER, never by pair — hence
-- `cluster_key` on a label row.
create table if not exists autodedup.labels (
  listing_lo  bigint not null,
  listing_hi  bigint not null,
  source      text   not null check (source in ('operator','gold_llm','cheap_llm','rule_certain')),
  label       smallint not null check (label in (0, 1)),
  confidence  real,
  votes       jsonb,
  split       text check (split in ('train','calibrate','test')),
  cluster_key bigint,
  created_at  timestamptz not null default now(),
  primary key (listing_lo, listing_hi, source),
  constraint autodedup_labels_order_ck check (listing_lo < listing_hi)
);
create index if not exists autodedup_labels_split_idx on autodedup.labels (split, source);

-- `thresholds` is PER STRATUM ({stratum: {t_hi, t_lo, precision_lb, n}}, ruling D10): a
-- stratum that cannot prove its precision bar ships propose-only, which is a threshold
-- value and not a code branch.
create table if not exists autodedup.models (
  version         text primary key,
  kind            text not null,
  feature_vocab   jsonb not null,
  feature_version smallint not null,
  weights         jsonb not null,
  intercept       double precision,
  calibration     jsonb,
  thresholds      jsonb,
  metrics         jsonb,
  trained_on      jsonb,
  is_active       boolean not null default false,
  created_at      timestamptz not null default now()
);
create unique index if not exists autodedup_models_active_uidx on autodedup.models (is_active)
  where is_active;

-- The sealed holdout: written once, never re-sampled, so a later recalibration cannot
-- quietly grade itself on its own training data. `sampling_rate` is kept per row so
-- recall can be Horvitz-Thompson weighted back to the population.
create table if not exists autodedup.eval_samples (
  id            bigserial primary key,
  stratum       text   not null,
  listing_lo    bigint not null,
  listing_hi    bigint not null,
  sampling_rate double precision not null,
  gold_label    smallint,
  engine_zone   text,
  engine_score  real,
  sealed_at     timestamptz not null default now(),
  constraint autodedup_eval_order_ck check (listing_lo < listing_hi)
);
create unique index if not exists autodedup_eval_pair_uidx
  on autodedup.eval_samples (listing_lo, listing_hi);
create index if not exists autodedup_eval_stratum_idx on autodedup.eval_samples (stratum);

------------------------------------------------------------------
-- realtime
------------------------------------------------------------------

-- Drained `for update skip locked`, the same idiom listing_detail_queue uses (CLAUDE.md
-- rule 19). Producers: an enqueue beside the existing dirty_properties enqueue on a
-- relevant change, plus a backstop sweep over listing_fp rows whose fp_version is stale.
create table if not exists autodedup.resolve_queue (
  listing_id  bigint primary key,
  enqueued_at timestamptz not null default now(),
  reason      text,
  priority    real not null default 0,
  attempts    smallint not null default 0,
  last_error  text,
  poisoned    boolean not null default false,
  claimed_at  timestamptz
);
create index if not exists autodedup_resolve_queue_claim_idx on autodedup.resolve_queue
  (priority desc, enqueued_at) where claimed_at is null and not poisoned;

create table if not exists autodedup.scan_cursor (
  name             text primary key,
  last_listing_id  bigint,
  last_snapshot_id bigint,
  watermark        timestamptz,
  updated_at       timestamptz not null default now()
);

-- The judge's own queue, so a provider outage never blocks the resolve pass.
create table if not exists autodedup.judge_queue (
  listing_lo  bigint not null,
  listing_hi  bigint not null,
  tier        text   not null check (tier in ('text','vision','gold')),
  priority    real   not null default 0,
  enqueued_at timestamptz not null default now(),
  claimed_at  timestamptz,
  batch_id    text,
  status      text not null default 'pending',
  primary key (listing_lo, listing_hi, tier),
  constraint autodedup_judge_queue_order_ck check (listing_lo < listing_hi)
);
create index if not exists autodedup_judge_queue_claim_idx on autodedup.judge_queue
  (priority desc, enqueued_at) where claimed_at is null;

------------------------------------------------------------------
-- RLS posture. Every base table, no exceptions: this project's default privileges
-- auto-GRANT on CREATE, and tests/test_migration_rls_grants.py rule 3 fails the
-- migration without the RLS line. Zero policies by design — service-role only.
------------------------------------------------------------------

alter table autodedup.settings           enable row level security;
alter table autodedup.runs               enable row level security;
alter table autodedup.iterations         enable row level security;
alter table autodedup.listing_fp         enable row level security;
alter table autodedup.image_band         enable row level security;
alter table autodedup.phash_pop          enable row level security;
alter table autodedup.exploded_blocks    enable row level security;
alter table autodedup.pairs              enable row level security;
alter table autodedup.judgements         enable row level security;
alter table autodedup.must_not_link      enable row level security;
alter table autodedup.clusters           enable row level security;
alter table autodedup.cluster_members    enable row level security;
alter table autodedup.cluster_conflicts  enable row level security;
alter table autodedup.merges             enable row level security;
alter table autodedup.verdicts           enable row level security;
alter table autodedup.labels             enable row level security;
alter table autodedup.models             enable row level security;
alter table autodedup.eval_samples       enable row level security;
alter table autodedup.resolve_queue      enable row level security;
alter table autodedup.scan_cursor        enable row level security;
alter table autodedup.judge_queue        enable row level security;

revoke all on autodedup.settings           from anon, authenticated;
revoke all on autodedup.runs               from anon, authenticated;
revoke all on autodedup.iterations         from anon, authenticated;
revoke all on autodedup.listing_fp         from anon, authenticated;
revoke all on autodedup.image_band         from anon, authenticated;
revoke all on autodedup.phash_pop          from anon, authenticated;
revoke all on autodedup.exploded_blocks    from anon, authenticated;
revoke all on autodedup.pairs              from anon, authenticated;
revoke all on autodedup.judgements         from anon, authenticated;
revoke all on autodedup.must_not_link      from anon, authenticated;
revoke all on autodedup.clusters           from anon, authenticated;
revoke all on autodedup.cluster_members    from anon, authenticated;
revoke all on autodedup.cluster_conflicts  from anon, authenticated;
revoke all on autodedup.merges             from anon, authenticated;
revoke all on autodedup.verdicts           from anon, authenticated;
revoke all on autodedup.labels             from anon, authenticated;
revoke all on autodedup.models             from anon, authenticated;
revoke all on autodedup.eval_samples       from anon, authenticated;
revoke all on autodedup.resolve_queue      from anon, authenticated;
revoke all on autodedup.scan_cursor        from anon, authenticated;
revoke all on autodedup.judge_queue        from anon, authenticated;

-- The bigserial sequences behind runs / iterations / cluster_conflicts / verdicts /
-- eval_samples: same posture as their tables.
revoke all on all sequences in schema autodedup from anon, authenticated;

reset lock_timeout;
