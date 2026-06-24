-- The collection-monitor producer (rule #16) must only alert on changes that
-- happened AFTER monitoring began for a (collection, property) pair — not any
-- change inside the lookback window. Without this, adding a property to a
-- monitored collection just after a price drop fires a stale "price drop".
--
-- The per-PROPERTY anchor is collection_properties.added_at (already present).
-- This column is the per-COLLECTION anchor: when monitoring was last turned on.
-- The producer gates every detector on greatest(added_at, monitoring_enabled_at),
-- so neither "added after the change" nor "enabled monitoring after the change"
-- can false-fire.
--
-- A trigger stamps the column on every false->true transition (and on insert
-- with monitoring already on) so the anchor is correct across ALL write paths
-- (API, MCP, future code), not just the one curation endpoint.

alter table collections add column if not exists monitoring_enabled_at timestamptz;

create or replace function collections_stamp_monitoring_enabled_at()
returns trigger language plpgsql as $$
begin
  if NEW.monitoring_enabled
     and (TG_OP = 'INSERT' or OLD.monitoring_enabled is distinct from true) then
    NEW.monitoring_enabled_at := now();
  end if;
  return NEW;
end;
$$;

drop trigger if exists collections_monitoring_enabled_at on collections;
create trigger collections_monitoring_enabled_at
  before insert or update on collections
  for each row execute function collections_stamp_monitoring_enabled_at();

-- Backfill already-monitored collections to their creation time. added_at is
-- always later than the collection's own creation, so it binds as the anchor —
-- exactly the desired "only changes after the property was added" semantic for
-- existing members. (This UPDATE doesn't flip monitoring_enabled, so the trigger
-- leaves the explicit created_at value intact.)
update collections
   set monitoring_enabled_at = created_at
 where monitoring_enabled and monitoring_enabled_at is null;
