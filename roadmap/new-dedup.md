> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

# NEW DEDUP — ground-up decision-layer rebuild

Full plan: [`docs/design/new-dedup/PROGRAM.md`](../docs/design/new-dedup/PROGRAM.md) (mission,
decisions ledger, simulation architecture, Wave 0-8 plan — the standing home for wave status).
Surgical removal spec: [`docs/design/new-dedup/CUTOFF.md`](../docs/design/new-dedup/CUTOFF.md).

This track **supersedes** [`roadmap/dedup-track.md`](dedup-track.md) — that file documents the
legacy engine's incremental development and is retained only as history; do not resume work
there.

## Why a rebuild instead of another iteration

The legacy dedup decision engine (candidate discovery → pHash/CLIP cosine → forensic vision
compare, `toolkit/dedup_engine.py` + friends) accumulated years of incremental patches and is
being torn out entirely (2026-08-05 operator directive) rather than patched further. The
replacement is built as a **simulation engine first** — every level computes merge/dismiss
outcomes "as if," over the whole database, into a droppable schema — with nothing writing to
production tables until the full stack is approved end-to-end. See PROGRAM.md's "Mission and
non-negotiables" for the full rationale.

## Status

🟡 **W0 nearly done** (Wave 0 — backup + teardown + scaffolding: legacy engine removed, only the
teardown migration + verification checklist left), **W1 in progress in parallel** (Wave 1 —
shared prerequisites). See PROGRAM.md's progress ledger for session-by-session detail; this file
tracks only the phase-to-phase status.

