-- CLIP image tags (dedup v2 Phase 2): the self-hosted zero-shot tagger's output,
-- one row per (image, model). fine_tag = the winning CLIP anchor (e.g.
-- 'cadastral_map'); logical_tag = collapsed to the engine's label space
-- (toolkit.image_classification.ROOM_TYPES, e.g. 'site_plan'). A FREE,
-- full-inventory replacement for the paid room classifier on the coarse,
-- dedup-relevant distinctions, AND the first tagger for the non-apartment
-- categories (dum/pozemek/komercni) which have zero classified images today.
-- Backend-only (the dedup engine reads it); no anon grant.

create table if not exists image_clip_tags (
  image_id     bigint not null references images(id) on delete cascade,
  model        text   not null,
  fine_tag     text   not null,
  logical_tag  text   not null,
  confidence   real,
  tagged_at    timestamptz not null default now(),
  primary key (image_id, model)
);

-- The engine pairs like-for-like images by logical tag, so that's the lookup.
create index if not exists image_clip_tags_logical_idx
  on image_clip_tags (logical_tag);
