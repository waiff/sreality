-- 434_brokers_active_id_index.sql
-- Cardinality Doctrine W4 (item 3) — the index that makes the activity semi-join cheap.
--
-- Migration 435 moves `status='active'` INTO the ranking CTE as a semi-join, because
-- hoisting the display join without hoisting the filter changes MEMBERSHIP, not just cost
-- (see 435's header). That semi-join needs to produce `id` restricted to actives, and
-- `brokers` carries no index that can: the existing `brokers_active_idx` is
-- `btree (status) WHERE status='active'` with no payload, so `select id from brokers where
-- status='active'` falls back to a full seq scan — measured 1,776 blocks.
--
-- BARE (id). NO INCLUDE PAYLOAD, DELIBERATELY.
-- `_BROKER_ROLLUP` UPDATEs 17 columns on every active broker every 10 minutes, including
-- `stats_computed_at = now()` which changes unconditionally. An INCLUDE carrying
-- display_name, any *_count or the timestamps would break HOT-update eligibility on every
-- qualifying row on every pass — 144 passes a day — and the index would bloat continuously,
-- decaying the win invisibly. Neither `id` nor `status` is in that SET list, so this index
-- is never touched by the rollup and no row ever migrates into or out of the partial
-- predicate. A bare (id) also carries no email/phone-shaped column, satisfying the standing
-- broker-PII constraint (the INCLUDE variant would have put a second physical copy of
-- 22,760 emails and phones on disk, outside the A6 perimeter's view enumeration).
--
-- HONEST CEILING. This index does NOT reach the "~600-700 block" floor and cannot.
-- `count(*) from brokers where status='active'` index-only-scans in 764 blocks with
-- Heap Fetches: 14,630 — 64% of rows need a heap visit because the 10-minute rollup keeps
-- most pages `all_visible = false`. The visibility map is a property of the TABLE, not the
-- index, so this index inherits the same penalty: expect ~764 blocks, not the ~112 its own
-- size suggests. It buys ~1,012 blocks (1,776 -> 764) on every shape, which is real. The
-- remaining lever is autovacuum on `brokers`, mirroring what migration 430 did for
-- listing_location_current — filed as a follow-up, deliberately NOT smuggled in here.
--
-- NOT CONCURRENTLY, and the reason is recorded rather than assumed: the Supabase MCP wraps
-- every payload in a transaction (SQLSTATE 25001) through both execute_sql and
-- apply_migration, so CONCURRENTLY is unavailable — the same constraint migration 429 hit.
-- `brokers` is 22 MB / 41,936 rows, of which 22,736 qualify: a sub-second build. Plain
-- CREATE INDEX takes SHARE, which blocks the rollup's UPDATE but not any reader, and
-- lock_timeout bounds the head-block to 6 s if a writer holds RowExclusive.
--
-- Rollback: migrations/reverts/434_revert_brokers_active_id_index.sql
set local lock_timeout = '6s';
set local statement_timeout = '60s';

create index if not exists brokers_active_id_idx
  on public.brokers (id)
  where status = 'active';
