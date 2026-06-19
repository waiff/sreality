-- 199_api_feed_keyset_indexes.sql
--
-- Composite keyset indexes for the two API list feeds that move to
-- cursor-based infinite scroll. Both are newest-first, append-only feeds
-- that grow under live inserts (every estimation run; every watchdog match),
-- so they page via WHERE (sort_col, id) < (cursor_sort, cursor_id) ORDER BY
-- sort_col DESC, id DESC — which wants the id tiebreaker IN the index next to
-- the sort column (the existing single-column (created_at DESC) /
-- (dispatched_at DESC) indexes order the sort column but can't resolve the
-- id-tiebreak within an identical timestamp from the index alone).
--
-- estimation_runs.id is a serial; notification_dispatches.id is a uuid —
-- both are valid deterministic tiebreakers (the id only has to make the
-- order total, not be time-ordered).

create index if not exists estimation_runs_created_id_keyset_idx
  on estimation_runs (created_at desc, id desc);

create index if not exists notification_dispatches_dispatched_id_keyset_idx
  on notification_dispatches (dispatched_at desc, id desc);
