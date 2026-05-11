-- 023_listing_notes.sql
-- Append-only journal of operator notes on individual listings.
--
-- Each row is one note; there is no edit/delete UI in v1. The operator
-- can SQL-prune if the table grows. The append-only shape mirrors
-- listing_freshness_checks (migration 006) — observability rows that
-- accumulate and are pruned out-of-band.
--
-- Reads via listing_notes_public (migration 025); writes via the API.

create table listing_notes (
  id          bigserial   primary key,
  sreality_id bigint      not null references listings(sreality_id) on delete cascade,
  body        text        not null check (length(body) between 1 and 4000),
  created_at  timestamptz not null default now()
);

create index listing_notes_by_listing on listing_notes (sreality_id, created_at desc);

alter table listing_notes enable row level security;
