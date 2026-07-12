-- 297: raise the llm_burn_rate WARN threshold to sit above the new steady burn.
--
-- The #739 check shipped warn=$90/24h when normal burn was ~$40-75/day. After mig 285
-- enabled the free dedup arms + the queue drain resumed, steady spend settled at
-- $117-126/day (measured Jul 11-12) — so the check warns PERMANENTLY and loses its
-- anomaly signal (the exact permanently-amber failure mode the alerting rebuild removed
-- elsewhere). 130 sits just above the observed steady band; fail stays 150 (the code
-- default) as the runaway/depletion early-warning. Raise fail temporarily only if the
-- compare blitz is dispatched.

UPDATE app_settings
SET value = jsonb_set(value, '{llm_spend_24h_warn_usd}', '130'::jsonb),
    updated_at = now(), updated_by = 'migration-297'
WHERE key = 'pipeline_check_thresholds';
