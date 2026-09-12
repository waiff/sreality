-- 509_location_w1a6_bodies_cursor.sql
--
-- Location-data program, W1-a6: ONE nullable column, so the bodies-first pass's
-- keyset survives the end of a run. PURELY ADDITIVE — no data change, no
-- constraint, no index, no rewrite (a nullable ADD COLUMN with no default is a
-- catalogue-only change). Safe to re-run.
--
-- WHY. The bodies pass drains `portal_raw_payloads` in 1 500-id windows of the
-- primary key (W1-a4) and `after_body_id` restarted at 0 EVERY RUN: W1-a2 chose
-- "a body the pass can never stamp costs one re-fetch per RUN", which was the
-- right trade while the eligible rows were spread across the keyset. They are
-- not. Two kinds of row the contract-version gate can never exclude — the body
-- of a DELISTED listing (the `l.is_active` join drops it) and a SUPERSEDED body
-- (the latest-body anti-join drops it) — sit in a long dead PREFIX. Run
-- 34695468715 (2026-09-12) walked 203 windows, 187 of them `eligible=0`, and
-- did not reach a mineable row until the 87th window at id 215,621: 468 s of a
-- 1 169 s pass, re-paid every run, for ever.
--
-- The rule this column buys is the one the LISTING scan already follows
-- (`_resume_point`): a pass that STOPPED on budget resumes where it stopped, a
-- pass that COMPLETED restarts at 0. Poison now costs one re-fetch per PASS
-- instead of per run, and the dead prefix is walked once per pass instead of
-- once an hour.
--
-- NULL IS MEANINGFUL, and it is why this is a nullable column rather than a
-- `DEFAULT 0`: NULL means "the pass completed (or never ran) — start the next
-- one at 0", a non-NULL value means "it stopped here". A row written before
-- this migration reads NULL, which is the safe answer: the next pass starts at
-- 0 exactly as it does today.
--
-- WHY IT IS ON `location_claim_batches` AND NOT ITS OWN TABLE. That table IS
-- this lane's run ledger and cursor store (it already holds `cursor_after_id`
-- for the listing keyset), one row per run, a few thousand rows. A second
-- single-row table would be a second definition of "where the lane got to".
--
-- NO INDEX. The read is the lane's existing newest-row lookup —
-- `lane = … AND source IS NOT DISTINCT FROM … ORDER BY started_at DESC, id DESC
-- LIMIT 1` — which is served by the same access path `_RESUME_SQL` already uses;
-- this column is projected, never filtered on.
--
-- APPLY PATH: the Supabase MCP or `apply_migration.yml`; statements autocommit
-- (no begin/commit here), which is also how the CI schema replay applies it
-- (`psql -v ON_ERROR_STOP=1 -q -f`). `location_claim_batches` is written a few
-- times an hour, so the brief ACCESS EXCLUSIVE of ADD COLUMN is uncontended;
-- `lock_timeout` bounds it anyway rather than letting it queue behind a run.
--
-- Backend/service-role only: RLS and the anon/authenticated revoke came with
-- migration 382 and ADD COLUMN does not touch either.
--
-- Rollback: alter table location_claim_batches drop column bodies_cursor_after_id;

SET lock_timeout = '30s';

alter table location_claim_batches
  add column if not exists bodies_cursor_after_id bigint;

comment on column location_claim_batches.bodies_cursor_after_id is
  'The bodies-first pass''s keyset position (portal_raw_payloads.id) at the end of this run; NULL = the pass completed or never ran, so the next one starts at 0.';

do $$
begin
  if not exists (
        select 1 from information_schema.columns
         where table_schema = 'public'
           and table_name = 'location_claim_batches'
           and column_name = 'bodies_cursor_after_id') then
    raise exception 'W1-a6: location_claim_batches.bodies_cursor_after_id was not created';
  end if;
end $$;
