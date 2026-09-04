-- 467: the definition-driven machine review of exam answers.
--
-- The exam's suggestions (461) were computed from tag NAMES alone, before a
-- single definition existed. The operator has since written all eighteen and
-- ratified one ruleset (what the photo is an image OF, in three tiers: yes /
-- no / skip). This table holds the model's answer to the exam's own question
-- WITH those definitions — one row per image, all tags at once — beside the
-- human's, so the review page can show every disagreement as a proposal the
-- operator accepts (through the exam's single /answer write path) or dismisses.
--
-- NEVER LABELS. Like 461's suggestions these rows train nothing and grade
-- nothing; the holdout census does not apply. The only way a verdict reaches
-- image_tag_labels is the operator pressing "apply".
--
-- PROVENANCE FROZEN AT CALL TIME: the asked list AND the exact definition
-- versions the model read. A row whose provenance no longer matches the
-- current set + active definitions is stale — never served, re-offered by the
-- lane — so re-dispatching after a definition edit is the whole refresh.
--
-- Dismissals reset on re-review: "I keep my answer against THAT verdict" says
-- nothing about a verdict under new wording.

begin;

create table tag_exam_machine_reviews (
  cohort_id    bigint not null references tag_exam_cohorts (id) on delete cascade,
  image_id     bigint not null references images (id) on delete cascade,
  asked_tag_ids       bigint[] not null,
  -- {"<tag_id>": <tag_definitions.version>} at call time.
  definition_versions jsonb not null,
  -- {"<tag_id>": "yes" | "no" | "skip"}; empty on error.
  verdicts     jsonb not null default '{}'::jsonb,
  model        text not null,
  error        text,
  -- Proposals the operator has looked at and kept their own answer on.
  dismissed_tag_ids bigint[] not null default '{}'::bigint[],
  reviewed_at  timestamptz not null default now(),
  primary key (cohort_id, image_id)
);

comment on table tag_exam_machine_reviews is
  'Machine verdicts on exam images against the ACTIVE tag definitions, stored '
  'beside the human answers as review-page proposals. Never labels, never '
  'training data; provenance (asked list + definition versions) frozen per row.';

alter table tag_exam_machine_reviews enable row level security;
revoke all on tag_exam_machine_reviews from anon, authenticated;

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
    'review_exam_image'
  ));

commit;
