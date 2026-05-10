-- 025_broker_fields.sql
-- Promote broker contact info from raw_json to typed columns so the
-- operator can query "all listings by broker X" without JSONB
-- extraction every time, and so the frontend can render the contact
-- card on the detail page without re-parsing JSON in the browser.
--
-- Source path: raw_json -> _embedded -> seller. Live coverage
-- (47,644 active listings sampled 2026-05-10): 96.07% have a
-- complete seller block; the remaining ~3.9% are private-seller
-- listings without an agent record and stay NULL.
--
-- Phone normalisation handled in the parser (scraper/parser.py) and
-- in the post-apply backfill step:
--   * sreality stores phones as [{code, type, number}, ...]
--   * prefer type='MOB' (mobile) over 'TEL' (landline); among same
--     type entries keep array order
--   * format as "+{code}{number}" when code is non-empty, else just
--     "{number}"
--
-- This migration is schema-only. Backfilling 45k+ rows via a single
-- jsonb_array_elements UPDATE exceeds the pgbouncer transaction
-- window, so the operator-approved apply procedure runs the backfill
-- in chunks of 1,500 rows via MCP execute_sql immediately after this
-- ALTER. See migration 022's apply log for the same pattern. The
-- backfill SQL is intentionally not embedded here so a fresh-rebuild
-- replay is fast and idempotent against an empty database.
--
-- All three columns nullable. No new indexes — broker filtering is
-- not a hot path yet; add a btree on broker_phone or broker_name
-- later if a toolkit / agent query needs it.

alter table listings
  add column if not exists broker_name  text,
  add column if not exists broker_email text,
  add column if not exists broker_phone text;
