-- 521_location_w10_gone_bodies_by_id.sql
--
-- W10, third cut, and the one that finishes. The STAMP is unchanged from 519 and
-- 520 -- a stored bazos body proven to be the category-index page a removed ad
-- answers with has its `http_status` corrected to 410 -- and both of those files
-- stay on disk unedited (CLAUDE.md rule 1). What changes is how the batch is
-- addressed, and that is the whole difference between a 900 s timeout and ~10 s.
--
-- THE REAL COST, MEASURED. `portal_raw_pages` interleaves nine portals in one id
-- sequence: 142,506 bazos detail pages are spread over ids 3..6,302,167, which is
-- **44 ids per bazos row**. 519 and 520 both bounded the batch by an id RANGE
-- (`r.id > v_after and r.id <= v_hi`), and the planner puts the two `html ilike`
-- tests FIRST in that scan's Filter -- ahead of `source = 'bazos'`. So every batch
-- detoasted every page in the range, ~44 of them per bazos page and most of them
-- idnes pages several times larger. A 1,000-page batch was really ~44,000
-- detoasts; at ~20 ms each that is the 900 s it spent. (519 additionally carried a
-- quadratic LIKE pattern, fixed in 520 and kept fixed here; that was real but it
-- was the smaller half.)
--
-- THE FIX: ADDRESS THE BATCH BY ID LIST, NOT BY ID RANGE. The keyset probe already
-- computes exactly the 2,000 bazos ids the batch is about, so it hands them to the
-- UPDATE as a `bigint[]` and the scan becomes `r.id = ANY(v_ids)` -- 2,000 index
-- probes, 2,000 detoasts, no other portal's page touched at all. Verified on prod
-- before writing this file: 1,000 ids, 9.7 s, 816 pages rejected by the gone filter
-- and 184 matched, against >900 s for the same work addressed by range.
--
-- Everything else -- why a gone page is not a body, why 410 and not a flag, why the
-- content hash AND the one-transaction timestamp, why both #1451 signals must fire,
-- why bazos only, and why nothing is deleted -- is documented in 519 and in
-- `docs/architecture.md` § Location data.
--
-- IDEMPOTENT AND RESUMABLE: the 2xx/NULL guard means the ~3.3k rows 519 committed
-- before it died are skipped without a second thought, every batch commits, and a
-- re-run finishes whatever a spent budget left. On empty tables (the CI schema
-- replay) the first keyset probe returns no ids and the whole file is a no-op.

set lock_timeout = '5s';
set statement_timeout = '900s';

do $$
declare
  v_after    bigint := 0;
  v_ids      bigint[];
  v_batch    bigint;
  v_total    bigint := 0;
  v_deadline timestamptz := clock_timestamp() + interval '45 minutes';
begin
  loop
    select array_agg(id), max(id) into v_ids, v_after
      from (select id from portal_raw_pages
             where source = 'bazos' and page_kind = 'detail' and id > v_after
             order by id limit 2000) q;
    exit when v_ids is null;

    update portal_raw_payloads p
       set http_status = 410
      from portal_raw_pages r
     where r.id = any(v_ids)
       and r.html ilike '%Inzerát byl vymazán%'
       and r.html ilike '%inzerce - Reality | Bazoš.cz%'
       and p.source = 'bazos'
       and p.page_kind = 'detail'
       and p.source_id_native = r.source_id_native
       and (p.http_status is null or p.http_status between 200 and 299)
       and abs(extract(epoch from (r.fetched_at - p.last_observed_at))) <= 5
       and p.body_sha256 = sha256(convert_to(r.html, 'UTF8'));

    get diagnostics v_batch = row_count;
    v_total := v_total + v_batch;
    commit;

    if clock_timestamp() > v_deadline then
      raise notice 'W10: budget spent at archive id % after % stamps this pass -- re-run this file to finish', v_after, v_total;
      return;
    end if;
  end loop;

  raise notice 'W10: stamped % bazos detail bodies 410 this pass (a gone page is not a body)', v_total;
end $$;

-- THE READOUT. A body whose bytes the HTML archive no longer holds -- the staging
-- row was overwritten by a later fetch, or the body only ever lived in R2 -- cannot
-- be classified from SQL at all. Those are left alone and counted, so the number is
-- on the record rather than implied by the stamp count.
do $$
declare
  v_total bigint;
  v_determinable bigint;
  v_stamped bigint;
begin
  select count(*),
         count(r.id) filter (
           where abs(extract(epoch from (r.fetched_at - p.last_observed_at))) <= 5),
         count(*) filter (where p.http_status = 410)
    into v_total, v_determinable, v_stamped
    from portal_raw_payloads p
    left join portal_raw_pages r
      on r.source = 'bazos' and r.page_kind = 'detail'
     and r.source_id_native = p.source_id_native
   where p.source = 'bazos' and p.page_kind = 'detail';

  raise notice 'W10 readout: % bazos detail bodies, % now stamped 410, % corroborated by the HTML archive, % undeterminable (archive superseded or body only in R2)',
    v_total, v_stamped, v_determinable, v_total - v_determinable;
end $$;

-- THE ASSERTION, over the oldest 10,000 archived bazos pages -- addressed by id
-- LIST for the same reason the stamp is, and the densest gone-page region of the
-- corpus (~17 % of them are the category index; it is the cohort the W2a-4 backfill
-- seeded from `portal_raw_pages`). Bounded rather than corpus-wide because a full
-- re-scan would be a twenty-minute tautology of the UPDATE above.
do $$
declare
  v_left bigint;
  v_ids bigint[];
begin
  select array_agg(id) into v_ids
    from (select id from portal_raw_pages
           where source = 'bazos' and page_kind = 'detail'
           order by id limit 10000) q;

  if v_ids is null then
    raise notice 'W10 check: no bazos detail pages archived; nothing to verify';
    return;
  end if;

  select count(*) into v_left
    from portal_raw_payloads p
    join portal_raw_pages r
      on r.id = any(v_ids)
     and r.source_id_native = p.source_id_native
   where p.source = 'bazos'
     and p.page_kind = 'detail'
     and (p.http_status is null or p.http_status between 200 and 299)
     and abs(extract(epoch from (r.fetched_at - p.last_observed_at))) <= 5
     and r.html ilike '%Inzerát byl vymazán%'
     and r.html ilike '%inzerce - Reality | Bazoš.cz%'
     and p.body_sha256 = sha256(convert_to(r.html, 'UTF8'));

  if v_left > 0 then
    raise exception 'W10: % bazos bodies whose stored page is the category index still read 2xx', v_left;
  end if;

  raise notice 'W10 check: none of the oldest 10000 archived bazos pages still has a 2xx body holding the category index';
end $$;
