# NEW DEDUP — Program plan and living progress ledger

Status: **DRAFT — awaiting operator approval.** Date: 2026-08-05.
This file is the standing home for wave status and next-session pickup instructions.
Companion: `CUTOFF.md` (surgical removal spec, approved separately).

## Mission and non-negotiables

Rebuild the dedup **decision layer** from scratch as a **simulation engine**: every level of the
new workflow computes merge/dismiss outcomes "as if", into a separate droppable schema, over the
entire database and **ignoring all legacy merge decisions**. Nothing writes to production tables
until the whole stack is approved end-to-end. Other rules:

- Operator owns ALL merge/no-merge logic; Claude never invents thresholds/weights/rules.
- Legacy code/comments/design docs (backup branch) are never consulted.
- Legacy manual decisions usable only as diagnostics, **per-case, requested in bold**, expect
  declines (operator prefers fresh manual review). Automatic legacy merges: never.
- Only dům ↔ komerční cross-category merges; sale ↔ rent never pair; same-portal pairs valid.
- Waves gate on operator confirmation; merge to main autonomously once a wave's gate passes.
- Explanatory communication style; every settings-panel knob carries a plain-language blurb.

## Decisions ledger (from the 2026-08-05 Q&A)

| Topic | Decision |
|---|---|
| Teardown | Full removal now; duplicate build-up in Browse accepted until cutover |
| Publication gate | Removed (M-0 flip first, then code+views) |
| Legacy marking | DB-only: `property_merge_events.generation='legacy'` backfill |
| Old DB objects | Queues dropped; decision ledger + golden + LLM verdict caches kept frozen |
| L0 fields | street=`street`/`street_name_key`, geo=`geom` 75 m, dispo=`disposition`, floor=`floor` ±2 (byt only), area=`usable_area` (`estate_area` for pozemek) |
| Area tolerance | **5% general, 2% pozemek** (exposed settings) |
| Clique guard | **PARKED** — operator runs separate location data-quality sessions; candidate audit ships pin/clique statistics only |
| Family semantics | First-shared-family vs waterfall = settings **toggle**, per level (pHash and embeddings separately); **default waterfall** |
| pHash | Global default ≤11 + per-tag overrides (drawing-tag risk) |
| Embeddings | DINOv2 on RunPod, **candidate-scoped** (embed only images of listings in candidate pairs), vectors in Supabase; ≥0.98 starting threshold, expect recalibration |
| RunPod | Set up in Wave 1; serverless/on-demand only, **<$1/day** run-rate; may reuse PR #804 harness |
| Vision | GPT-5-mini, manual batches only; qwen pluggable later |
| Taxonomy v1 | The operator-curated `image_training_examples` label set (49 labels: `interier -*`, `exterier -*`, `podklad -*`, standalone garáž/technické zařízení/other); "katastr" ≙ `podklad - katastrální mapa`; tag-family defaults reconfirmed at training-set finalization |
| Exact attrs (L1) | Ships inactive; calibrated only after full stack has produced a sample (Wave 7) |

## Simulation architecture (Q15-confirmed)

Schema `dedup_sim` (droppable wholesale). Two-tier recompute:

- **Evidence tier (expensive, computed once, reused across runs):** candidate pairs from L0
  (keyed by listing pair + path + inputs), and per `(pair, tag_family)` image-comparison
  evidence — best/qualifying pHash distances + pair counts, later best/qualifying DINOv2
  similarities. Evidence rows carry the image-set fingerprint so stale rows recompute when a
  listing's images/tags change. Additive columns expected as criteria evolve.
- **Decision tier (cheap, seconds-to-minutes):** a `simulation_runs` row snapshots the full
  settings JSON; decisions + as-if groups (union-find over merge edges, category guard applied)
  are materialized per run. Threshold/priority/pair-count/toggle changes → decision-tier rerun
  only. Radius/floor/area changes → candidate regeneration (hours, full corpus).
- **Retention:** statistics for all runs; browsable as-if grouping for the latest 3.
- **Compute placement:** GH Actions dispatch for evidence/candidate generation (free, existing
  ops pattern); decision-tier reruns via the API ("Run dedup simulation" button). RunPod pods
  spin up on demand for embedding batches and terminate (zero idle cost).

## Waves

Each wave: prerequisites → mechanics → audit page → testing → results → iteration → **gate**.
Session handoff points marked ⛳ (good places to end a session; update the ledger below).

