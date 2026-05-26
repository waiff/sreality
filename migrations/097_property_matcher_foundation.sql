-- 097_property_matcher_foundation.sql
-- Slice 3a of the multi-portal dedup track: the insert-time Tier-1 matcher
-- foundation. No portal scraper yet; this is the portal-agnostic engine +
-- the schema the matcher needs.
--
-- 1. synthetic_listing_id_seq — non-sreality listings need a value for the
--    `listings.sreality_id` PK (it is the global id ~9 FK tables hang off).
--    Decision #1 keeps sreality rows' PK == their real id and numbers
--    everything else ourselves. This sequence issues NEGATIVE ids
--    (descending from -1), which can never collide with sreality's positive
--    hash ids and make non-sreality rows unmistakable at a glance. sreality
--    rows never draw from it.
--
-- 2. property_identity_candidates — the review queue for ambiguous matches.
--    Tier 1's "multiple spatial hits -> never guess" branch enqueues an
--    ordered (left<right) property pair here instead of merging; the Slice 4
--    operator UI resolves them. Brought forward from the original Slice-4
--    sketch because the insert-time matcher writes to it now.

create sequence if not exists synthetic_listing_id_seq
  as bigint
  increment by -1
  minvalue -9223372036854775808
  maxvalue -1
  start with -1
  no cycle;

create table if not exists property_identity_candidates (
  id               bigserial primary key,
  left_property_id  bigint not null references properties(id) on delete cascade,
  right_property_id bigint not null references properties(id) on delete cascade,
  confidence       numeric,
  markers_matched  jsonb,
  tier             text not null,
  status           text not null default 'proposed'
                     check (status in ('proposed', 'merged', 'dismissed')),
  created_at       timestamptz not null default now(),
  reviewed_at      timestamptz,
  reviewed_action  text,
  -- Ordered pair: store each candidate once regardless of discovery order.
  constraint property_identity_candidates_ordered check (left_property_id < right_property_id),
  constraint property_identity_candidates_pair_key unique (left_property_id, right_property_id)
);

create index if not exists property_identity_candidates_status_idx
  on property_identity_candidates (status);

alter table property_identity_candidates enable row level security;
