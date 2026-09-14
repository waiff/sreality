-- 524_location_w15_every_listing.sql
--
-- W15 of the location simplification sprint. THE LANE COVERS EVERY LISTING, and
-- the audit page shows every listing the lane cannot place.
--
-- The operator's ruling, 2026-09-14: the 87,756 listings the W14 waterfall
-- reported as "not served" -- delisted rows that are not the display listing of
-- any active property -- are legitimate listings inside this programme. They must
-- be walked, re-mined, re-resolved like any other, and while their location
-- cannot be determined they must appear on `/new-dedup/pin-audit` so they are
-- visible and can be fixed when the mechanics improve.
--
-- SO THE SERVED SET RETIRES. `SERVED_LISTING_PREDICATE` (live, or the display
-- listing of a live property) was a W1 cost shortcut -- skip delisted rows in the
-- walks -- and it had quietly become a SECOND definition of "what counts", next
-- to the consumer rule. It is deleted from `location_data/claims_common.py` in
-- the same PR; the intake walk, the bodies pass, the resolver sweep and
-- `check_location_town_coverage` all lost it. THE CONSUMER RULE IS UNTOUCHED:
-- `SERVED_LOCATION_PREDICATE` -- a listing is served to consumers only when
-- `listing_location` has a geom or `country_status = 'foreign'` -- is now the ONE
-- rule in the programme, and the audit set is simply "every listing that fails
-- it". One definition, one cohort, fewer numbers to reconcile.
--
-- WHAT THIS FILE CHANGES.
--   1. `location_pin_audit_mv` v3. The cohort becomes ONE arm -- every listing
--      that fails the consumer rule -- instead of 514's `candidates` union
--      (unresolved store rows + a live property's row-less display listing)
--      narrowed by the served set. Every column, index, grant, the summary
--      function and the hourly cron survive unchanged; only the WHERE moves.
--      ~44 k rows become ~80 k: the delisted rows the operator asked to see.
--   2. `refresh_location_audit_waterfall()` (523's producer, CREATE OR REPLACE).
--      With the served set gone the chain has no served/not-served fork left in
--      it, so 14 rows become 9: every listing -> judged -> located, with the
--      hidden set deducted from the whole database and split by `state`.
--
-- A MATVIEW CANNOT CHANGE ITS COHORT IN PLACE, so § 1 is 518 as it left the
-- relation -- DROP + CREATE, its unique index (REFRESH CONCURRENTLY needs it),
-- its filter indexes, its grants, its registry row -- with one WHERE rewritten.
-- The CASCADE takes `location_pin_audit_summary()` (it reads the relation), so
-- that is re-created verbatim; `refresh_location_pin_audit_mv()` is NOT taken (a
-- string-bodied function records no dependency on what it reads), so the cron job
-- points at a live function throughout, and § 2 leaves it alone for the same
-- reason -- it already names the function it calls.
--
-- ADDITIVE. No listing is deleted, no verdict moves, no column is added to
-- `listings`: both halves are reads. Statement autocommit, so a lock_timeout
-- retry from statement 1 is resumable. Apply through `apply_migration.yml` on the
-- PR branch.

set lock_timeout = '5s';
set statement_timeout = '900s';

-- ---------------------------------------------------------------------------
-- 1. The matview, 518 § 1 with the W15 cohort.
-- ---------------------------------------------------------------------------

drop materialized view if exists location_pin_audit_mv cascade;

create materialized view location_pin_audit_mv as
with cohort as materialized (
  select
    l.id                                           as listing_id,
    l.property_id,
    l.sreality_id,
    l.source,
    l.source_id_native,
    l.source_url,
    l.category_main,
    l.category_type,
    l.disposition,
    l.area_m2,
    l.price_czk,
    l.is_active,
    l.first_seen_at,
    l.last_seen_at,
    -- The ONE label (migration 503). Mostly NULL here by construction -- that is
    -- the finding, not a defect -- but a row that knows its town without knowing
    -- where it stands says so.
    location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                           ll.obec_name, ll.cast_obce_name, ll.country_code,
                           ll.country_status)      as display_label,
    ll.country_status::text                        as country_status,
    ll.granularity::text                           as granularity,
    ll.match_confidence::text                      as match_confidence,
    ll.resolver_version,
    ll.resolved_at,
    (ll.listing_id is not null)                    as has_row,
    -- The saved verdict consumed evidence: a non-empty claim set. `claim_set_hash`
    -- is NOT NULL on every row and the resolver hashes the CONSUMED claim list, so
    -- an empty consumption is the digest of the empty list, not a NULL
    -- (location_data/resolver/serialize.py::claim_set_hash -> digest([])).
    (ll.claim_set_hash is not null
       and ll.claim_set_hash <> sha256('[]'::bytea)) as has_claims
  from listings l
       left join listing_location ll on ll.listing_id = l.id
  -- THE COHORT IS THE ONE RULE, NEGATED (W15). Every listing the consumer rule
  -- refuses to serve, and nothing else: no served-set arm, no union, no second
  -- census. `location_data.claims_common.SERVED_LOCATION_PREDICATE` rendered
  -- verbatim (inner alias `sl`, keyed on `l.id`) -- the left join above could
  -- answer the same question a second way and is deliberately not used for it,
  -- because the pinned text is what keeps this page and Browse on one definition.
  -- ONE SEQUENTIAL PASS of `listings` with a hash anti-join to `listing_location`:
  -- the shape 523's waterfall measured at 19.3 s on production, inside a 900 s
  -- refresh budget. 514's `listing_location`-driven form existed to dodge a 120 s
  -- default that the cron command has armed away since.
  where not EXISTS (SELECT 1 FROM listing_location sl WHERE sl.listing_id = l.id AND (sl.geom IS NOT NULL OR sl.country_status = 'foreign'))
)
select
  c.*,
  -- Evidence under an ACTIVE contract -- the whole question now that the claims
  -- under retired versions are deleted (W6-a). EXISTS, not a count: the page asks
  -- "is there any", and the probe stops at the first admissible claim. The join to
  -- the contract header is the same two primary-key hops the resolver's own read
  -- predicate makes (`resolve_db._ACTIVE_CONTRACT_ENTRY`); it is NOT dropped just
  -- because every surviving claim is expected to be active, because an operator
  -- claim carries no contract entry at all and must not count as portal evidence.
  exists (select 1
            from location_claims cl
                 join portal_contract_entries pce on pce.id = cl.contract_entry_id
                 join portal_contracts pc         on pc.id  = pce.contract_id
           where cl.listing_id = c.listing_id
             and pc.is_active)                     as claims_now,
  sib.found                                        as sibling_has_pin,
  -- The bucket the page filters on: live/delisted x "did the saved verdict
  -- consume anything". `l.is_active` is the page's ONLY live/delisted notion now
  -- that the served set is gone. `has_claims` is a plain boolean by here (no row
  -- yields false, never NULL), so the CASE is total. It refines 'unresolved' --
  -- under 'pending' there is no verdict to explain yet.
  case
    when c.is_active and not c.has_claims          then 'active_no_claims'
    when c.is_active                               then 'active_unresolved'
    when not c.has_claims                          then 'delisted_no_claims'
    else                                                'delisted_unresolved'
  end                                              as quality,
  -- W7-b's top-level cut. Three arms make 'pending' -- no verdict yet, queued
  -- for one, or a page change the standing verdict never saw -- and 'pending'
  -- wins any tie, because "the lane is not done" is the truthful answer whenever
  -- it applies. Everything else is the issue.
  case
    when not c.has_row                             then 'pending'
    when exists (select 1 from dirty_locations d
                  where d.listing_id = c.listing_id)
                                                   then 'pending'
    when snap.scraped_at > c.resolved_at           then 'pending'
    else                                                'unresolved'
  end                                              as state,
  now()                                            as refreshed_at
from cohort c
     left join lateral (
       select exists (
         select 1
         from listings sib
              join listing_location sll on sll.listing_id = sib.id
         where sib.property_id = c.property_id
           and sib.id <> c.listing_id
           and sll.geom is not null
       ) as found
     ) sib on true
     -- The newest snapshot of this listing, one descent of
     -- `listing_snapshots (listing_id, scraped_at DESC)`. LIMIT 1 and not
     -- max(): the index gives the first row and stops.
     left join lateral (
       select s.scraped_at
         from listing_snapshots s
        where s.listing_id = c.listing_id
        order by s.scraped_at desc
        limit 1
     ) snap on true;

-- REFRESH ... CONCURRENTLY requires a unique index. Listing-grain, so the PK of
-- `listings` is the key.
create unique index if not exists location_pin_audit_mv_pk
  on location_pin_audit_mv (listing_id);

-- The page's filter axes, and its two keyset lanes ((col, id) btrees, the shape
-- frontend/src/lib/keyset.ts pages against). 518's index set, unchanged.
create index if not exists location_pin_audit_mv_state
  on location_pin_audit_mv (state);
