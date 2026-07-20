-- 308_clip_phash_audit_tooling.sql
-- Foundation for two new operator audit pages: CLIP tagging/render-diagnostics audit
-- (/clip-audit) and pHash-threshold audit (/phash-audit). Three additive pieces:
--
-- 1. images_public gains `phash` — a display value (the 64-bit dHash), not sensitive
--    (unlike the embeddings/tags tables locked down in migration 237), so it's fine on
--    the SAME anon boundary the other per-image columns already use. CREATE OR REPLACE
--    only appends a trailing column — existing columns + order unchanged, the view's
--    anon SELECT grant covers it (same posture as migrations 236/239).
--
-- 2. image_tag_annotations — one mutable row per image, the operator's "this CLIP tag/
--    render score is wrong" flag + free-text note, keyed on image_id. Mirrors
--    dedup_decision_feedback's upsert-on-conflict shape (a mutable correction, not an
--    append-only log) at IMAGE grain instead of property-pair grain.
--
-- 3. phash_pair_notes — one mutable row per (ordered) image pair, the operator's note
--    from the pHash audit page. Same upsert shape, at image-PAIR grain.
--
-- Both new tables get RLS enabled + a `_public` read view (property_notes precedent) so
-- the audit pages can batch-read notes with the same anon Supabase client they already
-- use for everything else on the page; writes go through bearer/admin-gated API endpoints
-- only (api/routes/dedup.py), never PostgREST directly.

-- phash is APPENDED last (not inserted after storage_path) — CREATE OR REPLACE VIEW only
-- allows adding a trailing column; Postgres reads a mid-list insert as renaming every
-- column after it (confirmed live: 42P16 "cannot change name of view column").
create or replace view images_public as
select
  i.id,
  i.sreality_id,
  i.sequence,
  i.sreality_url,
  i.storage_path,
  ct.fine_tag      as clip_fine_tag,
  ct.logical_tag   as clip_logical_tag,
  ct.confidence    as clip_confidence,
  ct.render_score  as clip_render_score,
  i.phash
from images i
left join lateral (
  select t.fine_tag, t.logical_tag, t.confidence, t.render_score
  from image_clip_tags t
  where t.image_id = i.id
  order by t.tagged_at desc
  limit 1
) ct on true;

create table image_tag_annotations (
  id             bigserial primary key,
  image_id       bigint not null references images(id) on delete cascade,
  tag_flagged    boolean not null default false,
  render_flagged boolean not null default false,
  note           text check (char_length(note) <= 2000),
  created_by     text not null default 'operator',
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (image_id)
);

alter table image_tag_annotations enable row level security;

create view image_tag_annotations_public as
select image_id, tag_flagged, render_flagged, note, updated_at
from image_tag_annotations;

grant select on image_tag_annotations_public to anon, authenticated;

create table phash_pair_notes (
  id            bigserial primary key,
  image_id_a    bigint not null references images(id) on delete cascade,
  image_id_b    bigint not null references images(id) on delete cascade,
  note          text check (char_length(note) <= 2000),
  created_by    text not null default 'operator',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  check (image_id_a < image_id_b),
  unique (image_id_a, image_id_b)
);

alter table phash_pair_notes enable row level security;

create view phash_pair_notes_public as
select image_id_a, image_id_b, note, updated_at
from phash_pair_notes;

grant select on phash_pair_notes_public to anon, authenticated;
