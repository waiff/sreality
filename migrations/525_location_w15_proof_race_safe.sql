-- 525_location_w15_proof_race_safe.sql
--
-- W15's proof, re-run race-safe. Migration 524 applied everything it creates
-- (the audit matview v3 with 81,642 rows, its indexes and functions, the
-- 9-row waterfall) and then failed on its own fifth assertion, "4 audit rows
-- are served by the consumer rule" (run 34894377206, 2026-09-14 20:44Z).
--
-- The assertion compared a SNAPSHOT with a MOVING TARGET: the matview was
-- populated at 20:41:15, the proof ran at 20:44:33, and in between the lanes
-- resolved four of the ~80 k listings the snapshot lists. All four carry a
-- `resolved_at` newer than the matview's `refreshed_at` -- the lane at work,
-- not a contradiction. A contradiction would be a row the snapshot lists that
-- already HAD an answer when the snapshot was taken; that is what this file
-- asserts, and it is the check the hourly refresh can be held to at any hour.
--
-- Proof-only: no DDL, nothing dropped, nothing populated. psql applies it
-- statement by statement; it is safe to re-run.

set statement_timeout = '300s';
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
    raise exception '525: location_pin_audit_mv missing';
  end if;

  select array_agg(attname order by attnum) into cols
    from pg_attribute
   where attrelid = 'public.location_pin_audit_mv'::regclass
     and attnum > 0 and not attisdropped;

  for r in select unnest(array['state', 'quality', 'claims_now', 'has_row',
                              'has_claims', 'sibling_has_pin', 'refreshed_at']) as k
  loop
    if not (r.k = any(cols)) then
      raise exception '525: location_pin_audit_mv lost the % column', r.k;
    end if;
  end loop;

  -- Total and two-valued: every row is one of the two states, never NULL and
  -- never a third spelling the page has no label for.
  select count(*) into bad
    from location_pin_audit_mv
   where state is null or state not in ('pending', 'unresolved');
  if bad > 0 then
    raise exception '525: % rows carry a state outside (pending, unresolved)', bad;
  end if;

  -- A row with no store row at all can only be pending -- there is no verdict to
  -- call unresolved.
  select count(*) into bad
    from location_pin_audit_mv
   where not has_row and state <> 'pending';
  if bad > 0 then
    raise exception '525: % rows have no verdict yet but are not pending', bad;
  end if;

  -- The cohort IS the consumer rule negated: not one row in it may have been
  -- servable WHEN THE SNAPSHOT WAS TAKEN. A row resolved since (`resolved_at`
  -- newer than the matview's own `refreshed_at`) is the lane working between
  -- the populate and this check -- 524 tripped on four of them.
  select count(*) into bad
    from location_pin_audit_mv a
   where exists (select 1 from listing_location sl
                  where sl.listing_id = a.listing_id
                    and (sl.geom is not null or sl.country_status = 'foreign')
                    and sl.resolved_at <= a.refreshed_at);
  if bad > 0 then
    raise exception '525: % audit rows were already served by the consumer rule when the snapshot was taken', bad;
  end if;

  for r in select unnest(array['location_pin_audit_mv_pk',
                              'location_pin_audit_mv_state']) as k
  loop
    if not exists (select 1 from pg_class
                    where relname = r.k and relkind = 'i') then
      raise exception '525: index % missing', r.k;
    end if;
  end loop;

  -- The summary function came back from the CASCADE with its axis, and the
  -- refresh function was not a casualty of it.
  if to_regprocedure('public.location_pin_audit_summary()') is null
     or to_regprocedure('public.refresh_location_pin_audit_mv()') is null then
    raise exception '525: the pin-audit functions are missing';
  end if;
  if not exists (
    select 1 from pg_proc
     where oid = 'public.location_pin_audit_summary()'::regprocedure
       and 'state' = any(proargnames)
  ) then
    raise exception '525: location_pin_audit_summary() does not return state';
  end if;

  if not exists (select 1 from public.derived_artifacts
                  where name = 'location_pin_audit_mv') then
    raise exception '525: location_pin_audit_mv not registered in derived_artifacts';
  end if;

  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    if not exists (select 1 from cron.job where jobname = 'refresh-location-pin-audit') then
      raise exception '525: the hourly pin-audit refresh is not scheduled';
    end if;
  else
    raise notice '525: pg_cron unavailable, cron assertion skipped';
  end if;

  -- -------- the waterfall
  select count(*) into bad from location_audit_waterfall;
  if bad <> 9 then
    raise exception '525: waterfall has % rows, expected 9', bad;
  end if;
  for r in select unnest(array['all_listings', 'with_verdict', 'located',
                              'located_town', 'located_foreign', 'located_no_town',
                              'hidden', 'hidden_unresolved', 'hidden_pending']) as k
  loop
    if not exists (select 1 from location_audit_waterfall where step_key = r.k) then
      raise exception '525: waterfall is missing step %', r.k;
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
        raise exception '525: the first chain step lost % (must be 0)', r.lost;
      end if;
    elsif r.lost is distinct from prev - r.n then
      raise exception '525: step % lost % but the chain says %',
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
    raise exception '525: located % + hidden % <> all %', cur, v_lost, total;
  end if;
  -- ... and what step 3 lost is exactly "judged, still without a location".
  select n into prev from location_audit_waterfall where step_key = 'with_verdict';
  select lost into v_lost from location_audit_waterfall where step_key = 'located';
  if v_lost <> prev - cur then
    raise exception '525: judged-without-location % <> % - %', v_lost, prev, cur;
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
      raise exception '525: the sub-rows of % sum to % but % is %',
        r.k, r.s, r.k, cur;
    end if;
  end loop;

  -- Law 4: the share is measured against the WHOLE database.
  select count(*) into bad
    from location_audit_waterfall w
   where w.share_pct is distinct from
         coalesce(round(w.n::numeric * 100 / nullif(total, 0), 3), 0);
  if bad > 0 then
    raise exception '525: % rows carry a share that is not n/total', bad;
  end if;

  select n into cur from location_audit_waterfall where step_key = 'hidden';
  raise notice '525: the lane covers every listing; % listings, % in the audit set',
    total, cur;
end
$$;

reset statement_timeout;
