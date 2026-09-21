-- 545_sold_comps_read_surface.sql — the sold-comps READ surface (sold-comps W3).
--
-- Migration 542 put registered sales in `sold_transactions` + their fetch ledger in
-- `sold_transaction_fetches`, both deny-all to browser roles. This file is the ONE way
-- those facts reach a browser: one definer-style view over the SALES with an INVOKER
-- function on it ("the sales within N metres of this point"), and one narrow SECURITY
-- DEFINER function over the LEDGER ("when did we last look in this municipality").
--
-- WHY DEFINER-STYLE VIEW + INVOKER FUNCTION, FOR THE SALES. `sold_transactions` carries
-- `enable row level security` with ZERO policies, so `authenticated` sees nothing on it.
-- A plain view (security_invoker UNSET) runs as its OWNER and bypasses that deny-all —
-- the standing shape for MARKET data here, `properties_public` being the template
-- (migration 508). Migration 536's `security_invoker = true` is the TENANT shape and
-- would return zero rows. `sold_comparables` then stays SECURITY INVOKER (the default),
-- which is why it must read the VIEW and never the table.
--
-- WHY THE LEDGER GETS NO VIEW AT ALL. A cell is fetched only for a municipality where
-- SOME account holds a live deal-pipeline card, so the ledger's obec set is a projection
-- of tenant state (`property_pipeline`, RLS-scoped per account). A browser-readable
-- ledger would therefore hand every signed-in account the mappable list of towns every
-- OTHER account is working, and the cadence of it — market data it is not. So the ledger
-- has no `_public` view: `sold_coverage` is SECURITY DEFINER over the table, takes a
-- point, and answers about exactly ONE municipality — the one that contains the point —
-- with four facts and no cell list. It is registered in the admin-only relation registry
-- (tests/test_migration_rls_grants.py) so the next reader of that table has to say why.
--
-- WHY NO `SET` CLAUSE, AND WHY THREE ARGS — `sold_comparables` only. A set-returning
-- SQL function that is STABLE,
-- SECURITY INVOKER, a single SELECT and carries NO `SET` clause is INLINED by the
-- planner, so PostgREST's own filters, ORDER BY and LIMIT reach the GiST index on
-- `(geom::geography)` exactly as they would against the view (migration 537's contract).
-- A `SET search_path` would forfeit that, and none is needed: an invoker function resolves
-- PostGIS through the CALLER's path. `sold_coverage` below is SECURITY DEFINER and must pin
-- one, and it pins `public, extensions`: migration 001 installs PostGIS unqualified, which
-- lands in `public` on the CI replay but in `extensions` on the Supabase database — pinning
-- `public` alone applies in CI and fails in production with `type "geography" does not exist`. Migration 109 is the anti-pattern this avoids: ~50 optional
-- null-guarded filter params made that function un-inlinable, generically planned and
-- timed out. `sold_comparables` therefore takes the point and the radius and NOTHING
-- else — every other narrowing is a PostgREST predicate on the returned columns, and no
-- future filter may arrive here as an optional parameter. `sold_coverage` is deliberately
-- OUTSIDE that contract: a definer function never inlines anyway, and it is a one-row
-- indexed point lookup, so there is nothing for inlining to buy.
--
-- MULTI-TENANT NOTE. There is no `account_id` anywhere in this surface: a registered sale
-- is market data, not user state. The single `grant select ... to authenticated` on
-- `sold_transactions_public` (and `grant execute` on each function) IS the dissemination
-- switch — revoke it and the whole dataset goes dark to every signed-in account at once.
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
-- 2. the one read: sales within N metres of a point
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
-- 3. coverage: when did we last look HERE, and what did we find?
------------------------------------------------------------------

-- Without this the block cannot tell "we looked on <date> and this municipality holds no
-- registered sales" from "nobody has ever looked here" — the two read identically as an
-- empty table, and only one of them is honest. `source_total` is what the source said
-- the cell holds WITHOUT its 24-month window, against `record_count` for what a complete
-- walk took inside it — two populations, not a shortfall.
--
-- SCOPED BY MUNICIPALITY, NOT BY THE CELL BOX. The fetched cell is the obec envelope
-- expanded by the block's largest radius (5 km), so in Czech settlement density one
-- point sits inside many cells' boxes at once; "newest box containing the point" would
-- answer with whichever town happened to be walked last, and the block would print a
-- village's counts over a Prague address. The subject's own obec is the only cell whose
-- coverage is a statement about this point, so the point is resolved through the same
-- `admin_boundaries` obec polygon the cell was built from, and the obec NAME comes back
-- so the sentence can say which town it is about.
--
-- ANY-STATUS LAST ATTEMPT, TOO. A cell that has only ever FAILED is not a cell nobody
-- asked for: the first reads "we tried and could not", the second "add this property to
-- the pipeline". Collapsing them would render a broken fetch lane as operator inaction
-- on the only surface that reads this ledger.
--
-- ci-allow-ungated: sold_coverage — reads an admin-only relation on purpose and WITHOUT
-- is_platform_admin(): the operator's own listing page has to be able to say when we
-- last looked. It is narrow by construction — SECURITY DEFINER, a point in, four facts
-- about one municipality out, no cell list, no bbox, no `error`, no `pages`.
create or replace function public.sold_coverage(
  p_lat double precision,
  p_lng double precision
)
returns table (
  obec_kod            bigint,
  obec_name           text,
  fetched_at          timestamptz,
  record_count        integer,
  source_total        integer,
  truncated           boolean,
  last_attempt_at     timestamptz,
  last_attempt_status text
)
language sql
stable
security definer
set search_path = public, extensions
as $$
  with cell as (
    select b.id as obec_kod, b.name as obec_name
    from admin_boundaries b
    where b.level = 'obec'
      and st_covers(b.geom, st_setsrid(st_makepoint(p_lng, p_lat), 4326)::geography)
    limit 1
  )
  select c.obec_kod, c.obec_name,
         ok.fetched_at, ok.record_count, ok.source_total, ok.truncated,
         try.fetched_at, try.status
  from cell c
  left join lateral (
    -- an `ok` row carries `error` only when the page cap cut its walk short (sold_fetch)
    select f.fetched_at, f.record_count, f.source_total, f.error is not null as truncated
    from sold_transaction_fetches f
    where f.obec_kod = c.obec_kod and f.status = 'ok'
    order by f.fetched_at desc
    limit 1
  ) ok on true
  left join lateral (
    select f.fetched_at, f.status
    from sold_transaction_fetches f
    where f.obec_kod = c.obec_kod
    order by f.fetched_at desc
    limit 1
  ) try on true
$$;

-- The access path this migration introduces: the ledger is read by obec, and migration
-- 542's only index leads with `source`, which a btree cannot skip.
create index if not exists sold_transaction_fetches_obec_idx
  on sold_transaction_fetches (obec_kod, fetched_at desc);

comment on function public.sold_coverage(double precision, double precision) is
  'Coverage of the municipality containing (p_lat, p_lng): the newest successful fetch '
  'of that obec (fetched_at/record_count/source_total, and truncated = the page cap cut '
  'that walk short so record_count is NOT all the source holds) plus the newest attempt of ANY '
  'status. Zero rows means the point is in no known obec; a row with a null fetched_at '
  'means nobody has ever successfully looked there -- neither is the same answer as '
  '"we looked and found nothing" (record_count = 0), and the block must say which. '
  'SECURITY DEFINER over a table no browser role may read: the set of fetched cells is '
  'a projection of which towns some account has a live pipeline card in.';

revoke execute on function public.sold_coverage(double precision, double precision)
  from public, anon;
grant execute on function public.sold_coverage(double precision, double precision)
  to authenticated, service_role;

notify pgrst, 'reload schema';

reset lock_timeout;
