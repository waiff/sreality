-- 127_dedup_eligibility.sql
-- Dedup engine rebuild (rule A): a listing only participates in matching when
-- it has BOTH a street and a disposition — the two key identifiers the new
-- street+disposition engine keys on. Everything else is flagged and excluded.
--
-- A GENERATED STORED column rather than a maintained one: the rule is a pure
-- function of (street, disposition), so deriving it automatically on every
-- write means it can never drift out of sync and needs no backfill — the ALTER
-- computes it for every existing row in place. The scraper/parser writes
-- street + disposition; eligibility falls out for free.
--
--   'location_unclear'    — no street (street NULL or empty). Most non-sreality
--                           rows today (bazos/idnes/bezrealitky rarely yield a
--                           street) plus rural sreality rows (village addresses
--                           carry no street). Never matched.
--   'disposition_unclear' — has a street but no disposition (e.g. pozemky).
--   'eligible'            — has both; the only rows the engine considers.

ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS dedup_eligibility text
    GENERATED ALWAYS AS (
      CASE
        WHEN street IS NULL OR street = '' THEN 'location_unclear'
        WHEN disposition IS NULL            THEN 'disposition_unclear'
        ELSE 'eligible'
      END
    ) STORED;

-- The engine selects eligible rows only; a partial index keeps that scan tight
-- as the bulk of rows are (and will remain) location_unclear.
CREATE INDEX IF NOT EXISTS listings_dedup_eligible_idx
  ON listings (street, disposition)
  WHERE dedup_eligibility = 'eligible';
