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
| Negative semantics | **OVERTURNED 2026-08-27 — absence is not a negative.** No row means UNTOUCHED, and untouched never trains as negative. An image never reviewed for a tag must stay distinguishable, forever, from one reviewed and judged not-that-tag; membership of a review queue confers no label of any kind (see "Candidate retrieval" below — `tag_candidates` has no state column for exactly this reason). ~~The 2026-08-26 decision was: global default-negative scoped to `labeling_sample` membership — once in the pool, every active tag without an explicit row is an implicit negative for both display and training.~~ That is what produced migration 442's 72,000 manufactured negatives, and it is why the deletion PR keyed on `source = 'backfill_442'` exists. |
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

One row per explicit decision. No row = UNTOUCHED, and untouched never trains as
negative (operator ruling 2026-08-27, superseding the ledger row above; migration 450
restates it as a fresh `comment on table`, since migrations cannot be edited).
`excluded` rows are dropped at training time by definition, not by a separate flag.
Backend-only, admin-gated, same as `tag_taxonomy`.

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

## Tag definitions (migration 446)

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

**Supersede, never overwrite.** `tag_definitions` (migration 446) is the same latest-wins +
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

**Outlier-first contents.** The gallery's default order is cosine distance from the tag's OWN
centroid, farthest first — measured on `exterier - fasáda` (71 positives), the 12 nearest tiles
are exterior_facade/staircase_exterior and the 12 farthest are bathroom, document_text,
energy_certificate and garden. `list_positive_images_outlier_first` builds that centroid from
`source IN ('human','human_confirmed')` positives, the same predicate `tag_candidates` draws
against — migration 446 stamped `backfill_442` on negatives only, so `state='positive'` already
sheds every manufactured row and the source clause is the rail against an unreviewed machine
positive defining the centre it would be measured against. `MIN_POSITIVES_FOR_CENTROID` floors
it, applied in a `CASE` rather than a `HAVING` so a tag under the floor can still be told how
many it has; below the floor every distance is NULL and the ORDER BY degrades to
`list_positive_images`' own `updated_at DESC, image_id DESC`. **There is no threshold on the
distance and never can be** — measured inter-tag centroid distances span ~0.01 to ~0.42, so only
rank within one tag transfers, which is why the SPA renders a rank and a distance and never a
score. The route ranks; the SPA's "Newest first" is a client-side re-sort of the same fetched
rows, so flipping the order never refetches a visible grid.

**The order is not the window.** `LIMIT` is applied AFTER the distance sort, so a tag holding more
positives than `POSITIVE_IMAGE_LIST_MAX` comes back as the N *farthest* — and a time re-sort of
those is the newest OF THOSE, not the tag's newest (a freshly labeled image, being typical for the
tag, sits NEAR the centre and is exactly what the window drops). One fetch per tag is deliberate —
a cache entry per order is a second thing every retag has to keep true, and flipping would blank
the grid mid-sitting — so on a full page the SPA narrows the button's claim to "Newest of these N"
instead. Below the cap the fetched rows are the whole tag and the button keeps its full promise.

**Two floors, two populations, and never a fabricated count.** `centroid_positives` counts
`human`/`human_confirmed` positives; the overlap panel's identical-looking floor runs off
`nearest_tags`, whose centroid CTE has no source filter and counts every embedded positive
(`machine` is a writable source, so these are not the same set by construction — they merely
coincide while machine positives are near zero). The gallery's note therefore says
*human-verified* in as many words, so two different counts of a same-sounding quantity cannot
read as one contradiction. The basis is on screen whichever side of the floor the tag sits: a
centroid over 5 images and one over 200 are otherwise indistinguishable in the UI, and at five
each image is a fifth of the centre it is then measured against. `centroid_positives` is `null`
when nothing was computed — a `recent` read, or a read that FAILED — which is not the fact "this
tag has none", so the SPA drops the number from the sentence rather than rendering `0`, and a
failed `/positive-images` read raises the page's error banner instead of a data-shaped diagnosis
of a transport failure.

