-- 355_properties_merged_source_count_idx.sql
-- Additive index for the /dedup/merged-properties audit browse (the operator's
-- over-merge review): active properties whose child-listing count (source_count)
-- falls in a range, biggest groups first.
--
-- The feature's universe is multi-listing survivors (source_count > 1) — a small
-- minority, since the vast majority of properties are singletons at source_count
-- = 1. Without an index the range COUNT(*) + ORDER BY would scan every active
-- property on each debounced range change. This partial index covers exactly
-- that minority and carries the (source_count DESC, id DESC) sort, so both the
-- COUNT and the page SELECT stay index-driven over a tiny relation. The default
-- filter (min_listings = 2, i.e. source_count >= 2) implies the partial
-- predicate, so the planner uses it directly. Purely additive.

create index if not exists properties_merged_source_count_idx
  on properties (source_count desc, id desc)
  where status = 'active' and source_count > 1;
