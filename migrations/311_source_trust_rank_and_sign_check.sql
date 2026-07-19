-- 311_source_trust_rank_and_sign_check.sql
-- Phase 0 of the listing-identity refactor (docs/design/listing-identity-refactor.md).
-- Fully additive. Three things, no re-key:
--   1. source_trust_rank(text) — the ONE definition of per-portal trust order,
--      mirrored by toolkit/source_trust.py (kept in lockstep by
--      tests/test_source_trust.py). Replaces four inconsistent inline orderings
--      (recompute's CASE, condition-scoring's accidental ASC id tiebreak,
--      best_street's source='sreality' boolean, mig-300's ORDER BY sreality_id).
--   2. The sign<->source invariant, enforced at last. sreality rows carry a real
--      positive id; every other portal a negative synthetic one. Verified live
--      2026-07-19: 0 violations across 555k rows. The CHECK is written in the
--      forward-compatible form (permits a future NULL sreality_id for a
--      non-sreality row) so a later phase need not rewrite it.
--   3. Two view redefines that route their representative-listing / media-handle
--      pick through source_trust_rank instead of an id-sign accident.
-- Worker-safe: the CHECK is added NOT VALID then VALIDATE'd (SHARE UPDATE
-- EXCLUSIVE, never blocks the always-on writer); a short lock_timeout keeps the
-- brief catalog lock from parking at the head of listings' lock queue.

SET lock_timeout = '5s';

-- 1. Shared trust order ------------------------------------------------------
-- Single-statement IMMUTABLE SQL => the planner inlines it (folds to the CASE in
-- any ORDER BY, no per-row call overhead). NOT marked STRICT: a NULL/unknown
-- source must fall through to the ELSE rank, not return NULL.
CREATE OR REPLACE FUNCTION source_trust_rank(p_source text)
RETURNS smallint
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE p_source
        WHEN 'sreality'     THEN 1
        WHEN 'bezrealitky'  THEN 2
        WHEN 'idnes'        THEN 3
        WHEN 'mmreality'    THEN 4
        WHEN 'remax'        THEN 5
        WHEN 'maxima'       THEN 6
        WHEN 'ceskereality' THEN 7
        WHEN 'realitymix'   THEN 8
        WHEN 'bazos'        THEN 9
        ELSE 10
    END::smallint
$$;

COMMENT ON FUNCTION source_trust_rank(text) IS
    'Per-portal trust order (lower = more trusted). Single source of truth for '
    'representative-sibling selection; mirror of toolkit/source_trust.py. '
    'Non-sensitive static logic — deliberately callable by all roles so an '
    'anon-exposed view may reference it.';

-- Non-sensitive; leave it callable by every role (a future *_public view may
-- reference it). Explicit rather than relying on the project default ACL.
GRANT EXECUTE ON FUNCTION source_trust_rank(text) TO anon, authenticated, service_role;

-- 2. Sign <-> source invariant ----------------------------------------------
ALTER TABLE listings
    ADD CONSTRAINT listings_sreality_id_sign_check
    CHECK (
        CASE WHEN source = 'sreality'
             THEN sreality_id IS NOT NULL AND sreality_id > 0
             ELSE sreality_id IS NULL OR sreality_id < 0
        END
    ) NOT VALID;

ALTER TABLE listings VALIDATE CONSTRAINT listings_sreality_id_sign_check;

-- 3a. dedup_label_events: pick the survivor-side media handle by trust order,
--     not ORDER BY sreality_id (which, on a negative synthetic id, picked an
--     arbitrary non-sreality row — an id-sign accident frozen into golden sets).
--     Reproduced verbatim from migration 300 with only the two ORDER BY picks
--     changed (generated, not hand-edited).

