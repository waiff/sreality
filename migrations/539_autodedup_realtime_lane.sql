-- 539 — the real-time SHADOW lane's own state (PROGRAM.md E65/E66/E67, rules D4/D8).
--
-- Additive and idempotent. NOTHING here touches a table outside schema `autodedup`: ruling D8
-- forbids DDL on the shared hot tables, and the six retrieval probes are over DERIVED keys
-- (a simhash band, a pHash band, `floor(ln(area)/w)`, a cohort price decile) that no index on
-- `public.listings` could serve however it were written. The lane's reads of `public` are the
-- three index-served watermark cursors (`listings_pkey`, `listing_snapshots_pkey`,
-- `listings_inactive_at_idx`) plus id-keyed fact fetches; its writes are all below.
--
-- Four new relations and four columns:
--   * `fp_key`          — the posting lists the six probes look up, keyed by GENERATION,
--                          because a probe key is a function of the frozen calibration
--                          (K3's price decile) and two generations must not share one.
--   * `rt_calibration`  — E65: the six cohort-relative inputs of a decision, frozen so a
--                          pair's score cannot depend on WHEN it was scored.
--   * `rt_block_cell`   — the live census E64's rail reads, as a counter plus three capped
--                          sets (the three cardinalities are reported, never read by a rule).
--   * `rt_lease`        — mutual exclusion by lease-row CAS. NEVER `pg_advisory_lock`: a
--                          session lock strands over the transaction pooler.
--   * `pairs.from_lo` / `pairs.from_hi` — E66's bookkeeping. A pair is kept while EITHER side
--                          retrieves the other, which is what makes the fan-out cap
--                          order-insensitive; one boolean per direction is the whole mechanism.
--   * `pairs.evidence` / `pairs.context` — the strings a rule refused or certified on, and the
--                          census a promotion was taken under (E64 replays THAT census, never
--                          today's).
--   * `listing_fp.fp_digest` — what a re-score depends on. Equal digest, no re-decide: that is
--                          the lane's idempotence, and it is a column rather than a
--                          recomputation because the cheap answer has to be the common one.
--
-- NOT APPLIED by this branch. The lane is dark by default (`AUTODEDUP_REALTIME_ENABLED`), and
-- shadow mode's own kill switch (`autodedup_write_enabled`, E39/D4) is unchanged: no row of
-- `public.listings` and no `property_id` is ever written from here.

set lock_timeout = '5s';

------------------------------------------------------------------
-- probe postings
------------------------------------------------------------------

-- One row per (generation, probe, key, listing). The key is the probe's tuple flattened to
-- text with a unit separator, because the seven probe slots carry different arities and types
-- and a per-probe column set would be seven tables. Retrieval is `where generation = $1 and
-- probe = $2 and key_token = $3 order by listing_id` — the primary key, in the ascending
-- listing-id order the cohort pass fills its fan-out cap in (E17), so the two paths cannot
-- disagree about which candidate the cap discards.
create table if not exists autodedup.fp_key (
  generation  text   not null,
  probe       text   not null,
  key_token   text   not null,
  listing_id  bigint not null,
  primary key (generation, probe, key_token, listing_id)
);

-- A listing's keys are deleted whole and rewritten on every fingerprint change, so the lane
-- needs the reverse direction as much as the forward one.
create index if not exists autodedup_fp_key_listing_idx
  on autodedup.fp_key (generation, listing_id);

------------------------------------------------------------------
-- the frozen calibration (E65)
------------------------------------------------------------------

-- `payload` holds the six statistics inline while they fit; above that the lane writes
-- `artifact_url` and leaves the payload null, because a corpus-wide token frequency table at
-- 872k listings is an artifact, not a row. `digest` is a content hash of whichever of the two
-- carries it, and every pair row of the generation is stamped with it — a decision read back
-- under a different calibration is a decision nobody took.
create table if not exists autodedup.rt_calibration (
  generation    text        primary key,
  digest        text        not null,
  n_listings    integer     not null default 0,
  payload       jsonb,
  artifact_url  text,
  settings      jsonb,
  model_version text,
  built_at      timestamptz not null default now()
);

------------------------------------------------------------------
-- the live census (E64)
------------------------------------------------------------------

-- `n_listings` is EXACT — it is the only member `fungible_catalogue` and `rail_reopen` read.
-- The three sets behind `n_unit_shapes` / `n_brokers` / `n_source_native_ids` are capped and
-- the row says when a cap bound: those three feed `is_shape_stack`, which W8's verification
-- measured INERT as a warrant clause and which no rule floor reads.
create table if not exists autodedup.rt_block_cell (
  generation     text    not null,
  cell_key       text    not null,
  category_group text    not null,
  n_listings     integer not null default 0,
  shapes         jsonb   not null default '[]'::jsonb,
  brokers        jsonb   not null default '[]'::jsonb,
  source_ids     jsonb   not null default '[]'::jsonb,
  capped         boolean not null default false,
  updated_at     timestamptz not null default now(),
  primary key (generation, cell_key, category_group)
);

------------------------------------------------------------------
-- mutual exclusion
------------------------------------------------------------------

-- Lease-row CAS (the `location_data/resolver/lease.py` pattern), never a session advisory
-- lock: this lane runs behind the transaction pooler, where a session lock strands on a
-- connection the pool hands to somebody else. The same lease lets a scheduled Actions run and
-- a future worker lane share the code without overlapping.
create table if not exists autodedup.rt_lease (
  name       text        primary key,
  holder     text,
  taken_at   timestamptz not null default now(),
  expires_at timestamptz not null
);

------------------------------------------------------------------
-- pair-grain columns
------------------------------------------------------------------

alter table autodedup.pairs
  add column if not exists from_lo   boolean,
  add column if not exists from_hi   boolean,
  add column if not exists evidence  jsonb,
  add column if not exists context   jsonb,
  add column if not exists calibration_digest text;

comment on column autodedup.pairs.from_lo is
  'E66: the LOW side''s own retrieval reached the high side. A pair is kept while either '
  'direction holds, which is what makes the fan-out cap order-insensitive.';
comment on column autodedup.pairs.from_hi is
  'E66: the HIGH side''s own retrieval reached the low side.';
comment on column autodedup.pairs.context is
  'E64: the census this decision was taken under — replayed as stored, never re-read from '
  'today''s census.';

alter table autodedup.listing_fp
  add column if not exists fp_digest  text,
  add column if not exists generation text;

comment on column autodedup.listing_fp.fp_digest is
  'What a re-score depends on: equal digest and equal probe keys, no re-decide. The lane''s '
  'idempotence rail.';

------------------------------------------------------------------
-- RLS posture. Every new base table, no exceptions (tests/test_migration_rls_grants.py).
-- Zero policies by design — service-role only, exactly as migration 528 left the schema.
------------------------------------------------------------------

alter table autodedup.fp_key         enable row level security;
alter table autodedup.rt_calibration enable row level security;
alter table autodedup.rt_block_cell  enable row level security;
alter table autodedup.rt_lease       enable row level security;

revoke all on autodedup.fp_key         from anon, authenticated;
revoke all on autodedup.rt_calibration from anon, authenticated;
revoke all on autodedup.rt_block_cell  from anon, authenticated;
revoke all on autodedup.rt_lease       from anon, authenticated;

revoke all on all sequences in schema autodedup from anon, authenticated;

reset lock_timeout;
