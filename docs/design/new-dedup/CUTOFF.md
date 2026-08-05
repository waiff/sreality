# NEW DEDUP — Surgical cutoff specification

Status: **DRAFT — awaiting operator approval. Nothing here has been executed.**
Date: 2026-08-05. Grounded in a 5-agent code/DB/frontend reconnaissance + live DB counts.
Standing rule: once the backup branch exists, the removed decision code, its comments, and the
legacy dedup design docs are **never consulted again** for any purpose.

## 0. The two cuts, in one sentence each

- **Upstream cut (scrapers → decision layer):** scrapers, enrichment, image download, pHash
  computation, CLIP tagging/embedding lanes, and singleton-property creation all stay; every
  hand-off where they *feed work to* or *are gated by* the old decision engine is severed.
- **Downstream cut (decision layer → link mechanics):** everything from the moment a merge is
  *ordered* stays — `merge_properties` / `unmerge_group` and all they carry (operator state,
  pipeline reconcile, browse sync, merge-event ledger) — every code path that *decides whether*
  to order one goes.

## 1. Upstream cut points (C1–C7)

| # | Cut | Location | Action |
|---|---|---|---|
| C1 | Tagging → dedup dirty queue | `scripts/clip_tag_backfill.py` → `db.mark_properties_dedup_dirty_for_images`; `scraper/db.py` `_DEDUP_DIRTY_FOR_IMAGES_SQL` + fn | remove call + fn + SQL |
| C2 | Maintenance → dedup enqueue | `scripts/recompute_property_stats.py::_enqueue_imageless_for_dedup` (+ its SQL) | remove |
| C3 | Realtime worker dedup lane | `scraper/realtime_worker.py` — `DEDUP_*` constants, `_read_dedup_interval`, `_read_dedup_budgets`, `_dedup_sync`, `_dedup_pass`, lane registration, docstring refs | remove lane (maintenance lane stays) |
| C4 | Scheduled decision jobs | `.github/workflows/`: `dedup_engine.yml`, `dedup_batches.yml`, `dedup_model_compare.yml`, `clip_trial.yml`, `embedding_ab.yml`, `validate_render_detection.yml` (keep `clip_tag.yml`, `clip_retag.yml`, `compute_image_phash.yml` — signal producers) | delete + regenerate `workflowDocs.generated.ts` |
| C5 | Publication gate | see §3 — its own section; highest-risk seam | remove gate |
| C6 | Eligibility predicates | `toolkit/publication.py` (street/geo/byt-geo predicates — pure dedup-eligibility expressions) + `scraper/db.py:30` import + `api/routes/location_audit.py` | delete module + consumers |
| C7 | Pipeline health checks | `scripts/verify_pipeline.py`: remove `check_street_debt`, `check_geo_debt`, `check_eligibility_funnel`, `check_merge_latency`, `check_engine_health`, `check_merge_precision_sample` + their thresholds in `pipeline_check_thresholds`; keep llm/db/worker/dual-write checks (`llm_health.yml` untouched) | prune |

## 2. Decision-layer removal (code)

**Delete outright** (backend): `toolkit/dedup_engine.py`, `toolkit/clip_dedup.py`,
`toolkit/dedup_audit.py`, `toolkit/dedup_priorities.py`, `toolkit/dedup_model_overrides.py`,
`toolkit/dedup_settings.py`, `toolkit/dedup_batch_defer.py`, `toolkit/visual_match.py`,
`toolkit/image_classification.py`; `scripts/dedup_engine.py`, `scripts/submit_dedup_batch.py`,
`scripts/ingest_dedup_batch.py`, `scripts/build_golden_set.py`, `scripts/build_dedup_golden_set.py`,
`scripts/eval_identity.py`, `scripts/validate_vision_models.py`, `scripts/embedding_ab.py`,
`scripts/clip_trial.py`, `scripts/validate_render_detection.py`; `api/model_compare.py`.
Prune dedup entries from `api/llm_client.py` `_CALLED_FOR` allowlist and
`scripts/apply_r2_constraints.py` / `apply_r2_unique_guards.py` plans; drop the
`dedup_pair_audit` entry from `toolkit/listing_identity.py`'s backfill registry; remove the
dedup-only multi-alias branch in `api/location_filter.py`.

