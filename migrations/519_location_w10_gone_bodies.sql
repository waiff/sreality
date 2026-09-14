-- 519_location_w10_gone_bodies.sql
--
-- W10 of the location simplification sprint: A GONE PAGE IS NOT A BODY.
--
-- THE DEFECT. bazos answers a REMOVED ad with HTTP 200 and its CATEGORY INDEX
-- page (title "<slug> inzerce - Reality | Bazos.cz", plus the "Inzerat byl
-- vymazan" banner). Until PR #1451 (2026-09-13 19:37Z) `scraper.bazos_client`
-- did not recognise that, so the page was archived like any other detail body.
-- The bodies-first pass (`location_data.claims_intake`) mines THE LATEST body
-- of a key whose `http_status` is NULL or 2xx -- so for every bazos ad removed
-- before that fix it mines a category index, which carries no ad-level
-- location at all, and (where one exists) the ad's last LIVE page sits one
-- version earlier in `portal_raw_payloads`, stored and never read.
--
-- THE FIX, WITH NO NEW COLUMN AND NO FLAG. "Latest body" already means "latest
-- body whose fetch succeeded": all three payload predicates in the pass
-- (`_BODY_JOIN`, `_UNMINED_WINDOW_WHERE`, `_LATEST_BODY_ONLY`) carry
-- `http_status IS NULL OR BETWEEN 200 AND 299`. So the doctrine is expressible
-- in what the row already has: stamp a stored gone page with the TRUTHFUL
-- status of a removed ad, 410. The pass then skips it and falls through to the
-- previous body on its own -- no downstream filter, nothing to keep in sync.
-- `payloads._PRUNE_SQL` already ranks non-2xx rows last, so a stamped gone page
-- is also the first thing evicted when the version cap bites. That is the whole
-- change.
--
-- HOW A STORED BODY IS PROVEN TO BE A GONE PAGE. `portal_raw_pages` is a
-- latest-wins staging row per (source, source_id_native, page_kind) and holds
-- the HTML; `portal_raw_payloads` is the append-on-change store and holds
-- `body_sha256` of the raw bytes. Both are written in ONE transaction by
-- `scraper.db.upsert_portal_raw_page`, so for a payload row whose content IS
-- the archived page the two timestamps are the same transaction clock.
-- Measured on prod 2026-09-14, 800 bazos keys: the timestamp match
-- (|fetched_at - last_observed_at| <= 5s) and the CONTENT match
-- (sha256(html) = body_sha256) selected exactly the same 191 rows, with zero
-- disagreement in either direction. Both are required here anyway -- the hash
-- is the identity, the window is the corroboration.
--
-- BOTH #1451 SIGNALS MUST FIRE, not either. The client ORs them because it is
-- judging a live fetch it can re-try; this stamp is retroactive and applies to
-- pages that were once parsed as live ads, so it takes the conjunction. On
-- 14,000 sampled bazos pages the two signals never disagreed (715/715,
-- 274/274, 598/598, 997/997), so the conjunction costs nothing and cannot
-- silence a live body on one loose marker.
--
-- BAZOS ONLY, DELIBERATELY. mmreality, ceskereality, realitymix, remax and
-- idnes also answer 200 for a removed listing and their `_GONE_MARKERS` also
-- post-date some of their archive (ceskereality's archived-page markers landed
-- 2026-09-07; ~2.9% of its oldest 2,000 detail pages match). But those markers
-- are prose fragments that have not been shown to be impossible on a live page,
-- and a false stamp DESTROYS evidence -- the exact harm this migration exists
-- to undo. bazos is stamped because two independent signals corroborate each
-- other perfectly on it. Extending to another portal is one more block here
-- once its signals have been measured the same way.
--
-- NOTHING IS DELETED (rule #3's posture, applied to the archive): the gone body
-- and its R2 object stay exactly where they are. Only the status is corrected.
--
-- IDEMPOTENT: the 2xx/NULL guard means a second run stamps nothing. Batched by
-- `portal_raw_pages.id` keyset with a COMMIT per batch, because the driving
-- scan detoasts ~142k HTML bodies (~2.2 ms each, ~6 minutes) and one statement
-- would sit near the timeout. On empty tables (the CI schema replay) the first
-- keyset probe returns NULL and the whole file is a no-op.

set lock_timeout = '5s';
set statement_timeout = '900s';

do $$
declare
  v_after bigint := 0;
  v_hi    bigint;
  v_batch bigint;
  v_total bigint := 0;
begin
  loop
    select max(id) into v_hi
      from (select id from portal_raw_pages
             where source = 'bazos' and page_kind = 'detail' and id > v_after
             order by id limit 5000) q;
    exit when v_hi is null;

    update portal_raw_payloads p
       set http_status = 410
      from portal_raw_pages r
     where r.id > v_after
       and r.id <= v_hi
       and r.source = 'bazos'
       and r.page_kind = 'detail'
       and r.html ilike '%<title>%inzerce - Reality | Bazoš.cz%'
       and r.html ilike '%Inzerát byl vymazán%'
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
  end loop;

  raise notice 'W10: stamped % bazos detail bodies 410 (a gone page is not a body)', v_total;
end $$;

-- THE READOUT. A body whose bytes the HTML archive no longer holds -- the
-- staging row was overwritten by a later fetch, or the body only ever lived in
-- R2 -- cannot be classified from SQL at all. Those are left alone and counted,
-- so the number is on the record rather than implied by the stamp count.
do $$
declare
  v_total bigint;
  v_determinable bigint;
begin
  select count(*),
         count(r.id) filter (
           where abs(extract(epoch from (r.fetched_at - p.last_observed_at))) <= 5)
    into v_total, v_determinable
    from portal_raw_payloads p
    left join portal_raw_pages r
      on r.source = 'bazos' and r.page_kind = 'detail'
     and r.source_id_native = p.source_id_native
   where p.source = 'bazos' and p.page_kind = 'detail';

  raise notice 'W10 readout: % bazos detail bodies, % corroborated by the HTML archive, % undeterminable (archive superseded or body only in R2)',
    v_total, v_determinable, v_total - v_determinable;
end $$;

-- THE ASSERTION, over the oldest 40,000 archived ids -- the densest gone-page
-- region of the corpus (~17% of bazos detail pages there are the category
-- index) and the one the W2a-4 backfill seeded from `portal_raw_pages`. Bounded
-- rather than corpus-wide because a full re-scan would be a six-minute
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
     and r.html ilike '%<title>%inzerce - Reality | Bazoš.cz%'
     and r.html ilike '%Inzerát byl vymazán%'
     and p.body_sha256 = sha256(convert_to(r.html, 'UTF8'));

  if v_left > 0 then
    raise exception 'W10: % bazos bodies whose stored page is the category index still read 2xx', v_left;
  end if;

  raise notice 'W10 check: no bazos body below archive id % still reads 2xx with a category-index page stored', v_ceiling;
end $$;
