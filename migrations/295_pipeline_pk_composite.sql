-- 295_pipeline_pk_composite.sql
-- Phase 1 increment 3, part 6/6 — the pipeline PK hard break. GATED:
-- apply ONLY in the same deploy window as (and strictly AFTER the full
-- rollout of) the rewritten Python:
--   * api/pipeline.py: ON CONFLICT (account_id, property_id) in add/move,
--     account_id stamped into every property_pipeline AND
--     property_pipeline_events INSERT (add_card/remove_card/move_card),
--     account-scoped entry-stage lookup;
--   * toolkit/pipeline_identity.py: account-partitioned reconcile/unmerge.
-- Old code still running against this schema fails on every bookmark
-- (42P10: ON CONFLICT (property_id) can no longer infer) and every event
-- INSERT (NOT NULL violation) — a hard outage, not silent drift.
--
-- Also requires the legacy backfill to have run (the operator's first
-- signup) — the guard below refuses to apply while NULL account_id rows
-- remain, failing the migration instead of corrupting data.

begin;

do $$
begin
  if exists (select 1 from property_pipeline where account_id is null)
     or exists (select 1 from pipeline_stages where account_id is null)
     or exists (select 1 from property_pipeline_events where account_id is null) then
    raise exception 'unbackfilled account_id rows remain — do not apply 295 yet';
  end if;
end $$;

alter table property_pipeline alter column account_id set not null;
alter table pipeline_stages alter column account_id set not null;
alter table property_pipeline_events alter column account_id set not null;

alter table property_pipeline drop constraint property_pipeline_pkey;
alter table property_pipeline add constraint property_pipeline_pkey
  primary key (account_id, property_id);

-- A card's stage must belong to the card's account. FK checks bypass RLS, so
-- enforce it structurally: composite FK against a (account_id, id) unique on
-- pipeline_stages. Without this, a cross-account stage_id write would make
-- the account-partitioned reconciler silently no-op its keep-most-advanced
-- step and then drop the retired card.
create unique index if not exists pipeline_stages_account_id_id_uq
  on pipeline_stages (account_id, id);
alter table property_pipeline drop constraint property_pipeline_stage_id_fkey;
alter table property_pipeline add constraint property_pipeline_stage_id_fkey
  foreign key (account_id, stage_id) references pipeline_stages (account_id, id);

commit;
