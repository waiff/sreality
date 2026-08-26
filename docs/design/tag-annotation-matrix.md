# Tag annotation matrix

Status: in progress, 2026-08-26. Supersedes the training-set half of migration 309
(`image_training_examples`) and retires `/clip-audit` entirely. Companion to
`docs/design/new-dedup/PROGRAM.md` (the labeling program this grew out of) and
`docs/design/clip-linear-probe.md` (the not-yet-built trainer this is substrate for).

## North star

One permanent, per-(image, tag) annotation table is the single ground truth every
future per-tag classifier head trains from. Absence of a row is the only "untouched"
state — sparse storage, and "explicit vs. defaulted" falls out for free. The labeling
UI's only job is to fill that table in bulk, fast. Nothing else forks a second source
of truth for tag ground truth.

## Scope

In scope: the annotation data model, the labeling UI, retiring ClipAudit, promoting
the tag taxonomy to a permanent home. Out of scope, deliberately: the per-tag trainer
itself (sklearn `LogisticRegression` per tag, reading frozen CLIP embeddings from
`image_clip_embeddings`). `clip-linear-probe.md` already owns that design (currently
scoped to one multinomial head, v1, with multi-head explicitly deferred) and needs a
pass once this ships to consume the new table — that pass is not this sprint.

## Why the outside framing needed correcting

An external design conversation proposed this matrix without seeing the repo. Two
premises didn't survive contact with the codebase: the embedding backbone is CLIP
(`openai/clip-vit-base-patch32`, persisted in `image_clip_embeddings`), not DINOv2
(DINOv2 belongs to a wholly separate subsystem — RunPod image-pair similarity for the
dedup engine's L3 stage); and the taxonomy is ~49 free-text, family-grouped,
operator-curated labels, not ~15 fixed tags. Both change the UI's shape (tag-centric
batch review, not a flat image×tag grid) and are reflected below.

## Decisions ledger (operator, 2026-08-26)

| Topic | Decision |
|---|---|
| Negative semantics | Global default-negative, literally as specified. Scoped to `labeling_sample` membership (the curated pool) — an image never added to the pool is outside every head's dataset; once in the pool, every active tag without an explicit row is an implicit negative, for both display and training. |
| ClipAudit | Retire entirely — frontend page, `TrainControl`, and every backend route/table column exclusive to it. |
| Border cases | Keep `image_border_cases` separate from the new per-tag `excluded` state — different grain (whole-image quarantine vs. one ambiguous tag on an otherwise fine image). |
| UI shape | Tag-centric batch review is the default workflow; an image-centric detail view (all active tags on one image) is secondary, reached from a tile. |

## Current state being replaced

- `image_training_examples` (migration 309): one mutable label per image, free text,
  hard overwrite on relabel. Five live writers today: `confirm_proposal` and
  `bulk_confirm_proposals` (`toolkit/dedup_sim_labeling.py`), and ClipAudit's Train CTA
  (`set_training_example` / `bulk_set_training_examples`, `api/labeling.py`).
- `dedup_sim.taxonomy_labels` (migration 373): the operator-curated vocabulary,
  free text, add/rename/remove, living in a schema (`dedup_sim`) that
  `docs/design/new-dedup/PROGRAM.md` plans to drop wholesale at Wave 8. A permanent
  annotation matrix cannot depend on a table that disappears then — and the taxonomy
  itself currently has no permanent home post-drop either.
- `ClipAudit.tsx` + `TrainControl.tsx`: the older per-image dropdown/Train/border-case
  page migration 373 was built to replace. Still live, still the only writer of four
  `api/labeling.py` routes and two now-dead ones (`/phash-note`, unreferenced anywhere).
- `image_tag_annotations` (migration 308, `tag_flagged`/`render_flagged`) and
  `phash_pair_notes` (migration 308): both exclusively ClipAudit's; `phash_pair_notes`
  has zero frontend callers already (dead before this effort starts).
- Untouched by this work: `image_clip_tags` / `image_clip_embeddings` (production
  tagger output), `dedup_sim.labeling_sample` / `label_proposals` (machine-suggestion
  queue — stays in `dedup_sim`, stays droppable at Wave 8), `image_border_cases`.

## Data model

### `tag_taxonomy` (new, permanent, public schema — promotes `dedup_sim.taxonomy_labels`)

```sql
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
```

