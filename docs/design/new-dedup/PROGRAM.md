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
- **The operator must be able to hold every step of the pipeline in their head** (operator
  instruction 2026-08-27): no new rules, safeguards, or decision mechanisms enter this plan
  until the operator asks for them. Claude surfaces risks and spec gaps as flagged questions
  only — never as designed options.
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
| Embeddings (ruled 2026-09-05) | **DINOv3 ViT-B/16, 768-d `halfvec`, corpus-wide, is the PRIMARY embedding for all three consumers — the tag heads, Level 3 similarity, and path B** — conditional on the operator accepting the DINOv3 licence (free of charge + commercial use permitted; the terms in ENCODER-DECISION.md §2.8 are the operator's to accept). If declined: DINOv2 ViT-L/14-with-registers (Apache-2.0). ≥0.98 starting L3 threshold, expect recalibration. **The CLIP lane keeps running on new images in parallel** so results can be compared later. Cadence for new images: OPEN (see 2026-09-05 (b)) |
| Candidate path B (2026-08-27; vectors re-ruled 2026-09-05) | **Image-similarity candidate generation runs in parallel with path A**, all property types: batch k-NN over per-type priority **same-tag** image embeddings proposes pairs, using the **DINOv3 vectors** (same store as the heads and L3) once W3's retag supplies the new tags. **B is only another way to FIND pairs** — everything downstream (levels, rules, settings) is identical to A; nothing B-specific exists. B's two search parameters (neighbor count, minimum similarity to propose a pair) are not yet specified — the operator is asked at build time. Audit C (W5) shows whether CLIP vectors suffice or B should read DINOv2 vectors — operator decides |
| Gate 1 (ruled 2026-09-05; supersedes "Probe scope 2026-08-27") | **Target tags = 12**: fasáda, nezařízená místnost, půdorys, katastrální mapa, kuchyně, obývací pokoj, koupelna, garáž, jídelna, ložnice, technické zařízení, domovní vchod (open: which of the two "domovní vchod" tags — exteriér id 2 or interiér id 19). **Machine-made labels COUNT** toward the per-tag target; the operator expects ~300–400 positives per head, machine-labeled under the operator-approved definitions and process. The per-head agreement report stays a **diagnostic the operator reads**, not a threshold in code. **The training set is not finalized or reviewed yet — no training on it until the operator says so.** Open question carried: how ostatní's any-two-interior rule is represented at labeling time |
| RunPod | Set up in Wave 1; serverless/on-demand only, **<$1/day** run-rate; may reuse PR #804 harness |
| Vision | GPT-5-mini, manual batches only; qwen pluggable later |
| Taxonomy v1 | The operator-curated `image_training_examples` label set (49 labels: `interier -*`, `exterier -*`, `podklad -*`, standalone garáž/technické zařízení/other); "katastr" ≙ `podklad - katastrální mapa`; tag-family defaults reconfirmed at training-set finalization |
| Exact attrs (L1) | Ships inactive; calibrated only after full stack has produced a sample (Wave 7) |
| Gate 1 counting (2026-08-21) | **Border cases do not count toward Gate 1** — an image nobody could classify is not evidence a tag is learnable. The exclusion is a JOIN on `image_border_cases`, not a stamp on the training row, so clearing the flag makes the image count again with no relabelling. `gate_count` (unparked) is what every coverage surface reads; `confirmed_count` stays the raw inventory total |

## Simulation architecture (Q15-confirmed)

Schema `dedup_sim` (droppable wholesale). Two-tier recompute:

- **Evidence tier (expensive, computed once, reused across runs):** candidate pairs from L0
  (keyed by listing pair + path + inputs; paths = A1 street / A2 geo+dispo / A3 geo+area and,
  from W3, B image-similarity), and per `(pair, tag_family)` image-comparison
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
  gallery-flip hazard); iterate sample until 300 proposals for ≥50% of the **target** categories,
  then assess coverage with operator. Operator confirms/dismisses into the training set. ⛳ per
  sample round.
  **Gate 1: 150 training images (human or machine) per target tag — the 12 tags ruled 2026-09-05 — AND the operator has reviewed and finalized the set.**
