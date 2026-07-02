-- 265_detail_queue_completions.sql
--
-- Ledger of listing_detail_queue completions. The queue row is DELETEd when the
-- drain resolves it (detail written / listing gone) and first_seen_at is stamped
-- at detail-write time, so the pipeline's FIRST hop — enqueue -> detail write —
-- was unrecoverable. Each completion now INSERTs the row's lifecycle timestamps
-- here in the same transaction (scraper/db.py: complete_detail / fail_detail),
-- making that latency a queryable SLI (detail_latency_recent below).
--
-- EPHEMERAL, like listing_freshness_checks (architectural rule #9): rows older
-- than 7 days are dead weight. No pg_cron — each drain's reclaim_stale_claims
-- prunes its own source's expired rows at run start.

create table detail_queue_completions (
  id           bigserial primary key,
  source       text not null,
  native_id    text not null,
  priority     int,
  attempts     int,
  enqueued_at  timestamptz not null,
  claimed_at   timestamptz,
  completed_at timestamptz not null default now(),
  outcome      text not null check (outcome in ('written', 'gone', 'given_up'))
);

-- Serves both the 24h latency window and the per-source retention delete.
create index detail_queue_completions_source_completed_idx
  on detail_queue_completions (source, completed_at);

alter table detail_queue_completions enable row level security;
-- No anon policy: internal instrumentation, written only by the service-role drain.

-- The first-hop SLI the Health page will read: per-source enqueue->completion
-- latency over the trailing 24h.
create view detail_latency_recent as
select
  source,
  count(*) as completions,
  percentile_cont(0.5) within group
    (order by extract(epoch from (completed_at - enqueued_at))) as p50_seconds,
  percentile_cont(0.9) within group
    (order by extract(epoch from (completed_at - enqueued_at))) as p90_seconds
from detail_queue_completions
where completed_at > now() - interval '24 hours'
group by source;
