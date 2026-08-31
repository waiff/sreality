-- 463: ops_incident_runs — one Actions run contributes at most one failure.
--
-- APPLY BEFORE MERGE (same reasoning as 462: nothing here is hot-table DDL, it applies in
-- milliseconds, and the code that writes to it must never deploy ahead of the table).
--
-- W3.1/W3.2 correction. Migration 462 shipped `ops_incidents` with TWO producers writing
-- into one counter and NO correlation between them:
--   * scraper.portal_runner._record_run_crash records the exception in-process at t+0,
--     then re-raises — so the Actions run concludes `failure`;
--   * scripts/record_workflow_failures.py then finds that same run in the Actions API,
--     inserts it into `workflow_failures`, and records it AGAIN.
-- `_UPSERT_SQL` bumps `failure_count = ops_incidents.failure_count + 1` unconditionally, so
-- every portal crash was counted twice. That is not cosmetic: `ops_incident_min_failures = 2`
-- was MEASURED against a corpus where 35 of 63 red streaks are a LONE failure, so a
-- double-counted single crash crossed the onset threshold and alerted — the exact noise the
-- threshold exists to suppress — and a genuine six-portal fan-out rendered "12 failures"
-- for six.
--
-- The invariant this table adds: **one GitHub Actions run is at most one failure, in at most
-- one incident, no matter which producer sees it first.** The in-process producer always sees
-- it first (t+0 vs. the poller's 80–256 min throttle), so it wins the claim and the poller
-- skips the run entirely — which also saves the job-log download the poller would otherwise
-- spend on it.
--
-- `run_id` is the PRIMARY KEY rather than `(run_id, signature)` on purpose: two producers can
-- legitimately derive slightly different signatures from the same failure (an exception
-- object vs. a 64KB log tail), and a per-signature key would let exactly that difference
-- restore the double count.
--
-- Producers with no Actions run — the always-on Railway worker's probe/drain lanes — pass
-- NULL and are unaffected; they were never double-counted, because the poller cannot see them.

CREATE TABLE IF NOT EXISTS public.ops_incident_runs (
    run_id      bigint      PRIMARY KEY,
    -- Nullable and set in a second statement: the claim has to happen BEFORE the upsert
    -- (that is what makes it a claim), and the incident id does not exist until after.
    -- A claim that never gets linked is still a correct claim — it has already done its
    -- one job, which is to stop the second producer counting the same run.
    incident_id bigint      REFERENCES public.ops_incidents(id) ON DELETE CASCADE,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.ops_incident_runs IS
    'Run-grain dedupe ledger for ops_incidents (migration 463). One row per GitHub Actions '
    'run that has already been counted as a failure, by whichever of the two W3.1 producers '
    'saw it first. Ephemeral bookkeeping, not history: rows older than 30 days are pruned by '
    'toolkit.ops_incidents.auto_resolve (the poller''s window is hours, so a pruned run can '
    'never come back).';

-- Prune scan (auto_resolve deletes rows older than 30 days each poll).
CREATE INDEX IF NOT EXISTS ops_incident_runs_recorded_idx
    ON public.ops_incident_runs (recorded_at);

ALTER TABLE public.ops_incident_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ops_incident_runs FROM anon, authenticated;
