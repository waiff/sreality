-- 186_broker_listing_links_and_queues.sql
--
-- Broker intelligence, phase 1 (part 2): the listing<->broker links, the decoupled
-- work queue, the reversible merge ledger, and the run observability table. Purely
-- additive. The hot scrape write path stays clean: it only sets broker_identity_id
-- + enqueues dirty_broker_listings; ALL rollups/grouping/firm resolution happen
-- off-path in scripts.resolve_brokers (mirrors dirty_properties / rule #20).

-- Point-in-time links on each listing. broker_identity_id = the exact per-source
-- identity that posted this ad; broker_firm_id = the canonical firm as observed on
-- THIS listing (a broker can change firms — the listing keeps the historical fact).
alter table listings
  add column broker_identity_id bigint references broker_identities(id) on delete set null,
  add column broker_firm_id     bigint references firms(id)             on delete set null;

create index listings_broker_identity_idx on listings (broker_identity_id) where broker_identity_id is not null;
create index listings_broker_firm_idx     on listings (broker_firm_id)     where broker_firm_id is not null;
-- Stragglers: listings whose broker hasn't been attributed yet (covers every new
-- listing, inserted with NULL). The resolver's attach phase scans this.
create index listings_broker_unattributed_idx on listings (sreality_id)
  where broker_identity_id is null and raw_json is not null;

-- Decoupled work queue (dirty_properties analog, listing grain): writers enqueue a
-- sreality_id when a content change may have altered its broker block; the resolver
-- re-derives attribution for those + deletes them. New listings are covered by the
-- unattributed straggler scan above, so they are not enqueued here.
create table dirty_broker_listings (
  sreality_id bigint primary key references listings(sreality_id) on delete cascade,
  marked_at   timestamptz not null default now()
);
alter table dirty_broker_listings enable row level security;
comment on table dirty_broker_listings is
  'Phase-1 broker work queue: listing ids whose content changed since the last '
  'resolver pass (broker may have changed). scripts.resolve_brokers --incremental '
  'drains + re-attributes, then deletes; the daily full sweep clears it.';

-- Per-run observability (dedup_engine_runs analog). Anon reads the _public view.
create table broker_resolution_runs (
  id                       bigserial primary key,
  started_at               timestamptz not null default now(),
  ended_at                 timestamptz,
  mode                     text not null,                 -- 'incremental' | 'full' | 'backfill'
  identities_upserted      integer not null default 0,
  listings_attributed      integer not null default 0,
  brokers_recomputed       integer not null default 0,
  firms_recomputed         integer not null default 0,
  edges_built              integer not null default 0,
  components_formed        integer not null default 0,
  auto_merges              integer not null default 0,
  queued_for_review        integer not null default 0,
  shared_contacts_excluded integer not null default 0,
  notes                    text
);
create index broker_resolution_runs_started_idx on broker_resolution_runs (started_at desc);
alter table broker_resolution_runs enable row level security;

create view broker_resolution_runs_public as
select
  id, started_at, ended_at, mode,
  identities_upserted, listings_attributed, brokers_recomputed, firms_recomputed,
  edges_built, components_formed, auto_merges, queued_for_review,
  shared_contacts_excluded
from broker_resolution_runs;
grant select on broker_resolution_runs_public to anon;

-- Reversible cross-source merge ledger (property_merge_events analog): one row per
-- moved broker_identity. prev_broker_id is captured so unmerge is a deterministic
-- replay even after the survivor absorbs a third identity. bridge_value records the
-- contact that formed the edge (audit). Inert until a second source lands (phase 2).
create table broker_merge_events (
  id                 bigserial primary key,
  merge_group_id     uuid        not null,
  survivor_broker_id bigint      not null references brokers(id),
  retired_broker_id  bigint      not null references brokers(id),
  identity_id        bigint      not null references broker_identities(id),
  prev_broker_id     bigint      not null,
  reason             text        not null,
  bridge_kind        text,                                -- 'email' | 'phone' | NULL (operator)
  bridge_value       text,
  source             text        not null default 'auto'
                       check (source in ('auto', 'operator')),
  undone_at          timestamptz,
  undone_by          text,
  created_at         timestamptz not null default now()
);
create index broker_merge_events_group_idx    on broker_merge_events (merge_group_id);
create index broker_merge_events_survivor_idx on broker_merge_events (survivor_broker_id);
create index broker_merge_events_active_idx
  on broker_merge_events (merge_group_id) where undone_at is null;
alter table broker_merge_events enable row level security;

-- Operator-tunable resolver dictionaries (committed-seed -> app_settings pattern,
-- rule #14). Placeholders here; scripts.seed_broker_settings fills the domain
-- lists. broker_auto_merge_sources gates auto-merge per source: a cross-source
-- edge auto-merges only when BOTH its sources are listed — so a new portal queues
-- for review until its frequency distribution is validated (review fix).
insert into app_settings (key, value, description, updated_by) values
  ('broker_free_email_domains', '[]'::jsonb,
   'Free/ISP email domains that are NOT firms (gmail.com, seznam.cz, ...). Seeded by scripts.seed_broker_settings.',
   'migration_186'),
  ('broker_franchise_domains', '[]'::jsonb,
   'Domains treated as one brand-level firm despite many independent offices (re-max.cz, century21.cz, ...).',
   'migration_186'),
  ('broker_auto_merge_sources', '["sreality"]'::jsonb,
   'Sources whose cross-source contact bridges may auto-merge. A bridge auto-merges only when BOTH sides are listed; otherwise it queues for operator review.',
   'migration_186')
on conflict (key) do nothing;
