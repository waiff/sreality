-- 442_tag_annotation_matrix.sql
--
-- Tag annotation matrix — Wave A (additive only; see docs/design/tag-annotation-matrix.md).
--
-- Two permanent tables replace the training-set half of migration 309
-- (image_training_examples, one mutable free-text label per image) with a durable
-- per-(image, tag) tri-state fact: positive | negative | excluded. Every independent
-- per-tag classifier head trains from image_tag_labels; absence of a row is the only
-- "untouched" state.
--
-- tag_taxonomy promotes dedup_sim.taxonomy_labels out of the `dedup_sim` schema, which
-- docs/design/new-dedup/PROGRAM.md plans to drop wholesale at Wave 8 — a permanent
-- annotation matrix cannot hang off a table that disappears then. A real surrogate key
-- also replaces the current text-keyed join (renaming a tag today rewrites every
-- dependent row via toolkit/dedup_sim_labeling.py's cascade; tomorrow it is one UPDATE).
--
-- This migration is purely additive: it creates both tables and backfills them from
-- image_training_examples + dedup_sim.taxonomy_labels. Nothing is dropped. ClipAudit,
-- the old table, and dedup_sim.taxonomy_labels stay live and unaffected until Wave B
-- (frontend cutover) and Wave C (destructive drop, separately gated).

begin;

------------------------------------------------------------------
-- tag_taxonomy
------------------------------------------------------------------

create table tag_taxonomy (
  id          bigserial primary key,
  label       text not null unique check (char_length(label) between 1 and 100),
  family      text,
  active      boolean not null default true,
  created_at  timestamptz not null default now(),
  created_by  text not null default 'operator'
);

create index on tag_taxonomy (active);

alter table tag_taxonomy enable row level security;

comment on table tag_taxonomy is
  'Permanent home for the operator-curated tag vocabulary, promoted from '
  'dedup_sim.taxonomy_labels (migration 373) so it survives the dedup_sim schema''s '
  'planned Wave-8 drop. Backend-only — every read/write goes through the admin-gated '
  'FastAPI service, matching migration 373''s precedent, not migration 309''s public view.';

insert into tag_taxonomy (label, family, active, created_at, created_by)
select label, family, active, created_at, 'backfill:dedup_sim.taxonomy_labels'
from dedup_sim.taxonomy_labels
order by id;

------------------------------------------------------------------
-- image_tag_labels
------------------------------------------------------------------

create table image_tag_labels (
  image_id    bigint not null references images (id) on delete cascade,
  tag_id      bigint not null references tag_taxonomy (id) on delete cascade,
  state       text not null check (state in ('positive', 'negative', 'excluded')),
  created_by  text not null default 'operator',
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  primary key (image_id, tag_id)
);

create index on image_tag_labels (tag_id, state);

alter table image_tag_labels enable row level security;

comment on table image_tag_labels is
  'One row per explicit (image, tag) decision — positive, negative, or excluded. No row '
  'means untouched: displays and trains as negative once the image is in '
  'dedup_sim.labeling_sample. excluded rows are dropped at training time by definition, '
  'not by a separate flag. Backend-only, admin-gated, replaces image_training_examples '
  '(migration 309) as the ground truth every per-tag classifier head trains from.';

-- Backfill: each image_training_examples row becomes one positive + N-1 negatives
-- across every other active tag, per the literal migration spec (an image hand-labeled
-- T today is known, by the same human act, not to be every other active tag).
insert into image_tag_labels (image_id, tag_id, state, created_by)
select ite.image_id, tt.id, 'positive', 'backfill:image_training_examples'
from image_training_examples ite
join tag_taxonomy tt on tt.label = ite.label and tt.active
on conflict (image_id, tag_id) do nothing;

insert into image_tag_labels (image_id, tag_id, state, created_by)
select ite.image_id, tt.id, 'negative', 'backfill:image_training_examples'
from image_training_examples ite
cross join tag_taxonomy tt
where tt.active
  and tt.label <> ite.label
on conflict (image_id, tag_id) do nothing;

commit;