**The workbench does not depend on `dedup_sim`.** The gallery ("what this tag ACTUALLY
contains") reads `image_tag_labels JOIN images` directly through
`tag_definitions.list_positive_images`, not `tag_annotations.list_images_for_tag` — that one
is driven by the tag's REVIEW QUEUE (`tag_candidates` since migration 450; before that,
`dedup_sim.labeling_sample`), which is a work list rather than "every positive", and whose
rows carry no label semantics at all. This page is permanent, and it is about what a tag
contains, not about what is queued for review.

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

**Two edits on that page write immediately, and the "one sitting, one write" rule above is
about the DEFINITION only.** Reading a tag's contents is how drift gets caught, so the two
fixes it provokes happen in place rather than by navigating away: an `all tags` pill on every
gallery tile opens the **shared** `ImageTagDetailPanel` (`components/tag-annotations/`, used
by the Labeling page too — extracted, never copied), and a `rename` control on the selected
row edits the tag's label. Neither is part of the versioned document — a tri-state cell and a
tag's name are ground truth with no draft to batch into — and neither touches the definition
draft, its `base_version`, or `example_image_ids`. The gallery says so in as many words,
because it is the one place this page's contract does not hold.

Opened over a tag being read, the panel pins that tag above its family groups and offers
**four word-labeled outcomes instead of the three glyphs**: `keeps it` (positive), `not this
tag` (negative), `belongs elsewhere` (excluded · **pruned**) and `can't tell` (excluded ·
ambiguous). That is the whole point of the surface. With `⊘` plus a reason chip, the
DANGEROUS answer — a negative on an image whose subject really is present, which poisons that
head and is what the operator's own "never mark a tag negative when its subject is clearly
present, LEAVE IT OUT instead" rule forbids — costs one click, while the demanded answer costs
two plus a hunt. Naming the four and pricing them identically inverts that, and none of them
can clear the cell back to untouched: a human looked, and that fact is not discardable.
Everything else stays patched-in-place — the tile leaves the grid and joins a session-local
"moved out" strip with a `put back`, and the derived counts come from a re-fetched overview,
never recomputed in the SPA.

**Patch-in-place is a rule about the list ON SCREEN, and every escape from it is a place the
page would otherwise lie about a tag's contents.** Three, all of them cheap:
- A write lands on a tag that is *not* being read (the panel's second half is for exactly
  that — moving an image UNDER another tag). Nothing is mounted to blink, but that tag's
  gallery is cached and the overview refetch has already moved its count in the list on the
  left. So its `positive-images` key is invalidated rather than ignored; inactive queries only
  take the flag, and the refetch happens when the operator gets there. Skip it and, inside
  `main.tsx`'s 60 s `staleTime`, selecting that tag serves a gallery the write contradicted —
  a row reading 13 above twelve tiles.
- A `put back` resolves *after* a tag switch. The receipt strip is session-local and the
  switch empties it, so there is no held row to splice back and the cached grid is short of an
  image the server has just restored, with nothing left on screen to explain the hole. The
  restore reports whether it could patch; only the path where it could not falls back to an
  invalidate.
- The "showing the 300 most recent" note is read off the **fetched** length
  (`rows + movedOutShown`), never off the live one. `rows` is the patched cache and shrinks as
  images are moved out, so a truncated fetch of exactly 300 would quietly become 299 and drop
  the note — telling an operator writing a definition that they have seen the whole tag.

The rename has one rail of the same kind: **leaving a row clears the pending edit**, label and
error both. Hiding an editor is not dismissing it, and a `<input autoFocus>` that comes back
holding an abandoned label steals the focus from the click that re-selected the tag — one
Enter then commits a name nobody chose. That is precisely the accident the deliberate
no-commit-on-blur already guards against, arriving by the other door.

Deliberate non-additions **as the page first shipped**: no definition lifecycle state machine
(`active`/`superseded` is the whole vocabulary), no merge/split execution, no bulk edit tools.
The taxonomy is not settled yet; building tools to reshape it before the definitions exist
would be building on the guesses this page is meant to replace. The next section is where the
last two were reconsidered — not because the taxonomy got settled, but because writing the
definitions started producing collapse decisions, and executing those by hand in SQL turned
out to be the larger risk.

### Reshaping a tag from the workbench: filing a batch, and deleting

