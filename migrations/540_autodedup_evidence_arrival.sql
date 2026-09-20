-- 540 — what a real-time decision RESTED ON, so a late photograph can re-open it
-- (PROGRAM.md 11a, W9h: E92, E93, E94).
--
-- Additive and idempotent; NOT APPLIED by this branch. Nothing here touches a table outside
-- schema `autodedup` (ruling D8), and the lane stays dark (`AUTODEDUP_REALTIME_ENABLED`).
--
-- WHY. W9g fixed the frozen pHash population and the generation's scorer, and the parity gate
-- then said the live lane reads the export's facts. It says that about a listing the export
-- CARRIED. It says nothing about an arrival, and an arrival is decided before its photographs
-- exist as evidence: measured live, images land ~1 minute after `first_seen_at`, their dHash
-- is computed by an hourly job at :20 and CLIP vectors and tags by another at :40 (p50 2.47 h,
-- p90 4.51 h to a first tag), while the lane claims at `first_seen_at + 5..15 min`. About 80%
-- of arrivals are therefore decided with every `phash` NULL, 78% never get a second snapshot,
-- and NO feed was keyed on a hash or a vector arriving — so the listing was never re-decided
-- and its stored index keys never gained a pHash posting.
--
-- Four columns and a stamp make that state visible, and three indexes make the seventh feed
-- (the evidence sweep) index-served:
--   * `ev_images` / `ev_phash` / `ev_clip` / `ev_tags` — the gallery this generation's stored
--     decision was taken on: how many images existed, how many carried a pHash, how many
--     carried a CLIP vector and how many carried tags. The feed re-claims a listing when a
--     bounded probe of `public.images` disagrees with these.
--   * `ev_complete` — the derived "nothing is missing" flag, stored rather than computed in
--     the predicate so a PARTIAL index can serve the sweep's first arm.
--   * `first_decided_at` — when this generation first decided the listing, which is what the
--     horizon (`rt_evidence_horizon_hours`, default 48 h) is measured from. Set on insert and
--     PRESERVED on every later refresh, so a re-decision does not restart the clock.
--
-- The `pairs` index serves the sweep's second arm: a MERGE held in the band for want of
-- photographs carries `decision = 'evidence_pending'`, and that hold has to be re-opened when
-- the horizon passes even though no digest moved.

set lock_timeout = '5s';

------------------------------------------------------------------
-- what the decision rested on
------------------------------------------------------------------

alter table autodedup.rt_fp
  add column if not exists ev_images        integer,
  add column if not exists ev_phash         integer,
  add column if not exists ev_clip          integer,
  add column if not exists ev_tags          integer,
  add column if not exists ev_complete      boolean,
  add column if not exists first_decided_at timestamptz;

comment on column autodedup.rt_fp.ev_images is
  'E92: how many images the gallery held when this generation last decided the listing.';
comment on column autodedup.rt_fp.ev_phash is
  'E92: how many of them carried a pHash. Zero with ev_images > 0 is PENDING — the lane '
  'decided the listing before the hourly hash job reached its photographs.';
comment on column autodedup.rt_fp.ev_clip is
  'E92: how many carried a CLIP vector at decision time.';
comment on column autodedup.rt_fp.ev_tags is
  'E92: how many carried CLIP tags at decision time.';
comment on column autodedup.rt_fp.ev_complete is
  'E92: false while any of the three producers is behind the gallery. Stored rather than '
  'derived so the evidence sweep''s first arm is served by a partial index.';
comment on column autodedup.rt_fp.first_decided_at is
  'E92: when this generation FIRST decided the listing. The evidence horizon is measured from '
  'here, and a refresh preserves it — a re-decision must not restart the clock.';

-- The sweep's first arm: this generation's rows whose evidence is still incomplete. The
-- trial scope carries 76 of 4,974 (1.5%) in steady state, so the arm is a handful of rows.
create index if not exists autodedup_rt_fp_evidence_idx
  on autodedup.rt_fp (generation, listing_id) where ev_complete = false;

-- The sweep's second arm and its horizon: the rows decided recently enough that a producer
-- could still be behind them.
create index if not exists autodedup_rt_fp_decided_idx
  on autodedup.rt_fp (generation, first_decided_at);

------------------------------------------------------------------
-- the held merges
------------------------------------------------------------------

-- A merge that rests on photographs the lane does not have yet is HELD in the band with
-- `decision = 'evidence_pending'` (E93) and re-decided when the evidence lands or the horizon
-- passes. The horizon arm moves no digest, so the only way to find those pairs again is to
-- ask for them by reason.
create index if not exists autodedup_pairs_evidence_pending_idx
  on autodedup.pairs (generation, listing_lo, listing_hi)
  where decision = 'evidence_pending';

-- No new base table, so no new RLS statement is owed: `autodedup.rt_fp` and `autodedup.pairs`
-- already have row level security enabled and `anon` / `authenticated` revoked (migrations 528
-- and 539), and a column inherits the table's posture. No sequence is created here either.

reset lock_timeout;
