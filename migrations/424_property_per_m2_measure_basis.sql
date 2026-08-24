-- 424_property_per_m2_measure_basis.sql
-- W3 of the per-m2 measure program: ONE property-grain measure with a coherent
-- numerator and denominator, and a stamp naming the row both came from.
--
-- THE DEFECT. properties.current_price_czk is mirrored from the REPRESENTATIVE
-- child (the display listing), while properties.area_m2 was a golden-record pick
-- taken independently, in source-trust order, across ALL children. On a merged
-- multi-portal property the two therefore came from DIFFERENT listings, so every
-- consumer that divides one by the other was dividing one portal's price by
-- another portal's area. source_trust_rank puts mmreality at 4 — above five
-- portals — which is exactly how a listing-grain area defect escapes its own row
-- and becomes the denominator of a merged property's per-m2.
--
-- NOT FIXED BY REORDERING THE RANKS. source_trust_rank governs survivorship for
-- ~30 fields; re-ranking a portal to route around one parser bug is a per-portal
-- branch in shared code by another name (CLAUDE.md rule 21). The parser is fixed
-- at the parser; the measure is fixed here, at the grain.
--
-- SCOPE: this wave WRITES the label. Nothing reads it yet -- the column is not
-- projected onto properties_public or browse_list and no API route selects it,
-- so properties_public.price_per_m2 still renders a cross-row ratio unchanged.
-- Gating a surface on the stamp (projecting it onto the read model, then
-- suppressing or footnoting an unlabelled per-m2 in Browse, the listing header,
-- the extension and the watchdog price_per_m2 filter) is the following wave.
-- Writing the label first is what makes that wave possible: the stamp has to be
-- complete and verified before any surface can act on it.
--
-- Fully additive, catalog-only, no backfill: the column is populated by
-- scripts/recompute_property_stats.py (incremental */5 for dirty properties, the
-- daily full sweep for everything else) and by the three singleton-creation
-- paths, so it is complete within one full sweep of deploy.
--
-- DEPLOY ORDER: apply this migration BEFORE the code that references it merges.
-- scraper/db.py's insert path, toolkit/property_identity.py's split path and the
-- recompute all name the column + the function; without them present those
-- statements fail.

SET lock_timeout = '5s';

-- 1. The measure's basis stamp ----------------------------------------------
-- Holds a listings.id (the surrogate PK, like properties.repr_listing_ref_id) --
-- NOT the legacy sreality_id that properties.repr_listing_id carries. No FK: the
-- constraint would buy nothing (rule #3 -- listings are never deleted) and costs
-- a validate pass over 620k rows.
ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS price_per_m2_source_listing_id bigint;

COMMENT ON COLUMN properties.price_per_m2_source_listing_id IS
    'The listings.id whose price AND area both back this property''s per-m2 '
    'measure -- i.e. the numerator and the denominator came from ONE row. NULL '
    'means no single child supplies a valid pair (no price, no area, a '
    'zero/negative area, or the representative child carries a price but no '
    'area so area_m2 fell back to a sibling): the per-m2 is then a cross-row '
    'ratio that describes no single listing. Written by '
    'scripts/recompute_property_stats.py and the singleton-creation paths. '
    'WRITE-ONLY as of migration 424 -- no view, API route or surface reads it '
    'yet; the wave that projects it onto the read model and gates the display '
    'on it is separate.';

-- 2. ONE definition of "is there a valid per-m2 basis here" ------------------
-- Four writers stamp this column (the recompute, the straggler attach, the
-- insert-time singleton, the unmerge split). Copying the validity predicate into
-- each is how four definitions of one measure start; this is the single one they
-- all call. Single-statement IMMUTABLE SQL, so the planner inlines it.
CREATE OR REPLACE FUNCTION public.price_per_m2_basis(
    p_price_czk numeric,
    p_area_m2 numeric,
    p_listing_id bigint
)
RETURNS bigint
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN p_price_czk IS NOT NULL
         AND p_area_m2 IS NOT NULL
         AND p_area_m2 > 0
        THEN p_listing_id
    END
$$;

COMMENT ON FUNCTION public.price_per_m2_basis(numeric, numeric, bigint) IS
    'Returns p_listing_id when that one row can back a per-m2 measure (a price, '
    'and a strictly positive area), else NULL. The validity bound of the '
    'per-m2 measure, in one place, for every writer of '
    'properties.price_per_m2_source_listing_id.';

GRANT EXECUTE ON FUNCTION public.price_per_m2_basis(numeric, numeric, bigint)
    TO anon, authenticated, service_role;

-- 3. Record what the trust order now also governs ----------------------------
-- Unchanged ranks (deliberately -- see the header). The comment is restated in
-- full because COMMENT ON replaces rather than appends.
COMMENT ON FUNCTION public.source_trust_rank(text) IS
    'Per-portal trust order (lower = more trusted). Single source of truth for '
    'representative-sibling selection; mirror of toolkit/source_trust.py. '
    'Non-sensitive static logic -- deliberately callable by all roles so an '
    'anon-exposed view may reference it. '
    'ALSO GOVERNS THE PER-M2 DENOMINATOR (migration 424): when the '
    'representative child carries no area, properties.area_m2 falls back to the '
    'best-ranked child that does, so this order decides which listing''s area '
    'divides a merged property''s price. mmreality sits at rank 4, above five '
    'portals -- a listing-grain area defect there reaches merged properties '
    'through this function. Fix such a defect at the portal parser: reordering '
    'the ranks to route around one would silently change survivorship for the '
    '~30 other fields that share this order (CLAUDE.md rule 21).';

-- VERIFICATION (run after the next full sweep of recompute_property_stats):
--
--   -- must be 0: a stamped measure whose denominator came from a different
--   -- listing than its numerator.
--   SELECT count(*) FROM properties
--   WHERE price_per_m2_source_listing_id IS NOT NULL
--     AND price_per_m2_source_listing_id IS DISTINCT FROM repr_listing_ref_id;
--
--   -- the residual: properties that still have a computable per-m2 with no
--   -- single-listing basis (representative child priced but area-less, so
--   -- area_m2 came from a sibling). Expected small and multi-child only;
--   -- these are the rows a surface must not label as one listing's figure.
--   SELECT count(*) FROM properties
--   WHERE current_price_czk IS NOT NULL AND area_m2 > 0
--     AND price_per_m2_source_listing_id IS NULL;
--
--   -- and it must be multi-child only:
--   SELECT count(*) FROM properties
--   WHERE current_price_czk IS NOT NULL AND area_m2 > 0
--     AND price_per_m2_source_listing_id IS NULL AND source_count = 1;
