-- 445_tag_definitions.sql
--
-- Tag definitions — the versioned, written meaning of every tag_taxonomy row
-- (migration 442). Purely additive.
--
-- ~51 tags exist today as bare Czech strings, and two people apply them
-- differently. Before any per-tag classifier head can train on them, each tag
-- needs a WRITTEN definition: what it means, what counts, what does NOT count
-- (and which tag that case belongs to instead), which tags it is confusable with
-- and the visual tell that separates them. Writing the definitions is also the
-- diagnostic that settles the taxonomy itself — two tags whose does_not_count
-- lines cannot be written apart are one tag.
--
-- SUPERSEDE, NEVER OVERWRITE — the latest-wins + append-only-history idiom
-- listings/listing_snapshots already use. There are no drafts: every save inserts
-- a new row at version = max(version) + 1 with status 'active' and flips the
-- previously-active row to 'superseded', in ONE transaction. Exactly one active
-- version per tag is enforced by the partial unique index below, not by
-- application logic alone. A future PR stamps each annotation with the
-- definition_id it was decided under, so a definition change can requeue exactly
-- the annotations made under an old version — which only works because old
-- versions are immutable.
--
-- does_not_count / confusable_with reference OTHER TAGS BY tag_id, never by label
-- text, so a rename cannot rot a definition. They are stored denormalized inside
-- the versioned JSONB document deliberately: a version is an atomic snapshot of
-- what the operator wrote. Ids are resolved to labels at render/prompt time, and
-- ids that no longer exist are skipped — the same trade example_image_ids makes,
-- since no foreign key is possible on an array.
--
-- Backend-only: RLS on, no _public view, no anon/authenticated grants — every
-- read and write goes through the admin-gated FastAPI service, matching
-- migration 442's precedent.

begin;

create table tag_definitions (
  id                bigserial primary key,
  tag_id            bigint not null references tag_taxonomy (id) on delete cascade,
  version           int not null,
  means             text not null,
  counts            jsonb not null default '[]'::jsonb,
  does_not_count    jsonb not null default '[]'::jsonb,
  confusable_with   jsonb not null default '[]'::jsonb,
  leave_out_when    text,
  example_image_ids bigint[] not null default '{}'::bigint[],
  status            text not null check (status in ('active', 'superseded')),
  created_at        timestamptz not null default now(),
  created_by        text not null default 'operator',

  unique (tag_id, version)
);

-- Exactly one active version per tag. Also the second rail under a concurrent
-- save: a base_version check catches two tabs minutes apart, this catches two
-- overlapping transactions.
create unique index tag_definitions_one_active_idx
  on tag_definitions (tag_id) where status = 'active';

-- No (tag_id, version desc) index: the `unique (tag_id, version)` btree above
-- already serves both readers of that pair — the history list's ORDER BY version
-- DESC and the insert's max(version) — by backward scan.

alter table tag_definitions enable row level security;

comment on table tag_definitions is
  'Versioned written definition of a tag_taxonomy row (migration 442). Supersede, '
  'never overwrite: every save inserts version = max(version)+1 as ''active'' and '
  'flips the previously-active row to ''superseded'' in one transaction, so history '
  'is complete and immutable and a future annotation can cite the definition_id it '
  'was decided under. counts is a jsonb array of strings; does_not_count a jsonb '
  'array of {"case": text, "goes_to_tag_id": tag_taxonomy.id | null}; '
  'confusable_with a jsonb array of {"tag_id": tag_taxonomy.id, "tell": text}. '
  'Other tags are referenced BY ID, never by label text, and resolved at render '
  'time (ids that no longer exist are skipped) — the same trade example_image_ids '
  'makes, since no FK is possible on an array. Backend-only, admin-gated, same '
  'posture as migration 442.';

commit;
