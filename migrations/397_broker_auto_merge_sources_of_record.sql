-- 397: record the live value of app_settings.broker_auto_merge_sources.
--
-- WHY THIS NUMBER IS TAKEN. Production's migration ledger already holds a 397,
-- `broker_auto_merge_remax` (version 20260812142920), applied during the C1
-- implementation and then reverted by hand — `broker_auto_merge_sources` now
-- reads ["sreality","idnes"] with updated_by='claude_c1_safety_revert_pending_
-- operator_review'. That migration ships in draft PR #1032, which was blocked on
-- review: remax auto-merge rests on a single weak rung (remax publishes no phone,
-- so a pair can never reach the "2 independent bridges" gate) and an operator's
-- unmerge is silently re-applied by the next daily sweep. Enabling it stays an
-- operator decision. This file claims 397 so the number cannot be handed out
-- twice, and records in git that prod ran — and undid — a migration by that
-- number. #1032 renumbers to 398 if it is ever revived.
--
-- WHAT IT FIXES. `idnes` was added to the live value out-of-band on 2026-06-17
-- with no migration behind it, so a from-zero schema replay drifted back to
-- migration 186's ["sreality"] seed while production ran ["sreality","idnes"].
-- This is the same statement #1032 used for that half, kept because the drift is
-- real and independent of the remax question.
--
-- `remax` is deliberately NOT added. Idempotent and a verified no-op against
-- production (the value already contains idnes), so it is not re-applied there;
-- CI's schema replay applies it to a fresh container, which is the path that was
-- drifting.

begin;

set local lock_timeout = '5s';

update app_settings
   set value = value || '["idnes"]'::jsonb,
       updated_at = now(),
       updated_by = 'migration_397'
 where key = 'broker_auto_merge_sources'
   and not (value ? 'idnes');

commit;