Backend-only (no `_public` view), matching migration 373's precedent, not 309's older
public-view one — every read/write goes through the admin-gated API. A real surrogate
key replaces the current text-keyed join between `taxonomy_labels` and
`image_training_examples` (rename today rewrites every dependent row; rename tomorrow
is a one-row update). `label_proposals.label` stays free text — it is a transient
machine suggestion, not a durable reference, and resolving it to a `tag_id` happens
only at the moment a human sets a state.

### `image_tag_labels` (new, permanent, public schema)

```sql
create table image_tag_labels (
  image_id    bigint not null references images(id) on delete cascade,
  tag_id      bigint not null references tag_taxonomy(id) on delete cascade,
  state       text not null check (state in ('positive', 'negative', 'excluded')),
  created_by  text not null default 'operator',
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  primary key (image_id, tag_id)
);
create index on image_tag_labels (tag_id, state);
alter table image_tag_labels enable row level security;
```

One row per explicit decision. No row = untouched = displays and trains as negative
once the image is in `labeling_sample`. `excluded` rows are dropped at training time
by definition, not by a separate flag. Backend-only, admin-gated, same as `tag_taxonomy`.

### Migration/backfill (Wave A, additive)

1. Create both tables.
2. Backfill `tag_taxonomy` from `dedup_sim.taxonomy_labels` (id remap tracked via a
   temp mapping, not preserved 1:1 — nothing outside `dedup_sim` referenced the old ids).
3. Backfill `image_tag_labels` from `image_training_examples`: for every
   `(image_id, label)` row, insert `state='positive'` for that image's matching
   `tag_id`, and `state='negative'` for every other **active** tag — matches the
   spec's literal migration rule ("all other tags = negative") for the ~1,185 images
   that already have a hand-confirmed label; this is a few tens of thousands of rows,
   not a scale concern.
4. Nothing is dropped yet.

## Waves

**Wave A — additive schema + API (autonomous, no destructive DDL).**
`tag_taxonomy` + `image_tag_labels` tables and backfill; toolkit functions for
tri-state set/bulk-set/read scoped by tag; `GET /new-dedup/labeling/overview` extended
with `positive_count` / `negative_count` / `excluded_count` per tag (border-case
exclusion logic carries over, now applied to `positive_count` specifically); new
tag-scoped browse endpoint supporting the "kitchen = excluded" filter. Old tables and
ClipAudit untouched — nothing breaks.

**Wave B — frontend cutover.**
`NewDedupLabeling.tsx`'s proposal tiles get a three-way state control (replacing
Confirm/Dismiss) that writes `image_tag_labels` directly and updates the proposal's
`status` for bookkeeping; batch bar gains "Set selected: positive / negative /
excluded"; a state filter joins the existing status/tag filters; taxonomy strip shows
the three counts per tag; keyboard shortcuts (focus-roving + a key per state, next
tile) are added — there are none today, anywhere outside the lightbox's arrow keys.
An image-centric detail panel (opened from a tile) lists every active tag for that
image with the same tri-state control, grouped by family, for the multi-tag-on-one-
photo case. The relabel-in-place path (`POST /labeling/training-example`) is repointed
onto the new endpoint. ClipAudit, `TrainControl`, and their exclusive frontend/backend
surface are deleted in the same wave (route, nav entry, `lib/queries.ts` and `lib/api.ts`
functions used only by ClipAudit, `api/labeling.py`'s image-annotation/phash-note/
training-example(s)-bulk/by-label routes) — `border-case` routes and `setTrainingExample`
(shared with the relabel path until this wave repoints it) are preserved throughout.

**Wave C — destructive cleanup (pauses for operator confirmation + backup).**
Drop `image_training_examples`, `image_tag_annotations`, `phash_pair_notes`,
`dedup_sim.taxonomy_labels`; remove the now-fully-dead remainder of `api/labeling.py`.
Doc updates ride here: `docs/architecture.md` rule 15's signal-producers list,
`roadmap/new-dedup.md`'s labeling-program bullet, a status note (not a rewrite) on
`docs/design/clip-linear-probe.md` pointing at the new table as its future input.

## Explicitly deferred, not silently dropped

- The per-tag trainer. `clip-linear-probe.md` still specifies v1 as one multinomial
  head; multi-head was already anticipated there as a later step. This work is that
  step's data substrate, not the trainer itself.
- Renaming the `/new-dedup/labeling` route/prefix now that the taxonomy it manages is
  permanent, not dedup-scoped. Cosmetic; flagged, not fixed here.
- `tag_taxonomy.active` as a real non-destructive deactivation lever — the same gap
  `taxonomy_labels.active` already had (read, never set false). Pre-existing, not
  created by this work.
