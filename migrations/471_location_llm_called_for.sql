-- 470: two `called_for` values for the bazos free-text location lane (W2-10).
--
-- `extract_location_claims` is the production lane (`location_data.claims_llm`);
-- `location_llm_bakeoff` is the read-only three-model comparison
-- (`scripts/location_llm_bakeoff.py`). They are SEPARATE on purpose, per 468's own
-- rationale: each pass prices itself from the MEASURED average cost of its own call
-- type, and `llm_burn_rate`'s starvation arm (attempts>0, successes==0, spend==0) is
-- evaluated PER called_for — so a failed bake-off must not red the production lane's
-- arm, or vice versa.
--
-- Ship this BEFORE the code that calls the model: `LLMClient._record_call` on the
-- SUCCESS path is not wrapped in try/except, so a CHECK violation raises AFTER the
-- provider call has already been billed.
--
-- The list is restated whole because a check constraint cannot be extended in
-- place; nothing is removed.

begin;

alter table llm_calls drop constraint if exists llm_calls_called_for_check;
alter table llm_calls add constraint llm_calls_called_for_check
  check (called_for in (
    'parse_url',
    'summarize_listing',
    'compare_listing_images',
    'agent_estimation',
    'extract_building_units',
    'read_floor_plan',
    'refine_skill',
    'discover_condition_markers',
    'score_listing_condition',
    'summarize_region_dispositions',
    'enrich_listing_description',
    'classify_listing_images',
    'compare_listings_visually',
    'compare_listing_site_plans',
    'compare_listing_floor_plans',
    'screen_exam_image',
    'suggest_exam_answer',
    'review_exam_image',
    'label_image_bulk',
    'extract_location_claims',
    'location_llm_bakeoff'
  ));

commit;
