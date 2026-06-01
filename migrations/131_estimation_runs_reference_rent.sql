-- 131_estimation_runs_reference_rent.sql
--
-- Secondary rental reference on estimation_runs, from the MF "Cenová mapa
-- nájemného" (rent price map). Every rental estimate gets a second,
-- independent figure: the state's hedonic-model reference rent for the
-- subject's territory + size category, plus the published per-amenity
-- adjustments, scaled by area. It is a sanity-check shown ALONGSIDE the
-- comparables-based primary estimate — it never overrides it.
--
-- Stored as a single JSONB column (same reasoning as migration 085's
-- `scenario`): the payload is a structured breakdown — matched territory,
-- VK category, base per-m², each adjustment, novostavba flag, source
-- revision, final CZK — that we want to extend without piling up nullable
-- columns. NULL means "not computed": sale runs, territory miss, missing
-- area, or no rent-map revision ingested yet. Best-effort — the reference
-- calc never fails an estimation run.

ALTER TABLE estimation_runs
  ADD COLUMN reference_rent jsonb;

COMMENT ON COLUMN estimation_runs.reference_rent IS
  'MF Cenová mapa secondary rent reference: {territory, vk, is_novostavba, '
  'source_revision, source_date, base_per_m2, adjustments[], total_per_m2, '
  'area_m2, monthly_rent_czk}. NULL = not computed (sale run / territory miss / '
  'no revision). Read-only secondary; never overrides the primary estimate.';
