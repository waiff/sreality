-- Model-keyed description-enrichment cache (sticky-miss fix).
--
-- listing_description_enrichments was UNIQUE(sreality_id, snapshot_id): a MISS
-- (the LLM returned null / low-confidence for a field) still wrote a cache row,
-- so the listing's latest snapshot was retired from selection FOREVER — even
-- after a model upgrade that would now extract the field. For a stable
-- classified ad (rare new snapshots) that meant ~80% of cache rows had floor
-- still NULL and were never retried.
--
-- Widen the uniqueness to include `model` (mirrors building_attachment_analyses /
-- read_floor_plan's (attachment_id, model) cache, CLAUDE.md LLM-analysis section):
-- a model upgrade re-attempts every listing for a fresh extraction, while a
-- same-model re-run stays a no-op (no re-bill). `_select_pending` and the
-- enricher's cache-check / ON CONFLICT are all scoped to the current model.
--
-- Purely a uniqueness WIDENING: `model` is NOT NULL (migration 124) and every
-- existing (sreality_id, snapshot_id) row is unique, so each is still unique
-- under the wider key — no row conflicts, no data change.
ALTER TABLE listing_description_enrichments
  DROP CONSTRAINT IF EXISTS listing_description_enrichments_sreality_id_snapshot_id_key;

ALTER TABLE listing_description_enrichments
  ADD CONSTRAINT listing_description_enrichments_sid_snapshot_model_key
  UNIQUE (sreality_id, snapshot_id, model);
