-- 523_location_w14_audit_waterfall.sql
--
-- W14 of the location simplification sprint. The audit page states a number --
-- the listings no consumer can see -- and states it alone. The operator's words,
-- 2026-09-14: "Include a proper waterfall from the number 841408 on the location
-- audit page so that we know exactly what are the numbers we are looking at
-- there and that we are comparing the 'hidden' or 'unresolved' in light of the
-- entire db."
--
-- So the store gains the whole chain, from every listing ever collected down to
-- the hidden set, and the page reads it instead of standing next to an
-- unexplained 12 k. Six steps, measured on production 2026-09-14 19:0x UTC:
--
--   1  inzerátů v databázi                    841,428   100.0 %
--   2  z toho nezobrazitelných                 87,756    10.4 %   (the deduction)
--        · systém je nikdy neposuzoval         28,162
--        · posouzené, bez polohy               41,396
--        · poloha určená z dřívějška           18,198
--   3  zobrazitelných                         753,672    89.6 %   (-87,756)
--   4  z toho už posouzených                  753,658    89.6 %   (-14)
--   5  z toho se známou polohou               741,604    88.1 %   (-12,054)
--        · s přiřazenou obcí                  695,130
--        · v zahraničí (rozhodnuto)            45,582
--        · bod v ČR bez obce                      892
--   6  SKRYTÉ (co stránka vypisuje)            12,068     1.4 %
--        · zpracováno, nerozhodnuto            12,054
--        · čeká na zpracování                      14
--
-- ONE DEFINITION, NOT A SECOND CENSUS. Every row above is cut with the two
-- constants the rest of the lane already shares -- `SERVED_LISTING_PREDICATE`
-- (live, or the display listing of a live property) and `SERVED_LOCATION_PREDICATE`
-- (a point, or the determination that it is abroad), both rendered VERBATIM from
-- `location_data/claims_common.py` and pinned character for character by
-- tests/test_location_w14_audit_waterfall.py, the same rail
-- tests/test_location_w5_serve_resolved.py runs over the serving surfaces. A
-- migration is not importable at runtime; that test is the only thing standing
-- between the two texts. NO NEW COLUMN ON `listings`, and no flag: the chain is
-- computed from the store, so it can never disagree with what the page lists.
--
-- THE LAST ROW IS THE PAGE'S OWN SET, PROVED AND NOT ASSERTED. Step 6 is the
-- remainder (served minus located) and its split comes from
-- `location_pin_audit_mv` itself, joined inside the SAME statement, one snapshot,
-- so `12,054 + 14` cannot drift from `12,068`. A hidden listing the hourly
-- refresh has never seen (it arrived minutes ago) counts as 'čeká na zpracování',
-- which is what it is.
--
-- THREE KINDS OF ROW, because a funnel that pretends every row narrows the next
-- one lies about two of them:
--   'chain'     steps 1, 3, 4, 5 -- `lost` = the previous chain step's n minus
--               its own, so the losses are readable straight down the column;
--   'deduction' step 2 -- the set REMOVED between steps 1 and 3, i.e. step 3's
--               `lost` spelled as its own row so its three sub-rows have a parent;
--   'split'     the sub-rows, which partition their parent's n and carry no loss.
-- The DO block at the end proves all four arithmetic laws on the freshly written
-- table, so a wrong `lost` fails the apply instead of misinforming the operator.
--
-- COST, MEASURED. The statement is one sequential pass of `listings` (841 k), a
-- hash join to `listing_location` (813 k) and the two EXISTS probes the pinned
-- predicates spell -- EXPLAIN ANALYZE on production, 2026-09-14: **19.3 s**. It
-- runs inside the existing hourly `refresh_location_pin_audit_mv()` (pg_cron
-- 'refresh-location-pin-audit', :25, 900 s budget armed in the cron command),
-- after the matview refresh so the split reads the fresh relation. No second
-- job, no second cadence, no second definition of "now".
--
-- A TABLE AND NOT A MATVIEW. Fourteen rows rewritten whole once an hour: a
-- matview would need a unique index and REFRESH CONCURRENTLY to buy nothing. The
-- delete + insert share the refresh function's single transaction, so a reader
-- sees the old fourteen rows or the new fourteen, never zero. It is a derived
-- artifact like any other -- registered in `derived_artifacts` and stamped by its
-- producer (Corollary E, migrations 437/441), and listed in that rail's curated
-- `_ROLLUP_TABLES` because the catalog cannot tell a rollup from an ordinary table.
--
-- READ BY THE BROWSER, exactly like the audit matview: SELECT to `authenticated`
-- only, `anon` revoked, RLS on with a read-all policy (migration 349's shape --
-- the grant is the privilege, the policy is the row filter, and both are needed
-- in a from-scratch replay that has no Supabase default ACL). No write privilege
-- for either role, which is what the RLS-grants gate requires.
--
-- ADDITIVE and idempotent: a new table, a new function, one CREATE OR REPLACE of
-- the refresh entry point (body captured from the live catalog on
-- erlvtprrmrylhznfyaih 2026-09-14, md5(prosrc) 453fdde9b96d0bda2c56e07f4f1c5d0c,
-- 322 chars, byte-identical to migration 514 -- NO drift to fold in), and one
-- registry row. Nothing is dropped and no verdict moves. Statement autocommit, so
-- a lock_timeout retry from statement 1 is resumable. The cron job is NOT touched:
-- it already names the function this file replaces in place.

