-- 397: add `remax` to app_settings.broker_auto_merge_sources.
--
-- The gate (toolkit/broker_resolver.py decide_merges, line ~169) requires BOTH
-- sides of a cross-source pair to be in this set, so adding remax enables two
-- pairings at once: sreality<->remax and idnes<->remax.
--
-- EVIDENCE (measured on prod 2026-08-12, replaying _BRIDGE_CANDIDATES' logic):
--   * 2,048 personal-on-both-sides email bridges -> 1,055 distinct BROKER pairs
--     (1,039 vs sreality, 1,009 vs idnes). ~7x SMALLER than the already-accepted
--     idnes rollout, which produced 7,685 auto-merge groups since 2026-06-17 with
--     0 ever undone.
--   * Corroboration: 2,044 of 2,048 pairs have an exactly-matching diacritics-
--     folded name key (99.8%); 0 identities have a missing name. The ~4 that
--     disagree fall through to `contact_bridge_review` for the operator.
--   * Fan-out: max 2 partners per remax identity (one sreality + one idnes), max
--     1 remax partner per other-source identity. Largest connected component = 3
--     identities / 2 brokers, well under MAX_AUTO_MERGE_COMPONENT = 6. No
--     transitive chaining, so no recycled-contact fusion risk.
--   * Each merge therefore retires exactly one remax broker into one existing
--     broker.
--
-- CAVEAT the operator should know: remax publishes NO phone (1,079 email contacts,
-- 0 phones), so a remax pair can never reach the "2 independent bridges" rung —
-- every one of these merges rests on the single rung "one personal-on-both-sides
-- email + an exactly matching name". That is the weaker of the two paths.
--
-- REVERSIBILITY: auto-merges are logged to broker_merge_events (source='auto') per
-- merge_group_id and undone by api.broker_review.unmerge_group, which filters only
-- on undone_at — it replays an 'auto' group exactly like a manual one. Reverting
-- the flag itself is one UPDATE on this row; it does not un-apply merges already
-- made, which is why the ledger is the real undo.
--
-- Also records `idnes`, which was added to the live value out-of-band on
-- 2026-06-17 without a migration (the row still reads updated_by='migration_186').
-- Recording it here is a no-op against prod and makes a from-zero schema replay
-- match production instead of drifting back to the migration-186 seed.

begin;

set local lock_timeout = '5s';

update app_settings
   set value = value || '["idnes"]'::jsonb,
       updated_at = now(),
       updated_by = 'migration_397'
 where key = 'broker_auto_merge_sources'
   and not (value ? 'idnes');

update app_settings
   set value = value || '["remax"]'::jsonb,
       updated_at = now(),
       updated_by = 'migration_397'
 where key = 'broker_auto_merge_sources'
   and not (value ? 'remax');

commit;
