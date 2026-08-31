-- 464: the exam instrument decouples from the holdout role, and the label store
-- learns the difference between a draft and a decision.
--
-- THE OPERATOR'S RULING (2026-08-31). The pre-exam labels — 1,522 cells, mostly
-- positives, made quickly and without guidelines — are NOT the training set;
-- they are drafts. The trusted labeling instrument is the exam UI (whole-image
-- answers, definitions on hover, negatives written for every untouched tag), so
-- the operator's marked images will be RE-labeled through it. That makes two
-- kinds of exam cohort:
--
--   * purpose='holdout'  — today's contract, unchanged: random/stratified draws,
--     inverse-probability weights, EXCLUDED from every training read. exam_v1
--     and its 84 careful answers stay exactly this.
--   * purpose='curated'  — operator-marked images seated for careful re-labeling
--     through the same UI. NOT excluded from training: their answers ARE the
--     gold seed that teaches the machine labeler. frame='curated', probability 1
--     (drawn with certainty from a curated list — never weighted into
--     population statistics).
--
-- The one-exam-per-image rule (458's unique index) now works FOR the split: an
-- image seated in a curated cohort can never later be drawn into a holdout, so
-- nothing the machine learned from can ever grade it.
--
-- DRAFTS. Existing 'human' labels on images NOT in a holdout cohort demote to
-- source='human_draft'. Every truth-reader filters source IN
-- ('human','human_confirmed'), so drafts vanish from training, grading, draws
-- and statistics in one stroke — yet they remain queryable as the seed lists
-- for the curated draw, and re-answering an image in the exam overwrites its
-- drafts with 'human' (drafts never win an upsert). The demotion is EVENTED:
-- image_tag_labels carries an update trigger into image_tag_label_events, so
-- each demoted cell gets a real event row (state unchanged, source
-- human_draft) — which is why the events table's own source CHECK is restated
-- below BEFORE the UPDATE runs. The first apply attempt proved the ordering:
-- the trigger fired into the un-widened check and the whole transaction
-- rolled back.
--
-- QUEUES. The 2,282 open tag_candidates are cleared on the operator's explicit
-- order (clean slate; the draw lanes refill any tag in minutes).
--
-- BACKUPS (destructive-change policy): full copies of both touched tables are
-- kept in-database as backup_464_*; they are listed for the follow-up cleanup
-- drop once the operator confirms the new state.

begin;

create table backup_464_image_tag_labels as select * from image_tag_labels;
create table backup_464_tag_candidates as select * from tag_candidates;
alter table backup_464_image_tag_labels enable row level security;
alter table backup_464_tag_candidates enable row level security;
revoke all on backup_464_image_tag_labels from anon, authenticated;
revoke all on backup_464_tag_candidates from anon, authenticated;

alter table tag_exam_cohorts
  add column purpose text not null default 'holdout'
    check (purpose in ('holdout', 'curated'));

comment on column tag_exam_cohorts.purpose is
  'holdout = graded measurement, excluded from training (the original contract). '
  'curated = operator-marked images re-labeled carefully through the exam UI; '
  'their answers feed training and are excluded from population-weighted '
  'statistics instead.';

alter table tag_exam_members drop constraint tag_exam_members_frame_check;
alter table tag_exam_members add constraint tag_exam_members_frame_check
  check (frame in ('pure_random', 'stratified', 'curated'));

alter table image_tag_labels drop constraint image_tag_labels_source_check;
alter table image_tag_labels add constraint image_tag_labels_source_check
  check (source in ('human', 'human_confirmed', 'human_draft', 'machine', 'backfill_442'));

alter table image_tag_label_events drop constraint image_tag_label_events_source_check;
alter table image_tag_label_events add constraint image_tag_label_events_source_check
  check (source in ('human', 'human_confirmed', 'human_draft', 'machine', 'backfill_442'));

-- The demotion boundary, verified against live data before writing this file:
-- zero members carry pre-exam labels (preexisting_labels IS NULL on all 250),
-- so "on a holdout member" selects exactly the 672 exam-answer cells and
-- nothing else. 1,522 rows demote.
update image_tag_labels l
   set source = 'human_draft'
 where l.source = 'human'
   and not exists (
         select 1 from tag_exam_members m
         join tag_exam_cohorts c on c.id = m.cohort_id and c.purpose = 'holdout'
         where m.image_id = l.image_id
       );

delete from tag_candidates;

commit;