set lock_timeout = '5s';
set statement_timeout = '900s';

-- ---------------------------------------------------------------------------
-- 1. The relation. One row per step (and per split), ordered by (step_no, sub_no).
-- ---------------------------------------------------------------------------

create table if not exists location_audit_waterfall (
  step_key      text primary key,
  step_no       smallint    not null,
  -- 0 = the step itself; 1..n = the sub-rows that partition it.
  sub_no        smallint    not null default 0,
  kind          text        not null
                  check (kind in ('chain', 'deduction', 'split')),
  -- The step a sub-row (or the deduction) belongs to. NULL on a chain step.
  parent_key    text,
  label_cs      text        not null,
  n             bigint      not null,
  -- Chain rows only: the previous chain step's n minus this one's. NULL where
  -- "lost" is not a truthful word for the row.
  lost          bigint,
  -- Share of EVERY listing ever collected -- the whole point of the wave: the
  -- hidden set is read against 841 k, never against itself.
  share_pct     numeric(6,3) not null,
  refreshed_at  timestamptz  not null
);

comment on table location_audit_waterfall is
  'W14: the audit page''s chain from every listing ever collected down to the set no consumer can see. Rewritten hourly by refresh_location_pin_audit_mv(); cut with SERVED_LISTING_PREDICATE / SERVED_LOCATION_PREDICATE (location_data.claims_common).';

create unique index if not exists location_audit_waterfall_order
  on location_audit_waterfall (step_no, sub_no);

alter table location_audit_waterfall enable row level security;

revoke all on location_audit_waterfall from anon, authenticated;
grant select on location_audit_waterfall to authenticated;

do $pol$
begin
  if not exists (
    select 1 from pg_policy
     where polrelid = 'public.location_audit_waterfall'::regclass
       and polname = 'location_audit_waterfall_authenticated_read'
  ) then
    create policy location_audit_waterfall_authenticated_read
      on location_audit_waterfall
      for select to authenticated
      using (true);
  end if;
end
$pol$;

-- ---------------------------------------------------------------------------
-- 2. The producer. ONE statement, so every number shares one snapshot and the
--    arithmetic closes by construction rather than by luck.
-- ---------------------------------------------------------------------------

create or replace function refresh_location_audit_waterfall()
returns bigint
language plpgsql
security definer
set search_path = public
as $fn$
declare
  written bigint;