Both surfaces are pure reuse — **no new route and no migration**. Filing a batch is
`POST /tags/{tag_id}/annotations/bulk` called twice, destination then source; deleting is the
`DELETE /taxonomy/{tag_id}` that already backed `TaxonomyManageModal` on the Labeling page,
which is untouched.

**The motivating case is the acceptance case.** The overlap evidence put `interier - koupelna`
0.009 and 0.011 from its own two children (`… s vanou`, `… se sprchovým koutem`) — a parent
competing with its children, measurably unlearnable. The operator's decision was to collapse
them into the parent, and the first run of it was hand-written SQL: the 145 images positive on
either child were set positive on the parent, `source='human'`, `verified_at = now()`, taking
the parent from 19 to 164 human positives. Every one of those 145 carried a manufactured
`backfill_442` negative on the parent, which the write replaced; not one carried a genuine
human negative, so nothing real was overwritten. That is the shape the surface reproduces.

**A batch has to answer two questions, and the second is the one a naive implementation gets
wrong.** The destination obviously becomes positive. What happens to the SOURCE is a choice,
and "source becomes negative" is NOT a safe default — those 145 images genuinely ARE
bathrooms-with-bathtubs as well as bathrooms. So the source gets one of **three** outcomes,
named in the same words the `ImageTagDetailPanel`'s four buttons use, one vocabulary learned
once:
- **`keeps it`** — the image legitimately belongs to both, which is the motivating case. There
  is **no source write at all**: one API call, nothing removed, nothing patched out of the
  grid. This is a copy, and the UI never calls it a move. It is also the default, because it
  is simultaneously the safe answer and the common one.
- **`not this tag`** — the image was mis-filed here. The source becomes `negative`: a true,
  valuable negative the head learns from.
- **`belongs elsewhere`** — the subject IS present but another tag fits better. `excluded` with
  reason **`pruned`**, never negative. This is the operator's own rule ("never mark a tag
  negative when its subject is clearly present — LEAVE IT OUT instead") wired into the batch.

`can't tell` is deliberately absent: a batch being filed somewhere specific is by construction
not undecidable, and manufacturing 145 `ambiguous` exclusions at once would make the ambiguity
rate report a broken definition that isn't. One outcome applies to the whole batch — per-image
choices would defeat the point of a batch — so the panel states, before the click, which one is
about to be applied and to how many images, in a sentence that names both tags.

**Ordering is destination first, always.** If the source were written first and the destination
then failed, images would have left the source with nowhere to go. Destination-first means the
worst case is a duplicate positive on both tags — precisely the safe `keeps` state, and
recoverable by pressing Write again. Chunks are sequential (never `Promise.all`) at the
server's `BULK_STATE_MAX`, so the failure point is deterministic, and every terminal case
resolves into one result object rendered beside the button rather than a toast that scrolls
away.

**The destination may never be the tag being read.** Found in review, not in design: the
batch form's two values live inside the gallery component, which the page does not remount on
a tag switch, so after filing a batch the natural next move — open the destination tag and look
at what arrived — left the picker naming the tag now on screen. Write would then have issued
`positive` and `negative` for one tag over one id list, terminal state `negative`,
`source='human'` on that tag's own positives: exactly the manufactured lie the three-outcome
vocabulary exists to prevent, up to 200 images at a press. `bulk_set_state` is tag-scoped and
cannot see it; the invariant belongs in the UI and is now derived (`destReady`), so it gates
the write button, the click handler and the pre-write sentence together, with the picker and
the outcome re-seeded to `null`/`keeps` whenever the subject changes.

**Deleting a tag destroys every decision under it, and the honest number is not the row
count.** `remove_tag` DELETEs every `image_tag_labels` row for the tag and then the tag itself.
A typical tag carries ~1,300 manufactured `backfill_442` rows alongside a few dozen real human
decisions, so a confirm reading "1,440 annotations" buries the only number that matters. The
human count is therefore the headline in the largest type in the dialog, the manufactured
remainder is a quieter second line, and the acknowledgement checkbox — which names that human
count back — appears only when it is above zero, so it never decays into ritual. The dialog
also states what the written definition loses: `tag_definitions` cascades, so the active
definition and every stored version go too. That count comes from a per-tag query separate from
the page-level status supplying the version number, so while it is unanswered the dialog says
"every saved version of it" rather than printing a self-contradicting zero.

