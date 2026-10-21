-- 656_notifications_w6_prune.sql -- notifications W6 ("Migration E"): THE ONE DESTRUCTIVE WINDOW of the push program.
--
-- ===========================================================================
-- DESTRUCTIVE. Apply ONLY when all three hold, in this order:
--   1. W2b (#1951, the last of W1 -> W2a -> W3 -> W4 -> W2b) AND this file's own PR (W6) are MERGED AND DEPLOYED: the
--      three Railway statuses (API, vite, realtime-worker) read success on W6's merge commit (`gh api
--      repos/{owner}/{repo}/commits/<sha>/status`), and migrations 651-655 are applied. Unlike an additive file this one
--      applies AFTER its merge: until W6's code is live the API's feed still selects `message` and its share grant
--      still writes it, so applied first it would break the bell and every share. The drift check reads it present
--      meanwhile (it probes the trigger function and the two CHECKs by name).
--   2. backup_before_drop.yml (label notifications-w6) has backed up what this drops to R2 and read it back: the
--      whole notification_dispatches relation (every row, the system rows and the nine columns included), the four
--      tables whole, notification_subscriptions' and collections' columns as key + column CSVs, and app_settings
--      whole. The PR body's "Operator steps" holds the exact invocation.
--   3. The operator has said "yes, apply it" (rule 1; the `database` skill's destructive gate; ruling R8).
-- APPLY PATH: apply_migration.yml only (psql -f, statement autocommit, a plain 5 s lock limit, the workflow's
-- 30 x 20 s lock-timeout retry re-runs the file). NEVER through the MCP: its apply_migration wraps the payload in one
-- transaction, and this file's $pre$ must refuse before its transaction takes a lock. A "deadlock detected" error
-- is not one the workflow retries: dispatch it again (the transaction rolled back whole, and every step is
-- idempotent). Forward only (rule 1): a restore is that backup.
-- ===========================================================================
--
-- WHY (docs/design/notifications-push/PROGRAM.md § 1 rule 2, § 3.2 "Drop (W6)", § 3.8, § 5 W6; AUDIT.md F5, F21,
-- F23, F26, F27): W1-W2b moved every reader and writer off the old notification model: producers write facts and
-- the one identity (653), never routing (channel, status, target_channels) or the key (dedupe_key,
-- trigger_snapshot_id); the API runs no matcher (W2b), so no lease and none of its six settings; the outbox,
-- Telegram, Resend, the webhook and the unsubscribe are deleted (W1), so channel_sends, notification_suppression and
-- resend_webhook_events are written by nothing (0 rows each, ever); "Estimate rent" is deleted (W1a), so
-- estimation_run_id has no writer; system alerts left the bell (W1d) and ruling R2 (b) deletes their rows after a
-- backup; a share is labelled from its actor (`share_party_emails()`, migration 641) and its live name, so `message`
-- has no reader (W6 moves the bell's sentence onto them in the same PR). What stays unread is deleted.
--
-- LIVE FACTS, 2026-10-21 (read-only, production): notification_dispatches 947 rows, 906 of them system_health
-- (system_alert, SYSTEM account, none ever seen; the count grows until W1 deploys, so the delete takes them all);
-- every row's channel/status/error_message/target_channels at its default, 0 estimation_run_id, `message` on the
-- 2 share rows and the system rows only; 0 rows in channel_sends, notification_suppression, resend_webhook_events;
-- 0 watchdogs (so no `channels`, no cursor); 0 of 19 collections with a notify_channels pick; the nine settings
-- present; emit_verification_stale_alert() present and unscheduled since 620. pg_depend: every dependent of a
-- dropped column or table is its own default, CHECK, index, FK, sequence or TOAST (no view, policy, trigger or
-- foreign key elsewhere). Function sources: health_liveness() reads the lease until 655 applies (so $pre$ refuses
-- today), emit_verification_stale_alert() names the dropped columns (this file drops it).
--
-- WHAT GOES, in this order:
--   0. $pre$, before any lock, REFUSES while: the worker's heartbeat has no `notifications` lane (W2a not deployed);
--      the API still renews notification_matcher_lease (a renewal stamps now() + 15 min, so an expiry later than
--      5 min ago is a renewal in the last 20 min: W2b not deployed); 653's identity index or feed index or 654's
--      ledger is missing; a row names its ad by sreality_id alone (its listing_id NULL); any function body other than
--      the two this file replaces names a dropped object; any view, policy, trigger, foreign key, index, statistics
--      object or constraint outside this file's list depends on a dropped column or table (pg_depend); a row outside
--      system_health violates a narrowed CHECK. Every check tolerates an object a previous run already dropped, so a
--      lock-timeout retry re-runs the file.
--   1. ONE transaction, its tables locked first in the order a feed read takes them (dispatches, watchdogs):
--      (a) R2 (b): DELETE every source_kind = 'system_health' row, a NOTICE with the count, after asserting no
--          notification_deliveries row references one (the cascade would take it; SYSTEM has no member, so no device
--          could hold one). The only deletion in the table's history by our code (CLAUDE.md rule 16).
--      (b) sync_notification_dispatches_account_id() (641) without its SYSTEM else-branch: the watchdog,
--          collection_monitor and share arms verbatim, anything else raises. notification_dispatches_source_ck (274,
--          641) loses its system_health arm; notification_dispatches_change_kind_ck (652) the three kinds no row
--          carries: system_alert (gone with its rows), shared (relabelled by 652/653), broker_change (never emitted).
--      (c) notification_dispatches loses dedupe_key (+ its UNIQUE, replaced by 653's identity), trigger_snapshot_id,
--          channel (+ CHECK), status (+ CHECK), error_message, target_channels, sreality_id (+ its index),
--          estimation_run_id (+ its FK) and message: 24 columns -> 15. The four indexes 653's feed index replaces
--          go too: the unread partial index and the dispatched_at index (057), the keyset index (199) and
--          source_kind_idx (206; every reader that filters on source_kind also names the account (the feed index,
--          or 653's identity, whose prefix is (account_id, source_kind)) or a collection and property (their own
--          indexes); with the 906 system rows gone the table is ~45 rows).
--      (d) notification_subscriptions loses channels (208) and last_matched_first_seen_at (the cursor W2a deleted).
--      (e) channel_sends (207), notification_suppression and resend_webhook_events (367), notification_matcher_lease
--          (366): no CASCADE ($pre$ proved nothing outside them depends; RESTRICT would refuse a dependent anyway).
--          emit_verification_stale_alert() (274; unscheduled by 620, no caller). The nine settings rows, each
--          first copied to app_settings_history as its trigger would on an update (020's trigger fires on UPDATE
--          only, so a bare DELETE would leave no trail).
--   2. collections.notify_channels (211), its own statement after the commit: collections is read on every
--      curation surface, so its ACCESS EXCLUSIVE lock is held for that one catalog change only.
--   3. $post$: the 15 columns, the four tables, the function, the settings rows and the system rows gone, the
--      narrowed CHECKs and trigger as written, 653's identity index still valid.
--
-- NOT HERE. notification_deliveries / push_subscriptions (654) and every column the identity, the feed or the push
-- lane reads. collection_idx, property_idx and listing_id_idx stay (the collection FK and the detectors read them).
-- notification_dispatches_pre204_backup, an ad-hoc copy outside the migrations, is not this program's. #1910's
-- migration 643 drops emit_verification_stale_alert() too: both are DROP ... IF EXISTS, whichever runs first.
-- After this file 652's and 653's fills cannot re-run (they read dedupe_key); nothing needs them to.
-- No ci-allow-dynamic marker: no EXECUTE builds anything.
--
-- ROLLBACK: forward only (rule 1); the backup is the restore. Every statement is idempotent.

set lock_timeout = '5s';
set statement_timeout = '120s';

-- ---------------------------------------------------------------------------
-- 0. Preconditions, before any lock.
-- ---------------------------------------------------------------------------
do $pre$
declare
  c_tables constant text[] := array['channel_sends', 'notification_suppression', 'resend_webhook_events',
                                    'notification_matcher_lease'];
  c_settings constant text[] := array['notification_email_to', 'notification_telegram_chat_id',
    'system_health_channels', 'notifications_matcher_interval_seconds', 'notifications_match_window_listings',
    'notifications_monitor_interval_seconds', 'notifications_monitor_window_days',
    'notifications_outbox_interval_seconds', 'notifications_watermark_first_seen_at'];
  -- The dependents this file drops by name; every other dependent of a dropped column refuses.
  c_own_constraints constant text[] := array['notification_dispatches_dedupe_key_key',
    'notification_dispatches_channel_check', 'notification_dispatches_status_check',
    'notification_dispatches_estimation_run_id_fkey'];
  c_kinds constant text[] := array['new', 'price_drop', 'price_rise', 'inactive', 'reactivated', 'new_source',
                                   'shared_collection', 'shared_dashboard', 'shared_pipeline'];
  v_bad text;
  v_kind bigint;
  v_source bigint;
begin
  if exists (select 1 from public.worker_heartbeats
              where worker = 'realtime-worker' and not coalesce(details ? 'notifications', false)) then
    raise exception '656 refused: the realtime worker beats with no notifications lane: W2a is not deployed';
  end if;
  if to_regclass('public.notification_matcher_lease') is not null
     and exists (select 1 from public.notification_matcher_lease
                  where expires_at > now() - interval '5 minutes') then
    raise exception '656 refused: notification_matcher_lease was renewed in the last 20 min: W2b is not deployed';
  end if;
  if not exists (select 1 from pg_index i
                  where i.indexrelid = to_regclass('public.notification_dispatches_identity_uq')
                    and i.indisvalid and i.indisunique and i.indnullsnotdistinct)
     or to_regclass('public.notification_dispatches_account_feed_idx') is null then
    raise exception '656 refused: 653''s identity or feed index is missing or INVALID (they replace the key and '
      'the indexes this drops)';
  end if;
  if to_regclass('public.notification_deliveries') is null then
    raise exception '656 refused: notification_deliveries is missing: migration 654 (W4) is not applied';
  end if;
  -- A row whose legacy id is its only link to its ad would lose the ad with sreality_id (0 on 2026-10-21). The
  -- statement runs only while the column exists, so a re-run after the drop never reads it.
  if exists (select 1 from information_schema.columns
              where table_schema = 'public' and table_name = 'notification_dispatches' and column_name = 'sreality_id')
     and exists (select 1 from public.notification_dispatches where sreality_id is not null and listing_id is null) then
    raise exception '656 refused: a dispatch names its ad by sreality_id alone (listing_id NULL)';
  end if;

  -- Function bodies: pg_depend does not track plpgsql, so a body naming a dropped object is read off its source.
  select string_agg(p.oid::regprocedure::text, ', ' order by p.oid::regprocedure::text) into v_bad
    from pg_proc p
   where p.pronamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
     and p.oid is distinct from to_regprocedure('public.emit_verification_stale_alert()')::oid
     and p.oid is distinct from to_regprocedure('public.sync_notification_dispatches_account_id()')::oid
     and (p.prosrc ~ ('\m(' || array_to_string(c_tables || c_settings
                        || array['notify_channels', 'last_matched_first_seen_at', 'target_channels',
                                 'trigger_snapshot_id'], '|') || ')\M')
          or (p.prosrc ~ '\mnotification_dispatches\M'
              and p.prosrc ~ '\m(dedupe_key|channel|status|error_message|sreality_id|estimation_run_id|message)\M')
          or (p.prosrc ~ '\mnotification_subscriptions\M' and p.prosrc ~ '\mchannels\M'));
  if v_bad is not null then
    raise exception '656 refused: % still names a dropped table, column or setting', v_bad;
  end if;

  -- Catalog dependents of a dropped column: only its default and this file's own constraints and index.
  select string_agg(format('%s.%s <- %s', t.rel, t.col, pg_describe_object(d.classid, d.objid, d.objsubid)), '; ')
    into v_bad
    from (values ('notification_dispatches', 'dedupe_key'), ('notification_dispatches', 'trigger_snapshot_id'),
                 ('notification_dispatches', 'channel'), ('notification_dispatches', 'status'),
                 ('notification_dispatches', 'error_message'), ('notification_dispatches', 'target_channels'),
                 ('notification_dispatches', 'sreality_id'), ('notification_dispatches', 'estimation_run_id'),
                 ('notification_dispatches', 'message'), ('notification_subscriptions', 'channels'),
                 ('notification_subscriptions', 'last_matched_first_seen_at'),
                 ('collections', 'notify_channels')) as t(rel, col)
    join pg_attribute a on a.attrelid = to_regclass('public.' || t.rel) and a.attname = t.col and not a.attisdropped
    join pg_depend d on d.refclassid = 'pg_class'::regclass and d.refobjid = a.attrelid and d.refobjsubid = a.attnum
   where d.classid <> 'pg_attrdef'::regclass
     and not (d.classid = 'pg_constraint'::regclass
              and d.objid in (select k.oid from pg_constraint k
                               where k.conrelid = 'public.notification_dispatches'::regclass
                                 and k.conname = any (c_own_constraints)))
     and d.objid is distinct from to_regclass('public.notification_dispatches_sreality_id_idx')::oid;
  if v_bad is not null then
    raise exception '656 refused: a dropped column has a dependent outside this file: %', v_bad;
  end if;

  -- Catalog dependents of a dropped table: only what the table owns (its constraints, indexes, sequences,
  -- TOAST, triggers, policies are all 'a'/'i' or a constraint on itself).
  select string_agg(format('%s <- %s', c.relname, pg_describe_object(d.classid, d.objid, d.objsubid)), '; ')
    into v_bad
    from pg_class c
    join pg_depend d on d.refclassid = 'pg_class'::regclass and d.refobjid = c.oid
   where c.relnamespace = 'public'::regnamespace and c.relkind = 'r' and c.relname = any (c_tables)
     and d.deptype = 'n'
     and not (d.classid = 'pg_constraint'::regclass
              and exists (select 1 from pg_constraint k where k.oid = d.objid and k.conrelid = c.oid));
  if v_bad is not null then
    raise exception '656 refused: a dropped table has a dependent outside it: %', v_bad;
  end if;

  select count(*) filter (where change_kind <> all (c_kinds)),
         count(*) filter (where not (
              (source_kind = 'watchdog'           and subscription_id is not null and collection_id is null)
           or (source_kind = 'collection_monitor' and collection_id   is not null and subscription_id is null)
           or (source_kind = 'share'              and subscription_id is null)))
    into v_kind, v_source
    from public.notification_dispatches
   where source_kind <> 'system_health';
  if v_kind > 0 or v_source > 0 then
    raise exception '656 refused: % row(s) outside system_health carry a change_kind and % a source the narrowed '
      'CHECKs refuse (a ''shared'' row 652/653 did not relabel?)', v_kind, v_source;
  end if;
end
$pre$;

-- ---------------------------------------------------------------------------
-- 1. The prune: one transaction, the feed's tables locked in the order a feed read takes them.
-- ---------------------------------------------------------------------------
begin;

lock table public.notification_dispatches, public.notification_subscriptions in access exclusive mode;

-- (a) R2 (b): the system rows, after proving no delivery references one.
do $system$
declare
  v_deliveries bigint;
  v_deleted bigint;
begin
  select count(*) into v_deliveries
    from public.notification_deliveries x
    join public.notification_dispatches d on d.id = x.notification_id
   where d.source_kind = 'system_health';
  if v_deliveries > 0 then
    raise exception '656 refused: % notification_deliveries row(s) reference a system_health dispatch', v_deliveries;
  end if;
  delete from public.notification_dispatches where source_kind = 'system_health';
  get diagnostics v_deleted = row_count;
  raise notice '656: % system_health dispatch(es) deleted (ruling R2 (b))', v_deleted;
end
$system$;

-- (b) The account trigger without its SYSTEM branch (641's body verbatim but for the else arm), and the CHECKs.
create or replace function public.sync_notification_dispatches_account_id()
returns trigger language plpgsql as $$
begin
  if new.source_kind = 'watchdog' then
    select account_id into new.account_id from notification_subscriptions where id = new.subscription_id;
  elsif new.source_kind = 'collection_monitor' then
    select account_id into new.account_id from collections where id = new.collection_id;
  elsif new.source_kind = 'share' then
    if new.account_id is null then
      raise exception 'share dispatch needs its recipient account';
    end if;
  else
    raise exception 'notification_dispatches has no producer %', new.source_kind;
  end if;
  return new;
end;
$$;

alter table public.notification_dispatches drop constraint if exists notification_dispatches_source_ck;
alter table public.notification_dispatches add constraint notification_dispatches_source_ck check (
     (source_kind = 'watchdog'           and subscription_id is not null and collection_id is null)
  or (source_kind = 'collection_monitor' and collection_id   is not null and subscription_id is null)
  or (source_kind = 'share'              and subscription_id is null));

alter table public.notification_dispatches drop constraint if exists notification_dispatches_change_kind_ck;
alter table public.notification_dispatches add constraint notification_dispatches_change_kind_ck check (
  change_kind in ('new', 'price_drop', 'price_rise', 'inactive', 'reactivated', 'new_source',
                  'shared_collection', 'shared_dashboard', 'shared_pipeline'));

-- (c) The nine columns, their constraints and index first, then the four replaced indexes.
alter table public.notification_dispatches
  drop constraint if exists notification_dispatches_dedupe_key_key,
  drop constraint if exists notification_dispatches_channel_check,
  drop constraint if exists notification_dispatches_status_check,
  drop constraint if exists notification_dispatches_estimation_run_id_fkey;
drop index if exists public.notification_dispatches_sreality_id_idx;
alter table public.notification_dispatches
  drop column if exists dedupe_key,
  drop column if exists trigger_snapshot_id,
  drop column if exists channel,
  drop column if exists status,
  drop column if exists error_message,
  drop column if exists target_channels,
  drop column if exists sreality_id,
  drop column if exists estimation_run_id,
  drop column if exists message;

drop index if exists public.notification_dispatches_unread_idx;
drop index if exists public.notification_dispatches_dispatched_at_idx;
drop index if exists public.notification_dispatches_dispatched_id_keyset_idx;
drop index if exists public.notification_dispatches_source_kind_idx;

comment on table public.notification_dispatches is
  'One immutable row per real-world change, owned by one account (notifications push program, migrations 206, '
  '652-656): three producers (watchdog, collection_monitor, share), one identity (notification_dispatches_identity_uq), '
  'one feed. Deliveries are notification_deliveries rows. Nothing deletes a row but the account and collection '
  'cascades; migration 656 deleted the system_health history once, after a backup (ruling R2 (b)).';
comment on column public.notification_dispatches.source_kind is
  'Which producer wrote this event: watchdog (subscription_id set), collection_monitor (collection_id set) or share '
  '(the recipient''s account, collection_id set for a collection); notification_dispatches_source_ck.';

-- (d) The watchdog's routing pick and its retired cursor.
alter table public.notification_subscriptions
  drop column if exists channels,
  drop column if exists last_matched_first_seen_at;

-- (e) The never-written ledgers, the API's lease, the dead switch and the nine settings rows.
drop table if exists public.channel_sends;
drop table if exists public.notification_suppression;
drop table if exists public.resend_webhook_events;
drop table if exists public.notification_matcher_lease;

drop function if exists public.emit_verification_stale_alert();

insert into public.app_settings_history (key, value, replaced_at, replaced_by)
select s.key, s.value, now(), s.updated_by
  from public.app_settings s
 where s.key in ('notification_email_to', 'notification_telegram_chat_id', 'system_health_channels',
                 'notifications_matcher_interval_seconds', 'notifications_match_window_listings',
                 'notifications_monitor_interval_seconds', 'notifications_monitor_window_days',
                 'notifications_outbox_interval_seconds', 'notifications_watermark_first_seen_at');
delete from public.app_settings
 where key in ('notification_email_to', 'notification_telegram_chat_id', 'system_health_channels',
               'notifications_matcher_interval_seconds', 'notifications_match_window_listings',
               'notifications_monitor_interval_seconds', 'notifications_monitor_window_days',
               'notifications_outbox_interval_seconds', 'notifications_watermark_first_seen_at');

commit;

-- ---------------------------------------------------------------------------
-- 2. The collection's old channel pick, on its own: one catalog change under collections' lock.
-- ---------------------------------------------------------------------------
alter table public.collections drop column if exists notify_channels;

-- ---------------------------------------------------------------------------
-- 3. Postconditions.
-- ---------------------------------------------------------------------------
do $post$
declare
  v_cols text[];
  v_def text;
  v_kinds text[];
begin
  select array_agg(column_name::text order by column_name::text collate "C") into v_cols
    from information_schema.columns
   where table_schema = 'public' and table_name = 'notification_dispatches';
  if v_cols is distinct from array['account_id', 'actor_account_id', 'change_kind', 'collection_id', 'dispatched_at',
                                   'duplicate_of', 'event_ref', 'id', 'listing_id', 'prev_price_czk', 'property_id',
                                   'seen_at', 'source_kind', 'subscription_id', 'trigger_price_czk'] then
    raise exception '656 did not land: notification_dispatches has columns %', v_cols;
  end if;
  if exists (select 1 from information_schema.columns
              where table_schema = 'public'
                and ((table_name = 'notification_subscriptions'
                      and column_name in ('channels', 'last_matched_first_seen_at'))
                     or (table_name = 'collections' and column_name = 'notify_channels'))) then
    raise exception '656 did not land: a watchdog or collection channel column survived';
  end if;
  if to_regclass('public.channel_sends') is not null or to_regclass('public.notification_suppression') is not null
     or to_regclass('public.resend_webhook_events') is not null
     or to_regclass('public.notification_matcher_lease') is not null then
    raise exception '656 did not land: a dropped table survived';
  end if;
  if to_regprocedure('public.emit_verification_stale_alert()') is not null then
    raise exception '656 did not land: emit_verification_stale_alert() survived';
  end if;
  if exists (select 1 from public.app_settings
              where key in ('notification_email_to', 'notification_telegram_chat_id', 'system_health_channels',
                            'notifications_matcher_interval_seconds', 'notifications_match_window_listings',
                            'notifications_monitor_interval_seconds', 'notifications_monitor_window_days',
                            'notifications_outbox_interval_seconds', 'notifications_watermark_first_seen_at')) then
    raise exception '656 did not land: a notification settings row survived';
  end if;
  if exists (select 1 from public.notification_dispatches where source_kind = 'system_health') then
    raise exception '656 did not land: a system_health row survived';
  end if;
  if to_regclass('public.notification_dispatches_unread_idx') is not null
     or to_regclass('public.notification_dispatches_dispatched_at_idx') is not null
     or to_regclass('public.notification_dispatches_dispatched_id_keyset_idx') is not null
     or to_regclass('public.notification_dispatches_source_kind_idx') is not null
     or to_regclass('public.notification_dispatches_sreality_id_idx') is not null
     or to_regclass('public.notification_dispatches_dedupe_key_key') is not null then
    raise exception '656 did not land: a replaced index survived';
  end if;

  select pg_get_constraintdef(oid) into v_def from pg_constraint
   where conrelid = 'public.notification_dispatches'::regclass and conname = 'notification_dispatches_source_ck';
  if v_def is null or v_def like '%system_health%' or v_def not like '%''watchdog''%'
     or v_def not like '%''collection_monitor''%' or v_def not like '%''share''%' then
    raise exception '656 did not land: notification_dispatches_source_ck is %', v_def;
  end if;
  select array_agg(m[1] order by m[1] collate "C") into v_kinds
    from pg_constraint k
   cross join lateral regexp_matches(pg_get_constraintdef(k.oid), '''(\w+)''', 'g') as m
   where k.conrelid = 'public.notification_dispatches'::regclass and k.conname = 'notification_dispatches_change_kind_ck';
  if v_kinds is distinct from array['inactive', 'new', 'new_source', 'price_drop', 'price_rise', 'reactivated',
                                    'shared_collection', 'shared_dashboard', 'shared_pipeline'] then
    raise exception '656 did not land: notification_dispatches_change_kind_ck allows %', v_kinds;
  end if;

  v_def := pg_get_functiondef('public.sync_notification_dispatches_account_id()'::regprocedure);
  if strpos(v_def, '00000000-0000-0000-0000-000000000000') > 0
     or strpos(v_def, 'raise exception ''notification_dispatches has no producer %''') = 0 then
    raise exception '656 did not land: sync_notification_dispatches_account_id() still stamps SYSTEM';
  end if;

  if not exists (select 1 from pg_index i
                  where i.indexrelid = to_regclass('public.notification_dispatches_identity_uq')
                    and i.indrelid = 'public.notification_dispatches'::regclass
                    and i.indisvalid and i.indisready and i.indisunique and i.indnullsnotdistinct
                    and pg_get_expr(i.indpred, i.indrelid) = '(duplicate_of IS NULL)')
     or to_regclass('public.notification_dispatches_account_feed_idx') is null then
    raise exception '656 did not land: 653''s identity or feed index is no longer valid';
  end if;
end
$post$;

reset statement_timeout;
reset lock_timeout;
