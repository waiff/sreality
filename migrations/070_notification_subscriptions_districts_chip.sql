-- 070_notification_subscriptions_districts_chip.sql
--
-- One-shot UPDATE lifting existing Watchdog `filter_spec.districts`
-- from `text[]` to the new chip shape `[{name, context}]` introduced
-- by migration 069's two-array signature on `browse_stats`.
--
-- Backfill is loss-free: a legacy `"X"` chip becomes
-- `{"name": "X", "context": null}`, which under migration 069's SQL
-- behaves identically to today (a NULL ctx skips the narrowing
-- clause). The API model accepts both shapes in case any in-flight
-- request lands during deploy.
--
-- Idempotent — the `exists (string element)` guard skips rows that
-- have already been migrated, so re-running is a no-op.

update notification_subscriptions
set filter_spec = jsonb_set(
      filter_spec,
      '{districts}',
      (
        select jsonb_agg(
          jsonb_build_object('name', value, 'context', null)
        )
        from jsonb_array_elements_text(filter_spec->'districts') as value
      )
    )
where filter_spec ? 'districts'
  and jsonb_typeof(filter_spec->'districts') = 'array'
  and jsonb_array_length(filter_spec->'districts') > 0
  and exists (
    select 1
    from jsonb_array_elements(filter_spec->'districts') as e
    where jsonb_typeof(e) = 'string'
  );
