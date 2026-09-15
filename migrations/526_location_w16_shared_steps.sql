-- 526_location_w16_shared_steps.sql
--
-- W16 of the location simplification sprint: ONE step vocabulary for the audit
-- page's waterfall and the NEW DEDUP candidates funnel.
--
-- THE RULING (operator, 2026-09-14). The two readouts asked the same first
-- questions in different words, and at one step with genuinely different rules.
-- The funnel's "Known to the location engine" reads as "answered" but is the
-- same set this table calls `with_verdict` -- a `listing_location` row exists,
-- i.e. the engine JUDGED the listing. And the funnel's town step was cut with no
-- consumer rule at all, so its single "lost 99,889" silently added three unlike
-- things together:
--
--     ~53,374  judged, still without a location   (a real loss)
--      45,619  ABROAD -- an ANSWER, not a loss    (this table has always
--                                                  booked it as a split INSIDE
--                                                  `located`)
--        ~895  a point in Czechia, but no town    (a real, tiny loss)
--
-- So the candidates lane adopts THIS chain (the keys, the order, the three row
-- kinds), and the four booleans both producers cut with are spelled exactly once
-- in `location_data/location_steps.py`. A migration cannot import Python, so the
-- expressions below are that module's output VERBATIM and
-- tests/test_location_steps_vocabulary.py pins the two spellings together --
-- the same rail W5 uses to keep the serving surfaces on one rule.
--
-- WHAT THIS FILE CHANGES.
--
--   1. `has_town` gains dedup's obec RANK floor. 524 asked `obec_kod is not
--      null` and dedup additionally required the answer's granularity to rank at
--      or above `obec`; two spellings of one question. Adopting the stricter one
--      costs ZERO rows today (measured 2026-09-14: rows carrying an `obec_kod`
--      whose granularity ranks below obec = 0) and it means this page's town
--      number is from now on the number dedup can block on. `located_no_town`
--      stays the PLAIN COMPLEMENT (`located and not foreign and not has_town`),
--      so the three splits keep partitioning `located` by construction whatever
--      the data does.
--
--   2. `label_cs` is DROPPED from `location_audit_waterfall`. The nine Czech
--      strings become dead weight the moment the shared label module lands:
--      wording now lives in ONE file for BOTH languages and BOTH pages,
--      `frontend/src/lib/locationSteps.ts`, keyed by `step_key` -- the pattern
--      this page already proved with its client-side note map. A better sentence
--      must never cost a migration. The nine strings are not lost: they are in
--      git, in migrations 523 and 524, and now in that module.
--
--      The column is made NULLABLE before the producer is replaced and DROPPED
--      only after, so the hourly cron cannot land in a half-applied state (see
--      APPLY ORDER below).
--
--      NO pg_dump IS TAKEN, and none is needed: this is a 9-row derived rollup
--      that `refresh_location_pin_audit_mv()` rewrites from scratch every hour
--      (`delete from` + one `insert`). Recovery from any mistake here is one
--      function call, not a restore. Dropped under the operator's session-wide
--      permission for this wave.
--
-- Everything else is deliberately untouched: the table, its unique (step_no,
-- sub_no) index, RLS, the `authenticated`-only grant, the `derived_artifacts`
-- row, and the hourly entry point in 523 that calls this function AFTER the
-- matview refresh. NO new column on `listings`, no new table, no new job.
--
-- APPLY ORDER MATTERS, because psql applies this statement by statement and the
-- hourly cron fires at :25 -- so there is a real window BETWEEN two statements in
-- which `refresh_location_pin_audit_mv()` can run. Either naive order breaks it:
-- dropping the column first leaves the OLD producer inserting a column that is
-- gone, and replacing the producer first leaves the NEW one omitting a NOT NULL
-- column. So the column is made NULLABLE first -- after which BOTH producers
-- succeed -- then the producer is replaced, and only then is the column dropped.
-- No window in which the hourly refresh can fail.
--
-- APPLY THIS ONLY AFTER THE W16 SPA ROLLOUT IS GREEN. The deployed frontend on
-- `main` still asks PostgREST for `label_cs`; a select naming a column the table
-- no longer publishes is a 400, and the audit page's waterfall would go blank
-- until the new bundle lands. Merge the PR, wait for Railway's `vite` service to
-- report success on the merge commit (`gh api repos/{owner}/{repo}/commits/<sha>/status`),
-- then apply.
--
-- The producer's base CTE walks ~842 k listings (19.3 s today, plus one lookup
-- join), so the timeout is raised here and reset at the end, as 524 did. Safe
-- to re-run; it replays on an empty database.

set lock_timeout = '5s';
set statement_timeout = '900s';

-- ---------------------------------------------------------------------------
-- 1. Make the label optional FIRST. This is the statement that removes the apply
--    window: with `label_cs` nullable, the producer that is live right now (524's,
--    which writes the label) and the one installed two statements below (which does
--    not) are BOTH valid, so whichever one the :25 cron catches, it succeeds.
--    Guarded so the file re-runs after the column is already gone.
-- ---------------------------------------------------------------------------

do $nn$
begin
  if exists (select 1 from pg_attribute
              where attrelid = 'public.location_audit_waterfall'::regclass
                and attname = 'label_cs' and not attisdropped) then
    alter table location_audit_waterfall alter column label_cs drop not null;
  end if;
end
$nn$;

-- ---------------------------------------------------------------------------
-- 2. The producer, 524 § 4 with the shared flags and without the label. NINE
--    rows still:
--
--    1  all_listings       chain, lost 0
--    2  with_verdict       chain, lost = never judged
--    3  located            chain, lost = judged, still without a location
--         · located_town / located_foreign / located_no_town
--    4  hidden             deduction off step 1
--         · hidden_unresolved / hidden_pending
--
--    A candidate run stamps the same keys onto its own `stats` blob, with
--    `located_town` promoted from a split to a chain step (pairing is what that
--    page is about) and two dedup-only steps under it. The two readouts can
--    therefore differ only by SCOPE and by TIME -- which is exactly what the
--    candidates page now prints next to its chain.
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
    (step_no, sub_no, step_key, parent_key, kind, n, lost,
     share_pct, refreshed_at)
  with base as materialized (
    select
      l.id as listing_id,
      -- location_data.location_steps.step_flags_sql(), VERBATIM. `located` is
      -- claims_common.SERVED_LOCATION_PREDICATE asked off the LISTING; the left
      -- join below could answer the same question a second way and is
      -- deliberately not used for it, because the pinned text is the whole
      -- reason this page cannot drift from Browse -- or, now, from dedup.
      EXISTS (SELECT 1 FROM listing_location sl WHERE sl.listing_id = l.id AND (sl.geom IS NOT NULL OR sl.country_status = 'foreign'))
                                                      as located,
      (ll.listing_id is not null)                     as has_verdict,
      (ll.country_status = 'foreign')                 as is_foreign,
      -- W16: the obec RANK floor is part of the one definition of "has a town".
      -- By rank through `location_granularity_rank`, never by enum order.
      (ll.obec_kod is not null and gr.rank >= (SELECT r.rank FROM location_granularity_rank r WHERE r.granularity = 'obec'))
                                                      as has_town
    from listings l
         left join listing_location ll on ll.listing_id = l.id
         left join location_granularity_rank gr on gr.granularity = ll.granularity
  ),
  agg as (
    -- location_data.location_steps.step_counts_sql('b'), VERBATIM. THE THREE
    -- SPLITS ARE MUTUALLY EXCLUSIVE BY CONSTRUCTION, so they partition `located`
    -- whatever the data says: a row that is both abroad and Czech-towned is a
    -- resolver bug, not a reason for this apply to fail, and foreign wins
    -- because that is the answer the consumer rule serves.
    select count(*) as all_listings,
           count(*) filter (where b.has_verdict) as with_verdict,
           count(*) filter (where b.located) as located,
           count(*) filter (where b.located and not coalesce(b.is_foreign, false)
                              and coalesce(b.has_town, false)) as located_town,
           count(*) filter (where b.located and coalesce(b.is_foreign, false)) as located_foreign,
           count(*) filter (where b.located and not coalesce(b.is_foreign, false)
                              and not coalesce(b.has_town, false)) as located_no_town,
           count(*) filter (where not b.located)      as hidden
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
  select v.step_no, v.sub_no, v.step_key, v.parent_key, v.kind,
         v.n, v.lost,
         coalesce(round(v.n::numeric * 100 / nullif(g.all_listings, 0), 3), 0),
         now()
    from agg g, hidden_state h,
         lateral (values
           (1::smallint, 0::smallint, 'all_listings', null::text, 'chain',
            g.all_listings, 0::bigint),

           (2::smallint, 0::smallint, 'with_verdict', null::text, 'chain',
            g.with_verdict, g.all_listings - g.with_verdict),

           (3::smallint, 0::smallint, 'located', null::text, 'chain',
            g.located, g.with_verdict - g.located),
           (3::smallint, 1::smallint, 'located_town', 'located'::text, 'split',
            g.located_town, null::bigint),
           (3::smallint, 2::smallint, 'located_foreign', 'located'::text, 'split',
            g.located_foreign, null::bigint),
           (3::smallint, 3::smallint, 'located_no_town', 'located'::text, 'split',
            g.located_no_town, null::bigint),

           (4::smallint, 0::smallint, 'hidden', 'all_listings'::text, 'deduction',
            g.hidden, null::bigint),
           (4::smallint, 1::smallint, 'hidden_unresolved', 'hidden'::text, 'split',
            h.hidden_unresolved, null::bigint),
           (4::smallint, 2::smallint, 'hidden_pending', 'hidden'::text, 'split',
            h.hidden_pending, null::bigint)
         ) as v(step_no, sub_no, step_key, parent_key, kind, n, lost);

  get diagnostics written = row_count;
  return written;
end;
$fn$;

revoke execute on function refresh_location_audit_waterfall()
  from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 3. NOW the column goes -- no producer names it any more. The relation keeps
--    every other column, its unique (step_no, sub_no) index, its RLS policy and
--    its `authenticated`-only grant.
-- ---------------------------------------------------------------------------

alter table location_audit_waterfall drop column if exists label_cs;

comment on table location_audit_waterfall is
  'W16: the audit page''s chain from every listing in the database down to the set no consumer can see. Rewritten hourly by refresh_location_pin_audit_mv(); the cuts are the SHARED step flags of location_data/location_steps.py (the consumer rule plus the obec rank floor). Wording lives in frontend/src/lib/locationSteps.ts, keyed by step_key, never in this table.';

-- ---------------------------------------------------------------------------
-- 4. First fill, so the page has the nine label-less rows the moment this lands
--    rather than at :25.
-- ---------------------------------------------------------------------------

select public.refresh_location_audit_waterfall();

-- ---------------------------------------------------------------------------
-- 5. The proof. The four arithmetic laws and "expected 9" run against the real
--    numbers, exactly as 524/525 ran them; the consumer-rule assertion is
--    525's race-safe form -- a snapshot compared with ITSELF (`resolved_at <=
--    refreshed_at`), never with the moving store, because the lanes resolve
--    listings while an apply is running and 524 tripped on four of them.
-- ---------------------------------------------------------------------------

do $$
declare
  bad    bigint;
  total  bigint;
  cur    bigint;
  prev   bigint;
  v_lost bigint;
  r      record;
begin
  -- The label column is gone, and nothing else went with it.
  if exists (select 1 from pg_attribute
              where attrelid = 'public.location_audit_waterfall'::regclass
                and attname = 'label_cs' and not attisdropped) then
    raise exception '526: label_cs is still on location_audit_waterfall';
  end if;
  for r in select unnest(array['step_key', 'step_no', 'sub_no', 'kind',
                              'parent_key', 'n', 'lost', 'share_pct',
                              'refreshed_at']) as k
  loop
    if not exists (select 1 from pg_attribute
                    where attrelid = 'public.location_audit_waterfall'::regclass
                      and attname = r.k and not attisdropped) then
      raise exception '526: location_audit_waterfall lost the % column', r.k;
    end if;
  end loop;
  if not exists (select 1 from pg_class where relname = 'location_audit_waterfall_order'
                   and relkind = 'i') then
    raise exception '526: the (step_no, sub_no) index is missing';
  end if;
  if not has_table_privilege('authenticated', 'public.location_audit_waterfall', 'select') then
    raise exception '526: authenticated can no longer read the waterfall';
  end if;
  if not exists (select 1 from public.derived_artifacts
                  where name = 'location_audit_waterfall') then
    raise exception '526: location_audit_waterfall not registered in derived_artifacts';
  end if;

  -- Nine rows, and every step key the two readouts share or deduct.
  select count(*) into bad from location_audit_waterfall;
  if bad <> 9 then
    raise exception '526: waterfall has % rows, expected 9', bad;
  end if;
  for r in select unnest(array['all_listings', 'with_verdict', 'located',
                              'located_town', 'located_foreign', 'located_no_town',
                              'hidden', 'hidden_unresolved', 'hidden_pending']) as k
  loop
    if not exists (select 1 from location_audit_waterfall where step_key = r.k) then
      raise exception '526: waterfall is missing step %', r.k;
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
        raise exception '526: the first chain step lost % (must be 0)', r.lost;
      end if;
    elsif r.lost is distinct from prev - r.n then
      raise exception '526: step % lost % but the chain says %',
        r.step_key, r.lost, prev - r.n;
    end if;
    prev := r.n;
  end loop;

  -- Law 2: the hidden set is the complement of the located set over the WHOLE
  -- database, and what step 3 lost is exactly "judged, still without a location".
  select n into total from location_audit_waterfall where step_key = 'all_listings';
  select n into cur   from location_audit_waterfall where step_key = 'located';
  select n into v_lost from location_audit_waterfall where step_key = 'hidden';
  if cur + v_lost <> total then
    raise exception '526: located % + hidden % <> all %', cur, v_lost, total;
  end if;
  select n into prev from location_audit_waterfall where step_key = 'with_verdict';
  select lost into v_lost from location_audit_waterfall where step_key = 'located';
  if v_lost <> prev - cur then
    raise exception '526: judged-without-location % <> % - %', v_lost, prev, cur;
  end if;

  -- Law 3: every split partitions its parent exactly. This is what makes ABROAD
  -- an answer and never a loss: it is inside `located`, with the town and the
  -- point-without-a-town, and the three of them sum to it.
  for r in select w.parent_key as k, sum(w.n) as s
             from location_audit_waterfall w
            where w.kind = 'split'
            group by w.parent_key
  loop
    select n into cur from location_audit_waterfall where step_key = r.k;
    if cur <> r.s then
      raise exception '526: the sub-rows of % sum to % but % is %',
        r.k, r.s, r.k, cur;
    end if;
  end loop;

  -- Law 4: the share is measured against the WHOLE database.
  select count(*) into bad
    from location_audit_waterfall w
   where w.share_pct is distinct from
         coalesce(round(w.n::numeric * 100 / nullif(total, 0), 3), 0);
  if bad > 0 then
    raise exception '526: % rows carry a share that is not n/total', bad;
  end if;

  -- The consumer rule, race-safe (525): the audit matview's cohort IS the rule
  -- negated, so no row in it may have been servable WHEN THE SNAPSHOT WAS TAKEN.
  -- A row resolved since is the lane at work, not a contradiction.
  select count(*) into bad
    from location_pin_audit_mv a
   where exists (select 1 from listing_location sl
                  where sl.listing_id = a.listing_id
                    and (sl.geom is not null or sl.country_status = 'foreign')
                    and sl.resolved_at <= a.refreshed_at);
  if bad > 0 then
    raise exception '526: % audit rows were already served by the consumer rule when the snapshot was taken', bad;
  end if;

  select n into cur from location_audit_waterfall where step_key = 'located_foreign';
  select n into prev from location_audit_waterfall where step_key = 'located_town';
  raise notice '526: one vocabulary; % listings, % in a named town, % abroad (an answer, not a loss)',
    total, prev, cur;
end
$$;

reset statement_timeout;
reset lock_timeout;
