-- 539 — the real-time SHADOW lane's own state (PROGRAM.md E70/E71/E72, rules D4/D8).
--
-- Additive and idempotent. NOTHING here touches a table outside schema `autodedup`: ruling D8
-- forbids DDL on the shared hot tables, and the six retrieval probes are over DERIVED keys
-- (a simhash band, a pHash band, `floor(ln(area)/w)`, a cohort price decile) that no index on
-- `public.listings` could serve however it were written. The lane's reads of `public` are the
-- three index-served watermark cursors (`listings_pkey`, `listing_snapshots_pkey`,
-- `listings_inactive_at_idx`) plus id-keyed fact fetches; its writes are all below.
--
-- Eight new relations and seven columns:
--   * `fp_key`          — the posting lists the six probes look up, keyed by GENERATION,
--                          because a probe key is a function of the frozen calibration
--                          (K3's price decile) and two generations must not share one.
--   * `rt_fp`           — one row per listing of a generation: the five guard columns E17's
--                          rule floor reads, the re-score digest, the census cell the listing
--                          is counted in and the activity flag the revive sweep anti-joins.
--                          NOT migration 528's `listing_fp`, which is keyed on `listing_id`
--                          alone (so it cannot hold two generations) and which no lane has
--                          ever written.
--   * `rt_calibration`  — E70: the six cohort-relative inputs of a decision, frozen so a
--                          pair's score cannot depend on WHEN it was scored.
--   * `rt_block_cell`   — the live census E64's rail reads, as a counter plus three capped
--                          sets (the three cardinalities are reported, never read by a rule).
--   * `rt_lease`        — mutual exclusion by lease-row CAS. NEVER `pg_advisory_lock`: a
--                          session lock strands over the transaction pooler.
--   * `rt_scope_ids`    — the scope's membership SNAPSHOT, so an ordinary pass claims
--                          entrants without reading `public` at all (W9e/R3: the quarter's
--                          sweep was 231 MB of cold heap reads ~48 times a day).
--   * `rt_scope_scan`   — the scan ledger the entrant cadence and its rolling-day cap read.
--   * `rt_retire_event` — the retirement ledger W9e's rail measures a rolling day against.
--   * `pairs.from_lo` / `pairs.from_hi` — E71's bookkeeping. A pair is kept while EITHER side
--                          retrieves the other, which is what makes the fan-out cap
--                          order-insensitive; one boolean per direction is the whole mechanism.
--   * `pairs.evidence` / `pairs.context` — the strings a rule refused or certified on, and the
--                          census a promotion was taken under (E64 replays THAT census, never
--                          today's).
--   * `pairs.certificate` / `pairs.fp_lo` / `pairs.fp_hi` — the certificate is a COLUMN and not
--                          a parse of `decision`, because E63 can re-promote a certified pair
--                          under a `context_rule:` reason and E33 orders a component's edges
--                          certificate-first: a lane that clustered from stored rows without it
--                          would union in a different order than the cohort pass. The two
--                          digests are what a re-score depends on — equal digest, no re-decide.
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
-- the fingerprint row
------------------------------------------------------------------

-- Everything a pass reads about a listing WITHOUT a fact fetch. `cell_key`/`cell_group` are
-- stored rather than re-derived: a listing that moves (a resolved location, a category
-- correction) has to unbump the cell it LEFT, and `n_listings` is the exact counter
-- `fungible_catalogue` and E64's rail read.
create table if not exists autodedup.rt_fp (
  generation    text   not null,
  listing_id    bigint not null,
  category_main text,
  category_type text,
  area_m2       double precision,
  disposition   text,
  floor         integer,
  fp_digest     text,
  cell_key      text,
  cell_group    text,
  is_active     boolean not null default true,
  updated_at    timestamptz not null default now(),
  primary key (generation, listing_id)
);

-- The revive sweep's slice: `touch_listings` sets `is_active = true, inactive_at = null` on
-- the same id and appends no snapshot (rule #2), so a revived advert is invisible to all three
-- forward cursors. The fourth feed pages over THIS index and anti-joins the live flag.
create index if not exists autodedup_rt_fp_inactive_idx
  on autodedup.rt_fp (generation, listing_id) where is_active = false;

------------------------------------------------------------------
-- the frozen calibration (E70)
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
-- the scope's own membership, and the two ledgers that rail it (W9e)
------------------------------------------------------------------

-- WHY a side table rather than the sweep reading `public` every pass. The entrant sweep — the
-- feed that claims a listing whose location resolved INTO the scope after its arrival window
-- had passed — has no index-served path to a QUARTER: `listing_location` carries a btree on
-- `(obec_kod, granularity)` and NONE on `cast_obce_kod`, and D8 forbids adding one. Measured
-- live on the trial scope, the quarter's block (Praha-Vysocany 490245 through its parent obec
-- 554782) is a 29,265-block bitmap heap scan — 231 MB, 6.4 s, cold every time — and W9d ran it
-- once every three passes, ~48 times a day: ~11 GB a day of cold reads on the instance that
-- serves Browse, whether or not anything entered. This table is the membership SNAPSHOT that
-- scan produces, so an ordinary pass claims entrants with an anti-join inside schema
-- `autodedup` and reads NOTHING in `public`; the wide scan runs on a cadence that is data
-- (`rt_enter_interval_hours`) under a rolling-day cap that is data
-- (`rt_enter_max_scans_per_day`). `resolved_at` rides along because the scan already reads the
-- row: it is what gives the entrant feed the same settle lag every other feed has.
create table if not exists autodedup.rt_scope_ids (
  generation   text   not null,
  block_key    text   not null,
  listing_id   bigint not null,
  resolved_at  timestamptz,
  refreshed_at timestamptz not null default now(),
  primary key (generation, block_key, listing_id)
);

-- The claim path: this generation's snapshot in listing-id order, anti-joined against `rt_fp`.
create index if not exists autodedup_rt_scope_ids_claim_idx
  on autodedup.rt_scope_ids (generation, listing_id);

-- The scan ledger. Append-only, and the only thing that can answer the two questions the
-- cadence asks: when was THIS block last walked, and how many walks has the generation spent
-- in the last rolling day. A settings row could hold neither without being rewritten by every
-- pass.
create table if not exists autodedup.rt_scope_scan (
  id         bigserial   primary key,
  generation text        not null,
  block_key  text        not null,
  scanned_at timestamptz not null default now(),
  rows_found integer     not null default 0,
  elapsed_ms double precision
);

create index if not exists autodedup_rt_scope_scan_gen_idx
  on autodedup.rt_scope_scan (generation, scanned_at desc);

-- The retirement ledger (W9e). W9d's rail compared the departed count of ONE drift SLICE with
-- a fraction of the store, so it could only fire while the slice was at least that fraction —
-- above ~400,000 rows, or under a small `drift_slice` dispatch argument, the store could be
-- ground away a share a pass with the rail never speaking. Retirement is therefore measured
-- over a ROLLING DAY against the store as it stood when that day began, which is what this
-- table remembers: one row per pass that retired anything, with the store size the rail
-- measured itself against.
create table if not exists autodedup.rt_retire_event (
  id         bigserial   primary key,
  generation text        not null,
  retired_at timestamptz not null default now(),
  n_retired  integer     not null default 0,
  store_rows bigint      not null default 0
);

create index if not exists autodedup_rt_retire_event_gen_idx
  on autodedup.rt_retire_event (generation, retired_at desc);

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
  add column if not exists from_lo     boolean,
  add column if not exists from_hi     boolean,
  add column if not exists evidence    jsonb,
  add column if not exists context     jsonb,
  add column if not exists certificate text,
  add column if not exists fp_lo       text,
  add column if not exists fp_hi       text,
  add column if not exists calibration_digest text;

comment on column autodedup.pairs.from_lo is
  'E71: the LOW side''s own retrieval reached the high side. A pair is kept while either '
  'direction holds, which is what makes the fan-out cap order-insensitive.';
comment on column autodedup.pairs.from_hi is
  'E71: the HIGH side''s own retrieval reached the low side.';
comment on column autodedup.pairs.context is
  'E64: the census this decision was taken under — replayed as stored, never re-read from '
  'today''s census.';
comment on column autodedup.pairs.certificate is
  'The certificate the decision carried (K-A/K-B/K-C/K-R), as a COLUMN: E63 can re-promote a '
  'certified pair under a context_rule reason, so the decision string is lossy, and E33 '
  'orders a component''s edges certificate-first.';
comment on column autodedup.pairs.fp_lo is
  'What a re-score depends on: equal digests on both sides and the pair is not re-decided. '
  'The lane''s idempotence rail.';

-- The census cell of a merge, so E64''s rail can read the blocks a pass touched without
-- scanning the generation.
create index if not exists autodedup_pairs_gen_ctx_block_idx
  on autodedup.pairs (generation, (context ->> 'block')) where zone = 'merge';

------------------------------------------------------------------
-- RLS posture. Every new base table, no exceptions (tests/test_migration_rls_grants.py).
-- Zero policies by design — service-role only, exactly as migration 528 left the schema.
------------------------------------------------------------------

alter table autodedup.fp_key         enable row level security;
alter table autodedup.rt_fp          enable row level security;
alter table autodedup.rt_calibration enable row level security;
alter table autodedup.rt_block_cell  enable row level security;
alter table autodedup.rt_lease       enable row level security;
alter table autodedup.rt_scope_ids    enable row level security;
alter table autodedup.rt_scope_scan   enable row level security;
alter table autodedup.rt_retire_event enable row level security;

revoke all on autodedup.fp_key         from anon, authenticated;
revoke all on autodedup.rt_fp          from anon, authenticated;
revoke all on autodedup.rt_calibration from anon, authenticated;
revoke all on autodedup.rt_block_cell  from anon, authenticated;
revoke all on autodedup.rt_lease       from anon, authenticated;
revoke all on autodedup.rt_scope_ids    from anon, authenticated;
revoke all on autodedup.rt_scope_scan   from anon, authenticated;
revoke all on autodedup.rt_retire_event from anon, authenticated;

revoke all on all sequences in schema autodedup from anon, authenticated;

reset lock_timeout;
