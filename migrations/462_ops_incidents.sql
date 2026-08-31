-- 462: ops_incidents — ten emails become one, with the exception text attached.
--
-- APPLY BEFORE MERGE. Apply-then-merge is this repo's doctrine and migration 438 is why:
-- it merged, lost its lock race, and left six portals writing against a constraint the code
-- assumed existed for 29 hours. Nothing in this file is hot-table DDL, so it applies in
-- milliseconds -- but it must be LIVE before the code that writes to it deploys, or every
-- producer's best-effort try/except swallows an UndefinedTable and the wave ships inert.
--
-- W3.2 of the reliability program (docs/design/reliability-program.md).
--
-- THE PROBLEM THIS TABLE IS SHAPED BY. On 2026-08-26 six portal workflows failed with
-- byte-identical text -- `CheckViolation: new row for relation "listings" violates check
-- constraint "listings_area_basis_check"` -- and the operator received ten unrelated-looking
-- emails, because the two ops surfaces we already had could not see each other:
-- workflow_failures knows WHICH workflows are red and never WHY (it stores no failure reason
-- at all), and pipeline_check_results can alert but none of its checks observes scrapers.
--
-- So the grain here is deliberately neither "a run" nor "a workflow" but A REASON: one open
-- row per failure SIGNATURE (scripts/failure_signature.py), a key derived only from the error
-- text and never from workflow_path. That asymmetry is the whole mechanism -- it is what makes
-- six workflows collapse into one row, and it is why the fallback key for an UNREADABLE red is
-- the one shape that IS scoped by workflow_path (an unreadable red says nothing about why, so
-- merging two of them would manufacture a meaningless mega-incident).
--
-- ONE OPEN ROW PER SIGNATURE is enforced by a PARTIAL unique index, not a plain one: a
-- resolved incident is history and must never block the next occurrence from opening fresh.
--
-- WHY THREE WAYS TO CLOSE, not one:
--   1. success   -- every member workflow has posted a workflow_run_health.last_success_at
--                   newer than last_seen_at. The primary path; that table already exists and
--                   already holds exactly this.
--   2. max age   -- a workflow that is retired, disabled, unscheduled, or MOVED (a rename
--                   forks workflow_path) will never post a success, and an incident with no
--                   member workflow at all (the always-on Railway worker has none) can never
--                   take path 1. Without a max-age close those escalate forever -- a machine
--                   for manufacturing the exact alert fatigue this wave exists to end.
--   3. manual    -- toolkit.ops_incidents.resolve_incident, for the operator.
-- resolve_reason records which one fired, so "it closed" is never a mystery.
--
-- Delivery deliberately adds NO table: an incident that crosses the onset threshold writes ONE
-- system_health notification_dispatches row (migration 274 already widened both CHECKs for it)
-- and the shipped outbox delivers it. We are not building a second bell.

CREATE TABLE IF NOT EXISTS public.ops_incidents (
    id              bigserial   PRIMARY KEY,
    signature       text        NOT NULL,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    failure_count   integer     NOT NULL DEFAULT 1,
    -- Real .github/workflows/*.yml paths ONLY, so the success-based resolver can join
    -- workflow_run_health on them. Producer context that is not a workflow (a portal + lane
    -- on the always-on worker) goes in `origins` instead, precisely so it cannot poison the
    -- join into a permanently-unresolvable incident.
    workflow_paths  text[]      NOT NULL DEFAULT '{}',
    origins         text[]      NOT NULL DEFAULT '{}',
    sample_run_url  text,
    sample_excerpt  text,
    alerted_at      timestamptz,          -- when the single onset dispatch was written
    alert_count     integer     NOT NULL DEFAULT 0,
    resolved_at     timestamptz,
    resolve_reason  text                  -- success | max_age | manual: <note>
);

COMMENT ON TABLE public.ops_incidents IS
    'One OPEN row per failure signature (migration 462, reliability program W3.2). The key is '
    'derived from the error TEXT only, never from workflow_path, so N workflows failing for one '
    'reason are one incident. Crossing ops_incident_min_failures writes exactly one system_health '
    'notification_dispatches row -- there is no second alert table. Closes on member-workflow '
    'success, on ops_incident_max_age_hours, or manually.';

-- One OPEN incident per signature; resolved history is unconstrained and accumulates.
CREATE UNIQUE INDEX IF NOT EXISTS ops_incidents_open_signature_uq
    ON public.ops_incidents (signature) WHERE resolved_at IS NULL;

-- The resolver + the poller's "already know why this workflow is red" probe both scan open
-- rows newest-first.
CREATE INDEX IF NOT EXISTS ops_incidents_open_recent_idx
    ON public.ops_incidents (last_seen_at DESC) WHERE resolved_at IS NULL;

ALTER TABLE public.ops_incidents ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ops_incidents FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.ops_incidents_id_seq FROM anon, authenticated;

-- Thresholds ride the existing operator-editable blob rather than becoming constants in code,
-- so the numbers can move without a deploy. `||` rather than a fresh INSERT: migration 274
-- seeded this key with `on conflict (key) do nothing`, so re-inserting would be a silent no-op.
--
-- Every value is a SCALAR on purpose: verify_pipeline's load_thresholds merges only int/float
-- from this blob, so a JSON array here would be dropped and the code default would win forever,
-- undetectably.
--
-- Numbers are MEASURED over a 14-day corpus (554 failures, 36 distinct signatures), not guessed:
--   min_failures=2      -- 35 of 63 red streaks are a lone failure but only 6.3% of failures;
--                          ~9 of 36 signatures never reach 2, i.e. ~1.6% of failures unalerted.
--   max_age_hours=168   -- the largest gap INSIDE a still-live incident measured 27.9h, so any
--                          max age under ~36h closes live incidents; the longest genuine red
--                          streak was 267.5h, which is why success-based close is the primary
--                          path and max age is only the retired/renamed backstop.
--   log_excerpt_bytes   -- job logs measured 27KB-172KB; 4KB of the block ENDING at the error
--                          anchor is the readable part.
UPDATE app_settings
   SET value = value || jsonb_build_object(
         'ops_incident_min_failures',     2,
         'ops_incident_max_age_hours',    168,
         'ops_incident_log_excerpt_bytes', 4000
       ),
       updated_at = now()
 WHERE key = 'pipeline_check_thresholds';
