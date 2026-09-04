-- 468: `label_image_bulk` — the bulk machine-labeling pass gets its own
-- called_for value.
--
-- It could have reused 'review_exam_image', and that would have been a quiet
-- mistake: both passes price themselves from the MEASURED average cost of
-- their own call type, so sharing one label would let each pass's pre-flight
-- drift with the other's traffic. It is also the only per-pass spend record
-- against a hard $50 programme budget.
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
    'label_image_bulk'
  ));

commit;
