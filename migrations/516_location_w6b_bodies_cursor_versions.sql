-- 516_location_w6b_bodies_cursor_versions.sql
--
-- Location simplification sprint, W6-b: ONE nullable text column beside the
-- bodies cursor (migration 509), so a kept cursor knows what it is valid for.
-- PURELY ADDITIVE — no data change, no constraint, no index, no rewrite (a
-- nullable ADD COLUMN with no default is a catalogue-only change). Safe to re-run.
--
-- WHY. 509 let the bodies-first pass resume a run it had STOPPED, but a pass that
-- COMPLETED still stamped NULL and the next run restarted the walk at id 0. With
-- the backlog empty that is what every hourly hop now does: walk all 744 000
-- `portal_raw_payloads` rows — ~500 000 of them version-eligible bodies that can
-- never be stamped (bodies of unserved listings, and bodies some later body has
-- superseded) — to stamp nothing. Measured on 2026-09-13: `bodies=416s`,
-- `bodies=272s`, `bodies=176s` on idle hops, 3-7 minutes of IO an hour, for ever.
--
-- THE RULE THIS COLUMN BUYS. The cursor CONTINUES: "pass complete" means caught
-- up, not restart, and the next run walks only the ids above it (a new body is a
-- new row with a higher id, so it is found in seconds). The one event that makes
-- rows BELOW the cursor eligible again is a contract bump — the window's gate is
-- `p.contract_version IS DISTINCT FROM portal_contracts.version` — so the cursor
-- is stamped together with the active version set it was taken under, and
-- `_bodies_resume_point` honours it only while that set still holds. Unstampable
-- bodies are therefore re-fetched once per VERSION SET instead of once per pass.
--
-- THE SHAPE IS A CANONICAL PROJECTION, not a hash: `bazos@5,bezrealitky@2,…`,
-- sorted, built by `_ACTIVE_VERSIONS_SQL` from `portal_contracts WHERE is_active`.
-- An operator reading the ledger can see which set a cursor belongs to, and a
-- mismatch is legible rather than a pair of unequal digests.
--
-- NULL IS MEANINGFUL, which is why this is nullable with no default: NULL means
-- "no version set was recorded with this cursor", and `_bodies_resume_point`
-- treats that as a mismatch — so every row written before this migration (and any
-- row whose cursor is NULL) sends the next pass back to 0 exactly once, which is
-- the safe answer. Stamping NULL by hand is also the manual reset: a code change
-- that widens the served set, rather than a contract bump, needs one.
--
-- APPLY PATH: `apply_migration.yml` or the Supabase MCP; statements autocommit
-- (no begin/commit here), which is also how the CI schema replay applies it
-- (`psql -v ON_ERROR_STOP=1 -q -f`). `location_claim_batches` is written a few
-- times an hour, so the brief ACCESS EXCLUSIVE of ADD COLUMN is uncontended;
-- `lock_timeout` bounds it anyway rather than letting it queue behind a run.
--
-- Backend/service-role only: RLS and the anon/authenticated revoke came with
-- migration 382, and ADD COLUMN touches neither.
--
-- Rollback: alter table location_claim_batches drop column bodies_cursor_versions;

SET lock_timeout = '30s';

alter table location_claim_batches
  add column if not exists bodies_cursor_versions text;

comment on column location_claim_batches.bodies_cursor_versions is
  'The active contract-version set (source@version, sorted, comma-joined) the bodies-first cursor on this row was taken under; the next pass resumes from that cursor only while the set is unchanged, else it walks from 0.';

do $$
begin
  if not exists (
        select 1 from information_schema.columns
         where table_schema = 'public'
           and table_name = 'location_claim_batches'
           and column_name = 'bodies_cursor_versions') then
    raise exception 'W6-b: location_claim_batches.bodies_cursor_versions was not created';
  end if;
end $$;
