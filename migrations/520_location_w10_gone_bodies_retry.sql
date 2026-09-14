-- 520_location_w10_gone_bodies_retry.sql
--
-- W10, second cut: the SAME stamp as migration 519, with a pattern Postgres can
-- actually run. Migrations are append-only (CLAUDE.md rule 1), so 519 stays on
-- disk unedited and this file supersedes it.
--
-- WHAT WENT WRONG, AND IT WAS NOT THE PLAN. 519's plan was the right one -- a
-- pkey range scan of `portal_raw_pages` feeding an index probe into
-- `portal_raw_payloads` -- but its title test was
-- `html ilike '%<title>%inzerce - Reality | Bazoš.cz%'`. TWO internal wildcards
-- make LIKE quadratic: every '<title>' position is retried against every later
-- position, over a ~100 KB document. Measured against a single-wildcard pattern
-- on the same rows: ~2.2 ms per page vs >180 ms. A 5 000-page batch that should
-- have taken ~13 s spent the 900 s statement timeout (run 34824185812,
-- 2026-09-14 08:59Z) after committing 3 346 stamps.
--
-- THE PATTERN, FIXED. The `<title>` anchor is dropped -- the phrase
-- "inzerce - Reality | Bazoš.cz" is the category index's own title text and
-- selected EXACTLY the same pages without it (598/598 on the oldest 3 000
-- archived pages, where the anchored form also found 598) -- and the BANNER runs
-- first because it is the more selective of the two, so the ~83 % of pages that
-- are real ads are rejected on one single-wildcard scan. Both signals are still
-- required together, and the content hash still decides.
--
-- Batches are 1 000 pages, not 5 000, so a slow region costs a slow batch and
-- never a timeout; the loop carries a 45-minute wall-clock budget so a bad day
-- ends in a committed partial pass with a NOTICE instead of a killed job. Every
-- batch commits, the 2xx guard makes a re-run stamp only what is left, and on
-- empty tables (the CI schema replay) the first keyset probe returns NULL and
-- the whole file is a no-op.
--
-- Everything else -- why a gone page is not a body, why 410 and not a flag, why
-- the hash AND the timestamp, why both signals, why bazos only, and why nothing
-- is deleted -- is documented in 519 and in `docs/architecture.md`
-- § Location data. This file changes one predicate and the batch size.

set lock_timeout = '5s';
set statement_timeout = '900s';

do $$
declare
  v_after    bigint := 0;
  v_hi       bigint;
  v_batch    bigint;
  v_total    bigint := 0;
  v_deadline timestamptz := clock_timestamp() + interval '45 minutes';
begin
  loop
    select max(id) into v_hi
      from (select id from portal_raw_pages
             where source = 'bazos' and page_kind = 'detail' and id > v_after
             order by id limit 1000) q;
    exit when v_hi is null;

    update portal_raw_payloads p
       set http_status = 410
      from portal_raw_pages r
     where r.id > v_after
       and r.id <= v_hi
       and r.source = 'bazos'
       and r.page_kind = 'detail'
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
    v_after := v_hi;
    commit;

    if clock_timestamp() > v_deadline then
      raise notice 'W10: budget spent at archive id % after % stamps this pass -- re-run this file to finish', v_after, v_total;
      return;
    end if;
  end loop;

  raise notice 'W10: stamped % bazos detail bodies 410 this pass (a gone page is not a body)', v_total;
end $$;

-- THE READOUT. A body whose bytes the HTML archive no longer holds -- the staging
-- row was overwritten by a later fetch, or the body only ever lived in R2 --
-- cannot be classified from SQL at all. Those are left alone and counted, so the
-- number is on the record rather than implied by the stamp count.
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

-- THE ASSERTION, over the oldest 40 000 archived ids -- the densest gone-page
-- region of the corpus (~17 % of bazos detail pages there are the category
-- index) and the one the W2a-4 backfill seeded from `portal_raw_pages`. Bounded
-- rather than corpus-wide because a full re-scan would be a several-minute
-- tautology of the UPDATE above; bounded is still a real check with teeth.
do $$
declare
  v_left bigint;
  v_ceiling bigint;
begin
  select coalesce(min(id), 0) + 40000 into v_ceiling
    from portal_raw_pages where source = 'bazos' and page_kind = 'detail';

  select count(*) into v_left
    from portal_raw_payloads p
    join portal_raw_pages r
      on r.source = 'bazos' and r.page_kind = 'detail'
     and r.source_id_native = p.source_id_native
   where p.source = 'bazos'
     and p.page_kind = 'detail'
     and r.id <= v_ceiling
     and (p.http_status is null or p.http_status between 200 and 299)
     and abs(extract(epoch from (r.fetched_at - p.last_observed_at))) <= 5
     and r.html ilike '%Inzerát byl vymazán%'
     and r.html ilike '%inzerce - Reality | Bazoš.cz%'
     and p.body_sha256 = sha256(convert_to(r.html, 'UTF8'));

  if v_left > 0 then
    raise exception 'W10: % bazos bodies whose stored page is the category index still read 2xx', v_left;
  end if;

  raise notice 'W10 check: no bazos body below archive id % still reads 2xx with a category-index page stored', v_ceiling;
end $$;
