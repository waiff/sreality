-- 022_collections.sql
-- Operator-curated lists of listings ("collections").
--
-- Single shared workspace: no per-user identity, no sharing semantics,
-- no soft-delete. Reads happen via collections_public (migration 025);
-- writes happen via the FastAPI service. RLS is enabled with no
-- policies — anon never sees the raw tables.
--
-- updated_at is bumped by application code on rename/edit and on
-- membership changes (no trigger; same convention as estimation_runs).
--
-- on delete cascade on the listing FK is structurally consistent but
-- effectively dead: listings are never deleted (CLAUDE.md rule #3).

create table collections (
  id          bigserial   primary key,
  name        text        not null check (length(name) between 1 and 200),
  description text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create unique index collections_name_ci on collections (lower(name));

create table collection_listings (
  collection_id bigint      not null references collections(id) on delete cascade,
  sreality_id   bigint      not null references listings(sreality_id) on delete cascade,
  added_at      timestamptz not null default now(),
  primary key (collection_id, sreality_id)
);

create index collection_listings_by_listing on collection_listings (sreality_id);

alter table collections         enable row level security;
alter table collection_listings enable row level security;
