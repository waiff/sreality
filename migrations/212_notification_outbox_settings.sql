-- 212_notification_outbox_settings.sql
--
-- Sprint N PR 3: operator-tunable settings for the delivery outbox (the loop
-- that drains notification_dispatches.target_channels into channel_sends).
--
-- Recipient endpoints live in app_settings (operator-editable, history-tracked) —
-- the single operator's destination per channel. The TRANSPORT secrets stay in
-- env (RESEND_API_KEY / EMAIL_FROM / Telegram BOT_TOKEN); these are just the
-- WHO-to-reach. Empty = that channel is skipped by the outbox (no send, no
-- failed row), so the system stays dark until the operator fills them in.

insert into app_settings (key, value, description, updated_by) values
  ('notification_email_to', to_jsonb(''::text),
   'Operator destination email for notification delivery (the TO address; the '
   'EMAIL_FROM env var is the sender). Empty = the email channel is skipped by '
   'the outbox.',
   'migration_212'),
  ('notification_telegram_chat_id', to_jsonb(''::text),
   'Operator Telegram chat_id for notification delivery. Empty = the telegram '
   'channel is skipped by the outbox.',
   'migration_212'),
  ('notifications_outbox_interval_seconds', to_jsonb(120),
   'How often the delivery outbox drains channel sends (seconds). 0 keeps the '
   'task alive but idle. The loop only starts at all when a transport is '
   'configured (e.g. RESEND_API_KEY set), so it is a no-op until provisioned.',
   'migration_212')
on conflict (key) do nothing;
