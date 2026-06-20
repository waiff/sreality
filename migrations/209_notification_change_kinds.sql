-- 209: widen notification_dispatches.change_kind for the collection-monitor's
-- full taxonomy (Sprint C).
--
-- PR A's migration 206 added the change_kind CHECK with the watchdog's four
-- kinds (new / price_drop / price_rise / inactive). The collection-monitor
-- producer also emits, for a property in a monitored collection:
--   reactivated   — a delisted monitored property came back active
--   new_source    — the property gained a sibling listing on another portal
--   broker_change — the property's canonical broker changed
--
-- Additive: drop + re-add the CHECK with the superset (the four existing values
-- are preserved, so no row can violate it).

alter table notification_dispatches
  drop constraint if exists notification_dispatches_change_kind_ck;

alter table notification_dispatches
  add constraint notification_dispatches_change_kind_ck
  check (change_kind in (
    'new', 'price_drop', 'price_rise', 'inactive',
    'reactivated', 'new_source', 'broker_change'
  ));
