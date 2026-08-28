-- 459: the vision screener's output, and the call type that produces it.
--
-- WHY PERSIST THE SCREEN AT ALL. The stratified half of the exam over-samples the
-- screener's hits, and a sample is only weightable if the size of each stratum is
-- known: p = (drawn from stratum) / (size of stratum). Computing stratum sizes
-- in-memory during one long run would mean a 4,000-image pass that dies at 3,900
-- loses everything, and — worse — that nobody can audit afterwards WHY a given
-- image carried the probability it did. The screen is evidence, so it is stored.
--
-- WHY `error` IS A COLUMN AND NOT A DROPPED ROW. A truncated or failed response
-- looks exactly like "the screener saw nothing here". If those were binned into
-- the screen_none stratum, that stratum would fill with model failures instead of
-- genuine negatives and the recall estimate would inherit them. Errors are
-- recorded, counted, and made INELIGIBLE for the draw; a run whose error rate is
-- material fails loudly rather than quietly drawing around it.
--
-- The CHECK below is RESTATED IN FULL, not appended to. `llm_calls_called_for_check`
-- is a drop-and-add, so any value omitted here is silently removed from the vocabulary.
-- The live list (last set by migration 234) holds 15 values, four of which the
-- Python `CalledFor` literal does not carry; all are preserved verbatim.

begin;

create table tag_exam_screens (
  cohort_id    bigint not null references tag_exam_cohorts (id) on delete cascade,
  image_id     bigint not null references images (id) on delete cascade,
  guess_tag_ids bigint[] not null default '{}'::bigint[],
  model        text   not null,
  error        text,
  screened_at  timestamptz not null default now(),
  primary key (cohort_id, image_id)
);

create index tag_exam_screens_cohort_idx on tag_exam_screens (cohort_id);

comment on table tag_exam_screens is
  'What the vision screener guessed for each image considered for the stratified '
  'frame. Kept because a weighted statistic is only auditable if the stratum sizes '
  'it divided by are.';
comment on column tag_exam_screens.guess_tag_ids is
  'Routing tags the screener thought might apply. Empty array = it saw none of '
  'them, which is a real stratum and must keep a non-zero draw probability.';
comment on column tag_exam_screens.error is
  'Non-null when the call failed or was truncated. Such a row is NOT a negative — '
  'it is an absence of evidence — and is ineligible for the draw.';

alter table tag_exam_screens enable row level security;
revoke all on tag_exam_screens from anon, authenticated;

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
    'screen_exam_image'
  ));

commit;
