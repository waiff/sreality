-- 184: index for the street backfill's keyset scan.
--
-- WHY: backfill_portal_streets keyset-paginates by the PK scoped to one source
-- (source-id-ordered, candidate rows only). Without a supporting index the
-- planner either walks the global PK across ~190k other-source negative ids
-- before reaching sreality's positive ids, or builds a bitmap+sort that filters
-- ALL ~70k candidates (deref-ing the marker jsonb on each) before the LIMIT —
-- both exceed statement_timeout on sreality's large-jsonb rows.
--
-- FIX: a partial index on (source, sreality_id) over the candidate set
-- (street IS NULL AND is_active). With source equality it yields sreality_id
-- order directly, so the planner uses an ordered index scan that stops early at
-- LIMIT — marker deref happens on ~one chunk, not the whole table. Partial, so
-- it stays small and rows drop out as the backfill fills street.
drop index if exists listings_source_id_street_null_idx;
create index if not exists listings_source_active_street_idx
  on listings (source, sreality_id) where street is null and is_active;