create index if not exists location_pin_audit_mv_source
  on location_pin_audit_mv (source);
create index if not exists location_pin_audit_mv_category
  on location_pin_audit_mv (category_main);
create index if not exists location_pin_audit_mv_quality
  on location_pin_audit_mv (quality);
create index if not exists location_pin_audit_mv_sibling
  on location_pin_audit_mv (listing_id) where sibling_has_pin;
create index if not exists location_pin_audit_mv_last_seen
  on location_pin_audit_mv (last_seen_at, listing_id);
create index if not exists location_pin_audit_mv_first_seen
  on location_pin_audit_mv (first_seen_at, listing_id);

revoke all on location_pin_audit_mv from anon;
grant select on location_pin_audit_mv to authenticated;

-- Corollary E (migrations 437/440): the registry row survives a DROP of the
-- relation (it is a data row, not a dependency), but the insert is kept so a
-- replay that reaches this file first still registers the artifact.
insert into public.derived_artifacts
  (name, producer, host, cadence, staleness_budget, is_serving)
values ('location_pin_audit_mv', 'refresh_location_pin_audit_mv', 'pg_cron',
        '25 * * * *', interval '130 minutes', true)
on conflict (name) do nothing;

-- ---------------------------------------------------------------------------
-- 2. The summary function, 518 § 2 verbatim: the CASCADE above took it with the
--    relation it reads. Still ONE payload behind every number on the page -- the
--    matrix, the two header counts and the "how many match the current filters"
--    total are all sums over these ~250 rows, so they cannot tell different
--    stories.
-- ---------------------------------------------------------------------------

