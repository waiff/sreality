-- 206_notifications_unified_event_model.sql
--
-- PR A (joint foundation for the two notification sprints): generalize the
-- watchdog-only `notification_dispatches` into the UNIFIED notification event
-- model that both the watchdog matcher AND the collection-monitor producer
-- (Sprint C) write, that the one in-app Notifications feed reads, and that the
-- channel-delivery layer (Sprint N) drains. See
-- docs/design/notifications-unified.md for the shared contract.
--
-- We KEEP the physical table name `notification_dispatches` (no rename): every
-- reference is contained to api/notifications.py (no view / RPC / frontend
-- touches it — the feed goes through the bearer-gated API), and a rename would
-- buy a deploy-window where feed reads 500 for no semantic gain. Conceptually
-- this IS the "notifications" event table.
--
-- CORRECTION of a load-bearing doc error: migration 057's comment + CLAUDE.md
-- rule #16 claimed adding a delivery channel was "a one-line ALTER". That is
-- FALSE: migration 096 moved the dedup key to UNIQUE(subscription_id,
-- property_id, change_kind) with `channel` deliberately omitted, so a second
-- channel can never be a CHECK widen — the *grain* can't carry it. Delivery gets
-- its own ledger (Sprint N's `channel_sends`); this migration makes the EVENT
-- model source-generic + per-snapshot. (CLAUDE.md / ROADMAP are corrected in the
-- same PR; migration 057 is append-only and left untouched.)
--
-- What changes:
--   + source_kind            'watchdog' | 'collection_monitor' (existing rows = watchdog)
--   + collection_id          FK -> collections (set only for collection_monitor)
--   ~ subscription_id        NOT NULL dropped (NULL for collection_monitor rows)
--   + trigger_price/prev_price/trigger_snapshot_id   provenance ("why was I pinged")
--   + target_channels text[] producer-stamped non-in_app delivery channels (Sprint N)
--   + dedupe_key  text       SINGLE idempotency key, replacing the composite UNIQUE.
--                            Encodes the per-event identity so it spans BOTH sources
--                            (NULL subscription_id/collection_id can't anchor a composite
--                            unique) AND per-snapshot change events:
--                              new:        wd:{sub}:new:{property_id}        (once ever)
--                              price_drop: wd:{sub}:price_drop:{snapshot_id} (per change)
--                            The matcher's re-run idempotency lives here now.
--
-- DEPLOY NOTE (apply CLOSE to the api deploy; 096 precedent): dropping the old
-- composite UNIQUE makes the OLD matcher's `ON CONFLICT (subscription_id,
-- property_id, change_kind)` INSERT error until the new code deploys. matcher_loop
-- catches the per-pass error and continues (notifications briefly pause, NOT the
-- feed reads), self-healing on deploy. Per-snapshot price_drop is why both
-- constraints cannot coexist (the composite would reject a 2nd drop the dedupe_key
-- allows). The merge reconciler (toolkit/operator_state.py) is updated in lockstep.

begin;

alter table notification_dispatches
  add column if not exists source_kind text not null default 'watchdog',
  add column if not exists collection_id bigint references collections(id) on delete cascade,
  add column if not exists trigger_price_czk int,
  add column if not exists prev_price_czk int,
  add column if not exists trigger_snapshot_id bigint,
  add column if not exists target_channels text[] not null default '{}',
  add column if not exists dedupe_key text;

alter table notification_dispatches
  alter column subscription_id drop not null;

-- Backfill the dedupe_key for existing rows (all watchdog, all property_id NOT
-- NULL — verified 846/846 unique). 'new' rows MUST take the matcher's canonical
-- formula so a re-run never re-fires 'new' for an already-notified property;
-- historical 'price_drop' rows (older than the matcher's lookback window, so
-- never re-evaluated) take an id-unique legacy key.
update notification_dispatches
set dedupe_key = case
  when change_kind = 'new'
    then 'wd:' || subscription_id::text || ':new:' || property_id::text
  else 'wd:' || subscription_id::text || ':' || change_kind || ':legacy:' || id::text
end
where dedupe_key is null;

alter table notification_dispatches
  alter column dedupe_key set not null;

-- Swap the dedup primitive (the breaking step — see DEPLOY NOTE).
alter table notification_dispatches
  drop constraint if exists notification_dispatches_sub_property_kind_key;
alter table notification_dispatches
  add constraint notification_dispatches_dedupe_key_key unique (dedupe_key);

-- Exactly one source FK set, matching source_kind.
alter table notification_dispatches
  add constraint notification_dispatches_source_ck check (
    (source_kind = 'watchdog'           and subscription_id is not null and collection_id is null)
 or (source_kind = 'collection_monitor' and collection_id   is not null and subscription_id is null)
  );

-- change_kind value set (no prior CHECK existed; existing values new/price_drop
-- are in the set).
alter table notification_dispatches
  add constraint notification_dispatches_change_kind_ck
  check (change_kind in ('new', 'price_drop', 'price_rise', 'inactive'));

create index if not exists notification_dispatches_collection_idx
  on notification_dispatches (collection_id);
create index if not exists notification_dispatches_source_kind_idx
  on notification_dispatches (source_kind);

comment on column notification_dispatches.source_kind is
  'Which producer emitted this event: watchdog (filter match) or collection_monitor '
  '(a change to a property in a monitored collection). Exactly one of subscription_id / '
  'collection_id is set, enforced by notification_dispatches_source_ck.';
comment on column notification_dispatches.dedupe_key is
  'Single per-event idempotency key (replaces the composite UNIQUE). Deterministic per '
  '(source, event, subject-or-snapshot) so matcher re-runs never duplicate and a 2nd real '
  'price change is its own event. property_id is re-pointed on merge but the key is stable '
  '(see toolkit/operator_state.py).';
comment on column notification_dispatches.target_channels is
  'Producer-stamped non-in_app delivery channels for this event. The Sprint N outbox '
  'drains notification_dispatches x unnest(target_channels); in_app needs no entry (the '
  'feed reads this row directly).';

commit;
