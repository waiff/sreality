-- 205: the deal pipeline — property-grain, single-valued stage per property.
--
-- A "bookmark / interested" is the ENTRY stage of this pipeline, not a separate
-- flag: presence of a property_pipeline row == the property is in the pipeline.
-- Single-valued (one stage per property) is enforced by the PK on property_id,
-- which is why the pipeline can't be expressed at advert grain. Stages are a
-- TABLE (not an enum) so the operator can rename/reorder/add columns via the API
-- with no migration (the curated-index precedent). This migration is Phase 0:
-- the schema + the bookmark entry point. Stage moves (kanban) and the lossless
-- unmerge ledger come in later phases; the merge reconciler (best-effort, keep
-- most-advanced) ships alongside this in toolkit/pipeline_identity.py.

create table pipeline_stages (
  id          bigserial   primary key,
  key         text        not null,
  label       text        not null check (length(label) between 1 and 80),
  position    integer     not null,
  color       text        check (color is null or color in (
                'copper', 'sage', 'brick', 'ochre',
                'slate',  'plum', 'teal',  'sand'
              )),
  is_terminal boolean     not null default false,
  is_entry    boolean     not null default false,
  archived_at timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create unique index pipeline_stages_key_ci on pipeline_stages (lower(key));
-- at most one entry stage (the bookmark lands here)
create unique index pipeline_stages_one_entry on pipeline_stages (is_entry) where is_entry;
create index pipeline_stages_position on pipeline_stages (position) where archived_at is null;

-- THE operator deal state: at most one row per property (single-valued).
create table property_pipeline (
  property_id      bigint      primary key references properties(id) on delete cascade,
  stage_id         bigint      not null references pipeline_stages(id) on delete restrict,
  board_position   numeric     not null default 0,
  note             text        check (note is null or length(note) <= 2000),
  entered_stage_at timestamptz not null default now(),
  added_at         timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index property_pipeline_stage on property_pipeline (stage_id, board_position);

-- append-only move ledger (also the substrate for lossless unmerge in a later
-- phase). property_id carries NO cascade so history survives a property prune.
create table property_pipeline_events (
  id             bigserial   primary key,
  property_id    bigint      not null,
  from_stage_id  bigint      references pipeline_stages(id),
  to_stage_id    bigint      references pipeline_stages(id),
  reason         text        not null default 'operator'
                 check (reason in ('operator','merge_absorb','unmerge_restore','split_carry')),
  merge_group_id uuid,
  note_snapshot  text,
  created_at     timestamptz not null default now()
);
create index property_pipeline_events_property
  on property_pipeline_events (property_id, created_at desc);

alter table pipeline_stages         enable row level security;
alter table property_pipeline        enable row level security;
alter table property_pipeline_events enable row level security;

-- seed the default stages (operator-curated thereafter; this is the importer).
insert into pipeline_stages (key, label, position, color, is_entry, is_terminal) values
  ('interested', 'Zájem',        1, 'copper', true,  false),
  ('viewing',    'Prohlídka',    2, 'ochre',  false, false),
  ('offer',      'Nabídka',      3, 'teal',   false, false),
  ('won',        'Koupeno',      4, 'sage',   false, true),
  ('lost',       'Zamítnuto',    5, 'brick',  false, true);

-- anon read surface (property grain; the kanban + bookmark membership read these)
create view pipeline_stages_public as
  select id, key, label, position, color, is_terminal, is_entry
  from pipeline_stages
  where archived_at is null;

create view property_pipeline_public as
  select pp.property_id, pp.stage_id, ps.key as stage_key, ps.label as stage_label,
         ps.position as stage_position, ps.color as stage_color, ps.is_terminal,
         pp.board_position, pp.note, pp.entered_stage_at, pp.added_at, pp.updated_at
  from property_pipeline pp
  join pipeline_stages ps on ps.id = pp.stage_id;

grant select on pipeline_stages_public   to anon;
grant select on property_pipeline_public to anon;
