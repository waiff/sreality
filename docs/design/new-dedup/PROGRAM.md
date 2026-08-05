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
    attempted) — use this as the PITR reference point; no schema/data changes have landed since
    (both Day-0 freeze and M-0 are still blocked, see below).
  - **PR-1 (backend removal) launched** as a 12-stage background workflow (run id
    `wf_066de830-b20`) in an isolated worktree, branch `feature/new-dedup-backend-removal`,
    covering CUTOFF.md §1 (C1/C2/C3/C6/C7 + C5 code-only + C4 workflow-YAML), §2 (wholesale
    deletes + S3/S5/S6 splits), and §6 (backend-relevant docs/tests) — ending in a **draft PR**,
    explicitly not merged (see gating note below). Result pending as of this ledger entry.
  - Scaffolding: `roadmap/new-dedup.md` track created, `ROADMAP.md` index updated, old
    `roadmap/dedup-track.md` marked superseded. NEW DEDUP nav group / placeholder pages deferred
    to ride along with the PR-2 frontend-removal pass (same territory, avoids a throwaway page
    that PR-2 immediately restructures).
  - **Blocked by the permission classifier this session** (both explicitly part of CUTOFF.md §7's
    Day-0 freeze, both need the operator to either run them directly or grant permission):
    1. `gh workflow disable` on the 6 legacy decision workflows (dedup_engine.yml,
       dedup_batches.yml, dedup_model_compare.yml, clip_trial.yml, embedding_ab.yml,
       validate_render_detection.yml).
    2. The M-0 DB flip itself (`update app_settings set value='false'::jsonb where
       key='dedup_publication_gate_enabled'`) via Supabase execute_sql. **Confirmed live current
       value: `true`** — the gate is actively hiding un-evaluated new properties from Browse/map/
       watchdogs right now. The worker dedup lane is also confirmed live at
       `realtime_dedup_interval_seconds = 90` (NOT dark) — same freeze dependency.
    Net effect: the legacy engine + its scheduled jobs are still fully live; duplicates are
    accumulating per the accepted Q1 tradeoff, but the freeze itself hasn't landed yet. **PR-1
    must not be merged until M-0 actually lands** (PR-1 deletes the only code that stamps
    `properties.published_at`; merging it while the gate is still `true` would hide every new
    property with no self-heal).
  - **New sequencing risk identified** (not in the original CUTOFF.md §7 order): PR-1 renames
    `/dedup/properties/merge` → `/properties/merge` (+ merges/unmerge/merged/assets). Browse's
    live `mergeMode` calls the old paths. Merging PR-1 alone, before PR-2 (frontend) is ready to
    merge in the same window, breaks manual merge in production. **PR-1 and PR-2 should merge
    back-to-back, not PR-1-then-wait.**
  - `pg_dump`-to-R2 backup of the to-be-dropped tables (CUTOFF.md §4) not yet started — needs
    either a GH Actions one-off job (this session's environment has no local `.env`/`psql`/R2
    creds; GH Actions secrets `SUPABASE_DB_URL` + `R2_*` confirmed present) or the operator's own
    local machine.
  - Next session: once PR-1's workflow completes, review it; get the two blockers resolved
    (freeze + M-0); build the pg_dump-to-R2 backup job; launch PR-2 (frontend removal); only then
    consider merging PR-1+PR-2 together.
- 2026-08-05 — Program + cutoff drafts written; awaiting operator approval of both.
