# Tag annotation matrix

Status: in progress, 2026-08-26. Supersedes the training-set half of migration 309
(`image_training_examples`) and retires `/clip-audit` entirely. Companion to
`docs/design/new-dedup/PROGRAM.md` (the labeling program this grew out of) and
`docs/design/clip-linear-probe.md` (the not-yet-built trainer this is substrate for).

**Waves A and B shipped together, same day, PR #1185** — the doc/roadmap updates
originally planned for Wave C rode along with that PR instead, because retiring
ClipAudit already stopped every write to the three superseded tables then, not just
at the eventual DROP. Wave C (below) is now DDL-only: dropping tables nothing writes
to any more. See "Since Wave A/B shipped" below for follow-up additions.

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

## Since Wave A/B shipped

Two operator-requested additions, same data model, no new tables beyond one migration:

- **Assigned-tags row.** A tile only ever showed the ONE tag it was reviewing — with
  multi-label images now real, that stopped being the same thing as "everything this
  image is positive on." `tag_annotations.list_positive_tags_for_images` (one query,
  capped at `BATCH_IMAGE_MAX`) batches the lookup for a whole visible grid instead of
  one call per tile; the frontend accumulates it the same way it accumulates photos
  (never-seen ids only) and patches it locally from every mutation's own response
  (`patchPositiveTags`) rather than refetching. Renders as a small chip row directly
  under the photo, nothing when empty.
- **Tag flags, migration 443** (`tag_taxonomy.priority`, `tag_taxonomy.ready_for_training`,
  both `boolean not null default false`) — two operator-only signals surfaced in the
  "Modify labels" popup: `priority` pins a tag to the top of that list and marks it red
  (an attention flag, not a training input); `ready_for_training` is the operator's own
  call that a tag's set is solid enough for the eventual per-tag trainer to consume —
  independent of Gate 1, which only says a tag is LABELED enough, not reviewed.
  `tag_annotations.set_tag_flags` updates only the field(s) actually passed, so toggling
  one from the popup never clobbers the other.
- **Priority tags in red on the coverage chart too**, not just the popup — matching color,
  the active-filter copper highlight still wins if both apply.
