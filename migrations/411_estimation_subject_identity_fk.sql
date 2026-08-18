-- 411_estimation_subject_identity_fk.sql
--
-- LEDGER NOTE: applied to production while this branch was still numbered 410
-- (supabase_migrations records it as `410_estimation_subject_identity_fk`,
-- 2026-08-18 07:51 UTC). 410_broker_leaderboard_firm_filter landed on main 20
-- minutes earlier, so this file renumbered to 411. The DDL below is unchanged
-- and already applied + VALIDATEd; only the file's number moved.
-- Estimation subject identity: the surrogate listings.id is the ONLY handle a read
-- path may key on. This migration adds the referential guarantee behind it.
--
-- WHY NOW: GET /estimations filtered on the legacy input_sreality_id, which is NULL for
-- every post-Gate-2 non-sreality subject (migration 311's sign check makes a positive
-- sreality_id impossible off sreality). An empty id set then collapsed to "no filter"
-- and the endpoint returned the whole table. The read cutover to input_listing_id ships
-- with this file; the FK is what stops a stamp — especially the late-binding one below —
-- from ever pointing at a listing that does not exist.
--
-- SAFE BY RULE 3: listings are never deleted (delisting flips is_active), so a plain
-- NO ACTION foreign key can never block a write or cascade a loss. Verified live before
-- writing this file: 0 orphans on estimation_runs, 0 rows in building_runs.
--
-- NOT VALID first, VALIDATE second: ADD CONSTRAINT ... NOT VALID takes only a brief
-- SHARE ROW EXCLUSIVE lock instead of scanning the 683k-row listings side under ACCESS
-- EXCLUSIVE; VALIDATE then runs under SHARE UPDATE EXCLUSIVE, which blocks neither reads
-- nor writes. Same shape as migrations 313/314.
--
-- DELIBERATELY NOT ADDED — a CHECK (input_sreality_id IS NULL OR input_listing_id IS NOT
-- NULL). It holds for all 100 existing rows, but it is a live hazard for FUTURE inserts:
-- api/estimation_runs.py stamps the surrogate as
--     COALESCE(%(input_listing_id)s, (SELECT id FROM listings WHERE sreality_id = ...))
-- so an estimation on a sreality URL whose listing has not been scraped yet legitimately
-- lands input_sreality_id NOT NULL + input_listing_id NULL. That constraint would turn a
-- working degraded path into a hard 500 on the estimation submit. The read paths do not
-- need it: keying solely on input_listing_id means such a run simply does not appear
-- under any listing, which is the correct grain — we do not yet know which listing it is.
-- The late-binding resolver (rule 12: identity resolution, not result mutation) fills the
-- NULL once the listing appears.
--
-- INDEX NOTE (declared-vs-live drift, left as-is on purpose): migration 359 declared
-- estimation_runs_input_listing_id_idx as PARTIAL (`where input_listing_id is not null`);
-- the live index is NOT partial. That drift is in the safe direction and is now load-
-- bearing — the late-binding resolver scans for input_listing_id IS NULL, which a partial
-- index cannot serve. Keeping the live shape; recording it here so the next reader of 359
-- is not misled.

SET lock_timeout = '5s';

ALTER TABLE estimation_runs
  ADD CONSTRAINT estimation_runs_input_listing_id_fkey
  FOREIGN KEY (input_listing_id) REFERENCES listings(id) NOT VALID;

ALTER TABLE building_runs
  ADD CONSTRAINT building_runs_input_listing_id_fkey
  FOREIGN KEY (input_listing_id) REFERENCES listings(id) NOT VALID;

ALTER TABLE estimation_runs VALIDATE CONSTRAINT estimation_runs_input_listing_id_fkey;
ALTER TABLE building_runs   VALIDATE CONSTRAINT building_runs_input_listing_id_fkey;
