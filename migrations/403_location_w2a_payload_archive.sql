-- 403_location_w2a_payload_archive.sql
--
-- Location-data program, Wave W2a, PR W2a-1: the write-path columns the
-- append-on-change payload archive needs, plus its retention index.
--
-- Design: design/final/02-portal-contracts.md section 2.3.2 P1 (content-addressed
-- on a NORMALISED body; the raw-byte hash is kept "for forensics but is NOT the
-- uniqueness key") and P4 (version cap + `pinned`, enforced by the writer in the
-- same transaction as the append); design/final/01-schema.md section 4.0, which
-- owns the CREATE TABLE and states that "Section 02 owns the write path ... either
-- may add columns, but the identity (payload_sha256 unique per (source,
-- source_id_native, page_kind)) and the append-on-change discipline are normative
-- here"; design/final/06-migration-backfill.md W2a gate (b).
--
-- ADDITIVE ALTER, NOT A CREATE. portal_raw_payloads already exists (migration 382,
-- the W1 claims file) with its identity constraint and its prp_body_present CHECK
-- already in force, and location_claims.payload_id already FKs to it. This file
-- adds only what the writer needs. Two consequences of that, both deliberate:
--
--   * The 382 timestamp spellings STAND. 02 P1's prose calls them first_seen_at /
--     last_seen_at; 01 section 4.0 - the sole DDL owner - calls them
--     first_observed_at / last_observed_at, and that is what is applied. Renaming
--     an applied column to match prose is not an additive migration and buys
--     nothing.
--   * Nothing is backfilled. Verified against production 2026-08-13: the table
--     carries exactly 382's fifteen columns and ZERO rows, so every ADD COLUMN
--     below is a catalogue-only change - no rewrite, no default-fill cost, and the
--     NOT NULL DEFAULT columns are free even on a future large table (PG11+ stores
--     the default in the catalogue).
--
-- Backend/service-role only. RLS was enabled on the table by 382 and ADD COLUMN
-- does not touch it, but ACLs are re-asserted at the foot of the file: this project
-- has been bitten before by an object reachable from the browser roles, and the
-- re-assert is a no-op when the 382 revoke is already in force.

begin;

------------------------------------------------------------------
-- The write-path columns (02 section 2.3.2 P1 + P4).
------------------------------------------------------------------

alter table portal_raw_payloads
  add column if not exists body_sha256        bytea,
  add column if not exists content_encoding   text not null default 'identity',
  add column if not exists http_status        integer,
  add column if not exists version_seq        integer,
  add column if not exists pinned             boolean not null default false,
  add column if not exists normalizer_version text;

alter table portal_raw_payloads
  add constraint prp_content_encoding
  check (content_encoding in ('identity', 'gzip'));

------------------------------------------------------------------
-- The P4 retention index.
--
-- The pruner's question is always "within ONE (source, source_id_native,
-- page_kind) group, which unpinned bodies fall outside the newest `version_cap`
-- versions" - so the key is the group's three columns followed by version_seq
-- DESC, and the partial predicate keeps the first/latest/disputed rows (which are
-- never eviction candidates) out of the index entirely. The three indexes 382
-- already ships all lead on first_observed_at, which orders by OBSERVATION rather
-- than by version and would make the cap a sort instead of a range scan.
--
-- `where not pinned` is index-legal because `pinned` is NOT NULL DEFAULT false.
------------------------------------------------------------------

create index if not exists prp_retention on portal_raw_payloads
  (source, source_id_native, page_kind, version_seq desc)
  where not pinned;

------------------------------------------------------------------
-- Column semantics that are NOT self-evident from the names.
--
-- payload_sha256 first, because the design corpus itself is stale on it: 01
-- section 4.0's DDL block annotates it "sha256 of `body`", while 02 section 2.3.2
-- P1 - which owns the write path and is the later, normative statement - requires
-- the hash to be taken over a NORMALISED projection, "never over the bytes as
-- fetched". 00 section 3.3 declines to define hashing and defers to 02, so 02
-- wins. Migration 382 shipped the column with no comment at all, so this is the
-- first place the applied schema states which of the two it is. It matters
-- operationally: it is the difference between an archive bounded by real content
-- change and one that appends a row every time an ad slot or a CSRF token rerolls.
------------------------------------------------------------------

comment on column portal_raw_payloads.payload_sha256 is
  'sha256 of the NORMALISED body (the contract''s volatile_paths stripped, key '
  'order and whitespace canonicalised) - 02 section 2.3.2 P1, computed by '
  'location_data.payload_norm.normalise. This is the content address and the '
  'uniqueness key: an unchanged refetch collides here and only bumps '
  'last_observed_at. The raw-byte hash is body_sha256.';

comment on column portal_raw_payloads.body_sha256 is
  'sha256 of the body exactly as fetched, before normalisation. FORENSICS ONLY - '
  'never the uniqueness key (02 section 2.3.2 P1). Two fetches that differ only in '
  'volatile bytes share one row and one payload_sha256, and this column then '
  'carries the hash of whichever raw body was observed first.';

comment on column portal_raw_payloads.content_encoding is
  'How `body` / the R2 object is encoded: ''identity'' or ''gzip''. byte_size is '
  'always the DECODED length, so a round-trip verifier decodes by this column and '
  'compares against byte_size.';

comment on column portal_raw_payloads.byte_size is
  'Length of the DECODED body in bytes - the artefact as fetched, independent of '
  'content_encoding and of whether the body sits inline or in R2.';

comment on column portal_raw_payloads.version_seq is
  'Per (source, source_id_native, page_kind) append counter, 1-based, assigned by '
  'the writer as coalesce(max(version_seq), 0) + 1 in the same statement as the '
  'INSERT. It orders versions for the P4 cap; it is NOT globally unique and it is '
  'not an identity - id remains the key.';

comment on column portal_raw_payloads.pinned is
  'P4 (02 section 2.3.2): this body is exempt from the version cap because it is '
  'the FIRST version, the LATEST version, or is referenced by a claim carried by '
  'an open location_contradictions row. Recomputed authoritatively by the writer on '
  'every append - a row that stops being the latest must lose its pin, or the cap '
  'never bites.';

comment on column portal_raw_payloads.http_status is
  'HTTP status of the fetch that produced this body. A non-200 body is still a '
  'body: it is what a span mined from it indexes into.';

comment on column portal_raw_payloads.normalizer_version is
  'location_data.payload_norm.NORMALIZER_VERSION at write time. A profile change '
  'moves payload_sha256 for unchanged content, so a version bump appends one row '
  'per artefact - this column is what makes that cohort identifiable afterwards '
  'rather than indistinguishable from real churn.';

------------------------------------------------------------------
-- ADD COLUMN does not change a relation's ACL, so 382's revoke still holds. Cheap
-- insurance, and it keeps the grant posture readable from this file alone.
------------------------------------------------------------------

revoke all on portal_raw_payloads from anon, authenticated;
revoke all on sequence portal_raw_payloads_id_seq from anon, authenticated;

commit;
