-- 402_location_w2a_payload_churn.sql
--
-- Location-data program, Wave W2a, PR W2a-0: the shadow-hash churn instrument.
--
-- Design: design/final/02-portal-contracts.md section 2.3.2 P1 ("payload_sha256
-- is computed over a normalised projection of the body, never over the bytes as
-- fetched" — and the gate: "Measure churn before P2 is enabled — this is a gate,
-- not a preference") and design/final/06-migration-backfill.md section 6.9 OQ9
-- ("One week of shadow hashing before the W2a cutover turns the assumption into
-- a number"). The counters are writer-computed, never GENERATED (01 section 0.4).
--
-- This is an INSTRUMENT, not the archive: no body is ever stored here, only the
-- two competing hashes, the two byte sizes and three counters. The append-on-change
-- payload store (portal_raw_payloads, migration 382) is what this measurement
-- decides the volatile_paths and the P2 index-archiving switch for.
--
-- ONE ROW PER (source, source_id_native, page_kind, normalizer_version): a portal
-- refetched four times a day for a week writes ONE row, not 28. Two caveats on
-- that bound, both deliberate:
--   * index keys are WEEK-STAMPED upstream (…/{offset}/{week}), so the index
--     surface adds O(index pages) rows per week for as long as the flag is on.
--     This table is observability, not history (the same posture rule 9 gives
--     listing_freshness_checks): once a readout is taken, rows whose last_seen_at
--     is older than the window being reported are safe to DELETE, and the whole
--     table is safe to TRUNCATE when the instrument is switched off. There is no
--     auto-prune and nothing reads it but the operator's readout query.
--   * a normaliser change starts a NEW row per key rather than relabelling the
--     old one (see normalizer_version below).
--
-- Backend/service-role only: RLS on, Supabase's default anon/authenticated ACL
-- revoked at the foot of the file. No sequence to revoke (natural PK, no serial).

begin;

------------------------------------------------------------------
-- portal_payload_churn - one counter row per fetched artefact per normaliser.
--
-- `fetches` counts every fetch the instrument saw; `raw_changes` / `norm_changes`
-- count the fetches whose hash DIFFERED from the previously recorded one, so the
-- first sighting of a key contributes a fetch and zero changes. The ratio
-- norm_changes::float / nullif(fetches - 1, 0) per (source, page_kind) is the
-- number 02 section 2.3.2's storage projection is missing, and the raw-vs-norm
-- gap is what tells us whether a volatile profile is worth its complexity.
--
-- The denominator is FETCHES, uniformly, on every surface — never archive writes.
-- Several index archivers skip re-staging a body their freshness guard would
-- discard; those fetches are still counted, because a per-fetch rate plus the
-- row's own observed interval ((last_seen_at - first_seen_at) / (fetches - 1))
-- can be scaled to any archiving cadence, while fetches never counted cannot be
-- recovered.
--
-- `normalizer_version` is part of the PK, not an updated-in-place stamp: a profile
-- change must start a CLEAN cohort. Relabelling in place would blend @1-era
-- counters into the @2 readout and register one phantom change per key on its
-- first @2 fetch (the hash moved because the normaliser moved). Old-version rows
-- are simply left behind for comparison, or deleted.
--
-- `last_observation` is the idempotency key, NOT data. The drain's batch write is
-- retried whole on a transient pooler drop (scraper.portal_runner._flush_drain_batch
-- -> db.run_resilient) and the connection is autocommit, so a replay re-presents
-- bodies already counted. Every write carries a per-FETCH token that survives the
-- replay unchanged, and the DO UPDATE is gated on it moving — so a replayed batch
-- bumps nothing. Without it `fetches` would inflate while the hashes stayed put,
-- biasing the published rate LOW exactly when the drain is flaky.
------------------------------------------------------------------

create table portal_payload_churn (
  source              text not null,
  source_id_native    text not null,
  page_kind           location_page_kind not null,
  normalizer_version  text not null,
  first_seen_at       timestamptz not null,
  last_seen_at        timestamptz not null,
  fetches             int not null default 0,
  raw_changes         int not null default 0,
  norm_changes        int not null default 0,
  last_raw_sha256     bytea,
  last_norm_sha256    bytea,
  last_byte_size      int,
  last_norm_byte_size int,
  last_observation    text not null,
  primary key (source, source_id_native, page_kind, normalizer_version)
);
alter table portal_payload_churn enable row level security;

-- The readout is always per (source, page_kind) over the whole table; the PK's
-- leading column serves only the source prefix, so this index is what keeps the
-- per-surface aggregate — and the normalizer_version cohort split while a profile
-- change is rolling out — from scanning the table.
create index ppc_by_surface on portal_payload_churn (source, page_kind, normalizer_version);

revoke all on portal_payload_churn from anon, authenticated;

commit;
