-- 029_sales_estimate.sql
--
-- Adds sale-estimate support to estimation_runs alongside the existing
-- rental-estimate columns. The architecture is a single discriminator
-- (`estimate_kind`) plus parallel sale-shaped columns; rent runs keep
-- writing the rent columns, sale runs populate the sale columns, and
-- gross_yield_pct stays useful for both directions (rent estimate ÷
-- purchase price OR sale estimate paired with expected rent).
--
-- Why parallel columns rather than renaming the rent columns to
-- generic "value" columns:
--   * Migrations are append-only (CLAUDE.md architectural rule #1).
--   * Existing rows stay valid without a backfill copy step.
--   * Every reader site (`_RUN_COLUMNS`, frontend types, public views
--     if any are added) can adopt the new columns incrementally
--     without churn.
--
-- estimate_kind:
--   NULL on historical rows = implicit rent (every row written before
--   this migration is a rental run by construction). New rows set it
--   explicitly. The CHECK constraint allows NULL deliberately so we
--   don't have to backfill 010-era rows; application code defaults
--   missing values to 'rent' on read.
--
-- Confidence and warnings are written into the existing `confidence`
-- and `warnings` columns regardless of kind — the meaning of those
-- columns is "how good is this estimate", not "how good is this
-- rental estimate", so they carry over unchanged.

alter table estimation_runs
  add column estimate_kind text
    check (estimate_kind is null or estimate_kind in ('rent', 'sale')),
  add column estimated_sale_price_czk bigint,
  add column sale_p25_czk             bigint,
  add column sale_p75_czk             bigint;

create index on estimation_runs (estimate_kind);
