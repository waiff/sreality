-- Operator-tunable yield scenario on estimation_runs.
--
-- Today the SPA's YieldBlock persists per-run scenario edits
-- (monthly rent / fond oprav per m² / purchase price) to localStorage
-- under sreality.estimation.{run.id}.yield. That kept things simple
-- when the SPA was the only surface, but the Chrome extension needs
-- to read the same scenario from a different origin — so the
-- canonical home moves into the row.
--
-- Stored as a single JSONB column rather than three numeric columns
-- so we can extend the scenario shape later (occupancy %, mortgage
-- terms, exit assumptions) without piling up nullable columns. NULL
-- means "no operator overrides yet — render defaults": estimated
-- rent, 10 CZK/m², subject sale price.
--
-- Latest-wins; both the SPA and the extension PATCH the same row.
-- Yield % is NOT stored here — it's a deterministic function of
-- (rent, fond_per_m2, price, area_m2) computed on read, so caching
-- it would just create staleness risk.

ALTER TABLE estimation_runs
  ADD COLUMN scenario jsonb;

COMMENT ON COLUMN estimation_runs.scenario IS
  'Operator-tunable yield scenario: {rent_czk, fond_per_m2_czk, price_czk, updated_at}. '
  'NULL means use defaults. Latest-wins; SPA YieldBlock and Chrome extension both write.';
