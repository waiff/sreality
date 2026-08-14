-- 405_location_w2a_payload_stored_size.sql
--
-- Location-data program, Wave W2a: one column, so that "bytes reclaimed" survives
-- bodies moving out of Postgres.
--
-- Design: design/final/02-portal-contracts.md section 2.3.2 P4.3 ("if genuine cold
-- storage is wanted, it is R2"), which the writer now takes as the DEFAULT rather
-- than as the pathological path: location_data/payloads.py spills every body whose
-- compressed form is larger than Postgres's own TOAST threshold, so `body` is NULL
-- on essentially every row and `body_r2_key` is set on essentially every row.
--
-- THE HOLE THIS CLOSES. Both retention statements - the version cap in
-- location_data/payloads.py and the cold-window sweep in
-- location_data/payload_prune.py - report what they freed as
-- `octet_length(body)`. That was honest while spilling was rare (a spilled body's
-- size genuinely was not in the row) and becomes a permanent zero the moment
-- spilling is the norm: every eviction would report 0 bytes reclaimed, on the one
-- figure the storage sign-off is read from. `byte_size` cannot stand in - it is the
-- DECODED length, ~5x the stored one, so quoting it would overstate every reclaim by
-- the compression ratio.
--
-- ADDITIVE ALTER. portal_raw_payloads exists (migration 382, columns completed by
-- 403). Verified against production 2026-08-13 and re-verified for this file: the
-- table holds ZERO rows (both dual-write flags are off and nothing has been
-- backfilled), so this is a catalogue-only change - no rewrite, no default-fill.
-- Nullable rather than NOT NULL DEFAULT 0 on purpose: NULL means "this row predates
-- the column", which the readers coalesce to octet_length(body), and 0 would be a
-- lie about a body that does exist.
--
-- NO INDEX. Nothing filters or orders by this column; it is read alongside the row
-- it belongs to, by id, in statements that have already found the row.
--
-- Backend/service-role only. RLS was enabled by 382 and ADD COLUMN does not touch
-- it; the ACL re-assert at the foot is a no-op when 382's revoke is in force, and
-- cheap insurance because this project has been bitten by an object reachable from
-- the browser roles.

begin;

alter table portal_raw_payloads
  add column if not exists stored_byte_size integer;

comment on column portal_raw_payloads.stored_byte_size is
  'Length of the body AS STORED - after content_encoding, wherever the bytes live. '
  'For a spilled body it is the size of the R2 object, which is the only place that '
  'figure exists once `body` is NULL; for an inline body it equals '
  'octet_length(body). Readers coalesce the two in that order, so a row written '
  'before this column still answers. Distinct from byte_size, which is the DECODED '
  'length and is ~5x larger for the gzipped HTML every portal serves.';

revoke all on portal_raw_payloads from anon, authenticated;

commit;
