-- 483: keep presence checks out of the Health queue metrics.
--
-- Rule #3 since 2026-09-07: a complete walk nominates every active row it did
-- not see into listing_detail_queue at priority -1 (QUEUE_PRIORITY_VERIFY), and
-- the drain serves those LAST on purpose -- a backlog of page checks must never
-- delay a brand-new listing. The health matview (migration 354) reads the
-- queue through `listing_detail_queue_public` to compute claimable backlog and
-- p50/p90 queue lag as "how long does a new or changed listing wait", filtering
-- only failure-retry (priority 2). Rows designed to wait would satisfy that
-- filter, so the first big nomination (ceskereality: thousands per walk) would
-- turn detail_queue_backlog, detail_queue_lag and e2e_latency red for a queue
-- behaving exactly as specified, and bury the signal those checks exist for
-- (the 2026-08 ingest starvation).
--
-- The view keeps its columns and its posture; it now shows the ingest queue
-- only. It stays ungated because the health matview's refresh (pg_cron) reads
-- it and it carries no listing content -- source, priority and timestamps, the
-- same exposure as since migration 109.
-- ci-allow-ungated: listing_detail_queue_public redefinition of the migration-109 view the health matview refresh reads; queue timestamps only, no listing content
create or replace view listing_detail_queue_public as
  select source, priority, enqueued_at, claimed_at, given_up
  from listing_detail_queue
  where priority >= 0;

-- Presence checks are visible through this new view, admin-gated like every
-- operational view since migration 318 (SPA route-gating is not a boundary).
create or replace view listing_presence_checks_public as
select * from (
  select source, enqueued_at, claimed_at, given_up, attempts
  from listing_detail_queue
  where priority < 0
) __admin_gate
where is_platform_admin();

grant select on listing_presence_checks_public to authenticated;

comment on view listing_detail_queue_public is
  'The ingest queue (new, changed, failure-retry). Presence checks (priority < 0) live in listing_presence_checks_public — they are served last by design and must not count as ingest lag (rule #3, 2026-09-07).';
comment on view listing_presence_checks_public is
  'Page checks nominated by complete walks (rule #3, 2026-09-07): rows the index did not see, waiting for the drain to fetch their page. Served after every ingest row.';
