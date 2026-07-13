-- 300: dedup golden-set foundation (Session 2 of the LLM-cost/dedup-quality program).
--
-- One canonical, always-current label source (`dedup_label_events`) for every future dedup
-- precision replay, unioning the human (+ one forensic-vision) label streams that already exist
-- in the DB. Design: docs/design/dedup-vision-and-backlog-overhaul.md §4.
--
-- Sources unioned (each carries a distinct label_source so a benchmark can include/exclude any
-- stratum):
--   operator_merge          property_merge_events, source='operator', not undone            (+)
--   operator_dismissal      property_identity_candidates, reviewed_action='operator',
--                           status='dismissed'                                              (-)
--   operator_unmerge        property_merge_events, undone_by='operator', EXCLUDING the subset
--                           where the listing sits back on the original survivor today (a later
--                           re-merge contradicts the negative label)                          (-)
--   decision_feedback       dedup_decision_feedback, is_incorrect=true                    (+/-)
--   engine_site_plan_verdict property_identity_candidates whose forensic site-plan compare
--                           returned 'different_unit' (Sonnet read the parcel/unit label off
--                           the actual drawing) — NOT circular for benchmarking any FREE
--                           (pHash/cosine) signal, since the label comes from vision reading
--                           text, not from the free signal itself. New stratum identified
--                           2026-07-13: this population is what disproved the naive "count
--                           non-drawing images" free-signal fix this session (see PR body) —
--                           it is exactly the counter-evidence any future free-signal proposal
--                           on this guard must replay against.                                (-)
--
-- Listing-pair reconstruction (the media handle for vision benchmarks) is drift-safe: a merge
-- event's `listing_id` (the retired side) is immutable, but `properties.repr_listing_id` is a
-- recomputed pointer that can drift after a LATER merge lands more listings on the same survivor
-- (the exact mechanism behind the dedup_pair_audit self-paired-row bug documented in
-- docs/design/dedup-geo-town-pin-false-merge.md). So the survivor-side listing is picked as: any
-- listing currently on the survivor property that was NOT itself attached there by a merge event
-- AFTER this label's event — i.e. provably already present at label time. ~91.6% of positive
-- events reconstruct this way (515/562 on 2026-07-13); the remainder (a survivor whose only
-- other listings all arrived later) yield left_listing_id NULL and are still valid property-pair
-- labels, just without a listing-level media handle.
--
-- Supporting index: `dedup_label_events` and the operator_merge/operator_unmerge branches probe
-- "was this listing attached to the survivor by a LATER merge" once per candidate listing; without
-- an index on (listing_id, created_at), each check seq-scans property_merge_events (verified via
-- EXPLAIN: ~1.2s for 5 rows pre-index). Justified new index, not a new dependency (rule #7 n/a).
CREATE INDEX IF NOT EXISTS property_merge_events_listing_created_idx
    ON property_merge_events (listing_id, created_at);

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
         ORDER BY l.sreality_id LIMIT 1) AS left_listing_id,
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
         ORDER BY l.sreality_id LIMIT 1) AS left_listing_id,
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

COMMENT ON VIEW dedup_label_events IS
    'Always-current union of human (+ one forensic-vision) dedup labels. Benchmarks should NOT '
    'read this directly (it recomputes on every query and its labels grow every day) — freeze a '
    'named snapshot into dedup_golden_sets instead. See docs/design/dedup-vision-and-backlog-overhaul.md §4.';

-- A frozen, append-only snapshot of dedup_label_events (+ optionally the stale dedup_golden_pairs
-- strata, explicitly flagged) for one bake-off. Benchmarks read a named set here, never the live
-- view, so a precision number never shifts under the same set_name after publication.
CREATE TABLE IF NOT EXISTS dedup_golden_sets (
    id bigserial PRIMARY KEY,
    set_name text NOT NULL,
    frozen_at timestamptz NOT NULL DEFAULT now(),
    -- Labels at/before this timestamp are calibration-tainted (cosine bands + the §2.2 arms were
    -- validated against the decided corpus through 2026-07-10); NULL = no floor applied to this set.
    holdout_floor timestamptz,
    label_id text NOT NULL,
    left_property_id bigint NOT NULL,
    right_property_id bigint NOT NULL,
    left_listing_id bigint,
    right_listing_id bigint,
    is_same boolean NOT NULL,
    label_source text NOT NULL,
    category_main text,
    tier text,
    labeled_at timestamptz,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (set_name, label_id)
);
CREATE INDEX IF NOT EXISTS dedup_golden_sets_set_name_idx ON dedup_golden_sets (set_name);

COMMENT ON TABLE dedup_golden_sets IS
    'Frozen, append-only golden-set snapshots materialized from dedup_label_events (one row per '
    'labeled pair per set_name). Never edit existing rows; a re-run of the same set_name for a new '
    'bake-off should use a new set_name (e.g. suffix the date) so old benchmark numbers stay reproducible.';
