# Dedup LLM cost reduction — execution plan

**Status: EXECUTING (2026-07-08).**
- **Phase 2.2 (non-byt attribute auto-merge): SHIPPED (#722) + ENABLED.** Re-validated 99.6%
  on current data (572/574; the 2 misses are floor-plan dismissals the retained gate still
  catches). `dedup_nonbyt_attr_merge_enabled=true` live; watch `/dedup#funnel` "Area + exact
  price".
- **Phase 2.2 arms (a)+(b) (non-byt pHash single-pair + pair max-cosine): SHIPPED (2026-07-10),
  default OFF pending operator flip.** Re-validated on the then-current 1,283-pair non-byt
  decided corpus: pHash ≥1 pair 99.41% (1,017/1,023, 79.7% coverage); pair max-cosine ≥0.98
  99.71% (1,018/1,021, 79.6% coverage). Settings `dedup_nonbyt_phash_single_enabled` (bool) +
  `dedup_nonbyt_cosine_merge_min` (0=off; validated operating point 0.98). Distinct reasons
  `phash_single` / `cosine_high`; funnel node "Pair max-cosine". Both arms keep the floor-plan
  gate + both-site-plan step-aside unchanged.
- **Phase 3.3 (floor-plan gate scope-down): DROPPED.** Live data refuted the premise — the
  ≥4-pHash-identical stratum still gets 4.08% (1,343/30d) `different_layout` dismissals, i.e.
  the development shared-photo false-merge guard, not waste. The fid1 dismissal-memory piece
  already exists (the prior-dismissal consult).
- **Phase 2.1 (completeness-timing readiness): SHIPPED (#723) + ENABLED.** Defers a pair while
  either side has an image pending download (`storage_path NULL, download_attempts < 5`;
  bounded by the 5-attempt give-up). `dedup_defer_incomplete_downloads=true` live.
- **Phase 2.4 (shared-render blacklist): DECLINED.** Redundant where it matters — the
  `render_score >= 0.95` exclusion already targets "a development reusing renders across
  units" (its own code comment), so for byt it adds nothing; for non-byt it's modest precision
  only; the merge record is spotless (0/49k unmerged) so it prevents ≈0 real false merges; and
  it would move development pairs from the cheap gate-dismissal to the pricier visual path
  (cost-neutral-to-negative). Not worth a change to the load-bearing pHash query.
- **Phase 4.1 (targeted batch lane, original "warm only the first-priority room" design):
  SUPERSEDED (2026-07-14).** `dedup-vision-and-backlog-overhaul.md` §1 found the speculative
  pre-warmer (a second process re-deriving the engine's work-list — this section's original
  `submit_dedup_batch` collect() funnel, `--lane street|geo|candidates`, `--warm-rooms`) drew a
  near-disjoint pair set from the live engine (~1% overlap) — six independent divergences, not
  fixable by retargeting. **Rebuilt as engine-fed deferral (Session 4, PR merged 2026-07-14):**
  the engine's own sweep lanes (full street, geo, byt-geo, candidates — never dirty/realtime)
  spool a cold call straight into `dedup_batch_requests` (`batch_id NULL`) instead of paying
  inline, gated by `dedup_engine_batch_defer_enabled` (default off); `submit_dedup_batch`'s only
  job is now flushing that spool. Selection identity holds by construction. `dedup_batch_warmer_enabled`
  is retired (inert, kept only for historical `app_settings` rows). Full design +
  status: `dedup-vision-and-backlog-overhaul.md` §1.2/§5, `roadmap/dedup-track.md`.
- **Phase 4 item 2 (facade-as-dismisser, fid5): SHIPPED (2026-07-10), default OFF.**
  `dedup_facade_dismiss_enabled`: a confident facade Low qualifies for auto-dismiss on non-byt
  (byt never; all other conservatism unchanged — all-rooms-verdicted, High merges, Medium
  queues). Replay on decided pairs: facade-Low+no-High agreed with the final outcome 15/15
  (0 conflicts); 0 of 79 operator-approved non-byt merges carry a facade Low. Enabling is the
  operator's flip.
- **Phase 4.2 (harness): WORKING** after two fixes (#726 corpus, #727 room-source) + Gemini
  capability (#754 provider routing, #755 tool-schema strip, #760 3.x prices).
- **Model flips (4.3) / cheaper VLMs: CLOSED 2026-07-11, RE-CONFIRMED + widened 2026-07-13
  (Session 3, `docs/design/dedup-vision-model-bakeoff-2026-07.md`).** The 07-11 pass measured only
  compare RECALL (Haiku@768 20% / Haiku@1568 30% / Gemini-3.1-Pro@1568 72% vs Sonnet's 88.3%
  self-baseline). Session 3 added the missing dimension — PRECISION on a frozen golden set of
  confirmed-DIFFERENT pairs (`dedup_golden_sets` `2026-07-13-session3-baseline`) — across all three
  forensic lanes, for GPT-5-mini, Qwen3-VL-235B/30B, and Gemini-3.1-flash-lite (Gemini-2.5-flash-lite
  is 404/closed). **Result: every cheap candidate has good recall but 36–66% precision — they emit the
  auto-merge verdict (High / same_layout / same_unit) on a THIRD to TWO-THIRDS of confirmed-different
  pairs.** gpt-5-mini (reasoning) is best on recall (compare 88.3%, site recall 100%) but still 56–64%
  precision AND far too slow (~12 s/call, 63 min/run — the compare lane is ~30k calls/mo). The cheap
  VLM class is disqualified on PRECISION, not recall — a distinction the recall-only 07-11 pass could
  not see. Per-pair evidence is browsable on `/model-testing` (migration 303).
  gemini-2.5-pro/2.5-flash-lite retired for new projects; Google raised 3.1 Pro to $2/$12 on Jul-2.
  - **SUPERSEDED — the operator OVERRODE "Sonnet stays" (2026-07-13/14).** The precision analysis
    above still holds, but the operator prioritized **provider diversification off Anthropic** (credit
    depletes ~daily) over it and flipped **all three forensic lanes + enrichment to gpt-5-mini** via a
    new OpenAI Batch lane (PR #787, live **2026-07-13 22:25 UTC**), then flipped the **cosine cheap
    band (`dedup_visual_match_model_haiku`) + the LLM room-classify fallback (`llm_room_classify_model`)**
    to gpt-5-mini on **2026-07-14 10:23 UTC** (PR A) once the Anthropic credit outage made the Haiku
    cheap band 100%-error. The dedup funnel is now **Anthropic-free**; standing Anthropic burn ≈ $0
    (only on-demand estimation / parse_url / summarize stay on Sonnet). The cost path is now free-first
    arms + the **OpenAI** batch −50% (Session 4 engine-fed rebuild) + embedding coverage. Watch the
    false-merge risk (gpt-5-mini site_plan precision 86→52 on the hard photo-identical stratum) — the
    operator owns that audit; any backlog blitz stays gated on it.
- **Encoder upgrades (operator idea, evaluated 2026-07-11): CLOSED.** The June-26 DINOv2
  A/B (results were buried in workflow logs) showed WORSE pair separation than CLIP B/32
  (-0.0014 vs -0.0044) with hard negatives at cosine 1.0000 — identical marketing renders
  defeat any encoder; the render-score exclusion is the real mitigation. ViT-L/14 rejected
  (10-15x CPU for ~$2-3/day); DINOv3 re-run timed out and was not pursued.
- **768px (3.1):** still open — needs a Sonnet@768 recall run (~$2) whenever desired.

Update this list as phases ship.
Written for an autonomous executor session (Opus, max reasoning) with no access to the
investigation conversation — everything needed is in this file. The investigation behind it:
five parallel read-only replays of free signals (pHash, CLIP cosine, attributes, CLIP tags,
operator corrections) against every paid LLM verdict in `dedup_pair_audit` / the vision pair
caches, plus a cost anatomy of `llm_calls` and a code audit of the engine, batch and harness
paths.

**Goal:** cut dedup vision spend ~10× vs the current trajectory without accuracy loss.
Hard bar: any rule that auto-decides a pair without the LLM must replay at **≥99% agreement**
with existing engine/operator decisions. A 95% tier exists but every 95%-tier lever is
**default-OFF behind an operator gate** (see §Operator gates).

---

## 0. Baseline and evidence (do not re-derive; re-verify at most)

### Spend (last 30 d ≈ program lifetime, from `llm_calls`, timestamp column is `called_at`)

| Lane (`called_for`) | Model | Calls | $/30d | avg $/OK call |
|---|---|---|---|---|
| compare_listings_visually | sonnet-4-5 | 21.7k | **222.8** | 0.0103 |
| compare_listing_floor_plans | sonnet-4-5 | 9.2k | **155.8** | 0.0203 |
| compare_listing_site_plans | sonnet-4-5 | 4.5k | **69.3** | 0.0354 |
| compare_listings_visually | haiku-4-5 | 11.9k | 23.5 | 0.0020 |
| classify_listing_images | haiku-4-5 | 2.9k | 15.7 | 0.0070 |
| **Total dedup vision** | | | **~493** | $0.041/pair e2e |

Run-rate at plan time ≈ $40/day and growing. 12,149 distinct paid pairs / 30d; ~2,508
engine-decided pairs/mo (86.3% merged). Error calls bill $0. Caches work (repeat evaluations
cost ~$0.30 total; cache hit does not decrement the vision budget — `scripts/dedup_engine.py`
around the compare-budget decrement, grep `vision_budget`).

### Waste pockets (the levers' evidence)

1. **$203/mo (45% of pair spend) ends operator-QUEUED**, not resolved (visual $134, site $38,
   floor $30). Queue backlog ~12.7k in `property_identity_candidates`: filter
   `status='proposed'` and read the separate `engine_decision` column (mig 272) for
   `visual_inconclusive` — it is NOT a `status` value (status CHECK allows only
   proposed/merged/dismissed, mig 097). Queued pairs have **no ground truth** — free rules
   cannot be validated against them; don't try.
2. **Floor-plan gate spent $109/mo re-confirming pairs pHash had already decided** (gate ran on
   4,858 phash-merged + 453 phash-dismissed pairs; contradiction rate ~8.5%, and the known
   errors are false *dismissals* — see feedback fid1 below). Plus $10/mo of `no_2d_plan`
   verdicts (Sonnet paid to discover there is no plan) and $9/mo site `inconclusive`.
3. **Timing leak:** 458 byt pairs/mo (43.4% of byt paid merges) already satisfied the
   **existing** free pHash rule at decision time replay (98.9% precision) — the images/pHash/
   CLIP tags landed *after* the engine decided. The tagging-readiness gate covers tags but not
   late image downloads. Additionally ~736 pairs/90d (483 byt + 223 dům + 30 pozemek, ≈$35)
   had ≥2 pHash-identical images passing all exclusions yet still went paid — decision timing
   plus the geo path's forensic-High-only merge policy.
4. **Payload:** room compares send ALL images of a room type per side at 1568px
   (`toolkit/visual_match.py`, `_COMPARE_MAX_EDGE = DOCUMENT_MAX_EDGE`; the code's own comment
   says the intended follow-up is `COMPARISON_MAX_EDGE = 768`). Plan compares send up to
   `_MAX_PLANS_PER_SIDE = 20` plans/side at 1568px — why site plans are the priciest OK call.

### Ground-truth quality (why replays are trustworthy)

- `property_merge_events`: 46,835 `image_phash/auto` merges — **0 undone**; 2,403
  `visual_match/auto` — **0 undone**; all 92 undone merges belong to the retired
  `address_exact` rule (6.7% of its 1,372).
- Operator feedback (`dedup_decision_feedback`, 11 rows, all `is_incorrect=true`): implies
  ~0.2% error on visual-stage decisions. Themes: **stop-at-first-High on a generic feature**
  (fid8/fid11 — radiator/window matched; kitchen + půdorys never compared); same development,
  adjacent buildings (fid5 — operator asks for priority-room prompts and a
  **facade-as-dismisser option, "set this up as an option, turn it on"**); floor-plan-gate
  false dismissal of an 8-pair pHash match, re-dismissed ~25× over 3 days — no dismissal
  memory on the gate path (fid1, pair had `floor_plan_different_layout`); lighting used as a
  differentiator (fid7); pozemek merged on weak anchors at cos 0.8165 (fid10). Operator merges
  after an engine dismissal: **0**.

### Replay results (the validated numbers each phase cites)

| Signal / rule | Scope | Precision vs LLM | Coverage | Verdict |
|---|---|---|---|---|
| pHash min-matches 2→1, any image | non-byt | **99.77%** (438M/1D) | 79.5% of non-byt paid merges | SHIP |
| pair max-cosine ≥0.98 | non-byt | **99.57%** (464M/2D) | 84% | SHIP |
| union: cos≥0.98 OR pHash≤6 | non-byt | **99.61%** (506M/2D) | 90.2% of non-byt paid corpus | SHIP |
| areaΔ≤2% (coalesce `estate_area`) + price EXACTLY equal | non-byt | **99.6%** (723/726) | 71% of non-byt; fires on 328 queued + 40 operator-merges agree, 0 dismissals contradict | SHIP |
| existing byt pHash rule, re-checked after ingest completes | byt | **98.9%** (453M/5D) | 43.4% of byt paid merges | SHIP (timing fix, not a new rule) |
| cosine ≥0.96 auto-merge | byt | 95.9% | 58% | 95% tier — operator gate |
| any byt attribute-merge conjunction | byt | ≤98.0%, and fires on **29% of the 209 reconstructed wrong pairs** from the retired address rule (new-development trap) | — | DO NOT SHIP |
| auto-dismiss from low cosine / high pHash distance | all | ≤81% | — | DEAD — do not ship |
| price-ratio dismiss >1.5× | all | 65% (INVERTS — big gaps = same property, portal price-basis errors) | — | anti-signal |
| Δfloor=1 dismiss | all | 16% (84% same property — validates engine's Δ=1 tolerance) | — | anti-signal |
| price>1.15 AND disposition differs (adjacent) | byt | 97.4% (n=39) | $3.7/mo | 95% tier, marginal |

Why byt image signals cap at ~96–98%: the false positives are **literally identical shared
marketing/render images** across different units (7 LLM-dismissed pairs at cosine = 1.00).
No threshold sees through identical pixels → the fix is the shared-render blacklist (§2.4).

### CLIP tag quality (N=31,392 images with both CLIP tag and Haiku label)

kitchen recall 90.3% / precision 76.3% (130 real hallways/lobbies tagged kitchen at avg conf
0.65 vs 0.90 for correct tags); WC↔bathroom cross-labeled 18.6% of sanitary; merging
bathroom+toilet → one class = **99.4% recall**; living_room recall **37.1%** (single "sofa"
prompt); hallway precision 37.6%; `staircase_interior` precision **16.7%** and it sits in the
`NON_INTERIOR_TAGS` exclusion — the #1 false blocker of free pHash merges. Tag errors blocked
only ~$2–6/90d of paid spend — tag fixes are **precision** work, not savings. Re-tagging is
free (`scripts/retag_from_embeddings.py` re-runs zero-shot from 5.67M stored embeddings; flip
`app_settings.clip_taxonomy_retag_after`). CLIP coverage is 100% of stored images (the
readiness gate works); pozemek simply has no interiors.

### Batch + models

- Old warmer (retired in #695 / commit `bc2367f`, `dedup_batch_warmer_enabled=false` since
  2026-07-04): measured waste 79–93% of pre-bought verdicts never consumed. Retirement was
  correct; do NOT re-enable as-was.
- **99.2% of paid $ occurs ≥1h after both listings exist**; sweep lanes (full/geo/candidates/
  untagged) ≈ 62% of vision calls, realtime+dirty ≈ 38%. Anthropic batch = −50%, turnaround
  mostly <1h (max 24h) vs 6h sweep cadences.
- Models: `llm_visual_match_model` / `llm_floor_plan_match_model` / `llm_site_plan_match_model`
  = sonnet-4-5; `llm_room_classify_model` = haiku-4-5. Cosine routing live:
  `dedup_clip_cosine_enabled=true`, `dedup_cosine_haiku_min=0.93` (code defaults 0.90/0.70 in
  `toolkit/dedup_engine.py` `CosineBands`). Haiku-Low is operator-calibrated: **0/273 operator
  merges were Low-verdict pairs** (auto-dismiss calibration in `scripts/dedup_engine.py`).
  `validate_vision_models.py` harness: compare+classify only, `min_compare_recall=1.0`,
  **has never produced a passing run**; does not use `dedup_golden_pairs`
  (`scripts/build_golden_set.py`, `scripts/eval_identity.py`). `api/providers/gemini.py`
  exists and `GEMINI_API_KEY` is already in `dedup_engine.yml` env; the batch lane is
  Anthropic-only (`AnthropicProvider.build_batch_request_params`).

---

## 1. Ground rules (every phase)

1. **Replay before enable.** Every auto-decide rule ships with a replay run (SQL in §Appendix)
   re-executed on current data in the PR description. If precision moved below bar, stop.
2. **Kill switch per lever** via `app_settings` (registry in `toolkit/dedup_settings.py`),
   default reflecting the rollout decision; flips are operator-tunable and logged in
   `app_settings_history`.
3. **Merges stay reversible** (`property_merge_events` / `unmerge_group`); every new auto-merge
   arm writes a distinct `reason` (e.g. `auto/phash_single`, `auto/cosine_high`,
   `auto/attr_exact`) and a `dedup_pair_audit` row with a distinct stage/detail so the funnel
   can attribute it.
4. **Dashboard consistency is contractual:** any new stage/reason/called_for extends
   `frontend/src/lib/dedupFunnel.ts` (the shared registry both /dedup and /costs consume) in
   the same PR. Check `dedup_pair_audit` CHECK constraints before writing new stage values;
   widen via a new migration if needed, and deploy tolerant readers before writers
   (prompt-before-enum lesson).
5. **Architecture rule 15 touchpoints need explicit operator sign-off** (§Operator gates).
6. Migrations: append-only `NNN_*.sql`, applied via Supabase MCP `apply_migration`
   (project `erlvtprrmrylhznfyaih`). pg_cron in migrations must use the guarded do-block
   (migration 136 pattern) or CI replay fails. Heavy public aggregates → matview + pg_cron
   (anon 3s timeout).
7. DB reads in dev sessions: `psql`/`SUPABASE_DB_URL` are NOT available locally — use Supabase
   MCP `execute_sql`, read-only, bounded/LIMITed. MCP `execute_sql` can time out yet COMMIT —
   never use it for writes; migrations only via `apply_migration`.
8. Git: branch off **origin/main** — this is MANDATORY, not habit: at plan time the working
   tree sits on stale local branch `fix/dedup-feedback-flag-sync` (its change already merged
   as PR #720 / `77a64d4`; leave the branch alone), 4 commits behind origin/main, and
   `frontend/src/lib/dedupFunnel.ts` + `migrations/282_dedup_funnel_public.sql` (PR #721)
   exist ONLY on origin/main — rule 4's registry file "goes missing" if you branch from the
   local checkout. `git fetch origin && git checkout -b <branch> origin/main` first. One PR
   per phase item, draft early, CI green before merge (**main is not branch-protected — a red
   PR can deploy**). `gh pr edit` fails on this repo — use `gh api -X PATCH`.
9. Tests in `tests/test_dedup_engine.py` follow the existing `_FakeConn` pattern — remember it
   cannot catch CHECK/UNIQUE/FK violations; verify constraints against the live schema.
10. Update `roadmap/<relevant-track>.md` (check `ROADMAP.md` index for the dedup track) in the
    same PR as each shipped phase.

---

## 2. Phase 1 — Free-first (validated ≥99%; target −$45–60/mo direct + queue relief + latency)

### 2.1 Completeness-timing fix (the biggest single lever)
**Change:** a pair may consume paid vision only when BOTH sides are ingest-complete:
images downloaded (R2), `images.phash` computed, CLIP tags present. Extend the existing
tagging-readiness deferral (pairs already defer on `clip_tagged_at IS NULL`) to cover image
download + phash presence; AND re-run the free fast-path (`classify_pair` → pHash → cosine)
immediately before spending budget on a pair (cheap, in-memory — closes the ~736-pair/$35
leak where the free signal existed but the decision predated it).
**Where:** `scripts/dedup_engine.py` pair-selection/readiness section + the paid-path entry;
the deferral mirrors `floor_plan_pending` handling.
**Validation:** replay = §0 numbers (98.9% byt, existing rule). Acceptance: byt paid-merge
volume drops ≥30% within a week; funnel capture strip shows the shift free↔paid; median
new-pair merge latency does not regress (free path is faster).
**Risk:** starving the paid lane if ingest stalls — add a max-defer age (e.g. 48h) after which
the pair proceeds regardless; count deferrals in `dedup_engine_runs`.

### 2.2 Non-byt free auto-decide (three arms, one gate)
**Change:** for `category_main != 'byt'` pairs, auto-merge WITHOUT vision when ANY of:
(a) ≥1 pHash-identical image pair (Hamming ≤ `PHASH_IDENTICAL_MAX`=6, any room, i.e.
`PHASH_MIN_IDENTICAL_PAIRS` 2→1 for non-byt); (b) pair max CLIP cosine ≥ 0.98 (helper in
`toolkit/clip_dedup.py`; both sides must have embeddings); (c) area Δ≤2% using
`coalesce(area_m2, estate_area, usable_area)` + `price_czk` EXACTLY equal, keeping the
existing unit-marker contradiction guard. All existing rejects (category compatibility,
geo distance, house-number) still run first.
**Rule 15 touchpoint:** today "the forensic High verdict is the sole auto-merge gate" for the
geo path — this amends that. **Operator gate: get sign-off before merge** (the evidence is the
99.6–99.8% replays + 0 undone image-signal merges ever + 40 agreeing operator hand-merges).
**Downstream gates:** keep the floor-plan/site-plan gates on these merges only until Phase 3.3
ships Haiku there, then apply 3.3's skip logic — otherwise the saving just migrates lanes.
**Settings:** `dedup_nonbyt_phash_single_enabled`, `dedup_nonbyt_cosine_merge_min` (0 = off,
default 0.98), `dedup_nonbyt_attr_merge_enabled`.
**Acceptance:** non-byt paid compare volume −~85%; non-byt operator queue shrinks (attr arm
alone clears ~40% = ~330 queued pairs on first sweep); `property_merge_events` unmerge count
stays 0 over the observation window.

### 2.3 `no_2d_plan` / plan-presence from CLIP (skip paid discovery)
**Change:** before a paid floor-plan (resp. site-plan) call, check CLIP `floor_plan` /
`site_plan` tags (confidence ≥ `FLOOR_PLAN_MIN_CONFIDENCE`=0.50). If NEITHER side has one, the
gate resolves `no_2d_plan` (→ proceed, per rule 15) without the call.
**Validation:** replay against the 528 cached `no_2d_plan` verdicts: what fraction had zero
CLIP-tagged plans (expect ≫95%); quantify the conservatism loss from CLIP's 93.6% floor-plan
recall (a missed plan skips a gate that might have vetoed) — report in the PR; ship if the
false-skip rate on *contradiction-relevant* pairs is <1%.
**Saving:** ~$10–19/mo; also removes most site `inconclusive` burn on plan-less listings.

### 2.4 Shared-render blacklist (the byt ceiling-raiser)
**Change:** a nightly job (or maintenance step) marks images whose exact/near pHash
(Hamming ≤2) appears across > K distinct `property_id`s (start K=3) as `shared_asset`;
excluded from pHash matching, pair-cosine, and compare payloads (all families). Storage: a
flag/table keyed on phash or image_id + a migration.
**Why:** the 7 cosine=1.00 LLM-dismissed pairs (identical development renders) are exactly
what caps byt image signals at 96–98%; this also directly targets the wrong-merge themes
(new-dev marketing reuse).
**Validation:** re-run the byt cosine/pHash replay (§Appendix) with blacklisted images
excluded; report the new precision curve. If byt pHash precision reaches ≥99%, propose (do not
ship) byt expansion as a follow-up operator decision.

### 2.5 CLIP taxonomy v2 + free re-tag + $1.40 audit
**Change (code-only + free re-tag):** in `toolkit/room_taxonomy.py` +
`data/clip_taxonomy.json` + `scraper/clip_tagger.py`:
(a) merge bathroom+toilet → `sanitary` for matching (`ROOM_FAMILIES`, `DISTINCTIVE_ROOMS` —
keep fine tags); (b) demote `staircase_interior` out of `NON_INTERIOR_TAGS` (16.7% precision);
(c) add an entrance-lobby anchor collapsing to hallway; multi-anchor ensembles for kitchen and
living_room; drop the "(koupelna)"/"(WC)" parentheticals (English-trained text tower);
(d) confidence floor 0.70 for the two load-bearing roles — distinctive qualification and
non-interior exclusion (low-confidence exclusions count as untagged — recall-safe); when
adding ensembles, sum collapsed-class probability rather than argmax per-anchor.
**Then:** re-tag from stored embeddings (`scripts/retag_from_embeddings.py`, flip
`clip_taxonomy_retag_after`); run the Haiku audit — 2,000 images, 200/class, stratified by
CLIP-confidence tercile (oversample low) + portal + category, disjoint from the cached 31.9k
(`classify_listing_images` @ $0.0007/image ≈ **$1.40**). Gate: kitchen recall >90%, sanitary
precision >76%, per-class agreement ≥87% baseline. Report the before/after confusion matrix.

---

## 3. Phase 2 — Payload + floor-plan gate (target −$190–210/mo)

### 3.1 768px compare edge
**Change:** `toolkit/visual_match.py`: photo compares use `COMPARISON_MAX_EDGE = 768`
(the code's own stated follow-up); plan/document compares stay at 1568px (legibility).
**Gate:** a green `validate_vision_models.py` run for (sonnet-4-5, 768): historical Highs must
stay High (`min_compare_recall=1.0`). The harness has never passed — fixing whatever breaks it
is in scope for 3.2/4.2. Saving ≈ −40% of compare input tokens ≈ $80–90/mo at current volume.

### 3.2 Site-plan payload cap
**Change:** `_MAX_PLANS_PER_SIDE` 20 → 8 (most recent first). Validate on a 100-pair sample:
verdicts must be stable vs cached (report agreement; ship at ≥99%).

### 3.3 Floor-plan gate scope-down (**rule 15 amendment — operator gate**)
Today the gate runs on every pHash fast-path merge; that is the $109/mo confirm-only bucket
and its known failures are false dismissals (fid1). **Change, three parts:**
(a) skip the gate when pHash found ≥4 identical image pairs (replay first: gate-contradiction
rate in that stratum from `listing_floor_plan_matches` × phash counts; expect ≪8.5%);
(b) free `same_layout` when the two sides share a pHash-identical floor-plan-tagged image —
replay against the 6,153 cached `same_layout` verdicts before shipping;
(c) **dismissal memory:** a gate-vetoed (`different_layout`) pair records a dismissal with a
TTL/snapshot key so it is not re-evaluated ~25× (fid1 churn); re-open on image-set change.
**Settings:** `dedup_floor_plan_gate_min_phash_skip` (0 = never skip, default 4).

---

## 4. Phase 3 — Batch + models (residual Sonnet ÷2–3)

### 4.1 Targeted batch lane (NOT the old warmer)
**Change:** sweep lanes (full/geo/candidates/untagged — ~62% of calls) submit their
already-routed vision work as Anthropic batches (50% off) instead of sync; realtime worker +
dirty lanes stay sync. Reuse `dedup_batches`/`dedup_batch_requests` +
`submit_dedup_batch.py`/`ingest_dedup_batch.py` plumbing, but submit ONLY pairs the free
funnel has already routed to vision in this run (no all-rooms pre-buy — the old warmer wasted
79–93%). Fix the known cache-key drift first: warm-lane image filtering must mirror the
engine's render/shared-asset exclusions (cache is keyed `(a,b,room,model)` — unique index in
mig 129 — and lookups ignore `image_ids`: `_cache_lookup` in `toolkit/visual_match.py`;
the write is `_cache_store`/`store_visual_verdict` there, called from
`scripts/ingest_dedup_batch.py`; `submit_dedup_batch.py` only does lookups). Batch entries are
model-coupled: flush/ignore warm entries on any model flip. Scheduling: GH sub-hourly crons
are throttled to 2–3h real (fleet-measured) — drive submit/ingest from the realtime worker
lane or hourly crons, and mind a >1h batch leaving both-plan pairs in `floor_plan_pending`
defer (bounded by 2.1's max-defer age).
**Saving:** ≈ −$140/mo at current volume; compounds with model flips (discount applies to
whatever model runs).

### 4.2 Harness extension → green run
Extend `validate_vision_models.py` to floor-plan and site-plan lanes (replay historical
`same_layout`/`different_layout`/`same_unit`/`different_unit` verdicts as the recall set) and
wire in `dedup_golden_pairs` for precision (is_same=false pairs must not go High). Fix
whatever made all 12 prior runs fail or cancel (10 failed + 2 cancelled, 2026-06-17/18 —
post-credit-outage; likely infra).
**Nothing in 4.3 ships without a green run.**

### 4.3 Model flips behind the green harness
(a) `llm_floor_plan_match_model` → haiku-4-5 (≈ −$104/mo); (b) `llm_site_plan_match_model` →
haiku-4-5 (≈ −$46/mo); (c) compare cascade: Haiku walks the rooms; Sonnet is invoked only to
confirm a High/Medium before merge; Haiku Low is terminal (operator-calibrated 0/273).
Escalation ≈46% of rooms today, less after Phase 1 removes easy merges (≈ −35% of residual
compare Sonnet). Settings flips are production changes — **operator gate** per flip, staged
one lane at a time with a 1-week observation (unmerge count, feedback flags, verdict-mix drift
on /costs + /dedup funnel).
(d) Optional: Gemini Flash trial through the existing `GeminiProvider` on the classify lane
first (lowest risk), only after (a)–(c) settle; batch lane stays Anthropic.

---

## 5. Phase 4 — Queue frontier + precision (ongoing, small $, fixes flagged wrong decisions)

1. **Priority-room ordering:** distinctive room (kitchen/sanitary) or floor plan must
   corroborate before a High on a generic room can auto-merge (fid8/fid11 fix). Slight cost
   increase per merge pair; offset by the cascade. Replay: how many historical High-merges had
   only generic-room Highs — report scale before shipping.
2. **Facade-as-dismisser option** (fid5, operator-requested): compare facades when both sides
   have `exterior_facade`-tagged images; a confident "different building" verdict dismisses.
   Default OFF (`dedup_facade_dismiss_enabled`); replay against decided pairs first.
3. **Prompt hardening** (compare prompt in `app_settings`): lighting is not a differentiator
   (fid7); attend to floor level, kitchen geometry (corner vs one-wall), oven height, cabinets
   (fid5 note verbatim).
4. **Queue triage:** order the operator queue by pair cosine (desc) so high-yield pairs surface
   first; the queue is the calibration asset — ~50 operator labels/week create ground truth
   exactly where the model is uncertain (feeds the next replay round).

---

## 6. Operator gates (stop and ask; everything else is autonomous)

| Gate | Phase | What to present |
|---|---|---|
| Rule-15 amendment: geo/non-byt free auto-merge arms | 2.2 | replay table §0, 0-undone record, reversibility |
| Rule-15 amendment: floor-plan gate skip on strong pHash | 3.3 | contradiction-rate replay in the ≥4-match stratum + fid1 |
| Any production `app_settings` model flip | 4.3 | green harness run + staged rollout plan |
| Any 95%-tier lever (byt cos≥0.96 middle path; price×disposition dismiss) | — | default OFF; present precision + the rule-B trap evidence |
| Destructive migration (none planned) | — | database skill rules |

## 7. Measurement and success criteria

- **Weekly checkpoint** on /costs (feature table + dedup category card) and /dedup#funnel
  (capture strip free/paid/manual): expected trajectory ≈ $493/mo → ~$135 (after Phases 1–2)
  → ~$60–75 (after Phase 3) at current volume.
- **Invariants:** `property_merge_events` unmerges of image/attr-signal merges stay 0;
  `dedup_decision_feedback` inflow does not accelerate; funnel pairs-vs-evaluations stay
  consistent; matviews keep refreshing (pg_cron 15-min).
- Each phase PR includes before/after lane numbers from `llm_calls` (SQL below).

## 8. Appendix — replay corpus SQL (re-run per phase; Supabase MCP, read-only)

Corpus (paid, decided): latest terminal per canonical pair —
```sql
WITH decided AS (
  SELECT DISTINCT ON (least(left_sreality_id,right_sreality_id),
                      greatest(left_sreality_id,right_sreality_id))
         least(left_sreality_id,right_sreality_id) a,
         greatest(left_sreality_id,right_sreality_id) b,
         outcome, category_main, run_at
  FROM dedup_pair_audit
  WHERE stage='visual' AND source='engine' AND outcome IN ('merged','dismissed')
  ORDER BY 1,2, run_at DESC)
SELECT ... -- join signals per pair
```
(Schema: `left/right_sreality_id`, `left/right_property_id`, `run_at`, `created_at` — mig 227;
`source`/`merge_group_id` — mig 229. No `decided_at` column exists.)
Pair signals: max cosine `SELECT max(1-(ea.embedding <=> eb.embedding)) FROM
image_clip_embeddings ea JOIN images ia ON ... /* side A */, ...` and min Hamming
`min(bit_count((x.phash # y.phash)::bit(64)))`; run chunked (`(a+b)%8`) to stay inside MCP
timeouts. Lane spend: `SELECT called_for, model, count(*), sum(cost_usd) FROM llm_calls WHERE
called_at > now()-interval '30 days' AND called_for LIKE ANY(ARRAY['compare_%','classify_%'])
GROUP BY 1,2`. Verdict caches: `listing_visual_matches` (pair×room×model),
`listing_floor_plan_matches`, `listing_site_plan_matches` (pair grain),
`image_room_classifications` (image grain, cost pre-split per row).
