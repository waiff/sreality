-- 368_listing_discovery_seq.sql
--
-- Root cause of "Browse newest-first doesn't mirror the portal's own newest-first
-- order" (docs/design/portal-order-fidelity.md, Phase 1): listings.first_seen_at
-- is stamped at detail-drain WRITE time (batched up to 100 rows per transaction,
-- fetched concurrently on a thread pool, sometimes by two independent drain
-- processes at once) — not at index-walk DISCOVERY time. Every one of those is a
-- reordering between "we found this id" and the timestamp Browse sorts on.
--
-- Fix: a dedicated monotonic sequence, assigned once at the index-walk's ORIGINAL
-- enqueue time. Unlike now() (fixed for a whole statement/transaction, so up to
-- 1000 enqueued rows or 100 written rows can share one identical value),
-- nextval() advances once per row even inside a single multi-row INSERT — a true
-- relative discovery order, immune to every downstream batching/concurrency
-- reordering because the value is fixed before any of it happens.
--
-- listing_detail_queue.discovery_seq is populated by the column DEFAULT on every
-- existing enqueue_detail INSERT (no code change needed there) and, like
-- enqueued_at, is never touched by the ON CONFLICT re-enqueue path — a retried or
-- price-changed re-enqueue keeps the listing's original discovery position.
-- listings.discovery_seq is carried from the claimed queue row through the drain
-- write path and set ONCE: never overwritten on a later re-fetch (mirrors the
-- existing source_id_native / geom preserve-if-set pattern in
-- scraper.db._listing_update_set_sql's callers). NULL for rows written before
-- this migration, or via a path outside the queue (e.g. a manual --detail-only
-- single fetch) — that's fine, it only needs to be right going forward.

create sequence listing_discovery_seq;

alter table listing_detail_queue
  add column discovery_seq bigint not null default nextval('listing_discovery_seq');

comment on column listing_detail_queue.discovery_seq is
  'Assigned once at first enqueue (nextval, not now() -- distinct even within one '
  'multi-row INSERT). Never touched by the ON CONFLICT re-enqueue path in '
  'scraper.db.enqueue_detail, mirroring enqueued_at''s existing preserve-original semantics.';

alter table listings
  add column discovery_seq bigint;

comment on column listings.discovery_seq is
  'Monotonic sequence carried from listing_detail_queue.discovery_seq at first write; a true '
  'relative-discovery-order signal, unlike first_seen_at (stamped at batched/concurrent '
  'detail-drain WRITE time -- see docs/design/portal-order-fidelity.md). Set once, on INSERT '
  'only; scraper.db._listing_update_set_sql''s callers never let a later write change it '
  '(COALESCE(listings.discovery_seq, EXCLUDED.discovery_seq)). NULL for rows predating this '
  'migration or written outside the queue-driven drain path.';