begin
  delete from location_audit_waterfall;

  insert into location_audit_waterfall
    (step_no, sub_no, step_key, parent_key, kind, label_cs, n, lost,
     share_pct, refreshed_at)
  with base as materialized (
    select
      l.id as listing_id,
      -- location_data.claims_common.SERVED_LISTING_PREDICATE, verbatim (the
      -- alias `l` is part of that contract).
      (l.is_active
        OR EXISTS (SELECT 1 FROM properties pr
                    WHERE pr.repr_listing_ref_id = l.id AND pr.status = 'active'))
                                                      as served,
      -- location_data.claims_common.SERVED_LOCATION_PREDICATE, verbatim. The
      -- left join below could answer the same question a second way; it is
      -- deliberately NOT used for it, because the pinned text is the whole
      -- reason this page cannot drift from Browse.
      EXISTS (SELECT 1 FROM listing_location sl WHERE sl.listing_id = l.id AND (sl.geom IS NOT NULL OR sl.country_status = 'foreign'))
                                                      as located,
      (ll.listing_id is not null)                     as has_verdict,
      (ll.country_status = 'foreign')                 as is_foreign,
      (ll.obec_kod is not null)                       as has_town
    from listings l
         left join listing_location ll on ll.listing_id = l.id
  ),
  agg as (
    select
      count(*)                                                     as all_listings,
      count(*) filter (where b.served)                             as served,
      count(*) filter (where b.served and b.has_verdict)           as served_verdict,
      count(*) filter (where b.served and b.located)               as served_located,
      count(*) filter (where b.served and b.located
                         and coalesce(b.has_town, false))          as located_town,
      count(*) filter (where b.served and b.located
                         and coalesce(b.is_foreign, false))        as located_foreign,
      count(*) filter (where b.served and b.located
                         and not coalesce(b.is_foreign, false)
                         and not coalesce(b.has_town, false))      as located_no_town,
      count(*) filter (where not b.served)                         as not_served,
      count(*) filter (where not b.served and not b.has_verdict)   as ns_no_verdict,
      count(*) filter (where not b.served and b.has_verdict
                         and not b.located)                        as ns_verdict_no_location,
      count(*) filter (where not b.served and b.located)           as ns_located,
      count(*) filter (where b.served and not b.located)           as hidden
    from base b
  ),
  -- The hidden set's two states, off the relation the page itself lists. A row
  -- the last refresh never saw is 'čeká na zpracování' -- it arrived since, and
  -- the lane genuinely has not finished with it.
  hidden_state as (
    select
      count(*) filter (where coalesce(a.state, 'pending') = 'unresolved')
                                                      as hidden_unresolved,
      count(*) filter (where coalesce(a.state, 'pending') = 'pending')
                                                      as hidden_pending
    from base b
         left join location_pin_audit_mv a on a.listing_id = b.listing_id
   where b.served and not b.located
  )
  select v.step_no, v.sub_no, v.step_key, v.parent_key, v.kind, v.label_cs,
         v.n, v.lost,
         coalesce(round(v.n::numeric * 100 / nullif(g.all_listings, 0), 3), 0),
         now()
    from agg g, hidden_state h,
         lateral (values
           (1::smallint, 0::smallint, 'all_listings', null::text, 'chain',
            'Inzerátů v databázi celkem',
            g.all_listings, 0::bigint),

           (2::smallint, 0::smallint, 'not_served', 'all_listings'::text, 'deduction',
            'Nezobrazitelné inzeráty (stažené a nikde neukazované)',
            g.not_served, null::bigint),
           (2::smallint, 1::smallint, 'not_served_no_verdict', 'not_served'::text, 'split',
            'systém u nich polohu nikdy neřešil',
            g.ns_no_verdict, null::bigint),
           (2::smallint, 2::smallint, 'not_served_verdict_no_location', 'not_served'::text, 'split',
            'systém je posoudil, polohu neurčil',
            g.ns_verdict_no_location, null::bigint),
           (2::smallint, 3::smallint, 'not_served_located', 'not_served'::text, 'split',
            'polohu mají určenou z dřívějška',
            g.ns_located, null::bigint),

           (3::smallint, 0::smallint, 'served', null::text, 'chain',
            'Zobrazitelné inzeráty (běžící, nebo hlavní inzerát běžící nemovitosti)',
            g.served, g.all_listings - g.served),

           (4::smallint, 0::smallint, 'served_with_verdict', null::text, 'chain',
            'Zobrazitelné, u kterých už systém polohu řešil',
            g.served_verdict, g.served - g.served_verdict),

           (5::smallint, 0::smallint, 'served_located', null::text, 'chain',
            'Zobrazitelné se známou polohou (bod v mapě, nebo zahraničí)',
            g.served_located, g.served_verdict - g.served_located),
           (5::smallint, 1::smallint, 'located_town', 'served_located'::text, 'split',
            'mají přiřazenou obec',
            g.located_town, null::bigint),
           (5::smallint, 2::smallint, 'located_foreign', 'served_located'::text, 'split',
            'jsou v zahraničí (systém tak rozhodl)',
            g.located_foreign, null::bigint),
           (5::smallint, 3::smallint, 'located_no_town', 'served_located'::text, 'split',
            'mají bod v ČR, ale bez obce',
            g.located_no_town, null::bigint),

           (6::smallint, 0::smallint, 'hidden', 'served'::text, 'deduction',
            'Skryté: zobrazitelné, ale bez rozhodnuté polohy',
            g.hidden, null::bigint),
           (6::smallint, 1::smallint, 'hidden_unresolved', 'hidden'::text, 'split',
            'zpracováno, nerozhodnuto',
            h.hidden_unresolved, null::bigint),
           (6::smallint, 2::smallint, 'hidden_pending', 'hidden'::text, 'split',
            'čeká na zpracování',
            h.hidden_pending, null::bigint)
         ) as v(step_no, sub_no, step_key, parent_key, kind, label_cs, n, lost);

  get diagnostics written = row_count;
  return written;
