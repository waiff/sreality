-- 320_child_listing_id_ingest.sql
-- R2 Phase A1 of the listing-identity refactor, file 1 of 6
-- (docs/design/listing-identity-r2-pk-swap-runbook.md § 2).
-- Additive: the clean listing_id FK column on the three HOT ingest children.
--
-- Every statement is catalog-only (a nullable column with NO default never rewrites
-- the table), but each ALTER still takes a brief ACCESS EXCLUSIVE lock that contends
-- with the always-on realtime worker — hence the short lock_timeout: fail fast and
-- retry rather than park at the head of the table's lock queue and block ingest
-- behind us. The A1 files are split by table group for the same reason: one
-- transaction must not hold ACCESS EXCLUSIVE on 25 tables at once.
--
-- NO foreign key here. The FK to listings(id) is added NOT VALID -> VALIDATE in
-- Phase B, only after dual-write (Phase A2) and the backfill (Phase A4) have run.
-- Ordering is load-bearing: backfilling before dual-write ships can never converge,
-- because the worker keeps inserting fresh NULL-listing_id rows.
--
-- The partial "WHERE listing_id IS NULL" indexes that make the parity check
-- index-only are deliberately NOT here. Right now they would match every row
-- (nothing is backfilled yet) — an 8M-entry index built only to drain to empty.
-- They are created in Phase B, after the backfill, when they hold ~zero rows and
-- build in seconds. The backfill itself keyset-paginates the child PK and needs no
-- new index.

SET lock_timeout = '3s';

ALTER TABLE images ADD COLUMN IF NOT EXISTS listing_id bigint;
ALTER TABLE listing_snapshots ADD COLUMN IF NOT EXISTS listing_id bigint;
ALTER TABLE listing_videos ADD COLUMN IF NOT EXISTS listing_id bigint;
