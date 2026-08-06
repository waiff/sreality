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
