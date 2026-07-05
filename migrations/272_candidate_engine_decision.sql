-- 272_candidate_engine_decision.sql
-- Stop the candidate-queue treadmill: the 2h candidate drain re-formed and re-decided
-- EVERY still-proposed pair on EVERY run (~296 pairs, all forensic verdicts cache-hits,
-- all resolving visual_inconclusive again) — an infinite loop that neither merged,
-- dismissed, nor re-queued anything, and drowned the lane's budget in re-chewed work.
--
-- The fix is an engine-side re-decide policy: every time the engine LOOKS at a proposed
-- pair and leaves it proposed (queue outcome / free-mode skip / tagging defer) it stamps
--   * engine_decision          — the last non-terminal outcome ('visual_inconclusive',
--                                'skipped_unresolved', 'clip_deferred', ...)
--   * last_engine_decision_at  — when
-- and the candidate drain then loads ONLY candidates that are DUE:
--   last_engine_decision_at IS NULL                       (never looked at), or
--   older than app_settings.dedup_candidate_redecide_hours (backoff, default 24h), or
--   fresh photo evidence exists (images.clip_tagged_at > the stamp — the same signal
--   the prior-dismissal consult already trusts).
-- The operator /dedup review flow is untouched: status / reviewed_* stay the human
-- surface; these two columns are engine bookkeeping.

alter table property_identity_candidates
  add column if not exists engine_decision text,
  add column if not exists last_engine_decision_at timestamptz;

-- The due-filter scans proposed rows only; a partial index keeps it O(queue).
create index if not exists property_identity_candidates_proposed_decision_idx
  on property_identity_candidates (last_engine_decision_at)
  where status = 'proposed';

comment on column property_identity_candidates.engine_decision is
  'Last non-terminal engine outcome for this pair (queue reason / skip); engine bookkeeping '
  'for the re-decide backoff, not an operator field.';
comment on column property_identity_candidates.last_engine_decision_at is
  'When the engine last evaluated this proposed pair; the candidate drain re-decides only '
  'when NULL, older than dedup_candidate_redecide_hours, or new CLIP-tagged evidence exists.';