end;
$fn$;

revoke execute on function refresh_location_audit_waterfall()
  from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 3. The hourly entry point, 514 § 5 verbatim plus the waterfall. `security
--    definer` and `search_path` are re-stated because CREATE OR REPLACE keeps
--    neither by accident -- they are part of the definition being replaced.
-- ---------------------------------------------------------------------------

create or replace function refresh_location_pin_audit_mv()
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  t0 timestamptz := clock_timestamp();
  t1 timestamptz;
  n  bigint;
  w  bigint;
begin
  refresh materialized view concurrently location_pin_audit_mv;
  select count(*) into n from location_pin_audit_mv;
  perform stamp_derived_artifact(
    'location_pin_audit_mv', n,
    (extract(epoch from clock_timestamp() - t0) * 1000)::integer);
  -- W14. AFTER the refresh, so the hidden set's split reads the relation the
  -- page is about to list. `clock_timestamp()` advances inside a transaction, so
  -- the second stamp measures the waterfall alone and not the batch.
  t1 := clock_timestamp();
  w  := refresh_location_audit_waterfall();
  perform stamp_derived_artifact(
    'location_audit_waterfall', w,
    (extract(epoch from clock_timestamp() - t1) * 1000)::integer);
end;
$$;

revoke execute on function refresh_location_pin_audit_mv()
  from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 4. Corollary E: a derived artifact that does not declare its freshness is one
--    nobody can tell is stale. Same producer, cadence and budget as the matview
--    it ships with -- they are literally the same run.
-- ---------------------------------------------------------------------------

insert into public.derived_artifacts
  (name, producer, host, cadence, staleness_budget, is_serving)
values ('location_audit_waterfall', 'refresh_location_pin_audit_mv', 'pg_cron',
        '25 * * * *', interval '130 minutes', true)
on conflict (name) do nothing;

-- ---------------------------------------------------------------------------
-- 5. First fill, so the page has rows the moment this lands rather than at :25.
-- ---------------------------------------------------------------------------

