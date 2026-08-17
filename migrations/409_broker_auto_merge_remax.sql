-- 409: add `remax` to app_settings.broker_auto_merge_sources (operator decision D5).
--
-- WHAT THIS TURNS ON. The nightly cross-source sweep auto-merges a pair of broker
-- identities only when BOTH their sources are listed here AND the pair is
-- corroborated (>=2 distinct personal-contact bridges, or 1 bridge plus a matching
-- name — toolkit/broker_resolver.decide_merges). Adding remax lets remax identities
-- merge with sreality/idnes ones; it does NOT relax the corroboration bar.
--
-- WHY THE NUMBER IS 409, NOT 397. Migration 397 records the history: production's
-- ledger already holds a *different* 397 (`broker_auto_merge_remax`, applied
-- 2026-08-12 during the C1 implementation, then reverted BY HAND the same day to
-- ["sreality","idnes"] with updated_by='claude_c1_safety_revert_pending_operator_
-- review') because remax auto-merge had been enabled before independent review, and
-- review then surfaced two HIGH findings against the premise it rested on. 397 on
-- disk deliberately records only the idnes half and leaves remax off. 402-408 went
-- to the location-data program while this branch sat uncommitted (this file was
-- originally staged as 405, which 405_location_w2a_normalizer_cohort_by_surface.sql
-- claimed first — renumbered, not the file that lost the race). This file is the
-- forward migration that finally makes the operator's decision real.
--
-- WHY IT IS SAFE NOW. Both HIGH findings that blocked it are closed:
--   1. "An operator's unmerge is silently re-applied by the next sweep." Fixed by the
--      D5 suppression rail — broker_merge_suppressions (migration 401, live
--      2026-08-13): unmerge_group and dismiss_candidate record the operator's NO
--      against the durable identity pair, decide_merges filters those pairs out of
--      BOTH the auto-merge groups and the review queue, and _apply_merges re-reads
--      the table inside the write transaction as a backstop against transitive
--      chaining. Verified live: the rail has been through the 08-14, 08-15 and 08-17
--      full sweeps with the broker_merge_suppression health check reporting 0
--      violations throughout.
--   2. "The manual fallback is broken" (merge_brokers wrote broker_merge_events.source
--      = 'manual', violating migration 186's CHECK constraint, so every operator merge
--      500'd). Already fixed in PR #1029 -> 'operator', with a schema-backed test.
--
-- FREQUENCY VALIDATION (the gate D5 actually named — measured live 2026-08-13 by
-- replaying _BRIDGE_CANDIDATES + decide_merges against production):
--   * 1,059 of remax's ~1,073 email-bearing identities (98.7%) form a corroborated
--     cross-source bridge: 992 confirmed independently on BOTH sreality and idnes,
--     50 sreality-only, 17 idnes-only.
--   * Zero fan-out: no sreality/idnes identity is claimed by more than one remax
--     identity, and no remax identity claims more than 2 others. Every component is
--     <= 3 members, far under MAX_AUTO_MERGE_COMPONENT = 6, so the oversized-component
--     downgrade never fires on this batch and no recycled contact can chain a group.
--   * Corroboration quality: 1007/1009 idnes pairs and 1039/1042 sreality pairs match
--     on an exact normalised name key ON TOP of the personal-email bridge (>99.7%).
--     All 5 non-exact cases were inspected by hand and are genuine same-person matches
--     (maiden/married-name email variants resolving to one person; a Mgr./DiS. degree
--     suffix breaking strict string equality). 2 of those 5 auto-merge anyway on the
--     ">=2 distinct bridges" rung; the other 3 fall safely to manual review.
--   Re-checked live 2026-08-17 (headline only, not the full corroboration/fan-out
--   breakdown above): 1,062 of 1,085 email-bearing remax identities (97.9%) now
--   bridge to sreality/idnes — the batch grew with 4 more days of scraping but the
--   bridge rate and conclusion are unchanged.
--
-- SINGLE-RUNG NOTE. remax publishes email but no phone, so a remax pair can never
-- reach the "2 independent bridges" tier on two DISTINCT channels — in practice it
-- clears the bar via 1 bridge + a name match. That is the weaker rung, which is
-- exactly why the suppression rail was made prerequisite rather than shipping this
-- first. It is not, however, a "one bridge only" guarantee: two distinct freq-1
-- emails for the same person DO reach 2 bridges without any name match (see the
-- pure-layer tests pinning this both ways).
--
-- Idempotent: re-applying is a no-op once the value contains remax. Production is
-- expected to read ["sreality","idnes"] when this runs; CI's from-zero schema replay
-- reaches the same state through 186 -> 397 -> here.

begin;

set local lock_timeout = '5s';

update app_settings
   set value = value || '["remax"]'::jsonb,
       updated_at = now(),
       updated_by = 'migration_409'
 where key = 'broker_auto_merge_sources'
   and not (value ? 'remax');

commit;
