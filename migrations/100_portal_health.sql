-- 100_portal_health.sql
--
-- Per-portal Health-page support. Generalises the sreality-only scrape
-- stats into a registry-driven, multi-portal model so the Health dashboard
-- can show every data source we run — scheduled scrapers (sreality, bazos)
-- and on-demand URL parsers (bezrealitky, idnes_reality, remax) — each with
-- the metrics that make sense for its kind. Adding a portal later is a single
-- INSERT into `portals`; no code or schema change.
--
-- Pieces:
--   1. scrape_runs.source — which portal a run belongs to (default 'sreality'
--      backfills every existing row). scrape_runs_public + recent_scrape_runs
--      are re-created to carry it.
--   2. portals — operator-facing registry of known sources (label, kind,
--      stage, home_url, sort_order). Seeded with today's five.
--   3. Owner-privileged aggregate views, anon-granted, so the SECURITY
--      INVOKER summary RPC can read per-source activity without exposing base
--      tables (same contract as listings_public / scrape_runs_public):
--        - portals_public         (the registry, sans bookkeeping columns)
--        - portal_listing_counts  (source -> total / active listing counts)
--        - parsed_url_activity    (source_kind -> parse counts + recency)
--   4. portal_health_summary() — joins the registry to activity, returns one
--      JSON object per portal for the dashboard's source catalogue.

------------------------------------------------------------------
-- 1. scrape_runs.source
------------------------------------------------------------------

alter table scrape_runs
  add column source text not null default 'sreality';

create index scrape_runs_source_started_idx on scrape_runs (source, started_at desc);

-- Re-create the anon-readable view + RPC to carry the new column. Column
-- order matches the table so recent_scrape_runs can keep its
-- `returns setof scrape_runs` rowtype.
create or replace view scrape_runs_public as
  select
    id,
    started_at,
    ended_at,
    run_type,
    index_pages,
    listings_found_new,
    listings_scraped_new,
    listings_updated,
    listings_inactive,
    images_discovered,
    images_stored,
    errors,
    by_category,
    source
  from scrape_runs;

grant select on scrape_runs_public to anon;

create or replace function recent_scrape_runs(p_days int default 14)
returns setof scrape_runs
language sql
stable
security invoker
as $$
  select *
  from scrape_runs_public
  where started_at > now() - make_interval(days => p_days)
  order by started_at desc
$$;

grant execute on function recent_scrape_runs(int) to anon;

------------------------------------------------------------------
-- 2. portals registry
------------------------------------------------------------------

create table portals (
  source     text primary key,
  label      text not null,
  kind       text not null check (kind in ('scraper', 'parser')),
  stage      text not null default 'live'
               check (stage in ('live', 'pilot', 'on_demand', 'planned')),
  home_url   text,
  sort_order int  not null default 100,
  is_enabled boolean not null default true,
  added_at   timestamptz not null default now()
);

alter table portals enable row level security;
-- No anon policy. Reads go through portals_public (below), same pattern as
-- the other Health-page tables.

create view portals_public as
  select source, label, kind, stage, home_url, sort_order, is_enabled
  from portals;

grant select on portals_public to anon;

-- `source` is the join key into the activity tables: it matches
-- listings.source / scrape_runs.source for scrapers and
-- parsed_url_cache.source_kind for on-demand parsers.
insert into portals (source, label, kind, stage, home_url, sort_order) values
  ('sreality',      'Sreality',      'scraper', 'live',      'https://www.sreality.cz',    10),
  ('bazos',         'Bazoš',         'scraper', 'pilot',     'https://reality.bazos.cz',   20),
  ('bezrealitky',   'Bezrealitky',   'parser',  'on_demand', 'https://www.bezrealitky.cz', 30),
  ('idnes_reality', 'iDNES Reality', 'parser',  'on_demand', 'https://reality.idnes.cz',   40),
  ('remax',         'RE/MAX',        'parser',  'on_demand', 'https://www.remax-czech.cz', 50);

------------------------------------------------------------------
-- 3. Owner-privileged activity aggregates (anon-granted)
------------------------------------------------------------------

-- Per-source listing roll-up. Only emits aggregate counts + recency, so it
-- is safe to expose to anon even though the base `listings` table is RLS'd.
create view portal_listing_counts as
  select
    source,
    count(*)                                                            as listings_total,
    count(*) filter (where is_active)                                   as listings_active,
    count(*) filter (where is_active and last_seen_at > now() - interval '7 days')
                                                                        as listings_active_7d,
    max(last_seen_at)                                                   as last_seen_at
  from listings
  group by source;

grant select on portal_listing_counts to anon;

-- Per-source on-demand parse activity. Counts + recency only — no HTML,
-- no extracted payloads.
create view parsed_url_activity as
  select
    source_kind                                                         as source,
    count(*)                                                            as parses_total,
    count(*) filter (where parsed_at > now() - interval '30 days')      as parses_30d,
    max(parsed_at)                                                      as last_parsed_at
  from parsed_url_cache
  group by source_kind;

grant select on parsed_url_activity to anon;

------------------------------------------------------------------
-- 4. portal_health_summary()
------------------------------------------------------------------

create or replace function portal_health_summary()
returns jsonb
language sql
stable
security invoker
as $$
  select coalesce(jsonb_agg(
    jsonb_build_object(
      'source',             p.source,
      'label',              p.label,
      'kind',               p.kind,
      'stage',              p.stage,
      'home_url',           p.home_url,
      'listings_total',     coalesce(lc.listings_total, 0),
      'listings_active',    coalesce(lc.listings_active, 0),
      'listings_active_7d', coalesce(lc.listings_active_7d, 0),
      'parses_total',       coalesce(pa.parses_total, 0),
      'parses_30d',         coalesce(pa.parses_30d, 0),
      'last_scrape_at',     sr.last_run_at,
      'runs_7d',            coalesce(sr.runs_7d, 0),
      'scraped_new_7d',     coalesce(sr.scraped_new_7d, 0),
      'inactive_7d',        coalesce(sr.inactive_7d, 0),
      'errors_7d',          coalesce(sr.errors_7d, 0),
      'last_parsed_at',     pa.last_parsed_at,
      'last_activity_at',   greatest(sr.last_run_at, pa.last_parsed_at, lc.last_seen_at)
    )
    order by p.sort_order, p.label
  ), '[]'::jsonb)
  from portals_public p
  left join portal_listing_counts lc on lc.source = p.source
  left join parsed_url_activity   pa on pa.source = p.source
  left join (
    select
      source,
      max(ended_at) as last_run_at,
      count(*)                       filter (where started_at > now() - interval '7 days') as runs_7d,
      coalesce(sum(listings_scraped_new) filter (where started_at > now() - interval '7 days'), 0) as scraped_new_7d,
      coalesce(sum(listings_inactive)    filter (where started_at > now() - interval '7 days'), 0) as inactive_7d,
      coalesce(sum(errors)               filter (where started_at > now() - interval '7 days'), 0) as errors_7d
    from scrape_runs_public
    group by source
  ) sr on sr.source = p.source
  where p.is_enabled
$$;

grant execute on function portal_health_summary() to anon;
