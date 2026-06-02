-- 138_estimation_subject_attributes.sql
--
-- Additive: typed subject attributes for an estimation whose subject has no
-- resolved listings row (a pasted URL from a portal whose listing isn't in our
-- DB, parsed on demand). Lets the Estimation Detail page render the subject's
-- facts grid (building_type / condition / energy / ownership / furnished /
-- amenities / disposition / area / floor / locality) the same way it renders a
-- resolved sreality subject from listings_public.
--
-- NULL when input_sreality_id is set — the UI reads listings_public for those
-- (incl. non-sreality portals matched to an already-scraped row).

ALTER TABLE estimation_runs ADD COLUMN IF NOT EXISTS subject_attributes jsonb;
