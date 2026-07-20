-- 337_r2_phase_d_prep.sql
-- R2 Phase D steps 2-5 (docs/design/listing-identity-r2-pk-swap-runbook.md §5):
-- relax NOT NULL on every R2_CARRIERS legacy column that still enforced it, swap
-- estimation_cohort_entries's PK onto its surrogate, and pre-build the two indexes
-- + id NOT NULL the future Gate 1 PK swap on `listings` needs. Applied live via
-- scripts/apply_r2_phase_d_prep.py (the two CREATE UNIQUE INDEX calls on `listings`,
-- 562k+ rows, must run CONCURRENTLY, illegal inside this migration's transaction —
-- same reasoning as migration 333); this is the plain form for fresh rebuilds.

ALTER TABLE listing_condition_scores ALTER COLUMN sreality_id DROP NOT NULL;
ALTER TABLE listing_marker_extractions ALTER COLUMN sreality_id DROP NOT NULL;
ALTER TABLE listing_summaries ALTER COLUMN sreality_id DROP NOT NULL;
ALTER TABLE building_unit_extractions ALTER COLUMN sreality_id DROP NOT NULL;
ALTER TABLE listing_description_enrichments ALTER COLUMN sreality_id DROP NOT NULL;
ALTER TABLE listing_image_comparisons ALTER COLUMN sreality_id_a DROP NOT NULL;
ALTER TABLE listing_image_comparisons ALTER COLUMN sreality_id_b DROP NOT NULL;
ALTER TABLE listing_visual_matches ALTER COLUMN sreality_id_a DROP NOT NULL;
ALTER TABLE listing_visual_matches ALTER COLUMN sreality_id_b DROP NOT NULL;
ALTER TABLE listing_floor_plan_matches ALTER COLUMN sreality_id_a DROP NOT NULL;
ALTER TABLE listing_floor_plan_matches ALTER COLUMN sreality_id_b DROP NOT NULL;
ALTER TABLE listing_site_plan_matches ALTER COLUMN sreality_id_a DROP NOT NULL;
ALTER TABLE listing_site_plan_matches ALTER COLUMN sreality_id_b DROP NOT NULL;
ALTER TABLE property_merge_events ALTER COLUMN listing_id DROP NOT NULL;
ALTER TABLE manual_rental_estimates ALTER COLUMN sreality_id DROP NOT NULL;
ALTER TABLE manual_rental_estimates_history ALTER COLUMN sreality_id DROP NOT NULL;

-- estimation_cohort_entries: PK (estimation_run_id, sreality_id) -> (estimation_run_id,
-- listing_id). A fresh dedicated index, not a reuse of Phase B2's
-- estimation_cohort_entries_run_listing_id_key — an index already owned by one
-- constraint can't back a second (same reason Gate 1 pre-builds listings_id_pk_idx
-- instead of reusing listings_id_key).
CREATE UNIQUE INDEX IF NOT EXISTS estimation_cohort_entries_run_listing_id_pk_idx
  ON estimation_cohort_entries (estimation_run_id, listing_id);

ALTER TABLE estimation_cohort_entries DROP CONSTRAINT IF EXISTS estimation_cohort_entries_pkey;
ALTER TABLE estimation_cohort_entries
  ADD CONSTRAINT estimation_cohort_entries_pkey
  PRIMARY KEY USING INDEX estimation_cohort_entries_run_listing_id_pk_idx;
ALTER TABLE estimation_cohort_entries ALTER COLUMN sreality_id DROP NOT NULL;

-- Gate 1 prerequisites: the PK is currently the ONLY unique index on sreality_id,
-- so this must exist before the future swap or ON CONFLICT (sreality_id) code
-- errors instantly the moment the swap lands. listings_id_pk_idx is the fresh
-- index the PK will be promoted from at that same gate.
CREATE UNIQUE INDEX IF NOT EXISTS listings_sreality_id_uidx ON listings (sreality_id);
CREATE UNIQUE INDEX IF NOT EXISTS listings_id_pk_idx ON listings (id);

-- Instant — proven by the validated listings_id_present CHECK (migration 313), no scan.
ALTER TABLE listings ALTER COLUMN id SET NOT NULL;
