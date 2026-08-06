-- 373_new_dedup_labeling.sql
--
-- NEW DEDUP Wave 1 — labeling-program schema. Design: docs/design/new-dedup/
-- PROGRAM.md, W1: "Labeling page = ClipAudit clone minus dedup block, plus:
-- 'new tag vs original tag' toggle, sample management, tag add/rename/remove
-- + batch tooling. Secondary CLIP (stronger encoder) relabels growing samples
-- over taxonomy v1 into a sim-side proposal store (never image_clip_tags —
-- gallery-flip hazard)."
--
-- Three tables, all in the droppable `dedup_sim` schema (migration 372):
--
-- * taxonomy_labels — the operator-curated Taxonomy v1 vocabulary (PROGRAM.md
--   names it as 49 labels, but that list is explicitly not finalized and not
--   Claude's to invent — the operator builds it live through the Labeling
--   page, add/rename/remove). Free text, mirroring image_training_examples'
--   own "deliberately free text, not constrained" design (migration 309).
-- * labeling_sample — which images are currently in scope for secondary-CLIP
--   relabeling. Grows over time ("iterate sample until 300 proposals for
--   >=50% of categories"); membership alone, no label data.
-- * label_proposals — one row per (image, secondary-CLIP model): what the
--   stronger encoder proposes, pending operator review. Distinct from
--   image_clip_tags (the production tagger's live, gallery-facing table) and
--   from image_training_examples (the confirmed training set) by design —
--   confirming a proposal upserts into image_training_examples; it never
--   writes image_clip_tags.
--
-- All three are backend-only (no `_public` view / RLS grant), matching the
-- settings/simulation_runs precedent in migration 372: every read and write
-- goes through the admin-gated FastAPI service, never a direct Supabase
-- browser query.

------------------------------------------------------------------
-- taxonomy_labels
------------------------------------------------------------------

create table dedup_sim.taxonomy_labels (
  id          bigserial primary key,
  label       text not null unique check (char_length(label) between 1 and 100),
  family      text,
  active      boolean not null default true,
  created_at  timestamptz not null default now(),
  created_by  text not null default 'operator'
);

create index on dedup_sim.taxonomy_labels (active);

alter table dedup_sim.taxonomy_labels enable row level security;

------------------------------------------------------------------
-- labeling_sample
------------------------------------------------------------------

create table dedup_sim.labeling_sample (
  image_id  bigint primary key references images (id) on delete cascade,
  added_at  timestamptz not null default now(),
  added_by  text not null default 'operator',
  source    text not null default 'auto' check (source in ('auto', 'manual'))
);

create index on dedup_sim.labeling_sample (added_at desc);

alter table dedup_sim.labeling_sample enable row level security;

------------------------------------------------------------------
-- label_proposals
------------------------------------------------------------------

create table dedup_sim.label_proposals (
  image_id      bigint not null references images (id) on delete cascade,
  model         text not null,
  label         text not null,
  confidence    real,
  proposed_at   timestamptz not null default now(),
  status        text not null default 'pending'
    check (status in ('pending', 'confirmed', 'dismissed')),
  reviewed_at   timestamptz,
  reviewed_by   text,

  primary key (image_id, model)
);

create index on dedup_sim.label_proposals (status);
create index on dedup_sim.label_proposals (label);

alter table dedup_sim.label_proposals enable row level security;