drop function if exists location_pin_audit_summary();

create function location_pin_audit_summary()
returns table (
  state text,
  source text,
  category_main text,
  quality text,
  sibling_has_pin boolean,
  n bigint,
  refreshed_at timestamptz
)
language sql
stable
security invoker
set search_path = public
as $$
  select a.state, a.source, a.category_main, a.quality, a.sibling_has_pin,
         count(*)::bigint, max(a.refreshed_at)
  from location_pin_audit_mv a
  group by a.state, a.source, a.category_main, a.quality, a.sibling_has_pin
$$;

-- Revoke the default ACL, THEN grant back deliberately. `public` matters most:
-- the default is EXECUTE TO PUBLIC, which anon and authenticated inherit, so
-- naming only the two roles would leave the function callable.
revoke execute on function location_pin_audit_summary()
  from public, anon, authenticated;
grant execute on function location_pin_audit_summary() to authenticated;

-- ---------------------------------------------------------------------------
-- 3. The cron, re-asserted. `cron.schedule` on an existing jobname UPDATES it,
--    so this is idempotent; the guard is 136 / 274 / 510 / 514 / 515 / 518's --
--    the CI schema-replay container has no pg_cron and this file must still
--    apply there.
-- ---------------------------------------------------------------------------

do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'refresh-location-pin-audit',
    '25 * * * *',
    $$set statement_timeout='900s'; select public.refresh_location_pin_audit_mv();$$
  );
exception when others then
  raise notice 'pg_cron unavailable; location pin audit refresh not scheduled (%). Refresh via refresh_location_pin_audit_mv() on another scheduler.', sqlerrm;
end
$cron$;

-- ---------------------------------------------------------------------------
-- 4. The waterfall, 523 § 2 with the fork taken out. NINE rows, not fourteen:
--
--    1  Inzerátů v databázi celkem                        chain, lost 0
--    2  U kterých už systém polohu řešil                  chain, lost = never judged
--    3  Se známou polohou (bod v mapě, nebo zahraničí)    chain, lost = judged, no location
--         · mají přiřazenou obec / v zahraničí / bod bez obce
--    4  Bez rozhodnuté polohy -- tento seznam             deduction off step 1
--         · nevyřešeno / čeká na zpracování
--
--    Step 4 is a DEDUCTION off the whole database and not a chain step, because
--    it does not narrow step 3 -- it is step 3's complement, and calling it a
--    loss would double-count it down the column. Its split is read off
--    `location_pin_audit_mv` inside the SAME statement, so `nevyřešeno +
--    čeká` cannot drift from the number the page lists; a hidden listing the
--    hourly refresh has not seen yet counts as 'čeká na zpracování', which is
--    what it is. `SERVED_LOCATION_PREDICATE` is rendered verbatim here too, and
--    it is the only cut left in the statement.
-- ---------------------------------------------------------------------------

