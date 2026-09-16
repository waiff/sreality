-- 527: three `called_for` values for the autonomous dedup engine's LLM judge
-- (docs/design/autodedup/PROGRAM.md — the judge tiers text / vision / gold).
--
-- These are the ONLY shared-table writes the whole program makes: everything else it
-- produces lands in schema `autodedup` (migration 528). `llm_calls` is written by
-- `LLMClient._record_call`, so the judge's spend shows up in the existing cost rollups
-- and in `llm_burn_rate` without a second accounting path.
--
-- THREE VALUES, NOT ONE, per 468/470/471's own rationale: each pass prices itself from
-- the MEASURED average cost of its own call type, and `llm_burn_rate`'s starvation arm
-- (attempts>0, successes==0, spend==0) is evaluated PER called_for — so a failed gold
-- pass must not red the cheap text-judge lane's arm, or vice versa. The gold pass is
-- also the ground-truth lane whose spend the program's $200 budget is tracked against
-- separately from the trial's own band spend.
--
-- SHIP THIS BEFORE THE CODE THAT CALLS THE MODEL: `LLMClient._record_call` on the
-- SUCCESS path is not wrapped in try/except, so a CHECK violation raises AFTER the
-- provider call has already been billed.
--
-- The list is restated whole because a check constraint cannot be extended in place;
-- nothing is removed. Value list copied verbatim from migration 471 (the highest-
-- numbered file that restates `llm_calls_called_for_check`) with three values appended.
--
-- DIFF AGAINST THE CATALOG, NOT AGAINST FILE N-1. A whole-list restatement REVOKES any
-- value that is live but missing from the newest repo file, and this repo carries ad-hoc
-- migrations with no file at all (scripts/migration_objects.py's own docstring). So
-- `outreach_draft` is unioned in here: `api/outreach.py` (`_CALLED_FOR`, mounted at
-- api/main.py) INSERTs it into `llm_calls`, and NO migration in the tree ever added it to
-- the constraint -- either production carries it out-of-band (and 471's list would drop
-- it) or that lane has been raising a CHECK violation AFTER the provider bills. Adding it
-- is a union either way: it can only widen. Before `dry_run=false confirm=APPLY`, run
--   select pg_get_constraintdef(oid) from pg_constraint
--    where conrelid = 'llm_calls'::regclass and conname = 'llm_calls_called_for_check';
--   select called_for, count(*) from llm_calls group by 1 order by 2 desc;
-- and union anything live that is missing below -- ADD CONSTRAINT validates every
-- existing row, so a value in the table but not in the list fails the whole apply.
--
-- LOCK. `alter table ... add constraint ... check` takes ACCESS EXCLUSIVE on `llm_calls`
-- (a continuously-INSERTed hot table, ~293k rows) and full-scans it to validate. Without
-- a lock_timeout psql would wait forever with every incoming insert queued behind it, and
-- apply_migration.yml's 30x retry arm -- which only fires on "canceling statement due to
-- lock timeout" -- would be dead code. Plain SET, never SET LOCAL: the apply path is
-- statement-autocommit. The begin/commit block stays: the DROP+ADD pair must be atomic or
-- writers briefly see the table with neither constraint.

set lock_timeout = '5s';

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
    'location_llm_bakeoff',
    'outreach_draft',
    'autodedup_judge_text',
    'autodedup_judge_vision',
    'autodedup_judge_gold'
  ));

commit;
