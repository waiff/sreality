-- 530: admit the `oss` judge tier — the open-model arm of the W3 judge.
--
-- NOT yet in PROGRAM.md's decisions ledger: that ledger records operator rulings (D1-D10,
-- all ruled 2026-09-16) and this arm has not been ruled on, so it is not cited as though it
-- had been. The arm's own contract lives in `autodedup/judge_lane.py` (tier `oss`) and
-- `autodedup/oss_pod.py`; the one thing that binds here is that an `oss` verdict is NEVER
-- ground truth — gold is, and the tier-ranked reads in ui_sql/score_sql enforce it.
--
-- WHY. The vision step is the dominant LLM cost at full scale, so an open-source vision model
-- rented by the hour on a GPU pod is judged over the SAME pairs as the paid judge — the paid
-- verdicts then pay for themselves twice, once as the program's labels and once as the
-- yardstick the free arm is measured against. Those verdicts need a home, and the home is the
-- table that already holds every judgement at (pair, judge_version, tier) grain: a fourth tier
-- value, not a fourth table, so `autodedup.compare` reads one contract and the E29 "a pair is
-- judged once per (version, tier)" primary key keeps holding.
--
-- Migration 528 spelled the tier domain as an INLINE check on two tables, which Postgres named
-- `judgements_tier_check` / `judge_queue_tier_check`. Restating a check means dropping and
-- re-adding it; both statements are guarded so re-applying this file is a no-op, and the new
-- constraints are NAMED so the next widening does not have to guess.
--
-- ADDITIVE in effect: the accepted set only grows, so no existing row can fail the re-add and
-- the validation scan reads rows it is already allowed to keep. `lock_timeout` is still set —
-- both tables take an ACCESS EXCLUSIVE lock for the swap and neither may sit behind a long
-- reader while the judge lane is writing.

set lock_timeout = '5s';

alter table autodedup.judgements
  drop constraint if exists judgements_tier_check;
alter table autodedup.judgements
  drop constraint if exists autodedup_judgements_tier_ck;
alter table autodedup.judgements
  add constraint autodedup_judgements_tier_ck
  check (tier in ('text', 'vision', 'gold', 'oss'));

alter table autodedup.judge_queue
  drop constraint if exists judge_queue_tier_check;
alter table autodedup.judge_queue
  drop constraint if exists autodedup_judge_queue_tier_ck;
alter table autodedup.judge_queue
  add constraint autodedup_judge_queue_tier_ck
  check (tier in ('text', 'vision', 'gold', 'oss'));
