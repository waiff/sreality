-- 259_llm_calls_error.sql
-- Make LLM provider FAILURES auditable. Until now llm_calls held only SUCCESSFUL calls (one row
-- per completed call); a failed call (exhausted Anthropic credit, dead/rotated key, 5xx) wrote
-- NOTHING — so a total outage left zero trace and the liveness monitor (check_llm_health), which
-- keyed off "no rows / stale max(called_at)", stayed GREEN through an 8h credit outage that took
-- down dedup vision, condition scoring, estimations and summaries. LLMClient now records a
-- best-effort failure row (zero usage/cost, `error` set) on every provider exception; the health
-- check alarms on recent failures (esp. credit-balance errors) independent of pending work.
alter table llm_calls add column if not exists error text;

-- Recent-failures probe index. Partial on `error IS NOT NULL` keeps it tiny (failures are the
-- exception), and it self-serves the health query's `called_at desc` scan over the alert window.
create index if not exists llm_calls_error_recent_idx
  on llm_calls (called_at desc)
  where error is not null;

comment on column llm_calls.error is
  'Non-NULL = this call FAILED (provider exception); the row carries zero usage/cost. NULL = a '
  'successful call. Powers check_llm_health''s credit-exhaustion / provider-down alarm.';