- [x] PR-0: design docs landed (#960).
- [x] Backup branch `backup/pre-new-dedup-2026-08` + tag `backup-pre-new-dedup` cut.
- [x] Day-0 freeze (the 6 legacy decision workflow files were deleted outright by PR-1, which
      supersedes "disabled").
- [x] M-0 (`dedup_publication_gate_enabled=false`, `realtime_dedup_interval_seconds=0` — both
      confirmed live).
- [x] pg_dump backup of to-be-dropped tables to R2.
- [x] PR-1 (backend decision-layer removal) — merged (#966), including the CUTOFF §6 doc pass.
- [x] PR-2 (frontend decision-layer removal) — merged (#967), shipped a minimal NEW DEDUP nav
      placeholder (Dashboard + Settings stub) that W1 is now filling in.
- [ ] PR-3 (migration: table drops + view redefinition + legacy stamp) — not started.
- [ ] W0 verification checklist green → Gate 0 closes (blocked only on PR-3 now).

W1 (shared prerequisites + labeling program):
- [x] `dedup_sim` schema + `settings`/`settings_history`/`simulation_runs` (migration 372, #965).
- [x] Settings registry (`toolkit/dedup_sim_settings.py`), 12 knobs seeded from the decisions
      ledger, each with a plain-language blurb (#965).
- [x] Settings API (`api/routes/new_dedup.py`, admin-gated) + the real NEW DEDUP Settings page
      (`frontend/src/pages/NewDedupSettings.tsx`, replaces PR-2's placeholder) — operator can now
      review/tune every decided default ahead of any wave consuming it.
- [ ] Dashboard skeleton (funnel + cost table) — still just PR-2's placeholder; genuinely no data
      to show until W2+ produces candidates/decisions. Revisit once W2 lands.
- [x] Labeling page, tri-state rework (docs/design/tag-annotation-matrix.md, 2026-08-26) — the
      taxonomy and the confirmed ground truth are now PERMANENT tables outside `dedup_sim`:
      `tag_taxonomy` + `image_tag_labels` (migration 442, one positive/negative/excluded row per
      (image, tag) — every independent per-tag classifier head's future training set), promoted
      because `dedup_sim` is planned to drop wholesale at Wave 8 and a real surrogate key replaces
      the old text-keyed rename cascade. `dedup_sim.labeling_sample`/`label_proposals` stay as the
      transient machine-suggestion queue (`toolkit/dedup_sim_labeling.py`). `/new-dedup/labeling/*`
      API + `frontend/src/pages/NewDedupLabeling.tsx`: tag-centric batch review of proposals by
      default (a tri-state control replaces Confirm/Dismiss), a Sample browse mode that reaches
      every image in the pool for one tag/state (including ones no model ever proposed — answers
      "show me every image where kitchen = excluded"), an image-centric detail panel for the
      multi-tag-on-one-photo case, keyboard shortcuts (arrows/j-k + 1/2/3, none existed before),
      and per-tag positive/negative/excluded counts. **ClipAudit retired outright** (frontend page,
      `TrainControl`, and every backend route/table exclusive to it — `image_tag_annotations` and
      `phash_pair_notes` had zero live callers already); `image_training_examples` is superseded
      but not yet dropped (separately-gated destructive migration). "Border case" (#1113) is
      unaffected — still a whole-image flag orthogonal to any tag's state, still excluded from
      Gate 1 the same way. Secondary-CLIP scoring (`scraper/label_proposal_tagger.py` +
      `scripts/label_proposal_backfill.py`, dispatch-only GH Actions workflow) is separate infra
      from the DINOv2/RunPod embeddings path below — a stronger CLIP checkpoint, CPU, no RunPod
      dependency. Operator still needs to run several labeling rounds to reach Gate 1 (150 positive
      images/tag) — the tool is built, the labeling itself is ongoing curation work. The per-tag
      trainer itself (docs/design/clip-linear-probe.md) is a separate, not-yet-built follow-up.
      Follow-up same day: every tile shows its image's already-assigned tags in one batched call
      (`list_positive_tags_for_images`), and "Modify labels" gained two operator flags — `priority`
      (pins + reddens a tag needing attention) and `ready_for_training` (migration 443).
- [x] Tag definitions store + operator workbench (migration 446, `tag_definitions`) — each tag now
      gets a WRITTEN definition (means / counts / does_not_count / confusable_with + the visual
      tell / leave_out_when / example images), versioned supersede-never-overwrite with no drafts:
      one Save = one new active version, the previous one flipped to `superseded` in the same
      transaction, exactly one active per tag enforced by a partial unique index. Every save states
      the version it was written against (`base_version`) and is refused when that is no longer the
      active one, so a stale second tab cannot silently revert a definition. Other tags are
      referenced BY ID inside the versioned JSONB document (a rename can't rot a definition) and
      resolved to labels leniently on read. `toolkit/tag_definitions.py` + seven routes on
      `/new-dedup/labeling/*` + the `NEW DEDUP · Taxonomy` page (`/new-dedup/labeling/taxonomy`):
      tag list with a `v{n}` status chip, the editor, a gallery of what the tag ACTUALLY contains
      (read straight from `image_tag_labels`, no `dedup_sim` dependency), and CLIP-centroid overlap
      evidence (`nearest_tags`, cosine DISTANCE, min 5 embedded positives). **Writing the ~51
      definitions is now the operator-side blocker**: labeling at scale and per-tag heads both wait
      on them, and the definitions are also the diagnostic that settles the taxonomy — two tags
      whose does_not_count lines can't be written apart are one tag.
- [x] Annotation provenance + append-only history (migration 446) — every `image_tag_labels`
      cell now records WHO decided it (`source`: human / human_confirmed / machine /
      backfill_442), under WHICH `tag_definitions` version (`definition_id`, resolved at write
      time, never a parameter), when a human last checked it (`verified_at`, derived) and — on an
      excluded cell only, CHECK-enforced — WHY (`excluded_reason`: ambiguous vs pruned). This makes
      the **72,000 rows migration 442 manufactured from a one-hot assumption** (98% of the table)
      precisely identifiable; **nothing is deleted here** — the removal is a separate, gated,
      backed-up PR keyed on `source = 'backfill_442'`. History lands in `image_tag_label_events`,
      written by a TRIGGER rather than by any of the four (soon more) write paths, because a log
      every future writer must remember to append to is a log with holes; clearing a cell back to
      untouched is itself a recorded event. "Machine proposes, human disposes" is now a SQL rail
      (the upsert's `DO UPDATE … WHERE`), not a convention. `tag_overview` gains the provenance
      inventory and a per-tag **ambiguity rate** — ambiguous exclusions over decisions, with pruned
      rows outside numerator AND denominator so pruning can't dilute the signal, both halves scoped
      to what a HUMAN decided so neither the 72,000 backfill rows nor a future flood of unreviewed
      machine rows can bury it, and NULL (never 0) when nothing is decided.
      Above `AMBIGUITY_RATE_THRESHOLD` (0.15, with a 20-decision floor) the tag's DEFINITION is the
      problem, not the labeling.
- [x] Candidate retrieval + the per-tag review queue (migration 449, `tag_candidates`) —
      the review universe stops being `dedup_sim.labeling_sample` (1,200 untargeted images,
      one pool shared by all 51 tags, 943 of them never labeled) and becomes a PER-TAG queue
      filled by CLIP centroid retrieval: rare tags are a fraction of a percent of the corpus,
      so their candidates have to be FOUND, not stumbled on. `toolkit/tag_candidates.py`
      ranks a bounded, category-stratified, per-listing-capped pool against a centroid built
      **only** from that tag's human-verified positives (migration 442's 72,000 manufactured
      negatives and unreviewed `machine` rows excluded by predicate, never by deletion — this
      creates no dependency on the gated deletion PR). Three named mixes, not magic numbers:
      rank bands 50/30/20 (a pure top-N produces prototypical heads that fail on odd cases;
      the mid band is where the measured confusion clusters live; the random band is the
      honesty rail — sustained positives there mean the centroid is missing a mode) and a
      category mix that caps `byt` BELOW its corpus share, so every sitting dilutes the
      83.8%-byt labeled-set skew instead of inheriting it. Exact-hash collapse in SQL plus a
      Hamming-6 near-dup drop and a 2-per-property cap, so a head cannot look like it has 200
      examples when it has 40. **Queue membership carries no training semantics** — no state
      column, no reviewed flag, nothing to misread: absence is not a negative, which
      overturns migration 442's ledger decision (449 restates it as a fresh table comment).
      A tag under 15 human-verified positives is told so (`status='insufficient_positives'`,
      zero rows) rather than handed a garbage pool. Every band and category bucket also
      reports its YIELD (`positive` / `negative`), so the honesty rail is something the
      operator can read rather than something the design asserts. Two admin routes + `python -m
      scripts.draw_tag_candidates` (no workflow: no GPU, no torch, no R2). `sample_size` is
      REMOVED from the overview payload, not repurposed — per-tag `candidate_count` /
      `candidate_open_count` replace it. Does NOT unblock dropping `dedup_sim`: the
      secondary-CLIP proposal lane still writes `labeling_sample` and reads
      `label_proposals`.
- [x] RunPod client (`scripts/runpod_client.py`, #972/#975/#977) — launch/poll/terminate an
      on-demand pod, live cheapest-GPU catalog lookup, capacity fallback. Guaranteed teardown
      verified across 4 real live dispatches (3 zero-cost, 1 real ~1.7¢ pod rental).
- [x] RunPod end-to-end proof — a real pod (`g39f02wj642her`, RTX 3070) launched, billed, and
      was cleanly torn down. Pod status/logs turned out unreliable for on-demand Pods (design
      note left for Wave 5, which should have its job write results to Postgres/R2 instead of
      relying on either) — not a blocker for W1's actual goal, which was proving the launch→
      bill→terminate pipeline itself works.

Waves W2-W8 (candidate selection through production wiring) are not started; see PROGRAM.md.

## Data-quality prerequisite (operator-run, parallel to the code work)

Flagged during W0 recon (2026-08-05): **122,920 listings sit on 4,206 coordinate pins shared by
>10 listings each** (town/municipality centroids used as a geocoding fallback), with the biggest
single pin holding 1,029 listings. This is a candidate-storm risk for any future geo-proximity
fallback rung (L0's third path) and degrades geo-based candidate generation generally. The
operator is investigating this in separate data-quality sessions (per-portal geocoding /
enrichment fix, cost-aware) — not blocked on or blocking the code removal above, but a
precondition for trusting L0's geo-fallback path once built. The clique guard question (whether
candidate generation needs an explicit "too many pins share this exact point" veto) is **parked**
pending that investigation; see PROGRAM.md's decisions ledger.
