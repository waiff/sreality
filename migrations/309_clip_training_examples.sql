-- 309_clip_training_examples.sql
-- Data-collection foundation for a future few-shot linear-probe classifier on top of
-- the frozen CLIP embeddings (image_clip_embeddings) — this migration is ONLY the
-- training-set label store the operator builds from the /phash-audit page's "Train"
-- CTA; it does not touch the CLIP model, taxonomy, or tagger, and nothing reads this
-- table yet.
--
-- One label per image (unique on image_id, upsert-on-conflict — the same mutable-upsert
-- shape as image_tag_annotations/phash_pair_notes/dedup_decision_feedback): clicking
-- Train again with a different label OVERWRITES, it doesn't accumulate duplicate rows.
-- `label` is deliberately free text, not constrained to the current CLIP taxonomy —
-- the operator can pick an existing tag or type a new one (open-vocabulary label space,
-- since refining/splitting the taxonomy is plausibly the point of training a probe).
--
-- RLS enabled + a `_public` read view (property_notes precedent) so /phash-audit can
-- batch-read existing labels with the same anon Supabase client it already uses;
-- writes go through the bearer/admin-gated API only.

create table image_training_examples (
  id            bigserial primary key,
  image_id      bigint not null references images(id) on delete cascade unique,
  label         text not null check (char_length(label) between 1 and 100),
  created_by    text not null default 'operator',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index on image_training_examples (label);

alter table image_training_examples enable row level security;

create view image_training_examples_public as
select image_id, label, updated_at
from image_training_examples;

grant select on image_training_examples_public to anon, authenticated;
