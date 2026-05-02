-- 003_listing_fetch_failures.sql
-- Track listings whose detail fetch has failed, so we can:
--   1) Prioritise them in to_refetch (the run cap doesn't keep deferring
--      a listing that's consistently positioned past the cap in the
--      sreality index).
--   2) Eventually give up on permanently-failing listings so we don't
--      retry them every run forever (after `attempts >= 5`, given_up
--      flips to true and the row drops out of the active retry queue).
--   3) Make persistent failures observable: SELECT * FROM
--      listing_fetch_failures ORDER BY attempts DESC LIMIT 50;
--
-- A listing leaves this table when its detail is successfully fetched
-- and a row lands in `listings`. Until then it stays here, attempts
-- incrementing with each failed run.

create table listing_fetch_failures (
  sreality_id      bigint primary key,
  attempts         integer not null default 1,
  first_failure_at timestamptz not null default now(),
  last_failure_at  timestamptz not null default now(),
  last_error       text,
  given_up         boolean not null default false
);

create index on listing_fetch_failures (sreality_id)
  where given_up = false;

alter table listing_fetch_failures enable row level security;
