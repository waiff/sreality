-- 545_sold_comps_read_surface.sql — the sold-comps READ surface (sold-comps W3).
--
-- Migration 542 put registered sales in `sold_transactions` + their fetch ledger in
-- `sold_transaction_fetches`, both deny-all to browser roles. This file is the ONE way
-- those facts reach a browser: two definer-style views, and two INVOKER functions over
-- them — "the sales within N metres of this point" and "when did we last look here".
--
-- WHY DEFINER-STYLE VIEWS + INVOKER FUNCTIONS. Both base tables carry `enable row level
-- security` with ZERO policies, so `authenticated` sees nothing on them. A plain view
-- (security_invoker UNSET) runs as its OWNER and bypasses that deny-all — the standing
-- shape for MARKET data here, `properties_public` being the template (migration 508).
-- Migration 536's `security_invoker = true` is the TENANT shape and would return zero
-- rows. The functions then stay SECURITY INVOKER (the default), which is why they must
-- read the VIEWS and never the tables.
--
-- WHY NO `SET` CLAUSE, AND WHY THREE ARGS. A set-returning SQL function that is STABLE,
-- SECURITY INVOKER, a single SELECT and carries NO `SET` clause is INLINED by the
-- planner, so PostgREST's own filters, ORDER BY and LIMIT reach the GiST index on
-- `(geom::geography)` exactly as they would against the view (migration 537's contract).
-- A `SET search_path` would forfeit that; PostGIS lives in `public` here (migration 001),
-- so none is needed. Migration 109 is the anti-pattern this avoids: ~50 optional
-- null-guarded filter params made that function un-inlinable, generically planned and
-- timed out. `sold_comparables` therefore takes the point and the radius and NOTHING
-- else — every other narrowing is a PostgREST predicate on the returned columns, and no
-- future filter may arrive here as an optional parameter.
--
-- MULTI-TENANT NOTE. There is no `account_id` anywhere in this surface: a registered sale
-- is market data, not user state. The single `grant select ... to authenticated` on each
-- view (and `grant execute` on each function) IS the dissemination switch — revoke it and
-- the whole dataset goes dark to every signed-in account at once.
--
-- CAST AT EVERY CALL SITE: `geom` is geometry, so an uncast ST_DWithin/ST_Distance
-- measures DEGREES and silently answers a different question (migration 507's rail).
--
-- Additive and idempotent; no `--single-transaction`, so every statement is re-runnable
-- from statement 1 (.github/workflows/apply_migration.yml).

set lock_timeout = '5s';

------------------------------------------------------------------
-- 1. the facts, browser-readable — every column except `raw`
------------------------------------------------------------------

create or replace view sold_transactions_public as
select
    s.source,
    s.source_record_id,
    s.sold_at,
    s.price_czk,
    s.asking_last_czk,
    s.listed_at,
    s.published_at,
    s.category_main,
    s.category_type,
    s.subtype,
    s.disposition,
    s.area_m2,
    s.area_basis,
    s.usable_area,
    s.estate_area,
    s.geom,
    s.address_text,
    s.obec_kod,
    s.ku_kod,
    s.ulice_kod,
    s.photo_urls,
    s.source_url,
    s.fetched_at
from sold_transactions s;

comment on view sold_transactions_public is
  'Registered sales, browser-readable. Every `sold_transactions` column except `raw` '
  '(the archived source record, kept for re-parsing without re-fetching — it is not a '
  'read surface). Definer-style: no security_invoker, so it bypasses the base table''s '
  'deny-all RLS, which is what makes `sold_comparables` readable by `authenticated`.';

revoke all on sold_transactions_public from anon, authenticated;
grant select on sold_transactions_public to authenticated;

------------------------------------------------------------------
-- 2. the fetch ledger, browser-readable — only what "have we looked?" needs
------------------------------------------------------------------

-- Narrower than its table ON PURPOSE: `error` is our own failure text and `pages`/`id`
-- are lane bookkeeping. Neither answers the block's one question, and neither belongs in
-- a browser payload. `bbox` stays because the coverage read matches the point against it.
create or replace view sold_transaction_fetches_public as
select
    f.source,
    f.obec_kod,
    f.bbox,
    f.fetched_at,
    f.status,
    f.record_count,
    f.source_total
from sold_transaction_fetches f;

comment on view sold_transaction_fetches_public is
  'The sold-transaction fetch ledger, browser-readable through `sold_coverage`. Narrower '
  'than the table: no `error` (our failure text), no `pages`/`id` (lane bookkeeping). '
  'Definer-style for the same reason as sold_transactions_public.';

revoke all on sold_transaction_fetches_public from anon, authenticated;
grant select on sold_transaction_fetches_public to authenticated;

------------------------------------------------------------------
-- 3. the one read: sales within N metres of a point
------------------------------------------------------------------

-- The point is answered as `lat`/`lng`, not as `geom`: a geometry column reaches the
-- browser as hex EWKB, which no caller can read, and `properties_public` /
-- `listings_public` already spell a point for the browser exactly this way — one
-- spelling, not a second one.
--
-- `sold_age_days` exists so the sold-date filter is a plain integer predicate the
-- registry's `max_` prefix already routes (`.lte('sold_age_days', N)`); `sold_at` itself
-- stays the fact. `price_per_m2` / `price_per_m2_basis` come from migration 425's
-- measure functions unchanged — the sold columns are spelled like `listings` precisely
-- so no second per-m² definition is needed here.
create or replace function public.sold_comparables(
  p_lat double precision,
  p_lng double precision,
  p_radius_m double precision
)
returns table (
  source            text,
  source_record_id  text,
  sold_at           date,
  price_czk         integer,
  asking_last_czk   integer,
  listed_at         timestamptz,
  published_at      timestamptz,
  category_main     text,
  category_type     text,
  subtype           text,
  disposition       text,
  area_m2           numeric,
  area_basis        text,
  usable_area       numeric,
  estate_area       numeric,
  lat               double precision,
  lng               double precision,
  address_text      text,
  obec_kod          bigint,
  ku_kod            bigint,
  ulice_kod         bigint,
  photo_urls        text[],
  source_url        text,
  fetched_at        timestamptz,
  distance_m        double precision,
  sold_age_days     integer,
  price_per_m2      numeric,
  price_per_m2_basis text
)
language sql
stable
as $$
  select
    s.source,
    s.source_record_id,
    s.sold_at,
    s.price_czk,
    s.asking_last_czk,
    s.listed_at,
    s.published_at,
    s.category_main,
    s.category_type,
    s.subtype,
    s.disposition,
    s.area_m2,
    s.area_basis,
    s.usable_area,
    s.estate_area,
    st_y(s.geom) as lat,
    st_x(s.geom) as lng,
    s.address_text,
    s.obec_kod,
    s.ku_kod,
    s.ulice_kod,
    s.photo_urls,
    s.source_url,
    s.fetched_at,
    st_distance(s.geom::geography,
                st_setsrid(st_makepoint(p_lng, p_lat), 4326)::geography) as distance_m,
    (current_date - s.sold_at)::integer as sold_age_days,
    measure_price_per_m2(s.price_czk::numeric, s.area_m2,
                         s.category_main, s.category_type) as price_per_m2,
    measure_price_per_m2_basis(s.category_main, s.category_type) as price_per_m2_basis
  from sold_transactions_public s
  where st_dwithin(s.geom::geography,
                   st_setsrid(st_makepoint(p_lng, p_lat), 4326)::geography,
                   p_radius_m)
$$;

comment on function public.sold_comparables(double precision, double precision, double precision) is
  'Registered sales within p_radius_m metres of (p_lat, p_lng), with distance_m, '
  'sold_age_days and migration 425''s per-m2 measure + basis. STABLE, SECURITY INVOKER, '
  'single SELECT, no SET clause -- so it INLINES and PostgREST''s filters/ORDER BY/LIMIT '
  'reach sold_transactions_geog_gist. Never add an optional filter parameter here '
  '(migration 109); narrow with a PostgREST predicate on a returned column instead.';

revoke execute on function public.sold_comparables(double precision, double precision, double precision)
  from public, anon;
grant execute on function public.sold_comparables(double precision, double precision, double precision)
  to authenticated, service_role;

------------------------------------------------------------------
-- 4. coverage: when did we last look here, and what did we find?
------------------------------------------------------------------

-- Without this the block cannot tell "we looked on <date> and this area holds no
-- registered sales" from "nobody has ever looked here" — the two read identically as an
-- empty table, and only one of them is honest. `source_total` is what the source said
-- the cell holds against `record_count` for what we took, so the block can say how much
-- it is NOT seeing. Newest `ok` attempt whose cell contains the point wins.
create or replace function public.sold_coverage(
  p_lat double precision,
  p_lng double precision
)
returns table (
  fetched_at   timestamptz,
  obec_kod     bigint,
  record_count integer,
  source_total integer
)
language sql
stable
as $$
  select f.fetched_at, f.obec_kod, f.record_count, f.source_total
  from sold_transaction_fetches_public f
  where f.status = 'ok'
    and st_contains(f.bbox, st_setsrid(st_makepoint(p_lng, p_lat), 4326))
  order by f.fetched_at desc
  limit 1
$$;

comment on function public.sold_coverage(double precision, double precision) is
  'The newest successful sold-transaction fetch whose cell contains (p_lat, p_lng). '
  'Zero rows means we have never looked here -- which is NOT the same answer as '
  '"we looked and found nothing" (record_count = 0), and the block must say which.';

revoke execute on function public.sold_coverage(double precision, double precision)
  from public, anon;
grant execute on function public.sold_coverage(double precision, double precision)
  to authenticated, service_role;

notify pgrst, 'reload schema';

reset lock_timeout;