- **Batch-set inside the "all tags" detail panel** (`tag_annotations.bulk_set_state_for_image`,
  the mirror of `bulk_set_state` — fixes the image, varies the tags). Motivating case: a
  fitness-room image is genuinely negative on every room tag, but deciding 49 tags one at a
  time isn't practical, and leaving the rest implicit (untouched-defaults-negative) isn't
  auditable — did the operator actually look, or did nobody touch it yet? "Select all
  untouched" + "Set selected: negative/positive/excluded" closes out everything not already
  hand-decided in one action, without silently overwriting the couple of tags already set via
  their own tri-state control (a row leaves the batch the moment it's decided individually).
- **Named the tag a proposal tile's tri-state control is deciding**, directly above the
  buttons — operator feedback that it wasn't obvious the control acts on ONE tag (the
  proposal's own label, or a typed correction), not every tag on the image.
- **Filter proposals by the "Original tag" too** (`dsl.list_original_tags` +
  `list_proposals(..., original_tag=...)`). The page's New-tag/Original-tag toggle
  already swapped which badge a tile shows; the Tag filter dropdown hadn't followed —
  it stayed on Taxonomy v1 (`label`) even while the badges displayed the production
  CLIP tagger's OWN fine_tag, a completely different, fixed 19-value vocabulary
  (`data/clip_taxonomy.json`'s prompt anchors, zero DB cost to serve). The dropdown
  now swaps its whole option list with the toggle, `original_tag` joins
  `image_clip_tags` "latest model wins" (matching `images_public`'s own resolution,
  so the filter always agrees with what the badge shows), and the two filters are
  remembered independently so flipping the toggle back restores whichever tag was
  picked in that view. The toggle itself still never refetches the grid on its own —
  only actually dropping a set filter does.

## Tag definitions (migration 445)

The 51 tags are bare Czech strings. Two people looking at the same photo apply them
differently, and neither is wrong, because nothing anywhere says what "interier - chodba"
means. Independent per-tag heads trained on that inconsistency learn the inconsistency.
So the definitions come **before** any further labeling: each tag gets a written meaning,
a list of what counts, a list of what does NOT count (and which tag each of those cases
belongs to instead), the tags it is confusable with plus the visual tell that separates
them, and an optional "leave the image out of this head entirely rather than decide" rule.

Writing them is also the diagnostic that settles the taxonomy. If you cannot write a
`does_not_count` line that separates tag A from tag B, they are one tag — the document is
where that becomes undeniable instead of an argument. That is why this ships before any
merge/split tooling: the tool would be guessing, the definitions produce the answer.

**Supersede, never overwrite.** `tag_definitions` (migration 445) is the same latest-wins +
append-only-history idiom `listings`/`listing_snapshots` already use. There are **no
drafts**: every save inserts a row at `version = max(version) + 1` with `status = 'active'`
and flips the previously-active row to `'superseded'`, in ONE transaction. Because there is
no draft state server-side, the workbench batches every edit — text, example-image toggles,
"add this neighbour to confusable_with" — into local state and writes exactly once, when the
operator presses Save. Otherwise one sitting would produce thirty versions of one definition.

**Every save states what it was written against**, and there are two rails under that, for
two different races. `save_definition` takes a `base_version` — the version the editor
loaded, `null` for "this tag had no definition" — and it is an assertion, not a hint: the
supersede names that version (`... AND status = 'active' AND version = %(base_version)s`)
and a save that retires nothing is refused. That is the rail for the race that actually
happens: two browser tabs minutes apart, which are not overlapping transactions and which
no index can see. Without it, a save written against v2 while v3 was already active would
supersede v3 and land its own stale text as v4 — no error, the active definition silently
reverted to older wording. The partial unique index `tag_definitions_one_active_idx` is the
second rail, for genuinely overlapping transactions: the loser's read predates the winner's
insert, so its own insert trips the index. Both surface as the same `ValueError` ("this
tag's definition changed in another tab — reload and save again") and the same 422, and the
editor sends the version the FORM holds — never the newest one a background refetch has
since learned about, which would defeat the check precisely when it matters.

**Old versions are immutable, and that is the point.** A future PR stamps each annotation
with the `definition_id` it was decided under. A definition change then requeues exactly the
annotations made under the old version instead of invalidating a whole tag's set — which
only works if the old document still exists, verbatim, forever. Design for it; it is not
built here.

**Other tags are referenced by id, never by label text.** `does_not_count[*].goes_to_tag_id`
and `confusable_with[*].tag_id` are `tag_taxonomy.id`s, so renaming a tag cannot rot a
definition that points at it. They live denormalized inside the versioned JSONB document
deliberately — a version is an atomic snapshot of what the operator wrote — and are resolved
to labels at render/prompt time via `referenced_tags`, **skipping ids that no longer exist**.
`example_image_ids` makes the same trade for the opposite reason: no foreign key is possible
on a `bigint[]`, so a deleted image is skipped when the gallery renders. The write path is
strict where the read path is lenient: `save_definition` rejects a reference to a tag that
does not exist right now (the picker only ever offers real tags, so an unknown id at save
time is a bug), while a *later* deletion is normal and is absorbed on read.

**Overlap evidence.** `nearest_tags` builds one centroid per tag by averaging the CLIP
embeddings (`image_clip_embeddings`, the checkpoint named in `data/clip_taxonomy.json` — read
from there, never a second hardcoded copy) of that tag's positives, and returns the closest
other tags by pgvector `<=>`. That operator is a cosine **distance** — 0 is identical — so the
field is named `cosine_distance` and rendered as one, never silently converted to a
similarity. `MIN_POSITIVES_FOR_CENTROID = 5` floors both the subject and every candidate: a
centroid over fewer positives is one image's idiosyncrasies, not a tag's visual identity. A
tag under the floor gets `[]` rather than an error — the `CROSS JOIN` over an empty `subject`
CTE simply yields no rows, so there is no special case in Python. `embedded_positive_count`
counts positives that actually carry an embedding, so it can be lower than the overview's
`positive_count`; it is named differently for exactly that reason.

**The workbench does not depend on `dedup_sim`.** The gallery ("what this tag ACTUALLY
contains") reads `image_tag_labels JOIN images` directly through
`tag_definitions.list_positive_images`, not `tag_annotations.list_images_for_tag` — that one
reads `dedup_sim.labeling_sample`, which is a membership filter rather than "every positive",
and lives in a schema `docs/design/new-dedup/PROGRAM.md` plans to drop wholesale at Wave 8.
This page is permanent; wiring it to a doomed schema would be a defect on delivery day.

Surface: `toolkit/tag_definitions.py`, seven routes appended to
`api/new_dedup_labeling.py` (same `/new-dedup/labeling` prefix, same router-level
`require_admin`), and the `NEW DEDUP · Taxonomy` page at `/new-dedup/labeling/taxonomy` —
tag list on the left with a `v{n}` / `—` status chip, the definition editor on the right, the
positives gallery, and the overlap-evidence list. One sitting is one write, so every cap the
toolkit enforces is mirrored in the editor (`means` 500 chars, every other line 300, 24
example images) — a cap discovered only as a 422 would cost the whole sitting. For the same
reason a half-written row blocks Save instead of being dropped on the way to the server, and
a staged example that has stopped being positive on the tag (or fell past the 300 the grid
shows) is listed separately above the grid, because it still saves into the next version and
would otherwise be uncountable and unremovable. `tag_taxonomy.family` is NULL on all 51
live rows, so the page derives the family from the `" - "` prefix in the label; **no backfill
of `family` happens here** — that is a data change nobody asked for, and it would collide with
the very taxonomy work this page exists to inform.

Deliberate non-additions: no definition lifecycle state machine (`active`/`superseded` is the
whole vocabulary), no merge/split execution, no bulk edit tools. The taxonomy is not settled
yet; building tools to reshape it before the definitions exist would be building on the
guesses this page is meant to replace.

## Explicitly deferred, not silently dropped

- The per-tag trainer. `clip-linear-probe.md` still specifies v1 as one multinomial
  head; multi-head was already anticipated there as a later step. This work is that
  step's data substrate, not the trainer itself.
- Renaming the `/new-dedup/labeling` route/prefix now that the taxonomy it manages is
  permanent, not dedup-scoped. Cosmetic; flagged, not fixed here.
- `tag_taxonomy.active` as a real non-destructive deactivation lever — the same gap
  `taxonomy_labels.active` already had (read, never set false). Pre-existing, not
  created by this work.