- **W0 — Backup + teardown + scaffolding.** Execute `CUTOFF.md` §7 (PR-0, backup branch/tag,
  Day-0 freeze + M-0, pg_dump, PR-1/2/3). Scaffold: `NEW DEDUP` nav group (menu pattern per
  recon), placeholder Dashboard + Settings pages, `roadmap/new-dedup.md` track (+ centroid
  data-quality prerequisite entry for the operator's parallel sessions). ⛳ after each PR.
  **Gate 0: operator approves CUTOFF.md before execution; post-teardown verification checklist green.**
- **W1 — Shared prerequisites + labeling program.** `dedup_sim` schema foundation +
  `simulation_runs`; settings framework (registry + explanations); dashboard skeleton (funnel +
  cost table with estimates); RunPod account (operator) + serverless workflow (me); **Labeling
  page** = ClipAudit clone minus dedup block, plus: "new tag vs original tag" toggle, sample
  management, tag add/rename/remove + batch tooling. Secondary CLIP (stronger encoder) relabels
  growing samples over taxonomy v1 into a sim-side proposal store (never `image_clip_tags` —
  gallery-flip hazard); iterate sample until 300 proposals for ≥50% of categories, then assess
  coverage with operator. Operator confirms/dismisses into the training set. ⛳ per sample round.
  **Gate 1: 150 training images per active tag.**
- **W2 — Level 0: candidate selection.** Primary path + 2 fallbacks + byt floor rule, sim
  candidate store, **Candidate audit page** (type × path matrix; missing-field tables overall +
  per portal per type; pin/clique statistics). Recall diagnostic vs legacy manual merges only if
  granted (**bold request** at that moment). ⛳ after mechanics, after audit page.
  **Gate 2: operator satisfied no rightful candidates are lost to data quality.**
- **W3 — Linear probe + full retag.** Train probe on the gated training set (grouped splits,
  pinned encoder, versioned artifact); validate on the Labeling page; campaign-retag the corpus
  into the sim tag store. **Gate 3: operator accepts tag quality; per-type default tag-family
  orders reconfirmed against final taxonomy.**
- **W4 — Level 2: pHash.** Evidence computation over candidates; decision tier; settings
  (threshold 11 + per-tag overrides, pairs required =1, family toggle, drag-priorities per
  type); **pHash audit page** (side-by-side pairs, filter by type/tag/hamming/result);
  **Browse-as-if page** (BrowseExperience reduced-feature adapter over sim groups);
  **Suspicious-properties page** (concurrent price divergence; ≥N listings merged, default 6;
  best-pair-vs-next-tag divergence filter). ⛳ evidence / decisions / each page.
  **Gate 4: visual validation — no easy merges missed, no strong signal underused, threshold calibrated.**
- **W5 — Level 3: embeddings.** DINOv2 on RunPod (candidate-scoped), vectors in Supabase;
  evidence + decisions; audit A (pHash-style with similarity), B (click-an-image search),
  C (all-candidates pHash-vs-embeddings comparison); dismiss-decision validation; DINOv2 audit
  page. **Gate 5: measurable lift over pHash; similarity calibrated; dismiss confidence decided.**
- **W6 — Level 4: vision.** Batch selector (cohort filters, model routing), robust prompt +
  3-outcome contract, decision counts per type (all operator-editable in settings); results/
  stats/cost page; manual-review queue + notes (sandbox — never read by Claude);
  velocity-based daily cost + review-volume projections.
  **Gate 6: <$1/day projected (ex-backlog) AND ≤50/month to manual review (target <20).**
- **W7 — Level 1: exact attributes.** Analyze accumulated sim outcomes; operator defines the
  filter; activate toggle. **Gate 7: operator approves the filter.**
- **W8 — End-to-end approval + production wiring.** Full-corpus simulation signed off →
  productionize: engine writes real merges through `merge_properties` (generation `v2`),
  backlog + steady-state scheduling, monitoring/health, re-feed of everything undecided,
  Browse-as-if retired, `dedup_sim` dropped, docs/skills/CLAUDE.md updated.
  **Gate 8: operator orders the real run.**

## Parked / open items

- Clique guard (operator location-DQ sessions running in parallel; revisit before W2 gate).
- Embedding-search candidate rung for poor-geo listings (evaluate after W5).
- Qwen vision provider route (W6).
- Near-duplicate training labels flagged 2026-08-05 (operator cleanup via batch reassign).
- Interim unmerge has no UI home (API-only) until W8.
- CLAUDE.md "psql" guidance inoperable in cloud-only mode — fix text in W0 docs pass.

## Progress ledger (update every session, newest first)

- 2026-08-06 (session continuation, part 3) — Built the Labeling page (W1's last unstarted
  mechanic besides the Dashboard skeleton and RunPod, both separately blocked).
  - **Migration 373** (`dedup_sim_labeling`) applied live via MCP: `dedup_sim.taxonomy_labels`
    (the operator-curated Taxonomy v1 vocabulary — free text, add/rename/remove; deliberately
    NOT pre-seeded with the ledger's "49 labels" description, since PROGRAM.md itself flags that
    exact list as unfinalized and operator-owned), `dedup_sim.labeling_sample` (which images are
    in scope for the relabel job), `dedup_sim.label_proposals` (one row per (image, model): what
    the secondary CLIP proposes, `pending`/`confirmed`/`dismissed`). All three backend-only (no
    `_public` view), matching migration 372's settings/simulation_runs precedent. Every join/
    upsert/cascade statement verified live via `EXPLAIN` against the real schema before landing.
  - **`toolkit/dedup_sim_labeling.py`**: taxonomy CRUD (add; rename cascades to every
    `image_training_examples` + `label_proposals` row under the old text in one transaction;
    remove purges both, images untouched — mirrors `api/labeling.py`'s
    `delete_training_label` "images stay" semantics), `grow_sample` (newest not-yet-sampled
    images, optional category filter), proposal review (`list_proposals`,
    `confirm_proposal` — upserts into `image_training_examples`, the ONLY path that promotes a
    sim-side proposal into the real confirmed store, never `image_clip_tags` — gallery-flip
    hazard — `dismiss_proposal`, plus `bulk_confirm_proposals`/`bulk_dismiss_proposals` for the
    review queue's batch action). 40 hermetic tests (hand-rolled in-memory SQL dispatcher, since
    the queries are multi-table joins/cascades the simple key-value fake `dedup_sim_settings`
    tests use couldn't model).
  - **3 new registry settings** (`toolkit/dedup_sim_settings.py`, new `Category.LABELING`):
    `labeling_secondary_model` (text, default `openai/clip-vit-large-patch14`, decided=False —
    a starting pick pending calibration), `labeling_target_proposals_per_category` (300) and
    `labeling_gate1_target_per_tag` (150) — both `decided=True` since PROGRAM.md's own text
    states these numbers verbatim (the 300-proposal sample-widening target and the Gate 1
    criterion), not invented here.
  - **Secondary CLIP encoder — new, separate infra from the DINOv2/RunPod embeddings path**:
    `scraper/label_proposal_tagger.py` (a self-contained `ProposalTagger`, deliberately NOT a
    change to the production `scraper/clip_tagger.py` — zero risk to the live gallery tagger —
    zero-shot against whatever labels are currently active in `taxonomy_labels`, simple "a photo
    of {label}" prompts, no fine/logical collapse layer since proposals are flat single-label).
    `scripts/label_proposal_backfill.py` mirrors `clip_tag_backfill.py`'s shape (R2 download,
    sharded, chunked) but selects from `labeling_sample` minus already-proposed-for-this-model
    images. `.github/workflows/label_proposal_backfill.yml`, dispatch-only (2-way shard, smaller
    than the production 4-way — this is a curated sample, not the full corpus), needs the same
    R2 secrets as `clip_tag.yml`. No RunPod involved — this is a bigger CLIP checkpoint on CPU,
    unrelated to Wave 5's DINOv2-on-RunPod plan; don't conflate the two "secondary encoder"
    mentions in the program.
  - **API**: `api/new_dedup_labeling.py` (new file, mirrors the existing `api/labeling.py` vs
    `api/property_merge.py` one-file-per-concern split rather than growing
    `api/routes/new_dedup.py`), mounted at `/new-dedup/labeling/*`, admin-gated. 18 hermetic
    route tests (toolkit functions monkeypatched, so this layer only proves status codes +
    error-mapping, not SQL — that's the toolkit test file's job).
  - **Frontend**: `frontend/src/pages/NewDedupLabeling.tsx` + nav/route wiring
    (`routes.tsx`/`Shell.tsx`, third NEW DEDUP item after Dashboard/Settings) + ~150 lines of new
    `api.ts` functions/types. Structure: a Taxonomy v1 coverage strip (per-label
    confirmed/pending/dismissed counts + a Gate-1 progress bar, inline rename, two-step-confirm
    remove, add-label form) above a sample-management panel (size + grow-by-N-images form) above
    the proposal review grid (status tabs, a "New tag"/"Original tag" toggle that swaps the
    `ImageTagBadge` between the proposal's label and the image's live `clip_fine_tag` for visual
    comparison, per-tile confirm/dismiss, and a batch select-all + bulk confirm/dismiss bar
    scoped to the CURRENT `labeling_secondary_model` — an older model's leftover pending rows
    review one at a time only). Investigated first via a 4-way parallel research pass (ClipAudit's
    full structure — turned out to have **no dedup-pair UI to subtract**, the whole file is
    already single-image labeling; the `/labeling/*` schema family; the CLIP pipeline + taxonomy
    landscape — confirmed the "49 labels" vocabulary exists nowhere in code, design-doc-only;
    the `NewDedupSettings.tsx` wiring pattern) before writing any page code, so the page reuses
    established components (`FilterChip`-style toggles, `Tabs`, `ImageTagBadge`, the
    `fetchImagesByImageIds`/`imageSrc` Supabase-read pattern) rather than reinventing them. 10
    new vitest tests. `tsc --noEmit` clean, production `vite build` clean.
  - Full suite green: `pytest -q` 2612 passed (up from 2568 at session start); `vitest run` 390
    passed; `tsc --noEmit` clean; both codegen checks OK (`generate_workflow_docs.py` needed a
    regen for the new GH Actions file, `generate_filter_registry.py` already matched).
  - Not done, deliberately: the operator still has to run several `grow_sample` +
    `label_proposal_backfill.yml` + review rounds to actually reach Gate 1 (150 confirmed images
    per active tag) — this session shipped the tool, not the labeling itself. Dashboard skeleton
    stays a placeholder (same "no data yet" reasoning as prior sessions). RunPod is unrelated to
    this work (Wave 5 only). Not built: a way to deactivate a taxonomy label without hard-deleting
    it (`taxonomy_labels.active` is read by the backfill's label selector but nothing ever sets it
    false — the only lever today is the destructive DELETE, which cascades away confirmed training
    examples). Flagged by the review pass below as a real but low-severity gap; a follow-up PR if
    the operator hits it in practice, not addressed now to avoid unbounded scope growth.
  - **Adversarial review before merge** (5-dimension parallel pass — backend correctness, security/
    migration, the CLIP pipeline, frontend correctness, test quality — each finding independently
    re-verified by a second agent against the actual code): 16 findings, all 16 confirmed real
    on verification, all fixed same-session:
    - **High**: `confirm_proposal`/`dismiss_proposal` had no `status = 'pending'` guard (unlike
      their bulk siblings) — a stale/retried dismiss after a confirm would flip
      `label_proposals.status` without ever retracting the `image_training_examples` row the
      confirm had already written, silently diverging the two stores. Fixed: both now require
      `status = 'pending'` and 404 otherwise, exactly like the bulk functions.
    - **High**: the Labeling page's rename handler switched the active proposals filter to
      *whatever label was just renamed* rather than checking it was the SAME label being
      filtered — renaming an unrelated taxonomy row silently hijacked the operator's filter.
      Fixed by threading the pre-rename label text through the mutation and comparing it, not
      just checking "is some filter active".
    - **High**: the new `label_proposal_backfill.yml` GH Actions workflow interpolated
      `workflow_dispatch` string inputs directly into the shell `run:` block via `${{ }}` — a
      classic GH Actions script-injection surface (a `"` in the input breaks out of the quoted
      arg string), reachable by anyone who can dispatch the workflow, with every R2/DB secret in
      scope. Fixed: inputs now pass through `env:` and are referenced as quoted shell variables.
    - **Medium**: `grow_sample`'s SQL used a plain `JOIN` from `listings` to `properties`, so an
      image whose listing hasn't had a `properties` row attached yet (rule #19/#20: new rows land
      `property_id` NULL until the incremental maintenance cron runs) was silently excluded even
      with no category filter — contradicting the "newest not-yet-sampled images" contract. Fixed
      with a `LEFT JOIN`.
    - **Medium**: the secondary-CLIP tagger's confidence is a softmax over the active taxonomy
      labels — mathematically always exactly 1.0 when only one label is active (the realistic
      bootstrap state right after the operator adds their first label), making the confidence
      column meaningless exactly when an operator might lean on it most to triage a bulk-confirm.
      Fixed: falls back to raw cosine similarity (not softmax-normalized) when there's only one
      active label.
    - **Medium**: the page's confirm/dismiss mutations were one shared `useMutation` instance for
      the whole grid — TanStack Query's observer only reflects the most-recently-clicked tile's
      `isPending`/`variables`, so clicking Confirm on a second tile made an earlier still-in-flight
      tile's buttons visually re-enable, opening a real race (confirm and dismiss in flight for the
      same proposal at once). Fixed with a local per-image-id pending set, independent of which
      mutation call is "current".
    - **Medium** (×4, test-quality): `PUT /taxonomy/{id}`'s 422 path, `POST /proposals/dismiss`'s
      404 path, and `POST /proposals/bulk-dismiss`'s 422 path were untested at the route level
      (asymmetric with their tested siblings); the page's mutation tests only asserted API-call
      args, never that `invalidateQueries` actually fired or that the UI reflected it. All four
      closed — the three route tests added, and the confirm/dismiss/bulk-confirm page tests now
      also assert the proposals grid actually empties after the refetch.
    - **Low** (×3): the fake-conn test double for `INSERT ... ON CONFLICT DO UPDATE SET label,
      updated_at` was overwriting `created_by` on every call, diverging from Postgres' real
      partial-column update (fixed, + a regression test); `scripts/label_proposal_backfill.py` had
      zero tests unlike its production analogue `clip_tag_backfill.py` (added
      `tests/scripts/test_label_proposal_backfill.py`, same shape as the existing
      `test_clip_tag_backfill.py`); the GH workflow's cache-key comment claimed "a model swap just
      costs one cold cache fill", which is false (the static key never varies with the
      operator-tunable `labeling_secondary_model` setting, so `actions/cache`'s immutable-key
      behavior means a swap re-downloads on *every* run, forever) — comment corrected to state the
      actual (accepted, CI-cost-only) behavior rather than engineer a fully dynamic cache key for a
      low-severity, non-correctness issue.
    - Full re-run after all fixes: `pytest -q` 2626 passed (up from 2612), `vitest run` 392 passed
      (up from 390), `tsc --noEmit` clean, `vite build` clean, both codegen checks OK.
  - Next session: once the operator starts labeling rounds, watch for real usage friction (is the
    batch-review flow fast enough, does the coverage strip's progress read clearly); revisit W0's
    PR-3 (still the only blocker on Gate 0) if it hasn't landed by then; consider a non-destructive
    "deactivate a taxonomy label" affordance if the hard-delete-only gap above turns out to matter
    in practice.
- 2026-08-06 (session continuation, part 2) — With PR #965 merged and PR-2's minimal nav
  placeholder confirmed live, continued W1: made the Settings page real.
  - **Backend**: `api/routes/new_dedup.py` — `GET /new-dedup/settings` (full registry +
    effective values in one call), `PUT /new-dedup/settings/{key}` (validated write, 400 on a
    bad type/range/enum, 404 on an unknown key), `DELETE /new-dedup/settings/{key}` (drop the
    override, revert to the registry default). Admin-gated (`require_admin`), mounted in
    `api/main.py` alongside the other split-out routers. 7 new hermetic tests
    (`tests/api/test_new_dedup_routes.py`), including the 401-without-admin gate.
  - **Frontend**: `frontend/src/pages/NewDedupSettings.tsx` replaces PR-2's placeholder — all 12
    registry settings grouped by category (L0-L4 + general), each card showing the
    plain-language explanation, a `not yet calibrated` tag (ochre — this app's existing
    low-confidence/pending semantic, e.g. `EstimationList`'s confidence badge,
    `BuildingDetail`'s `awaiting_input` status) for the two settings the ledger flagged as
    calibration-pending, and an `edited` tag + "reset to default" once overridden. Controls are
    type-aware: a toggle switch for the boolean (mirrors `Settings.tsx`'s `FilterCell`), a
    number input with min/max + explicit Save once dirty, a native `<select>` for the two
    family-semantics enums (native select is this codebase's established pattern —
    `Watchdog.tsx`/`ListingMap.tsx`/others — not the interface-design skill's generic custom-
    dropdown guidance, since this is one page inside an already-coherent app, not a new
    product). Reused `frontend/src/lib/api.ts`'s existing `request()` + react-query mutation
    pattern (`AppSettingRow`'s shape) rather than inventing a new one. 6 new vitest tests.
  - Verified against the app's EXISTING design tokens/components (no `.interface-design/
    system.md` in this repo; treated `Settings.tsx`'s established patterns as the de facto
    system rather than running a fresh domain-exploration pass, since this is one page inside
    an existing coherent product, not a new one).
  - Full suite green: `pytest -q` 2568 passed; `tsc --noEmit` clean; `vitest run` 380 passed;
    both codegen checks (`generate_filter_registry.py`, `generate_workflow_docs.py`) OK.
  - Not done: the Dashboard skeleton (funnel + cost table) — genuinely has no data to show yet
    (candidates/evidence/decisions are W2/W4/W5); building more than PR-2's placeholder there
    now would be speculative. RunPod serverless workflow — still no `RUNPOD_API_KEY` secret.
- 2026-08-06 (session continuation) — Operator confirmed the freeze + PRs were landing and asked
  to check back in 5-10 minutes. Re-verified live state after the wait: **Day-0 freeze and M-0
  are both actually done now** (`dedup_publication_gate_enabled=false`,
  `realtime_dedup_interval_seconds=0`; the 6 legacy workflow IDs from the entry below no longer
  resolve via the GH API at all — PR-1 deleted the files outright rather than merely disabling
  them, which supersedes "disabled"). **PR-1 (#966) and PR-2 (#967) are both merged**
  (2026-08-06T05:12/05:14), landed by a parallel session that also finished the CUTOFF §6 doc
  pass this ledger flagged as missing (verified: PR-1's diff touches CLAUDE.md,
  architecture.md, both skills, and the legacy design-doc deletes). That session's own ledger
  entry (immediately below) landed as #968 before PR-1/PR-2 merged, then PR-1/PR-2 landed later
  once M-0 was applied — the entry order below is chronological-as-written, not
  chronological-as-true; treat this entry as the current source of truth. **PR-3 (teardown
  migration: table drops + view redefinition + legacy generation stamp) has NOT landed** — the
  only piece left before Gate 0's post-teardown verification checklist can go green.
  - Merged `origin/main` into this session's W1 branch (`feature/new-dedup-w1-foundation`,
    PR #965): one real conflict, in `ROADMAP.md`'s NEW DEDUP row (both this branch and #968 had
    edited it independently) — resolved by combining both: legacy-removed status from #968's
    wording + the W0+W1-parallel note from this branch's wording. `docs/design/new-dedup/
    PROGRAM.md` merged clean (this entry's insertion point didn't overlap #968's rewritten
    entry). Full suite re-run green post-merge; pushing and merging #965 now that CI is clean.
  - PR-2 already shipped "a minimal NEW DEDUP nav placeholder (Dashboard + Settings stub
    pages)" per its own description — checking that before building W1's dashboard skeleton
    further, to build ON it rather than duplicate it.
  - Continuing W1: with PR-2 landed, the dashboard skeleton + Labeling page are now unblocked
    (the nav-territory reason for holding them is gone).
- 2026-08-06 (later same day) — Operator asked whether to start W1 in parallel with the still-
  in-progress W0. Verified LIVE state instead of trusting the entry below (it undersold how far
  W0 had actually gotten in places, overstated it in one):
  - Day-0 freeze and M-0 both **still not done** — confirmed live (all 6 legacy decision
    workflows still `active` on GitHub; `dedup_publication_gate_enabled=true`;
    `realtime_dedup_interval_seconds=90`). Attempted both again (`gh api .../disable` on all 6
    workflow IDs, the two `app_settings` UPDATEs via Supabase MCP) — **blocked by the permission
    classifier again**, same as last session. Needs the operator to run these two directly or
    grant permission; exact commands were relayed to the operator in-chat this session.
  - pg_dump backup (CUTOFF.md §4) — **actually done**, contrary to the "not yet started" note
    below: workflow run `31052835193` succeeded 2026-08-05T22:27:48Z, after the pg_dump-17 fix
    (#964) landed.
  - **PR-1 (backend removal) branch — further along than this ledger said, but not opened.** The
    worktree (`feature/new-dedup-backend-removal`) is correctly based on PR-0's merge commit
    (`9d1eb177`, verified via `git merge-base` against `origin/main` — first miscalculated this
    against a stale *local* `main` ref, caught before reporting it, see
    [[worktree-absolute-path-stale-branch-hazard]]). Diff vs `origin/main`: 77 files,
    +179/-20,776, matching CUTOFF §1/§2 exactly. The ~8 decision-side test file deletes/edits and
    the S5 split (`api/property_merge.py` + `api/labeling.py`, wired into `api/main.py`) are done
    and staged. **Missing entirely: CUTOFF §6's doc pass** (CLAUDE.md rule 15, architecture.md
    §15, the `scraper-ops`/`llm-pipelines` skills, deleting the ~7 legacy design docs) — per
    CLAUDE.md's own same-PR-doc-update rule this needs to land before PR-1 opens for real review.
    Not pushed/opened this session (would need the doc pass first); left as-is for a future
    session or the operator's own continuation.
  - **W1 backend slice started** (operator chose this over finishing W0's blockers, in a fresh
    worktree/branch `feature/new-dedup-w1-foundation` off current `origin/main`, unrelated to the
    PR-1 branch above):
    - **Migration 372** (`dedup_sim_foundation`) applied live via MCP: schema `dedup_sim`
      (droppable wholesale, Wave 8) with `settings` + `settings_history` (override-only,
      mirrors `filter_registry.py` + `filter_visibility` migration 059 — a missing row means
      "use the code registry's default," so a later wave's new setting needs no migration) and
      `simulation_runs` (decision-tier run bookkeeping, mirrors `estimation_runs` shape,
      migration 010). Evidence-tier tables (candidates, pHash/embedding evidence) are NOT part
      of this migration — each lands with the wave that needs it (W2/W4/W5).
    - **`toolkit/dedup_sim_settings.py`**: the registry half — 12 `SettingDef`s, one per value
      PROGRAM.md's 2026-08-05 Q&A ledger already decided (L0 radius/floor/area tolerances, pHash
      threshold + family-semantics toggle, embeddings threshold + family-semantics toggle,
      RunPod cost cap, vision model + manual-batch-only toggle, L1 exact-attrs OFF), each with a
      plain-language `explanation` (mission non-negotiable) and a `decided` flag (false on the
      two the ledger flagged as starting points pending later-wave calibration: embeddings
      threshold, L1 enabled). No new thresholds invented — every default traces to the ledger.
      `effective_value`/`effective_settings`/`update_setting`/`reset_setting` CRUD, validated
      against each setting's declared type/range/enum. 20 hermetic tests, all passing.
    - **Fixed a latent CI gap** this migration exposed: `tests/test_migration_rls_grants.py`'s
      table-name regexes assumed every table lives in `public` (or unqualified) — `dedup_sim.*`
      broke both the created-table capture (truncated to just `dedup_sim`) and the
      RLS-enabled-table capture (schema-qualified name didn't match `\s+enable row level
      security` right after the truncated capture), producing a false "table dedup_sim never
      gets RLS" failure despite RLS being correctly enabled on all 3 real tables. Fixed the
      regex to handle a non-`public` schema prefix generically; full suite green (3089 passed).
    - **RunPod serverless workflow — not built, and deliberately so.** No `RUNPOD_API_KEY` (or
      similarly named) secret exists in GH Actions yet — the operator's RunPod account (W1's
      "(operator)" half) hasn't been created. Building pod-orchestration code now, with nothing
      real to call it against (candidate-scoped embedding computation is Wave 5) and no way to
      test it, would be exactly the kind of speculative scaffolding CLAUDE.md's conventions warn
      against. Reuse target once the account exists: PR #804's `scripts/embedding_gpu_bench.py`
      (`download_images`/`embed_images` pattern, presigned-URL manifest, no repo imports) — that
      harness is a bake-off tool (reads the legacy `dedup_label_events` golden set, which
      CUTOFF.md §4 drops), so its DATA SOURCE isn't reusable, but its POD-SIDE MECHANICS are.
    - **Dashboard skeleton + Labeling page — deliberately held**, not started: both sit in the
      same nav territory the ledger already decided to defer to ride with PR-2 (avoids building
      UI that PR-2's frontend removal immediately restructures); same reasoning extends from the
      dashboard (explicitly deferred below) to the Labeling page (not explicitly said before, but
      identical logic).
  - Next session: get the operator to unblock Day-0 freeze + M-0 (or grant permission), finish
    PR-1's CUTOFF §6 doc pass and open it as a draft PR, then continue W1 (candidate-store-
    adjacent settings will grow the registry once W2 starts; RunPod once the account exists).
- 2026-08-06 — W0 execution started (operator kickoff: "start with the dedup workflow refactor
  based on program.md and cutoff.md" = Gate 0 approval). Done this session:
  - **PR-0 merged** (#960, commit `9d1eb177`) — clip-linear-probe.md + this PROGRAM.md/CUTOFF.md
    pair landed on `main`. Note: the branch CUTOFF.md's §7 step 1 named
    (`feature/clip-audit-tag-management`) had already merged its own content separately via #954
    before this session started; PR-0 was re-cut as a fresh docs-only branch off current `main`.
  - **Backup cut**: branch `backup/pre-new-dedup-2026-08` + tag `backup-pre-new-dedup`, both at
    `9d1eb177` (post-PR-0 main). Supabase DB confirmed live and unmodified at
    **2026-08-05 21:55:22 UTC** (`select now()` reading taken before any Wave 0 DB write was
    attempted) — use this as the PITR reference point.
  - **Scaffolding merged** (#961) — `roadmap/new-dedup.md` track created, `ROADMAP.md` index
    updated, old `roadmap/dedup-track.md` marked superseded.
  - **pg_dump-to-R2 backup done and verified** (#963, fix #964) — a `workflow_dispatch`-only GH
    Actions job (`scripts/backup_new_dedup_teardown_tables.py`) dumped all 9 CUTOFF.md §4
    "Drop"-list tables/matviews to R2 under `backups/new-dedup-teardown/2026-08-05/`. First run
    failed on every table (`pg_dump` 16 vs. server 17.6 — "aborting because of server version
    mismatch"); fixed by pulling `postgresql-client-17` from the PGDG apt repo; re-run succeeded
    9/9 (largest: `property_identity_candidates`, 4.4 MB gzipped / ~158k rows). Satisfies the
    destructive-migration safety net ahead of PR-3.
  - **PR-1 (backend removal) — done, draft, CI green, mergeable**: [#966](https://github.com/waiff/sreality/pull/966),
    branch `feature/new-dedup-backend-removal`. Ran as a 12-stage background workflow covering
    CUTOFF.md §1 (C1/C2/C3/C6/C7 + C5 code-only + C4 workflow-YAML), §2 (wholesale deletes +
    S3/S5/S6 splits), §6 (backend docs/tests) — 120 files, +1,441/−28,772, 2,541 tests passing.
    Its own verification pass flagged one real blocker pre-fix-up (stale
    `workflowDocs.generated.ts`, since fixed) plus several non-blocking gaps (stale comments
    citing the removed engine, a few dead links to deleted design docs, one pre-existing dead
    threshold in `verify_pipeline.py` — none are regressions or build-affecting, left as
    follow-up cleanup rather than another fix round). Picked up a real merge conflict against
    `main` after the scaffolding/backup PRs landed (ROADMAP.md's Dedup row, `roadmap/dedup-track.md`'s
    superseded banner — both had been touched independently on both sides); resolved by a
    follow-up agent, merge commit `f99fb819`, now `mergeable: MERGEABLE` / `mergeStateStatus: CLEAN`.
    New backend route shapes it landed (ground truth for PR-2 and any future caller):
    `POST /properties/merge`, `GET /properties/merges`, `POST /properties/merges/{id}/unmerge`,
    `GET /properties/merged`, `POST /properties/assets/{link,unlink}` (all in
    `api/property_merge.py`, mounted at `/properties`), plus label/annotation CRUD moved to
    `api/labeling.py` under `/labeling/*` (image-annotation, phash-note, training-example(s),
    border-case). The old `/dedup/*` router is gone entirely.
  - **PR-2 (frontend removal) — done, draft, CI green, mergeable**: [#967](https://github.com/waiff/sreality/pull/967),
    branch `feature/new-dedup-frontend-removal`, **stacked on PR-1** (base branch
    `feature/new-dedup-backend-removal`, not `main` — it depends on PR-1's route renames). 62
    files, +240/−11,716, tsc/vitest/build all green. Repoints every Browse `mergeMode` /
    labeling-CRUD caller in `lib/api.ts` from the old `/dedup/*` paths to the new ones (spot-
    verified against the diff — matches the ground-truth route audit exactly), deletes the
    decision-layer pages/components/libs, trims the dedup sections out of Settings/Health/Costs/
    ListingDetail, and adds a minimal NEW DEDUP nav placeholder (Dashboard + Settings stub pages,
    real content comes in Wave 1). `/clip-audit` and all its labeling widgets
    (TrainControl/LabelCombobox/NoteFlagControl/ImageTagBadge/RenderBadge/ImageLightbox) are
    untouched. Non-blocking gaps from its own verification pass (a handful of now-orphaned
    `api.ts`/`queries.ts` exports, stale comments citing deleted backend modules, one unused
    `dedup_eligible_pct` type field) — cosmetic, left for follow-up cleanup, not a merge blocker.
  - **Still blocked by the permission classifier this session** (both explicitly part of
    CUTOFF.md §7's Day-0 freeze, both need the operator to either run them directly or grant
    permission — see the ask below):
    1. `gh workflow disable` on the 6 legacy decision workflows (dedup_engine.yml,
       dedup_batches.yml, dedup_model_compare.yml, clip_trial.yml, embedding_ab.yml,
       validate_render_detection.yml). Note: PR-1 already deletes 4 of these 6 outright
       (dedup_engine/dedup_batches/dedup_model_compare/validate_render_detection) plus
       clip_trial/embedding_ab — so once PR-1 merges, disabling becomes moot for those files;
       the gap is only the WINDOW between now and that merge.
    2. The M-0 DB flip itself (`update app_settings set value='false'::jsonb where
       key='dedup_publication_gate_enabled'`) via Supabase execute_sql. **Confirmed live current
       value: `true`** — the gate is actively hiding un-evaluated new properties from Browse/map/
       watchdogs right now. The worker dedup lane is also confirmed live at
       `realtime_dedup_interval_seconds = 90` (NOT dark) — same freeze dependency.
    Net effect: the legacy engine + its scheduled jobs are still fully live; duplicates are
    accumulating per the accepted Q1 tradeoff. **PR-1 must not merge until M-0 actually lands**
    (PR-1 deletes the only code that stamps `properties.published_at`; merging while the gate is
    still `true` would hide every new property with no self-heal). **PR-1 and PR-2 should then
    merge back-to-back** (PR-2 already stacked correctly for this).
  - **Operator ask to unblock the rest of W0**: either (a) run the `gh workflow disable` x6 and
    the M-0 SQL update yourself (exact statement above), or (b) grant this session permission to
    do so. Once M-0 is applied, PR-1 → PR-2 can merge in sequence; PR-3 (the actual teardown
    migration + view redefinition) is separately gated on your explicit OK per CLAUDE.md rule 1,
    independent of this.
  - Next session: get the two blockers resolved; merge PR-1 then PR-2; watch the post-merge
    verification checklist (CI green, a brand-new property visible in Browse without a stamp,
    manual merge/unmerge end-to-end, scrape/image/tag lanes unaffected, Health page clean); only
    then draft PR-3 (migration) — it stays separately gated regardless of Gate 0's status.
- 2026-08-05 — Program + cutoff drafts written; awaiting operator approval of both.
