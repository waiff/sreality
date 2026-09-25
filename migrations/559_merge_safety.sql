-- 559: merge safety — one per-advert price-step view, and no status event on a merge.
--
-- 1. `listing_price_steps`: ONE definition of a price step, read by all three consumers —
--    the property rollup (scripts/recompute_property_stats.py), the watchdog price-drop
--    matcher and the collection monitor (api/notifications.py). Until now each carried its
--    own window, and the two notification copies PARTITIONed BY PROPERTY: every advert of a
--    merged property went into one price series, so two portals quoting 5.0M and 5.2M read
--    as a drop and a rise on every scrape — a merge alone fired false alerts. A step here is
--    one advert's priced snapshot whose price differs from THAT advert's previous priced
--    snapshot (ordered by scraped_at, then id), so a step never spans two adverts.
--
--    Built with a LATERAL "previous priced snapshot" probe instead of lag(): a view with a
--    window function cannot take a join qual, so `JOIN batch` / `JOIN monitored` would
--    compute the window over every snapshot in the market first. Without one the view is
--    flattened into the caller and each consumer's own scope (a property batch, the
--    monitored set, a scraped_at window) drives it; the probe rides
--    listing_snapshots_listing_id_scraped_at_idx (migration 333).
--
-- 2. `log_property_status_event` (migration 392) skips a row that is or was `merged_away`.
--    `merge_properties` carries the retired property's status history onto the survivor and
--    then retires it with `is_active = false`, which fired the trigger and wrote a fresh
--    'inactive' row onto the absorbed property; an unmerge then showed a false inactive gap
--    and wrote an 'active' row at the unmerge instant. Retirement and reactivation are not
--    activity transitions of the real property: `unmerge_group` restores `is_active` in the
--    same statement that clears `merged_away`, so neither side of a merge/unmerge logs.
--
-- ADDITIVE: one new view (service-role only, security_invoker), one function body replaced
-- in place (same signature, same trigger). No row is written or removed.

set lock_timeout = '5s';

create view listing_price_steps
with (security_invoker = true) as
select
  l.property_id,
  s.listing_id,
  s.id           as snapshot_id,
  s.scraped_at,
  s.price_czk,
  prev.price_czk as prev_price_czk
from listing_snapshots s
join listings l on l.id = s.listing_id
cross join lateral (
  select p.price_czk
  from listing_snapshots p
  where p.listing_id = s.listing_id
    and p.price_czk is not null
    and p.scraped_at <= s.scraped_at
    and (p.scraped_at, p.id) < (s.scraped_at, s.id)
  order by p.scraped_at desc, p.id desc
  limit 1
) prev
where s.price_czk is not null
  and s.price_czk <> prev.price_czk;

comment on view listing_price_steps is
  'One row per price change WITHIN one advert (migration 559): the advert''s priced snapshot '
  'and the price of its own previous priced snapshot. The one price-step definition read by '
  'the property rollup, the watchdog and the collection monitor; a step never spans two adverts.';

revoke all on listing_price_steps from anon, authenticated;

create or replace function log_property_status_event() returns trigger
language plpgsql
as $$
begin
  if TG_OP = 'INSERT' then
    insert into property_status_events (property_id, is_active, event_at)
    values (NEW.id, NEW.is_active, now());
  elsif OLD.is_active is distinct from NEW.is_active
        and OLD.status <> 'merged_away' and NEW.status <> 'merged_away' then
    insert into property_status_events (property_id, is_active, event_at)
    values (NEW.id, NEW.is_active, now());
  end if;
  return NEW;
end;
$$;

revoke execute on function log_property_status_event() from public, anon, authenticated;
