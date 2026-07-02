-- 265_dedup_engine_runs_nullable_gauges.sql
-- Metrics hygiene (audit PR-F): the run row conflated MARKET GAUGES (eligible /
-- flagged_location / flagged_disposition — properties of the whole table) with RUN-SCOPED
-- counters (pairs, merges). Computing the gauges costs a ~9-second full-table aggregate
-- (parallel seq scan over ~470k rows), and every street-pass run paid it — including the
-- HOURLY dirty drain and the 2-hourly candidate drain, whose actual work (the scoped
-- eligible load) takes milliseconds. The gauges only need refreshing where they are
-- market-wide anyway: the FULL scan.
--
-- NULL = "not measured on this run": scoped runs (dirty / candidates) now write NULL
-- gauges, and the dashboards read gauges from the latest FULL-scan row (run_kind='full',
-- or legacy pre-262 rows where run_kind is NULL — those always carried market-wide
-- values). The geo lane's rows (new in PR-F: the geo pass previously wrote NO run row at
-- all, hiding its truncation/duration) carry their own lane's eligible count and are
-- excluded from the street gauge pickers by run_kind.

alter table dedup_engine_runs
  alter column eligible drop not null,
  alter column flagged_location drop not null,
  alter column flagged_disposition drop not null;

comment on column dedup_engine_runs.eligible is
  'Market-wide street+disposition eligible count. NULL on scoped runs (dirty/candidates '
  '— not measured; the ~9s full-table gauge scan only runs on full scans, migration 265). '
  'On run_kind=''geo'' rows it is the GEO lane''s eligible count, not the street gauge.';