**Split, don't delete:**
- **S5 — `api/property_dedup.py` + `api/routes/dedup.py`**: extract the mechanics surface into a
  new `api/property_merge.py` + routes under `/properties`: `POST /properties/merge`
  (was `/dedup/properties/merge`), `GET /properties/merges`, `POST /properties/merges/{group}/unmerge`,
  `GET /properties/merged`, `POST/GET /properties/assets/*`. Keep the label/annotation CRUD
  (training examples, border cases, image annotations, pair notes) — re-homed under `/labeling/*`.
  Everything else in the file (candidates, summary, evidence, phash-audit, funnel, feedback,
  model compare) is deleted.
- **S6 — `toolkit/room_taxonomy.py`**: keep vocabulary (`ROOM_FAMILIES`, `ROOM_TYPES`,
  `family_of`, `SITE_PLAN_ROOM_TYPE`, `FLOOR_PLAN_ROOM_TYPE`, `category_main_compatible` —
  the merge-chokepoint guard). Delete decision policy (`ImageRole`, `IMAGE_ROLE_REGISTRY`,
  `*_PRIORITY`, `DISTINCTIVE_ROOMS`, `dismiss_qualifying_tags`).
- **S3 — `toolkit/property_identity.py`**: snip the two candidate-table writes
  (merge stamps `status='merged'`; unmerge reverts) **before** the table drop. Also remove the
  two `publish_reason` stamps (§3). Everything else stays byte-identical.

**Operator manual merge stays.** Browse `mergeMode` (checkbox multi-select → merge), the merges
ledger and unmerge remain — they are curation mechanics, not engine decisions. Interim caveat:
the unmerge *button* lived on the deleted Dedup page; until the new production wave gives it a
permanent home, unmerge is API-only (ask me in a session).

## 3. Publication gate removal (decision Q2)

What it is: since migration 273, a new property is invisible in Browse/map/stats/watchdogs until
something stamps it `published_at`. The only stamper for ordinary new properties is the old
engine. Removal plan:
1. **M-0 (immediate, reversible, before any code lands):** set
   `app_settings.dedup_publication_gate_enabled = false` → gate inert, all properties visible.
2. Teardown migration: redefine `properties_public`, `properties_map_mv` (+ rebuild fn), and the
   browse projection **without** the gate predicate; drop `publication_gate_enabled()` and
   `publication_gate_health_public`; delete the setting row.
3. Code: delete `_stamp_publication_checked` (engine dies anyway), `_publish_sweep` in
   `recompute_property_stats.py`, the merge/split `publish_reason` stamps in `property_identity.py`.
4. **Watchdog re-anchor:** `api/notifications.py` anchors its "new property" cursor on
   `published_at` → re-anchor on `properties.created_at` (same semantics once gate is gone);
   adjust tests.
5. Columns `properties.published_at` / `publish_reason` are **kept frozen** (historical record;
   dropping them forces wide view churn for zero benefit). Prune later if ever desired.

## 4. Database objects (decision Q3)

**Keep live (shared substrate the new engine reuses):** `images` (+ `phash`, `clip_tagged_at`),
`image_clip_tags`, `image_clip_embeddings`, `image_training_examples`, `image_border_cases`,
`image_tag_annotations`, `phash_pair_notes`, `listing_image_comparisons` (agent tool, not dedup),
`llm_calls`, and all §2-mechanics tables (`properties`, `property_merge_events`,
`dirty_properties`, browse read model, curation/pipeline/notifications/assets).

**Keep frozen as legacy (stop all writes; never read for the new design):**
`dedup_pair_audit` (the legacy decision ledger), `dedup_decision_feedback` (manual notes),
`dedup_golden_pairs`, `dedup_golden_sets`, `dedup_vision_bakeoff_results`,
`dedup_model_compare_sets`, `listing_visual_matches`, `listing_site_plan_matches`,
`listing_floor_plan_matches`, `image_room_classifications` (paid verdict caches — Q3c: keep).

**Drop (teardown migration, after all code references are gone):**
`property_identity_candidates` + `_archive`, `dedup_dirty_properties`, `dedup_scan_state`,
`dedup_batches`, `dedup_batch_requests`, `dedup_engine_runs`, views/matviews
`dedup_engine_runs_public`, `dedup_scan_state_public`, `dedup_engine_flow_public`,
`dedup_queue_snapshot_public`, `dedup_recency_backlog`, `dedup_label_events`,
`dedup_funnel_resolutions_mv/_public`, `dedup_llm_cost_by_category_mv/_public` (+
`cron.unschedule` their refresh jobs), the `127_dedup_eligibility` partial index.
Stored blocking keys `street_name_key`, `street_source`, `geo_cell_key` (+ trigger) **stay** —
street_name_key is reused by the new Level 0; geo_cell_key is harmless and cheap.

