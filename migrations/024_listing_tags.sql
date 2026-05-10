-- 024_listing_tags.sql
-- Free-form operator tags + listing↔tag join table.
--
-- Tag colour is constrained to a fixed 8-name palette (matched in the
-- frontend by a tag-palette extension to globals.css). Anything outside
-- the palette is rejected at INSERT time, so the UI doesn't need to
-- defend against arbitrary CSS values and the visual language stays
-- coherent. Adding a new colour = a new migration that ALTERs the CHECK.
--
-- Tag names are unique case-insensitively (`hot` and `Hot` collide).
-- Rename / re-colour is out of scope in v1 — delete + recreate.

create table tags (
  id         bigserial   primary key,
  name       text        not null check (length(name) between 1 and 50),
  color      text        not null check (color in (
                'copper', 'sage', 'brick', 'ochre',
                'slate',  'plum', 'teal',  'sand'
              )),
  created_at timestamptz not null default now()
);

create unique index tags_name_ci on tags (lower(name));

create table listing_tags (
  sreality_id bigint      not null references listings(sreality_id) on delete cascade,
  tag_id      bigint      not null references tags(id)              on delete cascade,
  attached_at timestamptz not null default now(),
  primary key (sreality_id, tag_id)
);

create index listing_tags_by_tag on listing_tags (tag_id);

alter table tags         enable row level security;
alter table listing_tags enable row level security;
