-- 458: the sealed exam — a set of images the probes are GRADED on and must never
-- be TRAINED on.
--
-- WHY A HOLDOUT AT ALL. Per-tag probes are only worth shipping if we know how often
-- they are right, and a score measured on the images a model learned from is not a
-- measurement, it is a memory test. The exam is the one population no training read
-- may touch, so its numbers mean something.
--
-- WHY IT IS A MEMBERSHIP TABLE AND NOT A LABEL STORE. An operator's exam answer IS
-- a human judgement about an (image, tag) pair — exactly what image_tag_labels
-- means — so the answers go THERE, through the existing upsert, and this table
-- records only WHICH IMAGES are in the exam. A second answer store would put the
-- same kind of fact in two places and let the grading code read something different
-- from the training code. The cost of that choice, stated plainly: every training
-- read now owes an exclusion it cannot discover from the schema. That obligation is
-- discharged by ONE imported constant (toolkit.tag_holdout.HOLDOUT_EXCLUSION) and
-- policed by tests/test_holdout_exclusion_census.py, which fails on any new
-- statement reading image_tag_labels that neither excludes nor is explicitly
-- exempted with a reason.
--
-- MEMBERSHIP IS PROTECTION; sealed_at IS ONLY COMPLETION. Every member is excluded
-- from training from the moment it is inserted, sealed or not. The alternative --
-- protecting only sealed cohorts -- leaves a window during the draw where a
-- training run could consume images that are about to become the exam, which is the
-- exact leak this table exists to prevent. `sealed_at` therefore answers "is the
-- exam finished?", never "is it protected?".
--
-- WHY THE DRAW IS TWO PHASES. The 250 is 100 drawn purely at random plus 150 drawn
-- stratified on a vision screener's guesses, and the screener cannot run before the
-- cohort exists. So a cohort accepts inserts while sealed_at IS NULL and sealing is
-- a separate, explicit act. "One-way door" means a SEALED cohort is immutable, not
-- that a cohort takes one write.
--
-- WHY inclusion_probability. A stratified draw deliberately over-samples rare tags,
-- so raw counts over the 250 are not population estimates. Each row records the odds
-- under which it was drawn, and statistics weight by 1/p (inverse-probability
-- weighting, the arithmetic a pollster uses to over-sample a small group and still
-- report a national figure). Stratify, NEVER filter: every stratum keeps a non-zero
-- probability -- including "the screener saw nothing here" -- or recall is measured
-- only over what the screener already found, and the probe is graded on easy cases.
--
-- WHY preexisting_labels. A pure-random draw over 10.4M images can land on one of
-- the ~1,440 already labelled (~0.014%, so ~0 rows in practice). Excluding them
-- would make the frame "random over UNLABELLED images", which systematically omits
-- exactly the images retrieval has already found. So overlap is allowed and the
-- prior state is frozen here, keeping the frame honest and the rewrite auditable.
--
-- Posture copied from 450_tag_candidates (RLS on, explicit revoke, NO _public view),
-- deliberately NOT from 310_image_border_cases, whose public view would publish the
-- exam roster to any anon browser.

begin;

create table tag_exam_cohorts (
  id          bigserial primary key,
  name        text        not null unique,
  frame_size  bigint      not null,
  model       text        not null,
  revision    text,
  drawn_at    timestamptz not null default now(),
  sealed_at   timestamptz,
  sealed_by   text,
  note        text
);

comment on table tag_exam_cohorts is
  'One sealed exam. sealed_at answers "is it finished?", never "is it protected?" — '
  'membership alone excludes an image from training (see 458 header).';
comment on column tag_exam_cohorts.frame_size is
  'The N the pure-random inclusion probability was computed against, recorded so a '
  'later corpus size cannot silently rewrite what p meant.';
comment on column tag_exam_cohorts.revision is
  'image_clip_embeddings.revision in force at draw time (migration 456). A grade is '
  'only comparable to vectors from the same encoder.';

create table tag_exam_members (
  cohort_id             bigint  not null references tag_exam_cohorts (id) on delete cascade,
  image_id              bigint  not null references images (id) on delete cascade,
  frame                 text    not null check (frame in ('pure_random', 'stratified')),
  stratum               text    not null,
  inclusion_probability double precision not null
                          check (inclusion_probability > 0 and inclusion_probability <= 1),
  screen_guess_tag_ids  bigint[],
  preexisting_labels    jsonb,
  position              int     not null,
  primary key (cohort_id, image_id)
);

-- An image belongs to at most ONE exam, ever. Two cohorts sharing an image would
-- make "which grade is this image allowed to inform?" unanswerable.
create unique index tag_exam_members_one_exam_per_image
  on tag_exam_members (image_id);

-- The exclusion predicate's access path. Every training read probes this table by
-- image_id, so it has to be an index lookup rather than a scan of the exam.
create index tag_exam_members_image_idx on tag_exam_members (image_id);

comment on column tag_exam_members.stratum is
  'Which bucket the draw took this row from: pure_random, screen_hit:<tag_id>, or '
  'screen_none. Kept because the weighting is only auditable if the bucket is.';
comment on column tag_exam_members.inclusion_probability is
  'Odds this image had of being drawn. Statistics over the exam weight by 1/p; a '
  'raw count over a stratified sample is not a population estimate.';
comment on column tag_exam_members.screen_guess_tag_ids is
  'What the vision screener guessed, frozen at draw. NULL for the pure-random frame, '
  'which no machine touched. Never shown to the operator — it would anchor them.';
comment on column tag_exam_members.preexisting_labels is
  'image_tag_labels state for this image at draw time, frozen. Non-null only for the '
  'rare already-labelled draw; makes the overwrite auditable rather than invisible.';

alter table tag_exam_cohorts enable row level security;
alter table tag_exam_members enable row level security;

revoke all on tag_exam_cohorts from anon, authenticated;
revoke all on tag_exam_members from anon, authenticated;
revoke all on sequence tag_exam_cohorts_id_seq from anon, authenticated;

commit;