CREATE OR REPLACE VIEW dedup_label_events AS
WITH operator_merge AS (
    SELECT
        'merge:' || pme.id AS label_id,
        pme.survivor_property_id AS left_property_id,
        pme.retired_property_id AS right_property_id,
        (SELECT l.sreality_id FROM listings l
         WHERE l.property_id = pme.survivor_property_id
           AND l.sreality_id <> pme.listing_id
           AND NOT EXISTS (
               SELECT 1 FROM property_merge_events pme2
               WHERE pme2.listing_id = l.sreality_id AND pme2.created_at > pme.created_at)
         ORDER BY source_trust_rank(l.source), l.is_active DESC,
                  l.last_seen_at DESC NULLS LAST, l.sreality_id DESC LIMIT 1) AS left_listing_id,
        pme.listing_id AS right_listing_id,
        true AS is_same,
        'operator_merge'::text AS label_source,
        pr.category_main,
        NULL::text AS tier,
        pme.created_at AS labeled_at,
        pme.reason
    FROM property_merge_events pme
    JOIN properties pr ON pr.id = pme.survivor_property_id
    WHERE pme.source = 'operator' AND pme.undone_at IS NULL
),
operator_dismissal AS (
    SELECT
        'dismiss:' || pic.id AS label_id,
        pic.left_property_id, pic.right_property_id,
        pl.repr_listing_id AS left_listing_id,
        pr.repr_listing_id AS right_listing_id,
        false AS is_same,
        'operator_dismissal'::text AS label_source,
        pl.category_main,
        pic.tier,
        pic.reviewed_at AS labeled_at,
        coalesce(pic.markers_matched ->> 'reason', '') AS reason
    FROM property_identity_candidates pic
    JOIN properties pl ON pl.id = pic.left_property_id
    JOIN properties pr ON pr.id = pic.right_property_id
    WHERE pic.reviewed_action = 'operator' AND pic.status = 'dismissed'
      AND pl.status = 'active' AND pr.status = 'active'
),
operator_unmerge AS (
    SELECT
        'unmerge:' || pme.id AS label_id,
        pme.survivor_property_id AS left_property_id,
        pme.retired_property_id AS right_property_id,
        (SELECT l.sreality_id FROM listings l
         WHERE l.property_id = pme.survivor_property_id
           AND l.sreality_id <> pme.listing_id
           AND NOT EXISTS (
               SELECT 1 FROM property_merge_events pme3
               WHERE pme3.listing_id = l.sreality_id AND pme3.created_at > pme.created_at)
         ORDER BY source_trust_rank(l.source), l.is_active DESC,
                  l.last_seen_at DESC NULLS LAST, l.sreality_id DESC LIMIT 1) AS left_listing_id,
        pme.listing_id AS right_listing_id,
        false AS is_same,
        'operator_unmerge'::text AS label_source,
        pr.category_main,
        NULL::text AS tier,
        pme.undone_at AS labeled_at,
        'unmerge'::text AS reason
    FROM property_merge_events pme
    JOIN properties pr ON pr.id = pme.retired_property_id
    WHERE pme.undone_by = 'operator'
      -- Exclude the subset later RE-merged back onto the same survivor: that outcome
      -- contradicts the "these are different" label (the engine/operator changed its mind
      -- again), so it is not usable ground truth (~30/62 groups per the 2026-07-12 inventory).
      AND NOT EXISTS (
          SELECT 1 FROM listings l2
          WHERE l2.sreality_id = pme.listing_id AND l2.property_id = pme.survivor_property_id)
),
decision_feedback AS (
    SELECT
        'feedback:' || ddf.id AS label_id,
        ddf.left_property_id, ddf.right_property_id,
        pl.repr_listing_id AS left_listing_id,
        pr.repr_listing_id AS right_listing_id,
        (ddf.expected_outcome = 'should_merge') AS is_same,
        'decision_feedback'::text AS label_source,
        ddf.category_main,
        NULL::text AS tier,
        ddf.created_at AS labeled_at,
        coalesce(ddf.expected_outcome, '') AS reason
    FROM dedup_decision_feedback ddf
    JOIN properties pl ON pl.id = ddf.left_property_id
    JOIN properties pr ON pr.id = ddf.right_property_id
    WHERE ddf.is_incorrect = true
      AND ddf.expected_outcome IN ('should_merge', 'should_dismiss')
),
engine_site_plan_verdict AS (
    SELECT
        'siteplan:' || pic.id AS label_id,
        pic.left_property_id, pic.right_property_id,
        pl.repr_listing_id AS left_listing_id,
        pr.repr_listing_id AS right_listing_id,
        false AS is_same,
        'engine_site_plan_verdict'::text AS label_source,
        pl.category_main,
        pic.tier,
        coalesce(pic.reviewed_at, pic.created_at) AS labeled_at,
        'site_plan_different_unit'::text AS reason
    FROM property_identity_candidates pic
    JOIN properties pl ON pl.id = pic.left_property_id
    JOIN properties pr ON pr.id = pic.right_property_id
    WHERE pic.markers_matched ->> 'reason' = 'site_plan_different_unit'
      AND pic.status IN ('dismissed', 'proposed')
      -- operator_dismissal already claims the operator-reviewed subset of this same reason;
      -- avoid double-counting one candidate row under two label_source values.
      AND (pic.reviewed_action IS NULL OR pic.reviewed_action <> 'operator')
      AND pl.status = 'active' AND pr.status = 'active'
)
SELECT * FROM operator_merge
UNION ALL SELECT * FROM operator_dismissal
UNION ALL SELECT * FROM operator_unmerge
UNION ALL SELECT * FROM decision_feedback
UNION ALL SELECT * FROM engine_site_plan_verdict;

-- 3b. property_estimates_public: also credit estimations that resolved by URL
--     only (input_sreality_id NULL — created before the listing was scraped).
--     The mig-173 INNER JOIN on input_sreality_id silently dropped them, so the
--     Browse `with_estimates` filter under-counted. UNION ALL of two disjoint
--     arms (partitioned by input_sreality_id IS NULL) — planner-friendly and no
--     double count. Columns unchanged (property_id, run_count, last_run_at) so
--     CREATE OR REPLACE is legal.
CREATE OR REPLACE VIEW property_estimates_public AS
WITH matched AS (
    SELECT l.property_id, er.created_at
    FROM estimation_runs er
    JOIN listings l ON l.sreality_id = er.input_sreality_id
    WHERE er.status = 'success'
      AND er.input_sreality_id IS NOT NULL
      AND l.property_id IS NOT NULL
    UNION ALL
    SELECT l.property_id, er.created_at
    FROM estimation_runs er
    JOIN listings l ON l.source_url = er.input_url
    WHERE er.status = 'success'
      AND er.input_sreality_id IS NULL
      AND l.property_id IS NOT NULL
)
SELECT property_id,
       count(*)::int      AS run_count,
       max(created_at)    AS last_run_at
FROM matched
GROUP BY property_id;

GRANT SELECT ON property_estimates_public TO anon;
