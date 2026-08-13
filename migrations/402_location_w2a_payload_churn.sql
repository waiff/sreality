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
-- Bounded by construction: the natural PK is (source, source_id_native, page_kind),
-- so a portal refetched four times a day for a week writes ONE row, not 28.
--
-- Backend/service-role only: RLS on, Supabase's default anon/authenticated ACL
-- revoked at the foot of the file. No sequence to revoke (natural PK, no serial).

begin;

------------------------------------------------------------------
-- portal_payload_churn - one counter row per fetched artefact.
--
-- `fetches` counts every fetch the instrument saw; `raw_changes` / `norm_changes`
-- count the fetches whose hash DIFFERED from the previously recorded one, so the
-- first sighting of a key contributes a fetch and zero changes. The ratio
-- norm_changes::float / nullif(fetches - 1, 0) per (source, page_kind) is the
-- number 02 section 2.3.2's storage projection is missing, and the raw-vs-norm
-- gap is what tells us whether a volatile profile is worth its complexity.
--
-- `normalizer_version` stamps which normaliser produced last_norm_sha256, so a
-- profile change is visible as a version bump rather than as a phantom change
-- spike (rows carrying the old version are simply excluded from the readout).
------------------------------------------------------------------

create table portal_payload_churn (
  source              text not null,
  source_id_native    text not null,
  page_kind           location_page_kind not null,
  first_seen_at       timestamptz not null,
  last_seen_at        timestamptz not null,
  fetches             int not null default 0,
  raw_changes         int not null default 0,
  norm_changes        int not null default 0,
  last_raw_sha256     bytea,
  last_norm_sha256    bytea,
  last_byte_size      int,
  last_norm_byte_size int,
  normalizer_version  text,
  primary key (source, source_id_native, page_kind)
);
alter table portal_payload_churn enable row level security;

-- The readout is always per (source, page_kind) over the whole table; the PK's
-- leading column already serves it, so this index exists only to keep the
-- normalizer_version cohort split cheap while a profile change is rolling out.
create index ppc_by_surface on portal_payload_churn (source, page_kind, normalizer_version);

revoke all on portal_payload_churn from anon, authenticated;

commit;
