-- Extend the operator-tunable yield scenario with a renovation budget.
--
-- The yield calculator (SPA YieldBlock + Chrome-extension panel) gains a
-- `renovation_czk` input: a flat one-off renovation cost added to the
-- listing price to form the TOTAL ACQUISITION COST in the denominator:
--
--   gross yield = ((rent - fond oprav a SVJ) * 12)
--                 ----------------------------------
--                       (listing price + renovation)
--
-- No DDL needed — scenario is JSONB exactly so the shape can grow without
-- a column-per-input (see migration 085). Old rows simply lack the key,
-- which reads as NULL = no renovation = denominator is the price alone, so
-- every pre-existing scenario yields the identical number as before. This
-- migration only refreshes the column comment so the documented contract
-- stays the source of truth.
--
-- Yield % is still NOT stored — it's a deterministic function of
-- (rent, fond_per_m2, price, renovation, area_m2) computed on read by each
-- client; caching it would just create staleness risk.

COMMENT ON COLUMN estimation_runs.scenario IS
  'Operator-tunable yield scenario: {rent_czk, fond_per_m2_czk, price_czk, renovation_czk, updated_at}. '
  'renovation_czk is a flat one-off cost added to price_czk to form the total acquisition cost '
  '(the yield denominator); NULL/absent means no renovation. '
  'NULL column means use defaults. Latest-wins; SPA YieldBlock and Chrome extension both write.';