- **W2 — Level 0: candidate selection.** Primary path + 2 fallbacks + byt floor rule, sim
  candidate store, **Candidate audit page** (type × path matrix, **with a path-B column from day
  one** — empty until W3; missing-field tables overall + per portal per type; pin/clique
  statistics). Recall diagnostic vs legacy manual merges only if
  granted (**bold request** at that moment). ⛳ after mechanics, after audit page.
  **Gate 2: operator satisfied path A loses no rightful candidates to data quality (poor-geo
  gaps explicitly covered later by path B + the operator's parallel location-DQ work).**
- **W3 — Linear probe + full retag + candidate path B.** Train probe on the gated training set
  (grouped splits, pinned encoder, versioned artifact); validate on the Labeling page;
  campaign-retag the corpus into the sim tag store. Then **path B generation**: a batch k-NN job
  over per-type priority same-tag images on the DINOv3 vectors (off-DB, e.g. FAISS on a
  pod/runner; writes candidate pairs + best-similarity evidence into the sim store; candidate
  audit + funnel gain their B numbers; B's two search parameters asked of the operator at build
  time). **Gate 3: operator accepts tag quality; per-type default tag-family orders
  reconfirmed against final taxonomy; path B volume/quality reviewed with the operator.**
- **W4 — Level 2: pHash.** Evidence computation over candidates (A ∪ B); decision tier; settings
  (threshold 11 + per-tag overrides, pairs required =1, family toggle, drag-priorities per
  type); **pHash audit page** (side-by-side pairs, filter by type/tag/hamming/result);
  **Browse-as-if page** (BrowseExperience reduced-feature adapter over sim groups);
  **Suspicious-properties page** (concurrent price divergence; ≥N listings merged, default 6;
  best-pair-vs-next-tag divergence filter). ⛳ evidence / decisions / each page.
  **Gate 4: visual validation — no easy merges missed, no strong signal underused, threshold calibrated.**
- **W5 — Level 3: embeddings.** DINOv3 ViT-B/16 vectors (corpus-wide, computed on RunPod — the
  same store the heads and path B read), in Supabase;
  evidence + decisions; audit A (pHash-style with similarity), B (click-an-image search),
  C (all-candidates pHash-vs-embeddings comparison, plus CLIP-vs-DINOv3 on the same pairs, since
  the CLIP lane keeps running); dismiss-decision validation; DINOv2 audit
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
- **Path B risk, noted only — no mechanism designed (no-invented-rules instruction):** B has no
  location anchor, so identical marketing photos (developer catalogs, staged/stock interiors,
  reused renders across a project's units) can become candidate pairs and would merge at L2
  under the operator's current rules. The W3/W4 audits will make this visible; whether any rule
  is wanted is entirely the operator's call, made then.
- Qwen vision provider route (W6).
- Near-duplicate training labels flagged 2026-08-05 (operator cleanup via batch reassign).
- Interim unmerge has no UI home (API-only) until W8. Confirmed again on 2026-09-05 while
  running the Gate 0 checklist: the merge went through Browse's own `POST /properties/merge`,
  the unmerge had to go through the API by hand.
- ~~CLAUDE.md "psql" guidance inoperable in cloud-only mode~~ — FIXED 2026-09-05 (#1286). The
  bullet now says what to do when `psql` is absent or `SUPABASE_DB_URL` is unset: fall back to
  the Supabase MCP `execute_sql`, and carry the INTENT of the psql preference across (it was
  about context cost, not correctness) — one aggregate row per question, `md5(string_agg(...))`
  to compare a list without printing it, never a wide result set.

## Progress ledger (update every session, newest first)

- 2026-09-05 (f) — **DINOv3 readiness build: all four PRs up, draft, CI green, per entry
  (b)'s directive.** #1296 (migration 480, the vector store), #1300 (the bake-off harness),
  #1298 (the production embedding job + dispatch workflow), #1297 (the per-tag heads trainer +
  eval harness). **None of it has been run against real data** — no corpus pass, no bake-off
  dispatch, no training, no money spent, no gated weights downloaded, no vectors written
  anywhere — per entry (b)'s "training set is not finalized" ruling; everything is built and
  tested on synthetic/offline fixtures only. Merge order matters only for one mechanical
  cleanup: #1297 and #1298 each carry a temporary `tests/test_sql_schema_prepare.py` allowlist
  entry (self-flagged in both) excusing `image_dinov3_embeddings` from the schema-replay check
  because migration 480 isn't live in their branches — **delete both entries the day #1296
  merges**, or they'll silently keep masking a real PREPARE failure if the table is ever renamed.

  **Migration 480**: one row per (image, full six-fact encoder configuration) — model, revision,
  library, pooling, resolution, preprocessing, dtype are all part of the primary key (not a
  synthetic `encoder_id` some writer could set inconsistently), so a knob change adds a row
  instead of silently overwriting a differently-configured vector for the same image. `halfvec(768)`
  (pgvector 0.8.0 confirmed live) sits below the ~2 KB TOAST threshold that `vector(512)` on
  `image_clip_embeddings` sits above (ENCODER-DECISION.md §2.1) — a genuine read-cost win, not
  just a size one. RLS + REVOKE posture (migrations 237/447) replayed at creation, inside the
  same pgvector-conditional `DO` block migration 226 uses for CI replay. Additive; **not yet
  applied to production** — pending operator OK per the migration-safety gate.

  **Gate 1's 12th tag resolved differently than expected.** Asked the operator which
  "domovní vchod" tag (exteriér id 2 / interiér id 19) was the 12th target; the answer was
  that BOTH stay, now under separated names (`tag_taxonomy` live: id 2 = "exterier - domovní
  vchod", id 19 = "interier - domovní vchod / chodba") — the choice between them was never
  meant to be made, the ambiguity was in the shared name. Consequence for the heads trainer
  (PR 4): it takes a target tag id as a plain argument rather than hard-coding a resolved
  12-tag list, so it needs no further change whichever way Gate 1's list finally reads.

  **RunPod / HF acquisition note for PR 2/3**: `scripts/mirror_model_weights.py` +
  `mirror_model_weights.yml` already exist (an earlier session's prep, never run —
  `MANIFEST.json` does not exist in R2 yet) for mirroring Meta's raw, licence-accepted
  `.pth` download-e-mail files into R2, avoiding both Meta's time-limited links and an
  HF_TOKEN dependency for an unattended job. That consumption path (loading a raw state
  dict rather than `from_pretrained`) is NOT built here — out of scope for this readiness
  pass. The bake-off and the production job instead load gated weights the standard HF way
  (`from_pretrained(model_id, revision=..., token=...)`, the operator's `HF_TOKEN`), matching
  `scraper/clip_tagger.py`'s existing pin discipline. Revisit the R2-mirror consumption path
  later if an HF-token-free production lane becomes worth building.

- 2026-09-05 (e) — **GATE 0 CLOSED. Wave 0 finished for real (migration 475, #1286),
  eleven days after it was recorded closed.** Entry (b) above corrected the record; this one
  discharges it. Migration 475 applied live at **11:26:48 UTC**, verified immediately after.

  **Dropped** (all seven re-dumped to `backups/new-dedup-teardown/2026-09-05/` first — run
  33962204424, COPY row counts read out of the artifacts themselves and matching live
  exactly): `property_identity_candidates` 159,260 + `_archive` 5,542,
  `dedup_dirty_properties` 15,357, `dedup_scan_state` 3, `dedup_batches` 265,
  `dedup_batch_requests` 18,250, `dedup_engine_runs` 9,932 — **112 MB** — plus the six admin
  views (`dedup_engine_runs_public`, `dedup_scan_state_public`, `dedup_engine_flow_public`,
  `dedup_queue_snapshot_public`, `dedup_recency_backlog`, `dedup_label_events`) and
  `listings_dedup_eligible_idx` (migration 127), which pg_stat_user_indexes reported at
  **idx_scan = 0** while costing 6 MB of write amplification on every `listings` write. The
  backup script needed one fix to run at all: two of its nine relations were already dropped by
  migration 432 and `pg_dump --table` on a missing relation exits non-zero, so `check=True` had
  been turning the whole run red — a missing relation is now `ALREADY GONE`, named, and
  excluded from the failure list (both matviews are in the 2026-08-05 dump).

  **The publication gate is gone, not just inert.** CUTOFF §3 step 2 was never done in August:
  the dead predicate was still in `properties_public`, `browse_projection` AND
  `listing_feed_public` (that third one is not in CUTOFF's list — found by asking the catalog
  which view definitions mention the function, rather than trusting the doc). All three
  redefined without it, then `publication_gate_enabled()` and `publication_gate_health_public`
  dropped. `properties.published_at` / `publish_reason` kept frozen. **Note for anyone reading
  the old incident:** the PR-#707 InitPlan lesson now has no live example — the function is
  gone — but case 2 is still reachable through any `SECURITY DEFINER` gate, and the RLS
  policies are where it lives now. The `database` skill says so.

  **Two things the doc got wrong and live state settled.** (1) `properties_map_mv` does not
  embed the gate — it reads `browse_projection`, so redefining the projection was enough; no
  rebuild function mentions the gate at all. (2) CUTOFF says "the four `realtime_dedup_*`
  worker keys"; exactly one still existed. 24 `app_settings` keys deleted by literal name,
  never by `LIKE` — `dedup_%` would have swept away Wave 1's own `dedup_sim` keys. Inside
  `pipeline_check_thresholds`, which is a LIVE key (verify_pipeline / ops_incidents /
  system_alerts all read it), only the 11 dedup-engine checks came out; the 6 that are still
  code defaults survive, verified by grepping each key across the whole repo first.

  **A near miss worth keeping.** The three view definitions were taken from the latest
  migration that defines each, then checked against live by md5 of the ordered column list
  before anything was written. `properties_public` came back 82 columns against live's 83 —
  because migration 425 writes `create or replace view public.properties_public` and the
  "latest defining migration" regex (in `tests/test_browse_read_path_guardrail.py`, reused
  here) does not allow a schema prefix, so it had been resolving to migration 375, one column
  stale, for months. Its read-contract test was reading the wrong select list. The regex is
  fixed and all three hashes matched before the migration was written. **Hash-check a live
  object before restating it** — the lesson from the migration-438 outage, earning its keep.

  **Legacy stamp:** `property_merge_events.generation` = `'legacy'` on all 124,363 existing
  rows, nullable with no default so new rows cannot inherit the claim. Confirmed on a real
  merge during the checklist below: an operator merge writes `generation = NULL`, which is
  correct — it is neither the removed engine's nor Wave 8's.

  **CUTOFF §7 step-8 verification checklist — six items, ALL PASS** (run 2026-09-05, live):
  1. **CI green** — `CI: tests` and `CI: schema replay + SQL correctness` both succeeded on
     the branch. The replay job rebuilds the schema from migration zero, so it is what
     validated 475 itself. 6,374 tests pass locally, 247 skipped.
  2. **A brand-new property appears everywhere without a stamp** — of the properties created
     after the apply, 100% have `published_at IS NULL` and 100% are returned by
     `properties_public`, `browse_projection` and the watchdog matcher's own query shape
     (`properties_public` filtered on `first_seen_at`). They flow into `browse_list` and
     `properties_map_mv` on the next rebuild of each (15-minute and :07/:37 cadences — the
     lag is the cron, not the gate). 152,049 active properties carry no stamp and are visible.
  3. **Scrape lanes unaffected** — in the first three minutes after the apply, all eight
     portals wrote: sreality 2,057, bezrealitky 782, realitymix 526, bazos 280, idnes 260,
     ceskereality 235, remax 39, maxima 28. Realtime worker heartbeat 11 s old.
  4. **Image + tag lanes unaffected** — 983 image download attempts in the same window;
     `clip_tag.yml` and `compute_image_phash.yml` both green on their last runs, 5,830 CLIP
     tags written in the most recent lane run.
  5. **Health clean** — every active pg_cron job succeeded after the apply, including
     `refresh-health-dashboard` and `browse-list-rebuild`. The only failures in the table are
     pre-existing statement timeouts on health matview refreshes (the ~35% rate migration 432
     documented), none of them touching a dropped object.
  6. **Merge → carry-over → browse → unmerge, end to end** — performed by CLAUDE under the
     operator's explicit authorization in this session, through the real admin auth path (the
     `+claude-admin` smoke-test account's Supabase JWT; every merge route is `require_admin`,
     so no shared token was involved). Properties 227457 (survivor, older) and 258581, a
     genuine same-street/same-disposition/same-area/same-price byt pair on different portals.
     `POST /properties/merge` → retired went `merged_away`, its listing re-pointed, survivor
     `source_count` 1→2, **its notification_dispatch and status event carried onto the
     survivor**, one `property_merge_events` row, and `browse_list` updated synchronously
     (retired gone, survivor present — read-your-writes held). `POST
     /properties/merges/{id}/unmerge` → `conflicts: []`, both properties active again with one
     listing each, both back in `browse_list` and `properties_public`, event marked undone.
     **Recorded asymmetry, expected and not a regression:** the carried notification_dispatch
     and the status events stayed on the survivor rather than returning. That is CLAUDE.md
     rule 18's documented "unmerge/split are best-effort" for operator state, and append-only
     journals do not un-append; it predates this migration.

  Nothing in Wave 1, the encoder decision or Wave 2 was touched. No dedup rule, threshold or
  setting was added or changed — the 24 deleted keys configured code that no longer exists.

  **Numbering footnote.** This shipped as 474 and was renumbered to **475** when entry (d)
  below merged 474 first — `tests/test_migration_numbers.py` caught the collision, which is
  what it is for. The file had already been applied live under its old number, and the only
  place the number reached the DATABASE was the `generation` column's comment, which was
  corrected in place so live matches the file. Worth knowing for next time: this collision is
  structural, not carelessness — `ls migrations | tail` reads the number free WHEN YOU LOOK,
  and a long session between looking and merging is exactly the window another branch lands in.
- 2026-09-05 (d) — **The cutoff: a head's training set is a QUERY, not a list (migration 474).**
  The operator's objection: reviewing 1,149 fasáda positives is exactly the manual work the
  programme exists to remove. Answer: each head has a TARGET (`tag_taxonomy.training_target`,
  NULL = 300 — the per-class count past which a logistic probe on frozen CLIP features shows
  little further gain), and its set is defined as the ranked positives up to that target: the
  operator's own first (confirmed), then the machine's oldest-first in a total order. Past the
  target is the RESERVE. Because it is a query, removing a wrong positive pulls the first
  reserve image in with no bookkeeping, and confirming one (writing it as a human label) keeps
  it in. The review is therefore bounded: "To review" on `/new-dedup/training-set` = in-set
  positives still on the machine's word alone (fasáda: 282 of 1,149). **The trainer reads the
  same list** — `training_set_positive_ids` is the second sanctioned door beside
  `tag_holdout.training_label_rows` (which reads human labels ONLY, so the 10,544 machine labels
  would otherwise train nothing) — so what was reviewed and what is trained on cannot diverge.
  Every read tolerates 474 not being applied (default target for all heads).

- 2026-09-05 (c) — **Training-set review surface + the operator's reasons (migration 473).**
  10,544 machine labels existed with no way to look at them; `/new-dedup/training-set` reads
  them head by head (server-side filters by verdict and by who decided; paging with a unique
  tiebreaker; each tile names who decided and flags a label written under since-replaced
  wording; the holdout excluded and said so). A correction there is a HUMAN label, which the
  store's human-wins rail protects from every later machine pass. Then the operator's ask: a
  NOTE with each changed mark, so the why reaches the definition. `tag_label_notes` records
  (from_state, to_state, note) beside the write — one request for mark and reason — and the
  taxonomy page shows a head's open notes beside its editor with an "absorbed into vN" action.
  **THE ABSORPTION RULE (operator's words, ratified):** notes are NOT copied into the definition
  one sentence per note. The definition is read by a model and by a person, and either absorbs
  a short general rule and drowns in a list of specifics — think of a human annotator who must
  hold the whole definition in their head. The reviser reads a head's open notes TOGETHER,
  finds the rule they point at, states it ONCE at the level of the existing lines (most often
  as a DOES NOT COUNT boundary — the model treats that list as law and `confusable_with` as
  advice, measured on fasáda), saves, and marks the batch absorbed by that version so no note is
  read into two revisions. The rule lives in the migration comment and the module docstring,
  not only here.

- 2026-09-05 (b) — **Operator rulings on Gate 1, the labeling budget and the encoder** (docs
  only; the decisions-ledger rows above were rewritten to match). Gate 1: 12 target tags (listed
  in the ledger row); machine-made labels count; the agreement report is a diagnostic, not a
  gate; **the training set is not finalized — no training on it yet.** Budget: the remaining
  ~$47 is NOT to be spent unless needed; no paid next step had been agreed, so nothing is
  scheduled. Encoder: **DINOv3 ViT-B/16 as the primary embedding for heads + L3 + path B**,
  conditional on the operator accepting the licence (free + commercial-use yes; §2.8 terms are
  theirs to accept — needs the operator's Hugging Face click-through so an acceptance record
  exists); the CLIP lane continues on new images in parallel for later comparison (storage
  grows on both stores, ~6 GB/month combined). Directive: get the DINOv3 embedding job and the
  per-tag heads trainer READY so training starts the moment the set is finalized. Dependencies
  to be added with rule-7 justification as analysis/training-only extras: scikit-learn (heads),
  faiss (path B k-NN). Open, to be answered by the operator: (a) which "domovní vchod" tag is
  the 12th target (exteriér id 2 / interiér id 19); (b) embedding cadence for new images —
  near-real-time question raised; options costed in chat (nightly batch ≈ $0.05/day, up to 24 h
  latency · hourly pods ≈ $0.30–0.50/day, ~1 h · RunPod serverless per-request ≈ $0.3–1/day
  estimated, minutes — needs a measured number; CPU on the always-on worker rejected as a risk to
  the scrape loops). Design rule regardless of cadence: embedding + tagging are an asynchronous
  stage AFTER publication; nothing in the ingest path waits for them; the LLM labeler is never
  in the pipeline at all (training-set construction only). Next session (fresh context): the
  readiness build — bake-off harness rewrite, new vector table migration (RLS/REVOKE posture
  replayed, pgvector-conditional for CI), DINOv3 embedding job on RunPod with checkpoint/resume +
  write throttle, heads trainer + eval harness reading `image_tag_labels` with the holdout
  excluded — none of it run against the unfinalized training set.
- 2026-09-05 — **Program review + encoder decision draft (docs only; nothing decided).** A
  12-agent review pass (progress audit, four encoder studies, one-encoder feasibility, cost
  model, synthesis, three adversarial refuters, revision). Findings:
  1. **Correction to the 2026-08-27 entry: Gate 0 is NOT closed.** Migration 432 (#1167) dropped
     only the funnel/cost matviews + their `_public` views, archived the funnel matview and
     unscheduled its cron job. It did NOT drop the six legacy tables
     (`property_identity_candidates` + archive, `dedup_dirty_properties`, `dedup_scan_state`,
     `dedup_batches`, `dedup_batch_requests`, `dedup_engine_runs`), the six other views, or the
     127 eligibility index; did not add `property_merge_events.generation` (the legacy stamp W8
     needs); and the `app_settings` sweep + the CUTOFF §7 step-8 verification checklist were
     never run or recorded. W0 stays open on exactly those.
  2. **W1 tooling is complete** through #1280; seven 2026-09-05 PRs (#1263, #1264, #1265,
     #1266, #1268, #1269, #1280) were unrecorded until this entry. Live training-set state
     (measured today): 18 active heads carry human labels — 459 human positives (max 40 on one
     head), ~9.4k human negatives, 90 excluded, 1,054 `human_draft` cells; 13 heads carry
     250–1,130 gpt-5-mini machine positives over 10,544 labeled images (7,501 positives / 118k
     negatives), 5 heads none (předsíň/chodba, letecký snímek s ohraničením, chodba/schodiště,
     domovní vchod/chodba, wc, parkoviště); exam holdout = 250 images.
  3. **No trainer exists** (grep for sklearn / LogisticRegression / GroupKFold across toolkit,
     scripts, api = zero hits; no training extra in pyproject). W2–W8 not started. Note the
     numbering trap: roadmap's "W2 SHIPPED (#1228–#1231)" is the labeling sub-programme's wave
     (the sealed exam), not this file's W2 (Level 0 candidates), which has no code.
  4. **Gate 1 has three live ambiguities the operator must rule on** (not resolved here): the
     target list (11 spec-named tags vs 18 defined heads vs the 8 routing tags flagged
     `priority`); whether machine positives count toward "150 per tag" (today's `gate_count` is
     source-blind, so they do count on every surface); and the per-head agreement threshold that
     clears a head for machine building (deliberately not in code).
  5. **Encoder decision**: `docs/design/new-dedup/ENCODER-DECISION.md` (DRAFT — proposed,
     gated, operator-owned) recommends ONE encoder serving the tag heads, L3 similarity and path
     B — DINOv3 ViT-B/16, 768-d `halfvec` — gated on a ~$2 bake-off, a defined licence review,
     scikit-learn + faiss approvals under rule 7, and a disk-headroom check; DINOv2
     ViT-L/14-with-registers (Apache-2.0) is the licence-clean near-equal; SigLIP2 wins the tag
     job but fails near-duplicate retrieval; keeping CLIP B/32 is the zero-cost stick option with
     no licence grant and no measurement on job (b). Pre-flight readout run live today:
     `image_clip_embeddings` = 11,301,885 rows (10,489,289 `revision NULL` = pre-pin, 812,596
     pinned), table 31 GB, database 150 GB; 11,303,863 images stored in R2; velocity ≈ 55–60k
     images/day (39,794 listings first seen in the last 7 days). **No ledger decision moves until
     the operator rules.**
  Next session: operator rulings (Gate 1 meaning, bake-off go/no-go, dependency approvals,
  embedding cadence, sampling strategy for the remaining labeling budget); independently and
  in parallel, close W0 for real (the proper PR-3 migration + the recorded checklist).
- 2026-09-04 (b) — **The LLM builds the training sets; the gate decides which heads it may
  build (operator direction; migration 468).** Ruling: stop drawing human cohorts to rescue thin
  heads — wc and parkoviště are left alone — and push instead on the model labeling in quantity
  for the heads already defined. Two pieces, in the order evidence demands. First
  `scripts/exam_agreement.py`: per-head precision / recall of the machine review against the
  human exam answers, with the ratified grading rule enforced — a cell grades ONLY when both
  sides said yes or no, an abstention on either side trains nothing and grades nothing (scoring
  it as a negative would punish the model for obeying the leave-out rule and inflate the
  denominator), and precision/recall are None rather than 0.00 when nothing was proposed, since
  "never proposed" and "always wrong" are opposite facts. Second `scripts/label_images.py` +
  `label_images.yml`: one call per image carrying the ACTIVE definitions of the NAMED heads,
  verdicts written to `image_tag_labels` as `source='machine'` — no new store, because that
  upsert already refuses to overwrite a human cell and stamps `definition_id` + `model` (the
  exam keeps its separate table only because there the suppression would hide the disagreement
  worth reading). Rails: no exam member is ever labeled (holdout unseen, curated is the
  operator's); heads named explicitly, no label-everything switch, since bulk labeling is only
  justified for a head the gate cleared; a leave-out stored as excluded/'pruned', NEVER as a
  negative; an unusable reply writes nothing at all; resume by provenance, so a definition edit
  re-opens exactly the heads whose wording moved; lane defaults to dry_run.
  **Cost structure, measured from the rendered prompt:** the 768px photo and the reasoning
  dominate — the eighteen definitions add only ~$0.0016/image — so a cheap screen-then-verify
  two-stage would pay for the photo twice and save almost nothing. One good pass per image is
  the right shape. With ~$47 of the $50 left, that is roughly 4-8k labeled images IN TOTAL,
  once; a purely random draw would spend most of it re-confirming the already-strong heads, so
  the sampling strategy is an open operator decision to be taken WITH the agreement numbers.

- 2026-09-04 — **One ruleset, written the same way on every tag; the machine reviews the exam
  against it (migration 467).** The operator's audit question — is "left out" vs "negative"
  applied by one logic across fasáda-among-houses, open-plan kitchen/dining/living, the three
  document kinds, a bathroom seen through a door, a toilet inside a bathroom — exposed that the
  eighteen definitions carried the ratified three-tier calculus unevenly (only kuchyně spelled
  out the present-but-secondary tier; koupelna/wc wrote the same case as a named exclusion, i.e.
  a NO). The ruleset, now stated once: the question is what the photo is an image OF; the tag's
  SUBJECT is what the definition says (a room kind, a composed shot, a document kind), never the
  object inside it — so a toilet is not a wc room and a building among many is not a fasáda
  (absent subject = negative), while "left out" is reserved for the subject itself being present
  but secondary. Documents are the deliberate exception: kinds are exclusive, one decisive
  feature assigns exactly one, the rival is negative. Three rulings: fasáda = ONE building even
  inside a joined block (a long angled/distant stretch of 3+ is the street, negative); a CLOSED
  garage door in a house photo is negative (subject absent), reversing the earlier left-out; every
  space tag now carries the same three sentences (composed on it → yes; glimpsed → no; clearly
  and substantially in frame but composed elsewhere → skip), every document tag the same
  exclusivity sentence, and the prompt/card label for that field reads "leave out (skip)" instead
  of "undecidable". Definitions saved live (all 18 bumped). Then the mechanism the operator asked
  for: a definition-driven machine review — one gpt-5-mini call per exam image carrying ALL
  eighteen definitions and the three-tier rule once, yes/no/skip per tag, stored in its own table
  (`tag_exam_machine_reviews`, provenance = asked list + definition versions frozen per row; stale
  rows never served, re-offered by the lane; dismissals reset on re-review) and rendered on the
  review page as PROPOSALS per row: apply = the exam's own whole-image /answer, keep mine = a
  dismissal. Never labels. The 461 suggestions (name-only, pre-definition) stay as the anchoring
  audit and are not the same thing.

- 2026-08-31 — **The exam instrument decouples from the holdout role; drafts declared (operator
  ruling; migration 464).** The operator's reframing, accepted after working the inconsistencies
  through: the pre-exam labels (1,522 cells, mostly positives, made without guidelines) are NOT
  the training set — they are DRAFTS, and the trusted labeling instrument is the exam UI. So
  cohorts carry a PURPOSE: 'holdout' (unchanged contract — random/stratified, weighted, excluded
  from training; exam_v1 + its 84 careful answers stay the yardstick) and 'curated'
  (operator-marked images re-seated for careful re-labeling through the same UI; their answers
  ARE training material; frame='curated', p=1, excluded from population-weighted statistics by
  FRAME, never by luck). The one-exam-per-image index now works FOR the split: a trained-on image
  can never later enter a holdout. Mechanics: HOLDOUT_EXCLUSION narrowed to purpose='holdout'
  (one constant; census marker updated); the WARM-UP deliberately stays cohort-blind (the
  answer-refusal rail only refuses NON-members, so a curated member served as practice would be
  silently accepted — caught by the inventory pass); existing 'human' labels off holdout members
  demoted to source='human_draft' (drafts never win an upsert, are read by no truth path, and
  seed the curated draw); tag_candidates cleared on operator order (2,282 rows; backups
  backup_464_*). The curated draw is rarest-first, 20/tag across all 16 flagged categories.

- 2026-08-30 — **Exam keys become letters, sets cap 12, machine suggestions ON (operator
  ruling; PR: exam letters + suggestions, migration 461).** Three operator instructions in one
  turn: (1) set_2 extended by "chodba / předsíň, ložnice, chodba / schodiště, vstupní dveře" —
  mapped to tags 30/26/18 one-to-one; "vstupní dveře" has no single tag, so BOTH entrance tags
  (2 exterier, 19 interier) were seated, trimming either is one array edit. (2) The exam keys
  are the letter grid w e i o / s d k l / y x n m — twelve positions laid out like the keyboard
  (Czech QWERTZ's digit row is shifted), sets capped at 12 where the keys run out. (3) Each
  exam image is pre-run through gpt-5-mini and the suggested buttons get a subtle dot
  (`tag_exam_suggestions`, `scripts/suggest_exam_answers.py`, suggest action on the screen
  lane). This REVERSES the exam's founding no-suggestion posture ("an exam the machine helped
  answer cannot grade the machine") — the operator ordered it knowing that context. Recorded
  consequence: sittings now measure agreement with a machine-ANCHORED human, not blind
  agreement. Mitigations built in: every suggestion is stored beside the final answer with the
  exact question list it answered (suggested-vs-final anchoring stays computable per image and
  per tag, forever); a suggestion is a mark, never a pre-filled verdict; a stored suggestion is
  served ONLY when its asked list equals the sitting's current list (sets grow by columns — a
  3-tag answer must not mark a subset of 8 buttons while looking complete); the suggester's
  prompt is precision-tuned, the exact opposite of the screener (a wrong mark anchors, an
  omission merely leaves a button unmarked). The worker engine was extracted to
  `toolkit/vision_batch.py` — screen and suggest share one loop (budget checked pre-call in the
  worker, per-worker connections), third consumer (W3 machine relabel) already on the roadmap.

- 2026-08-27 (part 2) — **Course correction (operator instruction): no invented rules.** The
  operator's standing principle, now in the non-negotiables: the pipeline must stay simple
  enough to hold in one head; NO new rules, safeguards, or mechanisms enter the plan until the
  operator asks. Retracted from part 1 accordingly: the three path-B anti-catalog safeguard
  options (the RISK stays noted under Open items with no mechanism attached); W4's "per-path
  safeguard settings" clause; and the two Claude-added tag classes (pooled `interiér – ostatní`,
  `other` sink). The probe target list is now **exactly the 11 tags the operator's spec names**:
  interier - kuchyně · interier - koupelna se sprchovým koutem · interier - koupelna s vanou ·
  interier - koupelna · technické zařízení / místnost · exterier - fasáda · podklad - půdorys ·
  podklad - katastrální mapa · podklad - letecký snímek s ohraničením subjektu · garáž ·
  exterier - parkoviště. Open operator questions carried (answered whenever the operator
  chooses; nothing is designed around them meanwhile): (a) how "interior" is recognized for
  ostatní's any-two-interior rule at labeling/probe time; (b) path B's two search parameters
  (neighbor count, minimum similarity to propose) at W3 build time. Images matching none of the
  11 classes are handled inside implementation space (abstain — no trained class, no operator
  labeling budget) unless the operator directs otherwise.
- 2026-08-27 — **[PARTLY SUPERSEDED by part 2 above — the safeguard options and the two added
  tag classes described here were retracted; read part 2 first.]** Plan updated on two operator
  edits (docs only, no engine code; challenges raised and recorded). **(1) Candidate path B** — previously only a parked poor-geo idea — is
  now a first-class selection path in parallel with A for all types (ledger row added; W2's audit
  is B-ready; B builds in **W3**, not W5: it needs the new tags but NOT DINOv2, because it runs
  on the existing corpus-wide CLIP 512-d vectors — so the candidate-scoped DINOv2 decision stands
  and no ~35 GB corpus backfill returns). Challenge raised, parked as an operator decision at the
  W3 gate: B has no location anchor, so developer-catalog/stock photos would merge at L2 on one
  identical pair — safeguard options under Open items. **(2) Probe v1 narrowed to ~10–15 target
  tags** (was: the whole ~49-label taxonomy). Gate 1 re-scoped to target tags (border-case
  exclusion from 2026-08-21 unchanged); existing granular interior labels fold into a pooled
  class via the training-time collapse map, so prior labeling effort still counts. Proposed
  target list (13 — operator confirms at the next labeling round): interier - kuchyně · interier
  - koupelna se sprchovým koutem · interier - koupelna s vanou · interier - koupelna · technické
  zařízení / místnost · exterier - fasáda · podklad - půdorys · podklad - katastrální mapa ·
  podklad - letecký snímek s ohraničením subjektu · garáž · exterier - parkoviště · interiér –
  ostatní (pooled: all other interior rooms; serves ostatní's any-two-interior rule) · other
  (non-interior OOD sink). Follow-up promoted from low-severity (2026-08-06) to real W1 work:
  `taxonomy_labels` needs a non-destructive **probe-target flag** so the coverage strip + Gate-1
  bar track target tags only (today the only lever is the cascading DELETE). Also noting for the
  record: the W0 teardown migration (the long-open "PR-3" blocker) **landed 2026-08-25 via
  #1167 (mig 432, cardinality W0b)** — Gate 0's remaining item is done. **[CORRECTED 2026-09-05: overstated — 432 closed only part of CUTOFF §4; Gate 0 is still open. See the 2026-09-05 entry.]** Next session: implement
  the probe-target flag + scope the coverage UI to it; then labeling rounds continue.
- 2026-08-21 (part 2) — **Gate 1 stops counting border cases** (operator decision, reversing the
  call recorded in part 1 below — which had deliberately left `confirmed_count` alone rather than
  redefine the gate metric unilaterally). Operator's rule, verbatim: *"border case does not count
  toward gate 1 unless removed from border case group."*
  - **`taxonomy_overview` now returns three numbers instead of one**, from a single subquery
    (`count(*) FILTER (WHERE bc.image_id IS NULL)` over a LEFT JOIN — still **no migration**):
    `gate_count` (what Gate 1 measures), `border_case_count` (parked), and `confirmed_count`
    (their sum, the raw inventory). The gate predicate is computed in SQL, not subtracted in the
    client, so it has exactly one definition for every future consumer — W3's trainer included.
  - **"Unless removed from the group" is free**, because the exclusion is a JOIN and not a column
    on the training row: clearing the flag restores the count immediately, with no relabelling and
    no backfill. Pinned by its own test.
  - **Every coverage surface moved to `gate_count`** — the bar and its value, the sort, the
    domain max, the `≤ N imgs` ceiling, and the tag-picker counts. `confirmed_count` survives in
    exactly the two places that mean "rows that exist", not "progress": the manage modal's
    "N confirmed · M pending" line and its remove-confirmation ("N training examples go with it"),
    which is literally what the DELETE takes.
  - The chart's border annotation flipped meaning with the number and now reads `· N parked`,
    outside the bar rather than inside it. It stays visible on purpose: a tag sitting on a pile of
    parked images is a signal about the TAG (too vague to label against), not just those photos.
  - Tests: 2 new backend cases (the split; unflagging restores the count) and 2 new page cases
    (the bar reads the gate number; the ceiling narrows by it), both page ones verified to go red
    when the surface is pointed back at `confirmed_count`.

- 2026-08-21 — **Labeling page: the "Border case" flag** (operator request, pointing at
  /clip-audit as the model). The flag itself is not new — `image_border_cases` (migration 310) and
  `/labeling/border-case` have existed since /clip-audit; the review grid that is meant to REPLACE
  that page simply had no way to reach them, so "unclear even to a human" had nowhere to go except
  a wrong tag or a Dismiss that means something else.
  - **Shared, not copied.** The read, the write and the stability policy moved into one hook,
    `lib/useBorderCases.ts`, with `components/BorderCaseButton.tsx` as the (purely presentational)
    control; /clip-audit's `TrainControl` now renders that same button instead of owning its own
    mutations, and its two hand-rolled `border-cases` queries are gone. That is the same lesson
    TrainControl itself records — a second labeling surface holding a byte-for-byte copy is how
    these two drifted apart before.
  - **Grid stability is the hook's job, not each page's.** Ids ACCUMULATE and only never-seen ones
    are ever requested, so a review — which changes the visible id list — can't blank every flag in
    the grid the way an id-list-keyed query would (PR #994's lesson, now enforced in one place).
    Toggles patch the store; nothing invalidates. Writes are optimistic and roll back from
    `onSettled`, never `onError`, so main.tsx's global "the write failed" toast still fires —
    rule #22's cache policy, same idiom as `pipelineCache`.
  - **Independent of the verdict, deliberately.** A border case is not a third review outcome: the
    tile keeps its Confirm/Dismiss buttons and its place in the grid, and the flag is offered on
    DISMISSED tiles too (which get no tag picker) — "I rejected the model's tag" and "I can't tell
    what it should be" are two different facts and the schema keeps them apart. Nothing
    auto-dismisses; if the operator wants both, that is two clicks.
  - **The data-quality consequence, made visible.** Gate 1 counts `image_training_examples` rows
    and a border case IS one, so flagging alone would have let a tag reach "150 confirmed" on
    images nobody could classify. `taxonomy_overview` now also returns `border_case_count` (the
    uncertain slice of that same total, one added LEFT JOIN — **no migration**), rendered as a
    brick "· N border" beside the bar. Deliberately NOT netted out of `confirmed_count`: whether
    those images train, validate, or get dropped is a W3 decision, and silently redefining the
    gate metric mid-program is not this query's call.
  - Tests: `useBorderCases.test.tsx` (5), `BorderCaseButton.test.tsx` (4), 4 more on the page,
    `TrainControl.test.tsx` rewritten around the split, plus a backend case pinning the
    confirmed/border overlap. Every behavioral guard was verified to go red under a mutation that
    removes it — which is how a sixth test was caught as vacuous (it "pinned" a monotonic-merge
    guard against a late read that the query key already makes unreachable) and deleted along with
    the wrong rationale in the comment.
  - Suites: `pytest -q` 4493 passed / 112 skipped, `vitest run` 631 passed (61 files),
    `tsc --noEmit` clean, `vite build` clean, `eslint` clean on every touched file.

- 2026-08-19 — **Labeling page: the small/large photo switch, shared with Browse** (operator
  request: "the same switch as we have on the browse page"). Taken literally — the control was
  private to `BrowseExperience.tsx`, so it moved to `components/ImageSizeToggle.tsx` and both pages
  now render the SAME component rather than a copy that can drift.
  - **Same mechanism, not just the same buttons.** Both grids express the choice as one
    `--*-min` custom property on the grid wrapper, flowed by `auto-fill minmax(min(var(…),100%),1fr)`,
    with the large value **exactly double** the small one. On the review grid that is
    `TILE_MIN` 14rem → 28rem: 14rem reproduces today's density (against the page's own
    `max-w-5xl` it flows to the same four columns the fixed `md:grid-cols-4` gave) and degrades
    to 3/2/1 on narrower windows instead of cramming four in; 28rem is two big tiles. The
    `aspect-[4/3]` frame is untouched, so the photo doubles while the fixed-rem buttons and tag
    picker below it do not.
  - **Its own persisted key** (`sreality.newDedupLabeling.imageLarge`), NOT Browse's — resizing
    tiles here must never reshape the listing cards there. Pinned by a test that asserts Browse's
    key stays untouched. The boolean-preference machinery moved out of `browseLayout.ts` (a
    Browse-specific module) into `lib/persistedFlag.ts`; `browseLayout` re-exports `readFlag` so
    its existing entry point and tests are unchanged.
  - Tests: first `ImageSizeToggle.test.tsx` (3 — pressed state, sets-a-value-not-toggles, the
    a11y group label each surface passes) plus 2 on the page (the grid's track floor changes and
    the choice survives a remount; Browse's key stays clean). Both page tests verified to go red
    when the flag is de-persisted or the grid ignores it.
  - Suites: `vitest run` 617 passed (59 files), `tsc --noEmit` clean, `eslint` clean (7 pre-existing
    warnings elsewhere, none in the touched files).
- 2026-08-10 — **Labeling page: click a tile to enlarge it** (operator request, pointing at
  /clip-audit as the model). Reuses the SHARED `ImageLightbox` — the one full-screen photo modal
  behind listing detail's gallery and /clip-audit — rather than a second one-off, so keyboard nav,
  the scroll lock and the badge treatment stay identical across the app. A four-up tile is too
  small to judge a room tag on; the modal's arrow keys then walk the rest of the grid without
  going back to it.
  - **The gallery is parallel to the TILES, not to the images**: a position is one proposal row,
    so two models' proposals on the same photo are two stops (exactly as they are two tiles) and
    each stop carries its own proposed tag. Tiles whose photo hasn't arrived yet are skipped, so
    the index handed to the modal is a position in that gallery and never in `proposals` — a
    pinned regression, since the two lists differ precisely while images are still streaming in.
  - **The modal must never contradict the tile it was opened from.** The grid's default view
    badges the *proposed* tag, which `images_public` knows nothing about, so `ImageLightbox` gained
    an optional `tagAt(index)` override; on "Original tag" it is omitted and the lightbox's own
    default (the image row's CLIP call) is already right. Deliberately an override rather than
    synthesising an `ImagePublic` with the label swapped in — a row must not claim CLIP predicted
    something it didn't.
  - Also in the shared component: the position is now **clamped** to the array. The grid behind can
    shrink (a reviewed tile leaving its tab), and an out-of-range index rendered nothing at all
    while the dialog kept holding the page's scroll lock. First real `ImageLightbox.test.tsx`
    (5 tests) pins that plus nav/Escape/badge; both new behaviours were verified to go red when
    reverted.
  - Housekeeping: `rowKey`/`draftKey` were the same string built two ways — now one module-scope
    helper (also what lets the gallery memo drop an `exhaustive-deps` suppression). `cursor-zoom-in`
    on all three thumbnail surfaces that open the modal, so the affordance reads the same app-wide.
  - Suites: `vitest run` 513 passed (48 files), `tsc --noEmit` + `eslint` clean.
- 2026-08-07 (part 2) — **Labeling page review ergonomics** (operator request, four edits): a
  collapsible taxonomy chart, filtering the grid by tag + filtering the tag list by how much
  training data it already has, an **All** tab alongside pending/confirmed/dismissed, and — the
  substantive one — **the grid no longer churns when you review a tile**.
  - **The churn had two independent causes**, both fixed:
    1. `ORDER BY proposed_at DESC` had no tiebreaker, and the backfill inserts a whole batch in
       one transaction — so every row in it carries the *same* `now()` and Postgres was free to
       return ties in a different order on each call. The grid genuinely reshuffled on any
       refetch. Every list query is now totally ordered (`, image_id DESC`).
    2. Reviewing one proposal invalidated the whole proposals query AND the image query was keyed
       on the current id list, so a confirm swapped in an empty cache entry and every tile lost
       its photo at once. Single-tile actions now **patch the cached list in place** (drop the row
       on Pending, patch it in place elsewhere) and photos accumulate in a page-level id→image map
       that only ever fetches ids it has never seen. Other tabs are invalidated, never the visible
       one. Five tests pin this; all five were verified to go red when the old invalidate-and-
       refetch behavior is restored.
  - **The All tab** (`status='all'`) is the union of the other three — every `label_proposals` row
    plus training examples that never had a proposal, as synthetic `model='manual'` rows, same as
    the Confirmed tab already did. A new `trained_label` on every row (the image's *current*
    `image_training_examples` label, or NULL) is what lets the page grey already-handled tiles
    without a second query; reviewing a tile there greys it **in place**, nothing moves.
    `status` is now validated server-side — an unknown value 422s instead of silently listing
    everything while the tab claims to be filtered. **No migration**: both new reads are plain
    queries over existing tables.
  - **The coverage ceiling** ("≤ N training images") narrows the chart *and* the grid's tag
    select — the Gate-1 question is "which tags are still short" — but deliberately NOT the
    per-tile correction picker, which must always offer the whole vocabulary. The currently
    filtered tag is never hidden by the ceiling.
  - Suites: `pytest -q` 2690 passed / 32 skipped, `vitest run` 503 passed, `tsc --noEmit` clean.
- 2026-08-07 — **Labeling page: correct a wrong suggestion instead of only accept/reject it**
  (operator request, pointing at /clip-audit's combobox as the model). Every non-dismissed tile now
  carries a `LabelCombobox` seeded from the proposal's tag; Confirm writes whatever is in it.
  Already-confirmed tiles get "Save tag", which relabels in place through the EXISTING
  `/labeling/training-example` endpoint (the same one /clip-audit's Train CTA uses) rather than
  re-running the confirm flow.
  - **Backend**: `confirm_proposal(..., label=None)`. The override lands in
    `image_training_examples`; `dedup_sim.label_proposals.label` deliberately keeps the model's own
    prediction, so "model said X, operator said Y" stays derivable by comparing the two tables —
    **no migration needed**, which is why none was written.
  - **A freehand correction also registers itself in `dedup_sim.taxonomy_labels`** (same
    transaction, `ON CONFLICT (label) DO NOTHING`). This is load-bearing, not tidiness: the coverage
    chart, the tag picker's options, AND `scripts/label_proposal_backfill.py`'s class list all read
    the taxonomy table, not the training set — so an unregistered label would be invisible in the
    chart, never re-offered for the next image, and impossible for the secondary encoder to ever
    propose. Migration 379 backfilled exactly this class of gap once already; this closes the door
    that would have reopened it.
  - **Adversarial review before merge** (3 dimensions — backend, frontend, UX/regression — each
    finding independently re-verified): 15 candidates, 9 confirmed, deduplicating to 5 real issues,
    all fixed. Two were things neither the type-checker nor jsdom tests could have caught:
    1. *(high)* The picker sat directly ABOVE the Confirm/Dismiss row, so its downward-opening
       absolutely-positioned dropdown painted over those buttons and would have swallowed the first
       click aimed at Confirm — committing whichever taxonomy option happened to sit at that
       y-offset. Both pre-existing usages avoid this by laying picker and action side-by-side; this
       was the first vertical layout. Fixed by moving the picker BELOW the action row.
    2. *(high)* Off-taxonomy labels silently dead-ending (the registration fix above).
    3. *(high)* The page-level `drafts` map was keyed by `image_id` alone while proposals are keyed
       `(image_id, model)` — two models' proposals for one image shared a draft slot, so correcting
       one tile rewrote the other's tag. Re-keyed to `${image_id}:${model}`.
    4. *(high, found while the review ran)* "Confirm selected" ignores per-tile corrections (the
       bulk endpoint takes ids only), so a corrected-AND-selected tile would have had its fix
       silently overwritten by the model's label. A corrected tile now drops out of the selection.
    5. *(medium)* The client always sent its draft, even untouched — a page whose proposal list
       predated a taxonomy rename would resurrect the retired spelling. The label now travels only
       when it is an actual correction; otherwise the server uses its own stored value.
  - I'd also independently caught and fixed a clipping bug before the review: the tile card's
    `overflow-hidden` (there to round the photo) would have clipped the dropdown to nothing. Moved
    it to the inner photo wrapper and pinned it with a structural test that walks the listbox's
    ancestors — verified the test genuinely fails when the class is put back, so it isn't vacuous.
  - Suites: `pytest -q` 2683, `vitest run` 494, `tsc --noEmit` clean, `vite build` clean, both
    codegen checks fresh. No migration in this PR.
- 2026-08-06 (session continuation, part 7) — Fixed a gap in the just-shipped Labeling page
  (operator report: taxonomy showed "0 labels, 0 sampled" and the Confirmed tab was empty despite
  an existing 48-label / ~1,185-image training set). Root cause: `dedup_sim.taxonomy_labels`
  opened empty — nothing ever seeded it from `image_training_examples`'
  pre-existing labels, even though `taxonomy_overview`'s confirmed_count already LEFT JOINs on
  label text (it just had nothing on the left side to join against) and PROGRAM.md's own
  Taxonomy v1 definition (line 38) already points at that table. **Migration 379** backfills
  `dedup_sim.taxonomy_labels` with the 48 distinct `image_training_examples` labels
  (`on conflict (label) do nothing`, one-time, not an ongoing sync) — applied live, confirmed
  48 rows. Separately, `list_proposals(status='confirmed')` in `toolkit/dedup_sim_labeling.py`
  now drives FROM `image_training_examples` (LEFT JOINing the most-recently-confirmed
  `label_proposals` row per image, via `DISTINCT ON`, for display provenance only) instead of
  querying `label_proposals` alone — the pre-existing 1,185 images were trained via
  `/phash-audit`'s Train CTA, never through a proposal, so the Confirmed tab was only ever
  going to show this page's own review actions without it. First cut used a UNION keyed off
  `label_proposals.label`; adversarial review caught that `/phash-audit`'s Train CTA can still
  relabel an image AFTER it's confirmed here (it only ever writes `image_training_examples`,
  never touches `label_proposals`), which would have shown a stale label — switched to always
  reading `te.label` live instead. Verified live: returns exactly 1,185 rows, one per image, all
  `model='manual'` (no secondary-CLIP proposals exist yet). Tests added in
  `tests/toolkit/test_dedup_sim_labeling.py` (no-duplicate-when-both-exist, label filter,
  manual-model tagging, stale-label-after-relabel regression).

- 2026-08-06 (session continuation, part 6 — parallel to the RunPod session below) — Built the
  Labeling page (W1's last unstarted mechanic besides the Dashboard skeleton, which stays a
  placeholder — genuinely no data until W2+).
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
- 2026-08-06 (session continuation, part 5) — **W1's RunPod deliverable closes out.** After
  #982's redesigned pass criteria landed, re-dispatched once more
  ([31085275059](https://github.com/waiff/sreality/actions/runs/31085275059)): pod `ci87ta3vltru6l`
  launched on the RTX 3070 fallback (RTX A2000 again had no capacity), ran for 126s, was cleanly
  terminated — **SMOKE TEST PASSED**, workflow run status `success`, estimated cost **~$0.0046**.
  `desiredStatus` stayed `RUNNING` and the logs endpoint still 400'd, exactly as the part-4 entry
  predicted — no longer treated as failures. Total real spend across all 5 live dispatches this
  session: ~2.2¢. The RunPod client, its capacity/pricing edge cases, and the cost-safety
  guarantee are now proven against real infrastructure end-to-end; nothing further needed here
  until Wave 5 builds the real embedding batch job on top of `RunPodClient.run_job`.
- 2026-08-06 (session continuation, part 4) — Operator added RunPod account funds. Re-dispatch
  ([31083936844](https://github.com/waiff/sreality/actions/runs/31083936844)) got past the
  account-balance blocker and, for the first time, **actually launched a real pod**: `g39f02wj642her`
  on an `NVIDIA GeForce RTX 3070` (\$0.13/hr, after the cheaper RTX A2000 again had no capacity —
  the fallback from #977 worked as designed). Real GPU-hours were billed (~482s ≈ 1.7¢) and the
  pod was cleanly terminated by the client's `finally` guarantee. **Two real API limitations
  surfaced that no amount of pre-reading the docs caught, only actually running it did:**
  - `desiredStatus` never left `RUNNING` for the entire wait window, even though the smoke
    test's own command should finish in well under a minute. On-demand Pods appear to track pod
    (rental) lifecycle, not the inner container process — there's no evidence they self-report
    "my command finished," unlike RunPod's separate Serverless product.
  - The documented SSE logs endpoint (`GET /pods/{id}/logs`) returned a bare 400 with no
    pod-specific detail — RunPod's REST API doesn't appear to actually expose Pod log retrieval
    yet (a Feb-2025 GitHub feature request corroborates this), despite docs suggesting otherwise.
  - **Neither is a code bug** — `run_job`'s `finally`-terminate held regardless, so the pod was
    still torn down correctly. Fixed the *expectations*, not a defect: `wait_for_exit`/
    `fetch_logs` docstrings now say plainly not to rely on either signal; the smoke test's pass
    criteria dropped the "success marker in logs" requirement (structurally unreliable) in favor
    of "a real pod launched on a real GPU, accrued measurable cost, and was torn down cleanly" —
    which IS what actually matters for Wave 1's "prove the pipeline works" goal. Wait window cut
    480s → 120s (no point paying to wait for a status flip that isn't coming).
  - **Design note for whoever builds Wave 5's real embedding batch job:** don't rely on RunPod
    Pod status or logs for "is it done" / "what did it produce" — have the job write its result
    directly to Postgres or R2 (it'll have network access + credentials via env) and have the
    orchestrator poll THAT for completion instead.
  - Also fixed pre-existing (unrelated, landed via #971) `filterRegistry.generated.ts` codegen
    staleness that was blocking this PR's `build` check — bundled since it had to be regenerated
    to get CI green, not touched otherwise.
  - **W1's RunPod deliverable is now genuinely done**: client built, 23 hermetic tests, and the
    core guarantee (launch on a real GPU, bill real time, always tear down) verified against
    real infrastructure across 4 live dispatches. Log/status-based completion detection is
    explicitly NOT solved and explicitly deferred to Wave 5's real design (see note above).
- 2026-08-06 (session continuation, part 3) — Operator confirmed `RUNPOD_API_KEY` was added as
  a repo secret; asked to finish the RunPod piece (Labeling page picked up by a different
  session in parallel). Built `scripts/runpod_client.py` (#972) + a `new_dedup_runpod_smoke_test`
  workflow_dispatch to prove it end-to-end. Three real live dispatches, two real bugs found and
  fixed by actually running it rather than trusting the code on paper — **zero cost incurred
  across all three**, since every failure happened before RunPod ever started billing (the
  point of `run_job`'s launch-outside-try / terminate-in-finally split):
  1. Run [31081790027](https://github.com/waiff/sreality/actions/runs/31081790027) — `cheapest_gpu()`'s
     `communityPrice is not None` filter let through a placeholder catalog entry (id "unknown",
     price 0), which always "won" as cheapest and got rejected by RunPod's pod API (400, not a
     valid `gpuTypeIds` value). Fixed in #975: require `communityPrice > 0`.
  2. Run [31082278388](https://github.com/waiff/sreality/actions/runs/31082278388) — the
     (correctly-picked, this time) cheapest real GPU, RTX A2000, had zero free community-cloud
     instances at that moment — a live availability condition, not a bug (RunPod's community
     cloud is peer-hosted). Fixed in #977: `NoCapacityError` + `run_job_with_fallback`, which
     tries `eligible_gpus()` in ascending price order and only advances past a genuine
     capacity 500, not any other failure.
  3. Run [31083022260](https://github.com/waiff/sreality/actions/runs/31083022260) — the
     fallback logic worked exactly as designed (RTX A2000 → no capacity → tried RTX 3070 next),
     but that attempt hit `"Your account balance is too low to rent a pod. Please add funds to
     your account."` **This is the actual current blocker, and it's on the operator's side, not
     code**: a payment method on file isn't the same as an available RunPod balance. Needs the
     operator to add funds/credit in the RunPod dashboard (Billing) before any pod can launch.
  - Minor known cosmetic gap (not fixed, not worth its own PR): the smoke-test driver's
    top-level error message ("job failed on every eligible GPU type") is misleading for case 3
    — the fallback didn't actually exhaust every option, it stopped correctly on a non-capacity
    error after trying 2. Functionally correct (it did NOT keep retrying), just an imprecise
    log line; worth tightening whenever this file is touched again for the real end-to-end run.
  - Next session (once funds are added): re-dispatch `new_dedup_runpod_smoke_test.yml` from
    main, confirm a pod actually boots + runs the CUDA op + reports `SMOKE_TEST_OK` + terminates,
    note the real elapsed time / cost in this ledger. That closes out W1's RunPod deliverable.
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
