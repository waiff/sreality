-- 210: operator-tunable cadence + window for the collection-monitor producer
-- (Sprint C). Both have safe code defaults (api/notifications.py:_read_int_setting
-- returns the default when the row is absent), so this seed is for operator
-- visibility/tuning via the Settings page, not correctness.

insert into app_settings (key, value, description, updated_by) values
  ('notifications_monitor_interval_seconds', to_jsonb(86400),
   'How often the collection-monitor producer runs (seconds; 0 disables). Default daily.',
   'migration_210'),
  ('notifications_monitor_window_days', to_jsonb(7),
   'Lookback window (days) for collection-monitor change detection '
   '(price moves / inactive / reactivated / new_source). The per-event dedupe_key '
   'keeps re-scans over an overlapping window idempotent.',
   'migration_210')
on conflict (key) do nothing;
