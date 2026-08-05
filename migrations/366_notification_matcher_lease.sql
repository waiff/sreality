-- 366_notification_matcher_lease.sql
--
-- Wave 3 (watchdogs & notifications), detection axis. Single-runner guard for the
-- three notification producer passes (match_once / match_changes_once /
-- match_monitored_collections_once), which run as an asyncio loop inside EVERY
-- FastAPI replica (api/notifications.matcher_loop). With N replicas the market-wide
-- scans fan out N× — the per-event dedupe_key already prevents DUPLICATE
-- notifications (rule #16), so this is a wasted-work / cursor-churn guard, not a
-- correctness one, but it matters at public scale.
--
-- Same pooler-proof primitive as migration 279 (property_maintenance_lease): a
-- single lease ROW claimed by one atomic UPDATE ... RETURNING compare-and-set —
-- sound over the transaction-mode pooler (a session pg_advisory_lock would strand,
-- the mig-279 lesson). Expiry self-heals a crashed holder. Internal object: RLS on,
-- no grants (only the service-role matcher loop touches it). Co-hosted in the API
-- (Phase 1 A8), NOT moved to the dark-by-default worker.

create table if not exists notification_matcher_lease (
  id         smallint primary key default 1 check (id = 1),
  holder     text,
  expires_at timestamptz
);
insert into notification_matcher_lease (id) values (1) on conflict (id) do nothing;
alter table notification_matcher_lease enable row level security;
-- Internal object: RLS on + no policy already denies authenticated, but strip the
-- Supabase default-ACL grant explicitly too (the recurring default-ACL-leak footgun).
revoke all on notification_matcher_lease from anon, authenticated;

comment on table notification_matcher_lease is
  'Single-row lease serializing the notification producer passes across API '
  'replicas (migration 366). Claimed by one atomic UPDATE ... RETURNING '
  '(api/notifications.matcher_loop); expiry self-heals a crashed holder. Pooler-'
  'proof, unlike a session advisory lock (the mig-279 lesson).';