comment on table location_audit_waterfall is
  'W15: the audit page''s chain from every listing in the database down to the set no consumer can see. Rewritten hourly by refresh_location_pin_audit_mv(); the one cut is SERVED_LOCATION_PREDICATE (location_data.claims_common).';

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
      count(*) filter (where b.has_verdict)                        as with_verdict,
      count(*) filter (where b.located)                            as located,
      -- THE THREE SPLITS ARE MUTUALLY EXCLUSIVE BY CONSTRUCTION, so they partition
      -- `located` whatever the data says. 523 asked `has_town` without excluding
      -- foreign, which holds only while no row is both -- and W15 widens the located
      -- set by ~68 k delisted rows nobody has audited for that. A contradiction
      -- (foreign WITH a Czech obec) is a resolver bug, not a reason for this apply to
      -- fail; foreign wins, because that is the answer the consumer rule serves.
      count(*) filter (where b.located
                         and not coalesce(b.is_foreign, false)
                         and coalesce(b.has_town, false))          as located_town,
      count(*) filter (where b.located
                         and coalesce(b.is_foreign, false))        as located_foreign,
      count(*) filter (where b.located
                         and not coalesce(b.is_foreign, false)
                         and not coalesce(b.has_town, false))      as located_no_town,
      count(*) filter (where not b.located)                        as hidden
    from base b
  ),
  -- The hidden set's two states, off the relation the page itself lists.
  hidden_state as (
    select
      count(*) filter (where coalesce(a.state, 'pending') = 'unresolved')
                                                      as hidden_unresolved,
      count(*) filter (where coalesce(a.state, 'pending') = 'pending')
                                                      as hidden_pending
    from base b
         left join location_pin_audit_mv a on a.listing_id = b.listing_id
   where not b.located
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

           (2::smallint, 0::smallint, 'with_verdict', null::text, 'chain',
            'U kterých už systém polohu řešil',
            g.with_verdict, g.all_listings - g.with_verdict),

           (3::smallint, 0::smallint, 'located', null::text, 'chain',
            'Se známou polohou (bod v mapě, nebo zahraničí)',
            g.located, g.with_verdict - g.located),
           (3::smallint, 1::smallint, 'located_town', 'located'::text, 'split',
            'mají přiřazenou obec',
            g.located_town, null::bigint),
           (3::smallint, 2::smallint, 'located_foreign', 'located'::text, 'split',
            'jsou v zahraničí (systém tak rozhodl)',
            g.located_foreign, null::bigint),
           (3::smallint, 3::smallint, 'located_no_town', 'located'::text, 'split',
            'mají bod v ČR, ale bez obce',
            g.located_no_town, null::bigint),

           (4::smallint, 0::smallint, 'hidden', 'all_listings'::text, 'deduction',
            'Bez rozhodnuté polohy — tento seznam; nikde je neukazujeme, dokud polohu nemají',
            g.hidden, null::bigint),
           (4::smallint, 1::smallint, 'hidden_unresolved', 'hidden'::text, 'split',
            'nevyřešeno',
            h.hidden_unresolved, null::bigint),
           (4::smallint, 2::smallint, 'hidden_pending', 'hidden'::text, 'split',
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
-- 5. First fill, so the page has the nine rows the moment this lands rather
--    than at :25. (The matview was populated by its CREATE above.)
-- ---------------------------------------------------------------------------

select public.refresh_location_audit_waterfall();

reset statement_timeout;
reset lock_timeout;

-- ---------------------------------------------------------------------------
-- 6. The proof.
-- ---------------------------------------------------------------------------

do $$
declare
  cols  text[];
  bad   bigint;
  total bigint;
  cur   bigint;
  prev  bigint;
  v_lost bigint;
  r     record;
begin
  -- -------- the matview
  if to_regclass('public.location_pin_audit_mv') is null then
    raise exception '524: location_pin_audit_mv missing';
  end if;

  select array_agg(attname order by attnum) into cols
    from pg_attribute
   where attrelid = 'public.location_pin_audit_mv'::regclass
     and attnum > 0 and not attisdropped;

  for r in select unnest(array['state', 'quality', 'claims_now', 'has_row',
                              'has_claims', 'sibling_has_pin', 'refreshed_at']) as k
  loop
    if not (r.k = any(cols)) then
      raise exception '524: location_pin_audit_mv lost the % column', r.k;
    end if;
  end loop;

  -- Total and two-valued: every row is one of the two states, never NULL and
  -- never a third spelling the page has no label for.
  select count(*) into bad
    from location_pin_audit_mv
   where state is null or state not in ('pending', 'unresolved');
  if bad > 0 then
    raise exception '524: % rows carry a state outside (pending, unresolved)', bad;
  end if;

  -- A row with no store row at all can only be pending -- there is no verdict to
  -- call unresolved.
  select count(*) into bad
    from location_pin_audit_mv
   where not has_row and state <> 'pending';
  if bad > 0 then
    raise exception '524: % rows have no verdict yet but are not pending', bad;
  end if;

  -- The cohort IS the consumer rule negated: not one row in it may be servable.
  select count(*) into bad
    from location_pin_audit_mv a
   where exists (select 1 from listing_location sl
                  where sl.listing_id = a.listing_id
                    and (sl.geom is not null or sl.country_status = 'foreign'));
  if bad > 0 then
    raise exception '524: % audit rows are served by the consumer rule', bad;
  end if;

  for r in select unnest(array['location_pin_audit_mv_pk',
                              'location_pin_audit_mv_state']) as k
  loop
    if not exists (select 1 from pg_class
                    where relname = r.k and relkind = 'i') then
      raise exception '524: index % missing', r.k;
    end if;
  end loop;

  -- The summary function came back from the CASCADE with its axis, and the
  -- refresh function was not a casualty of it.
  if to_regprocedure('public.location_pin_audit_summary()') is null
     or to_regprocedure('public.refresh_location_pin_audit_mv()') is null then
    raise exception '524: the pin-audit functions are missing';
  end if;
  if not exists (
    select 1 from pg_proc
     where oid = 'public.location_pin_audit_summary()'::regprocedure
       and 'state' = any(proargnames)
  ) then
    raise exception '524: location_pin_audit_summary() does not return state';
  end if;

  if not exists (select 1 from public.derived_artifacts
                  where name = 'location_pin_audit_mv') then
    raise exception '524: location_pin_audit_mv not registered in derived_artifacts';
  end if;

  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    if not exists (select 1 from cron.job where jobname = 'refresh-location-pin-audit') then
      raise exception '524: the hourly pin-audit refresh is not scheduled';
    end if;
  else
    raise notice '524: pg_cron unavailable, cron assertion skipped';
  end if;

  -- -------- the waterfall
  select count(*) into bad from location_audit_waterfall;
  if bad <> 9 then
    raise exception '524: waterfall has % rows, expected 9', bad;
  end if;
  for r in select unnest(array['all_listings', 'with_verdict', 'located',
                              'located_town', 'located_foreign', 'located_no_town',
                              'hidden', 'hidden_unresolved', 'hidden_pending']) as k
  loop
    if not exists (select 1 from location_audit_waterfall where step_key = r.k) then
      raise exception '524: waterfall is missing step %', r.k;
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
        raise exception '524: the first chain step lost % (must be 0)', r.lost;
      end if;
    elsif r.lost is distinct from prev - r.n then
      raise exception '524: step % lost % but the chain says %',
        r.step_key, r.lost, prev - r.n;
    end if;
    prev := r.n;
  end loop;

  -- Law 2: the hidden set is the complement of the located set over the WHOLE
  -- database -- the wave in one line.
  select n into total from location_audit_waterfall where step_key = 'all_listings';
  select n into cur   from location_audit_waterfall where step_key = 'located';
  select n into v_lost from location_audit_waterfall where step_key = 'hidden';
  if cur + v_lost <> total then
    raise exception '524: located % + hidden % <> all %', cur, v_lost, total;
  end if;
  -- ... and what step 3 lost is exactly "judged, still without a location".
  select n into prev from location_audit_waterfall where step_key = 'with_verdict';
  select lost into v_lost from location_audit_waterfall where step_key = 'located';
  if v_lost <> prev - cur then
    raise exception '524: judged-without-location % <> % - %', v_lost, prev, cur;
  end if;

  -- Law 3: every split partitions its parent exactly -- the three located splits
  -- sum to `located`, and the two hidden states sum to `hidden`.
  for r in select w.parent_key as k, sum(w.n) as s
             from location_audit_waterfall w
            where w.kind = 'split'
            group by w.parent_key
  loop
    select n into cur from location_audit_waterfall where step_key = r.k;
    if cur <> r.s then
      raise exception '524: the sub-rows of % sum to % but % is %',
        r.k, r.s, r.k, cur;
    end if;
  end loop;

  -- Law 4: the share is measured against the WHOLE database.
  select count(*) into bad
    from location_audit_waterfall w
   where w.share_pct is distinct from
         coalesce(round(w.n::numeric * 100 / nullif(total, 0), 3), 0);
  if bad > 0 then
    raise exception '524: % rows carry a share that is not n/total', bad;
  end if;

  select n into cur from location_audit_waterfall where step_key = 'hidden';
  raise notice '524: the lane covers every listing; % listings, % in the audit set',
    total, cur;
end
$$;