**What survives a delete is the events history.** Since migration 446 the history trigger fires
on DELETE as well as INSERT, and `image_tag_label_events` carries the tag's label denormalised
onto the row — that table deliberately has **no foreign keys** precisely so a cascade cannot
erase the record of its own deletion. `remove_tag` deletes the annotations before the tag, so
the label subselect still resolves. Deletion is therefore recoverable, but by a hand-written
SQL job, not a button: the dialog says exactly that and promises no restore that does not
exist. Cross-tag references in other definitions are JSONB `tag_id`s, so they keep the
reference and simply stop resolving to a name.

Afterwards the page must not keep half-pointing at something gone: the overview and definition
lists are patched rather than refetched, the four per-tag caches are `removeQueries`'d before
the prefix invalidations, the definition form is reset, and the `?tag=` param is cleared — and
a stale `?tag=` link from anywhere else says the tag is gone instead of surfacing a raw 404.

## Provenance and history (migration 446)

Migration 442 left 73,499 rows in `image_tag_labels` and **72,000 of them — 98% — are not
decisions anybody made.** Its backfill manufactured them from a one-hot assumption ("this
image was labeled kitchen, therefore it is negative for the other 50 tags"), and the only
thing separating them from operator work was a free-text `created_by`. This migration makes
them precisely identifiable so the eventual deletion is exact, and so every consumer can
exclude them starting now. **It deletes nothing** — no `DROP`, no `DELETE`, no destructive
`ALTER`; the removal is a separate, gated, backed-up PR whose predicate is literally
`WHERE source = 'backfill_442'`.

**`created_by` alone cannot draw that line, and assuming it could would have destroyed the
ground truth.** Migration 442 stamped *both* arms of its backfill with the same
`backfill:image_training_examples` string — `442:82` writes one positive per image, `442:88`
writes the N−1 one-hot negatives — and `created_by` is set on INSERT only, so no later
operator write ever rewrote it. Only the second arm is fiction; the first *transcribed*
`image_training_examples`, the operator's own hand-labels from `/phash-audit`'s "Train" CTA
(migration 309), and those ~1,440 positives are the only ground truth the system has. So the
backfill names the fiction by what it actually is: **a `negative`, backfill-stamped, that
nobody has touched since** (442 wrote `created_at` and `updated_at` off one transaction clock,
so `updated_at = created_at` is exactly "never re-decided" — and a cell the operator *has*
re-decided is a decision, never deletable, whichever way it went). Everything else becomes
`human`, with `verified_at` stamped from `updated_at`.

Four fields land on the annotation itself. **`source`** has four values, and the split that
matters is `human_confirmed` (a machine proposed it, a person affirmed it) versus `machine`
(a machine decided it, **nobody has looked**) — not the same evidence, and training and every
metric filter on this one column forever, so a single-column predicate has to express the
whole distinction. `human_confirmed` requires **both** halves of agreement: the operator kept
the machine's label *and* said yes. A human *overriding* a machine is plain `human`, and so is
a human *rejecting* one — pressing ✗ on a proposal, or a batch "Set selected: negative", is the
machine being wrong, and stamping that `human_confirmed` would drive measured per-tag agreement
to 100% however bad the encoder is, destroying the one number this provenance exists to make
measurable. `toolkit/dedup_sim_labeling.py` therefore keys the affirmation on the *same*
predicate that marks the proposal `confirmed`, so the two rows it writes in one transaction
cannot contradict each other. The disagreement is preserved in the event history rather than in
a fifth source value. **`definition_id`** cites the `tag_definitions` version (444) the decision
was made under — never a parameter, always resolved inside the INSERT by a subquery on the
annotation's own `tag_id`, which is what makes "requeue exactly the annotations decided under
the old wording" sound and citing another tag's definition structurally impossible. It is
`ON DELETE SET NULL`, never RESTRICT: `tag_definitions` cascades off `tag_taxonomy`, and a
RESTRICT would make the live `remove_tag` route start failing. **`verified_at`** is derived,
never supplied (no caller knows better than `now()`), and carried forward with `coalesce` so
a machine can never erase a human's verification. **`excluded_reason`** distinguishes
`ambiguous` from `pruned` — identical effect on training, opposite meaning for diagnostics —
and a CHECK, not a convention, makes it impossible on a non-excluded row.

**The human-wins rail is in SQL, not in a convention.** The upsert's `DO UPDATE … WHERE`
lets a machine write land only on a cell that is untouched, machine-written or backfill; a
human write always lands. `set_state` reports `applied: false` when it was refused. Known
limit, unchanged: the bulk paths use `executemany` and cannot report suppression per row —
their `updated` has always been "cells submitted".

**History is captured by a TRIGGER** (`image_tag_labels_log_event` → `image_tag_label_events`),
not by application code. There are already four write paths and the machine-proposal loop will
add more; the table's entire value is that it is COMPLETE, and a log every future writer must
remember to append to is a log with holes. The alternative's failure mode is concrete and this
repo has shipped it before (`scrape_runs errors=0 on crash`: nine hand-copied `finally` blocks,
one missed). `clear_state` DELETEs, and reverting a cell to untouched **is** a decision — the
trigger records it as an event with `state` NULL. Precedent here is migration 392's
`properties_log_status_event`, down to the `event_at` / `created_at` split. The events table
carries **no foreign keys at all**, deliberately: an audit log with `ON DELETE CASCADE`
destroys the record it exists to keep, and RESTRICT would make `remove_tag` fail forever once
any event existed — so bare `bigint`s plus a denormalized `tag_label` snapshot. Accepted cost:
the trigger is invisible to `_FakeConn`, so `image_tag_label_events` has **no behavioural unit
coverage** — its executing gate is CI's migration-replay job, backed by
`tests/test_migration_445_tag_provenance.py`, which asserts what the migration DECLARES. Do not
fake a trigger to manufacture coverage; a fake that models a trigger is a second, drifting
implementation of it.

**The ambiguity rate** is `ambiguous_decided / decided`, computed server-side in `_OVERVIEW_SQL`
against two named constants (`AMBIGUITY_RATE_THRESHOLD = 0.15`, `AMBIGUITY_MIN_DECISIONS = 20`)
bound as parameters and echoed in the overview payload, so no surface keeps a second copy of the
number it renders. Four properties are the whole point. **Pruned exclusions sit outside both the
numerator AND the denominator** — leaving them in would let pruning *dilute* the rate (prune a
hundred images and a broken tag reads healthy), the exact corruption the two-reason split exists
to prevent. **Both halves are scoped to what a human decided** (`source IN ('human',
'human_confirmed')`): 72,000 manufactured `backfill_442` negatives would drive every tag to ~0
and the signal would never fire once — the concrete reason this ships before any more labeling —
and unverified `machine` rows would do the same thing for the same reason, so the loop this PR
builds the substrate for cannot bury the signal it is measured by. The rate is about *human*
indecision; "go fix the DEFINITION" is a human call. **An excluded cell with no reason at all
counts as ambiguous**, never as a silent third bucket: a deliberate prune always names itself,
so an unexplained exclusion is "nobody could decide" — which is also exactly what the grid
renders for such a cell, and the store and the screen must not disagree about the same row.
And **`decided = 0` yields NULL, never 0**, because a tag with no decisions is unknown, not
healthy (`LLM health checks false-green`). Above the threshold the tag's DEFINITION is the
problem, not the labeling, and the coverage strip's chip is a link straight into that tag's
workbench. The payload publishes `ambiguous_decided_count` alongside the whole-inventory
`ambiguous_count` so a surface can render the fraction the rate was actually computed from
instead of a near-miss of it.

`positive_count` / `gate_count` / `negative_count` / `excluded_count` are deliberately
**unchanged** and still include the backfill rows; `backfill_count` is what makes that inventory
legible. Narrowing them is the deletion PR's decision, not this one's.

## Candidate retrieval (migration 450)

**The problem.** `dedup_sim.labeling_sample` was 1,200 images picked as "the 2,000 newest
with a storage_path" — untargeted, ONE pool shared by all 51 tags, and 943 of them never
labeled at all. Rare tags are a fraction of a percent of the corpus, so a random pool
cannot build their training sets: candidates have to be FOUND, not stumbled on. From here
they are, per tag, by ranking a bounded pool against a centroid of that tag's positives.
`public.tag_candidates` is that store; `toolkit/tag_candidates.py` is the retriever;
`list_images_for_tag` and `tag_overview` are repointed onto it. Nothing in `dedup_sim` is
dropped or even touched — the secondary-CLIP proposal lane still writes `labeling_sample`
and reads `label_proposals`, so the candidate reader is off that schema but the schema is
not yet droppable.

**Membership carries no training semantics.** This is the load-bearing property and the
reason the table has NO state column, NO reviewed flag and NO status. A candidate is an
image somebody should LOOK at for this tag — not a positive, not a negative, not a
default. Whether one has been DECIDED is derived by joining `image_tag_labels`, the only
place a decision has ever lived. There is deliberately nothing there for a future reader
to mistake for a label; `tests/test_migration_450_tag_candidates.py` asserts the absence
of those column names, because the shape is the guarantee.

**Every row records why it was drawn** — `draw` (which rank band), `category_main` (which
quota), `pool_rank` / `pool_size` / `distance` (where it sat in the ranked pool, and rank
means nothing without the size it is a rank OF), `centroid_positive_count` (how much
evidence the centroid carried), `definition_id` (which written definition was active,
resolved inside the INSERT from the row's own tag_id — migration 446's rule). Sampling
provenance is auditable rather than asserted.

**The centroid is human-verified positives only** (`state = 'positive' AND source IN
('human','human_confirmed')`). Migration 442's 72,000 manufactured negatives and every
unreviewed `machine` row are excluded BY PREDICATE, never by deletion — so this work
creates no dependency on the gated deletion PR. The floor is `MIN_VERIFIED_POSITIVES = 15`,
the smallest population retrieval was ever measured at (median AUC 0.942 across 28 tags,
min 0.859). Below it the tag is told so — `status='insufficient_positives'`, zero rows
written — instead of being handed a pool built from one operator's idiosyncrasies.
`_COUNT_VERIFIED_POSITIVES_SQL`'s predicate is byte-identical to the centroid CTE's: if
the gate and the centroid disagreed about the population, the gate would be a lie.

**Three deliberate mixes, all named constants in one module.**
- *Rank bands* — `BAND_MIX` head 0.50 / mid 0.30 / random 0.20. A pure top-N produces
  prototypical training sets that fail on odd cases. The head is the only band that can
  build a rare tag's positive set (measured precision@100 of 72-100%, 8-33x base rate);
  the mid band is where the three measured confusion clusters live (bathrooms, circulation,
  living spaces) and is where hard negatives come from; the random band is the honesty
  rail — the only source of a truthful base rate and the only band that can surface a
  positive the centroid is blind to. Sustained positives out of `random` mean the centroid
  is missing a mode, and `candidate_summary` reports each bucket's yield so that check can
  actually be READ instead of hand-counted off a grid. There is no redistribution between
  bands: a shortfall is reported as requested vs inserted, never quietly back-filled.

  **A band is only its band if its ORDER is.** Three things make that true, and each was a
  live defect in the first cut. (a) `random` samples the WHOLE pool, head included — a
  sample that skipped the head would be a sample of the pool minus its highest-yield
  region, which is not a base rate; the overlap is resolved in `select_candidates`, which
  walks the head first and skips a repeat `image_id`. (b) The union is ordered by a
  per-band ordinal (`band_ord`: rank inside the head, a shuffle inside the two sampled
  bands), never by `pool_rank` across all three — `select_candidates` consumes the order
  greedily and stops each band at its quota, so a similarity sort would hand `mid` and
  `random` the nearest THIRD of their 3x overfetch and nothing else, making the tail
  structurally unreachable and biasing the base rate high. (c) The mid band's upper bound is
  `greatest(5% of the pool, head_fetch + mid_fetch)`: the lower bound is the head's
  OVERFETCHED window while the upper bound is a percentile, so on a thin pool the predicate
  could become `rank > H and rank <= M` with `H > M` — a band empty by arithmetic, taking
  every hard negative near the confusion clusters with it.
- *Categories* — `CATEGORY_MIX` byt 0.30 / dum 0.25 / pozemek 0.20 / komercni 0.15 /
  ostatni 0.10. The labeled set is 83.8% byt against a 43.9% corpus, with `pozemek`
  under-represented 5.7x. Capping byt BELOW its corpus share means every sitting dilutes
  the skew instead of merely not adding to it, and the thin categories get a floor. A side
  effect worth naming: inside an off-type category, the head band surfaces that category's
  hard negatives, which a byt-only pool never produces.
- *Pool* — `POOL_IMAGES_TARGET = 20,000` images per draw — the WHOLE budget when the draw is
  pinned to one category, that category's share of the mix otherwise. A pinned draw
  allocates the whole count to one category, so scaling its pool by the mix share would
  shrink the pool while the quota (and with it the head's fetch window) grew: at the UI's
  prefilled count of 120 pinned to `komercni` the old sizing gave a 3,000-image ceiling
  whose 5% mark sat at 150, under a head window of 180. Drawn as a listing lottery then
  an image lottery (`POOL_IMAGES_PER_LISTING = 4`), never by consecutive `image_id`:
  30,000 consecutive ids came from 2,106 listings, so an id-range "random" sample is a
  listing sample wearing a disguise. The inner sort is `random()`, never `sequence` — a
  listing's first photos are the exterior/living-room hero shots and floor plans sit at the
  end, so ordering by sequence would make `pudorys` structurally unreachable. TABLESAMPLE
  SYSTEM was rejected: it samples pages, and pages are insert-time clusters.

**Rank and percentile, never an absolute cosine.** Measured inter-tag centroid cosines span
0.58-0.99 (median 0.81), so a cosine that means "very close" for one tag means "unrelated"
for another. Bands are defined by rank and percentile within one tag's own pool; `distance`
is stored for auditing a single pool and is documented on the column as non-transferable.
No global threshold exists and none may be added. For the same reason **whole-pool holdout
rank is never computed**: held-out kitchens ranked ~2,000th of 31k because ~2,000 genuine
unlabeled kitchens outranked them, while that same ranking was 100% precise@100. Treating
unlabeled-outranking-holdout as an error would be measuring the retriever's success as a
failure.

**What the near-duplicate collapse does and does not catch.** Exact-hash collapse happens in
SQL (`PARTITION BY phash`, with `phash IS NULL` rows explicitly kept — a naive
`DISTINCT ON (phash)` would fold every un-hashed image into one row). Then a greedy Python
pass drops anything within `NEAR_DUP_MIN_HAMMING = 6` of an already-accepted or
already-stored hash, and caps `PER_PROPERTY_CAP = 2` rows per (tag, property).

*What "already-stored" means:* the tag's queue AND everything already DECIDED for the tag.
The second arm is not optional — the 1,440 human positives predate `tag_candidates`
entirely, so a queue-only history is blind to the whole of today's ground truth, and the
byte-identical twin of a stored positive would be queued, labeled again, and inflate the
head by exactly the amount the rail exists to prevent. It mirrors the pool query's own
`NOT EXISTS ... image_tag_labels` exclusion, which keys on `image_id` and so cannot see a
twin under a different id. Both arms are bounded at `PHASH_HISTORY_MAX`; the label arm
orders human decisions first, so the bound sheds migration 442's manufactured rows before
it sheds a real one. *Catches:*
the same photo reused across agencies, and re-encodes/rescales. *Does not catch:* the same
room from a different angle (that is the per-property cap's job, not the hash's), the same
real property listed on two portals and photographed separately, and crops. *Over-catches:*
dHash collapses distinct floor plans — mostly-white documents hash alike — which is why 6
and not the dedup engine's `l2_phash_hamming_threshold = 11`: a false collapse hides a
distinct image from review permanently, a false keep costs one click. `dropped_near_dup` is
reported per draw so an over-collapsing tag is visible rather than inferred.

**Bounded and degrading honestly.** A wall-clock budget (45s, API-safe) bounds the call, and
each category's `SET LOCAL statement_timeout` is DERIVED from what is left of it, capped at
`DRAW_STATEMENT_TIMEOUT_MS = 60,000` and floored at `DRAW_MIN_CATEGORY_MS = 5,000` (below
that the category is skipped whole). A fixed 60s ceiling would not have been a bound at all:
a category starting at 44.9s of a 45s budget could still run a further 60s, so one
synchronous admin request would reach ~105s and die at the proxy on top of already-committed
categories. `scripts/draw_tag_candidates.py` passes `max_seconds=0` — the 45s default is
request-shaped, and a runner inheriting it would drop a category on every tag of an
`--all-ready` sweep.

**Degradation order is a policy, not an accident.** Categories run SMALLEST QUOTA FIRST, so
whatever the budget cuts is cut from the largest. Running in `CATEGORY_MIX` order would
always sacrifice `komercni` and `ostatni` — the two thinnest, the two the mix gives a floor
to — while guaranteeing `byt`, the category capped below its corpus share precisely to
dilute the skew. Since `category_main` is stored per row, that drift would be durable in the
table rather than merely momentary.

A cancelled category reports `status='timeout'` and the others still land; an exhausted
budget reports `skipped_budget`. Each category is three steps — score, select, insert — with
the SQL steps in their own transactions and the near-duplicate pass BETWEEN them: that pass
is pure CPython (a comparison is ~150ns, so a full `PHASH_HISTORY_MAX` compare list costs
~15ms per candidate row) and holding a transaction open across it buys nothing. Past
`PHASH_HISTORY_MAX = 50,000` rows per arm, a near-duplicate of one of the OLDEST candidates
can slip through, which costs one click where an unbounded scan would cost a request.
Concurrency is not defended against: two simultaneous draws can select the same image, the
PK plus `ON CONFLICT DO NOTHING` makes that a no-op, and the per-property cap can be
exceeded by at most one row. Single-operator platform; accepted, not fixed.

**What the repoint changes for the operator.** `sample_size` is REMOVED from the overview
payload, not repurposed: it meant "images in the one pool every tag shared", and nothing in
the new world means that. `candidate_image_count` (distinct images queued for at least one
tag) is a different quantity with a different denominator, and the per-tag
`candidate_count` / `candidate_open_count` are what the operator actually works against. A
tag with no candidates now shows an empty browse where it used to show all 1,200 sample
images — that is the correction landing, not a regression. `list_images_for_tag` UNIONs the
tag's queue with everything already decided for that tag, so the 1,440 legacy positives —
decided long before retrieval existed — stay visible instead of reading as deleted labels.
Ordering is `(drawn_at DESC, pool_rank ASC, image_id DESC)`, a total order, so the grid does
not reshuffle between refetches.

Every band and category bucket also carries its YIELD (`positive` / `negative`, derived by
the same join that derives `open`). Without it the panel could say how much work was left
but never whether the retrieval was working, and the tripwire this design names in five
places — sustained positives out of an unranked sample mean the centroid is missing a mode —
would have had no surface to fire on. The panel puts each bucket's yield in its chip title
and states the random band's outright once anything in it has been decided.

**Running it.** `POST /new-dedup/labeling/tags/{id}/candidates` (admin-gated; `drawn_by` is
never a request field, same rule as `source`) and `python -m scripts.draw_tag_candidates`
(`--all-ready --target 200`, resumable with no ledger of its own — the stored rows are the
marker). No GitHub Actions workflow: the job needs no GPU, torch, R2 or model download, so
a runner offers it nothing a terminal does not, and a scheduled top-up has no meaning until
the operator's labeling cadence is known.

**Deliberately deferred:** no `tag_candidate_runs` table (per-run yield and loss are
reported live in the draw response; only the CURRENT pool composition is persisted), no
probe-score retrieval, no hard-negative mining, no autonomy dials, no deletion.

## Explicitly deferred, not silently dropped

- The per-tag trainer. `clip-linear-probe.md` still specifies v1 as one multinomial
  head; multi-head was already anticipated there as a later step. This work is that
  step's data substrate, not the trainer itself.
- Renaming the `/new-dedup/labeling` route/prefix now that the taxonomy it manages is
  permanent, not dedup-scoped. Cosmetic; flagged, not fixed here.
- `tag_taxonomy.active` as a real non-destructive deactivation lever — the same gap
  `taxonomy_labels.active` already had (read, never set false). Pre-existing, not
  created by this work.