**Legacy stamping (decision Q1/Q4, DB-only):** teardown migration adds
`property_merge_events.generation text` and backfills `'legacy'` on all existing rows; the future
production engine writes `'v2'`. No UI badge.

**`app_settings` cleanup:** delete every `dedup_*` key, the four `realtime_dedup_*` worker keys,
`llm_visual_match_*`, `llm_site_plan_match_*`, `llm_floor_plan_match_*`, `llm_room_classify_*`,
`clip_tagging_priority_region_ids` stays (tagging lane keeps running).

## 5. Frontend

**Delete:** pages `Dedup.tsx`, `PhashAudit.tsx`, `LocationAudit.tsx`, `ModelTesting.tsx` +
routes + nav entries; components `DedupAuditHistory/BackfillProgress/CandidateReset/Factors/`
`Funnel/PipelineOverview/PipelineTimeline/Breakdown`, `DecisionFeedbackControl`,
`ModelCompareButton`, `DedupEngineSection`, `DedupTagPrioritiesSection`, `EligibilityMatrix`;
libs `dedupDiff/dedupFeedbackCache/dedupFunnel/dedupPaths/dedupQueueHealth` (+ tests); the
dedup sections in `Settings.tsx`; the dedup banner, per-portal dedup-ready check and
publication-gate section in `Health.tsx`; the dedup-spend card in `Costs.tsx`;
`MergeDecisionsChip` in `ListingDetail.tsx`; all dead `lib/api.ts` / `lib/queries.ts` entries.

**Keep:** `/clip-audit` **stays until the Wave-1 Labeling page replaces it** (it is the
operator's live labeling tool); `TrainControl`, `LabelCombobox`, `NoteFlagControl`,
`ImageTagBadge/RenderBadge`, `ImageLightbox`, `imageTags.ts`; Browse `mergeMode` and its
mutations (API paths updated per §2/S5).

## 6. Docs, tests, and hygiene

- **Docs:** rewrite CLAUDE.md rule 15 (legacy engine removed; mechanics + this program),
  `docs/architecture.md` §15, `scraper-ops` skill (dedup lane, publication gate, verify checks),
  `llm-pipelines` skill (dedup vision tools). **Delete from main** the legacy decision design
  docs (`multi-portal-dedup.md`, `dedup-byt-precision.md`, `clip-visual-embeddings.md`,
  `dedup-cost-reduction.md`, `dedup-vision-and-backlog-overhaul.md`,
  `dedup-vision-model-bakeoff-2026-07.md`, `dedup-geo-town-pin-false-merge.md`, …) — they
  survive in the backup branch and git history only. `clip-linear-probe.md` is **kept**
  (new-design input; committed in W0).
- **Tests:** delete the ~25 decision-side test files (recon list); edit the seam-asserting ones
  (`test_recompute_property_stats.py`, `test_property_identity.py`,
  `test_realtime_worker.py`, `test_verify_pipeline.py`, `test_room_taxonomy.py`,
  `test_street.py`, route-coverage + RLS-enumeration tests); keep the full mechanics net.
- Root `node_modules/` + `scratch_*.json` deleted.

## 7. Backups & execution order (Wave 0)

1. **PR-0:** ship branch `feature/clip-audit-tag-management` + commit
   `docs/design/clip-linear-probe.md` + this `docs/design/new-dedup/` pair.
2. **Backup:** branch `backup/pre-new-dedup-2026-08` + tag `backup-pre-new-dedup` at the
   post-PR-0 `main`. Record Supabase PITR restore timestamp in PROGRAM.md.
3. **Day-0 freeze:** `gh workflow disable` the five decision workflows; verify the worker dedup
   lane is dark (`realtime_dedup_interval_seconds=0`); apply **M-0** (gate off). From this moment
   no new automatic decisions occur (Q1 accepted: duplicates accumulate).
4. **pg_dump** of every to-be-dropped table → R2 `backups/new-dedup-teardown/` (destructive-
   migration gate satisfied).
5. **PR-1 backend removal** (§1, §2, §3 code, §6 docs/tests, workflowDocs regen).
6. **PR-2 frontend removal** (§5).
7. **Migration PR-3** (§4 drops + view redefinitions + legacy stamp + cron unschedule), applied
   via MCP after PR-1/2 are live.
8. **Verification:** CI green; a brand-new property appears in Browse/map/watchdog without any
   stamp; manual merge via Browse mergeMode works end-to-end (merge → collections/pipeline/
   notifications carried → browse updated → unmerge via API); scrape/image/tag lanes unaffected;
   Health page clean.
