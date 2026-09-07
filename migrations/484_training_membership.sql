-- 484: image_tag_labels.in_training — membership is STORED, not computed.
--
-- The operator's correction (2026-09-07): "you were supposed to keep the
-- reserve out of the training positive. I do not want to review 800 or a
-- thousand images if I do not need to. I asked you to keep whatever was in the
-- training positive set and I would move images from reserve to the training
-- positive set as I would deem necessary."
--
-- Two earlier models both got this wrong. Migration 474's `training_target`
-- computed membership from a rank, so the boundary moved on its own whenever a
-- label changed and the operator's reviewed set was never stable. Removing the
-- cutoff outright (PR #1321) then dumped every reserve positive into the
-- training set, which is the same failure from the other side: it made a
-- reviewed 300 into an unreviewed 836.
--
-- So membership is a fact about a row, written only by a person:
--   in_training = true   -> the head trains on this label
--   in_training = false  -> reserve; a candidate the operator has not admitted
-- A machine write can propose a label but never admits it: every machine cell
-- lands false. The operator moves rows in and out, and nothing else does.
--
-- BACKFILL. What was in the training set at this moment must stay in it.
--
-- The pre-#1321 rank was `(source = 'machine') ASC, created_at ASC, image_id
-- ASC` — the operator's labels first, then the machine's oldest-first. Its own
-- comment claimed the cutoff "never wobbles between two reads", and that was
-- true; it wobbled across WRITES. Confirming one reserve image made it 'human',
-- which moved it from rank ~400 to rank ~1 and evicted whatever sat at rank 300
-- — a photo the operator had already reviewed and accepted, silently returned
-- to the reserve. (That is why the in-set count did not move when a reserve
-- image was marked "applies": one in, one out.) So replaying that rank today
-- reproduces the drift rather than the reviewed set.
--
-- The reconstruction therefore drops the source split and admits, per head,
-- the union of:
--   1. the oldest `target` positives by created_at — stable, because a later
--      re-label cannot change when a row was created; and
--   2. every positive the operator labeled themself, wherever it ranks —
--      under the old expression those floated to the front, so they were in
--      the set, and the operator promoted them on purpose.
-- Everything past that stays in the reserve, which is the point: a bounded
-- review, not a thousand photos.
--
-- Negatives are admitted wholesale: nobody reviews ten thousand negatives, and
-- a wrong negative among them is noise where a wrong positive among 300 is a
-- third of a percent of label error.

begin;

alter table image_tag_labels
  add column if not exists in_training boolean not null default false;

comment on column image_tag_labels.in_training is
  'Does the head train on this label? Written by the operator only: a machine '
  'write always lands false (reserve). Replaces migration 474''s computed '
  'training_target, so a reviewed set stays exactly as it was reviewed.';

-- 1. The oldest `target` positives per head — the stable half of the old set.
with ranked as (
  select l.image_id, l.tag_id,
         row_number() over (
           partition by l.tag_id
           order by l.created_at asc, l.image_id asc
         ) as set_rank
  from image_tag_labels l
  where l.state = 'positive'
    and l.source in ('machine', 'human', 'human_confirmed')
    and not exists (
      select 1 from tag_exam_members hx
      join tag_exam_cohorts hc on hc.id = hx.cohort_id and hc.purpose = 'holdout'
      where hx.image_id = l.image_id
    )
),
targets as (
  select id as tag_id, coalesce(training_target, 300) as target from tag_taxonomy
)
update image_tag_labels l
   set in_training = true
  from ranked r
  join targets t on t.tag_id = r.tag_id
 where l.image_id = r.image_id and l.tag_id = r.tag_id
   and r.set_rank <= t.target;

-- 2. Every positive the operator labeled themself, wherever it ranks: the old
--    expression floated these to the front, so they were in the set.
update image_tag_labels l
   set in_training = true
 where l.state = 'positive'
   and l.source in ('human', 'human_confirmed')
   and not l.in_training
   and not exists (
     select 1 from tag_exam_members hx
     join tag_exam_cohorts hc on hc.id = hx.cohort_id and hc.purpose = 'holdout'
     where hx.image_id = l.image_id
   );

-- Negatives and left-outs: negatives all train, left-outs never do.
update image_tag_labels set in_training = true
 where state = 'negative'
   and source in ('machine', 'human', 'human_confirmed');

-- The reserve is queried per head constantly; the partial index is the read.
create index if not exists image_tag_labels_reserve
  on image_tag_labels (tag_id, updated_at desc, image_id desc)
  where state = 'positive' and not in_training;

commit;
