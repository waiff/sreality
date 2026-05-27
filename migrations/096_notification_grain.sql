-- 096_notification_grain.sql
-- Slice 2b: notifications move to the property grain + a change-event kind.
--
-- notification_dispatches gains:
--   property_id  -- the canonical property the dispatch is about (FK).
--   change_kind  -- 'new' (property newly matched the filter) or 'price_drop'
--                   (a price decrease observed in the lookback window).
--                   Default 'new' so existing rows + the migrated "new listing"
--                   matcher keep their meaning.
-- The dedup grain becomes (subscription_id, property_id, change_kind): one
-- notification per real property per event kind, instead of once per portal
-- listing. The old (subscription_id, sreality_id) unique is dropped -- with
-- change_kind it would block a property from ever firing more than one kind.
--
-- sreality_id stays on the row (= the property's representative listing) so
-- the feed join + the "run estimation from dispatch" path keep working.
--
-- property_id is left NULLABLE (not SET NOT NULL): the property-grain matcher
-- always sets it, but tightening now would risk the brief deploy window below.
--
-- Deploy note: the pre-2b matcher used ON CONFLICT (subscription_id,
-- sreality_id); once that unique is dropped its INSERT errors. matcher_loop
-- catches the per-pass error and continues (notifications briefly pause),
-- self-healing when the Slice 2b api deploys. Apply close to that deploy.

alter table notification_dispatches
  add column if not exists property_id bigint
    references properties(id) on delete cascade,
  add column if not exists change_kind text not null default 'new';

update notification_dispatches d
set property_id = l.property_id
from listings l
where l.sreality_id = d.sreality_id and d.property_id is null;

alter table notification_dispatches
  drop constraint if exists notification_dispatches_subscription_id_sreality_id_key;

alter table notification_dispatches
  add constraint notification_dispatches_sub_property_kind_key
  unique (subscription_id, property_id, change_kind);

create index if not exists notification_dispatches_property_idx
  on notification_dispatches (property_id);
