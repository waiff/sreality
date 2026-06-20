-- 208_subscription_channels.sql
--
-- Sprint N PR 2: per-watchdog delivery-channel opt-in. A watchdog subscription
-- declares WHICH channels its matches fan out to (beyond the always-on in-app
-- feed); the matcher folds this (minus 'in_app') into the event's
-- `notification_dispatches.target_channels`, which the Sprint N outbox drains.
--
-- Channel is a DELIVERY PREFERENCE, not a match predicate — it stays out of
-- filter_spec / _build_match_clauses (so Browse ↔ Watchdog filter lockstep is
-- untouched). Default '{}' = in-app only (today's behaviour), so this is a pure
-- additive no-op until the operator opts a watchdog into email/telegram.

alter table notification_subscriptions
  add column if not exists channels text[] not null default '{}';

comment on column notification_subscriptions.channels is
  'Non-in_app delivery channels this watchdog fans out to (email/telegram). The '
  'matcher folds these into notification_dispatches.target_channels; the outbox '
  'delivers them. in_app is always implicit (the feed shows every event).';
