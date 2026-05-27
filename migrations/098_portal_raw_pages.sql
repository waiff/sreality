-- 098_portal_raw_pages.sql
-- Slice 3b: raw-HTML staging for HTML/crawler portal sources (bazos first).
--
-- HTML sources land their fetched page bytes here so fetch is decoupled from
-- parse: a page can be RE-PARSED without re-fetching as the classifieds
-- parser improves (politeness + iteration). Generic by `source`, so future
-- portals reuse the same table. Latest-wins on (source, source_id_native,
-- page_kind) — a re-fetch overwrites the stored HTML and resets parse state.
--
-- Append-only/ephemeral retention like listing_freshness_checks: rows are
-- safe to delete once parsed and the listing is persisted; no automated
-- pruner — manual SQL when it grows. The durable record is the `listings` /
-- `listing_snapshots` row the parse produces, not this staging row.

create table if not exists portal_raw_pages (
  id               bigserial primary key,
  source           text not null,
  source_id_native text not null,
  source_url       text not null,
  page_kind        text not null check (page_kind in ('index', 'detail')),
  html             text not null,
  http_status      int,
  fetched_at       timestamptz not null default now(),
  parsed_at        timestamptz,
  parse_error      text,
  constraint portal_raw_pages_key unique (source, source_id_native, page_kind)
);

create index if not exists portal_raw_pages_unparsed_idx
  on portal_raw_pages (source, fetched_at)
  where parsed_at is null;

alter table portal_raw_pages enable row level security;