select public.refresh_location_audit_waterfall();

reset statement_timeout;
reset lock_timeout;

-- ---------------------------------------------------------------------------
-- 6. The proof. Every arithmetic law the page relies on, checked on the rows
--    that were just written.
-- ---------------------------------------------------------------------------

do $$
declare
  bad   bigint;
  total bigint;
  prev  bigint;
  cur   bigint;
  lost  bigint;
  r     record;
begin
  if to_regclass('public.location_audit_waterfall') is null then
    raise exception '523: location_audit_waterfall missing';
  end if;

  -- Fourteen rows, six steps, and the keys the page reads by name.
  select count(*) into bad from location_audit_waterfall;
  if bad <> 14 then
    raise exception '523: waterfall has % rows, expected 14', bad;
  end if;
  for r in select unnest(array['all_listings', 'not_served', 'served',
                              'served_with_verdict', 'served_located', 'hidden',
                              'hidden_unresolved', 'hidden_pending']) as k
  loop
    if not exists (select 1 from location_audit_waterfall
                    where step_key = r.k) then
      raise exception '523: waterfall is missing step %', r.k;
    end if;
  end loop;

  -- Law 1: the chain narrows, and `lost` is the previous chain step's n minus
  -- this one's. RED by: a hand-typed loss, or a step inserted out of order.
  prev := null;
  for r in select step_no, step_key, n, lost
             from location_audit_waterfall
            where kind = 'chain'
            order by step_no
  loop
    if prev is null then
      if r.lost <> 0 then
        raise exception '523: the first chain step lost % (must be 0)', r.lost;
      end if;
    elsif r.lost is distinct from prev - r.n then
      raise exception '523: step % lost % but the chain says %',
        r.step_key, r.lost, prev - r.n;
    end if;
    prev := r.n;
  end loop;

  -- Law 2: the deduction rows. `not_served` is what step 1 loses on the way to
  -- step 3; `hidden` is what `served` loses on the way to `served_located`.
  select n into total from location_audit_waterfall where step_key = 'all_listings';
  select n into cur   from location_audit_waterfall where step_key = 'served';
  select n into prev  from location_audit_waterfall where step_key = 'not_served';
  if prev <> total - cur then
    raise exception '523: not_served % <> % - %', prev, total, cur;
  end if;
  select n into prev from location_audit_waterfall where step_key = 'served_located';
  select n into lost from location_audit_waterfall where step_key = 'hidden';
  if lost <> cur - prev then
    raise exception '523: hidden % <> served % - located %', lost, cur, prev;
  end if;

  -- Law 3: every split partitions its parent exactly.
  for r in select w.parent_key as k, sum(w.n) as s
             from location_audit_waterfall w
            where w.kind = 'split'
            group by w.parent_key
  loop
    select n into cur from location_audit_waterfall where step_key = r.k;
    if cur <> r.s then
      raise exception '523: the sub-rows of % sum to % but % is %',
        r.k, r.s, r.k, cur;
    end if;
  end loop;

  -- Law 4: the share is measured against the WHOLE database, which is the wave.
  select count(*) into bad
    from location_audit_waterfall w
   where w.share_pct is distinct from
         coalesce(round(w.n::numeric * 100 / nullif(total, 0), 3), 0);
  if bad > 0 then
    raise exception '523: % rows carry a share that is not n/total', bad;
  end if;

  -- The browser role reads it; anon must not.
  if not has_table_privilege('authenticated', 'public.location_audit_waterfall', 'SELECT') then
    raise exception '523: authenticated cannot read the waterfall';
  end if;
  if has_table_privilege('anon', 'public.location_audit_waterfall', 'SELECT') then
    raise exception '523: anon can read the waterfall';
  end if;
  if not exists (select 1 from public.derived_artifacts
                  where name = 'location_audit_waterfall') then
    raise exception '523: location_audit_waterfall not registered in derived_artifacts';
  end if;

  raise notice '523: waterfall written, % listings down to the hidden set', total;
end
$$;
