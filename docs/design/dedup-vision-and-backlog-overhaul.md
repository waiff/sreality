# Dedup vision + backlog overhaul — validated findings and program plan

**Status: INVESTIGATION COMPLETE (2026-07-12). This doc is the Session-1 deliverable of the
LLM-cost-reduction + dedup-quality program; §6 is the operator-set sequence for Sessions 2–5.**
It extends — does not replace — `docs/design/dedup-cost-reduction.md` (the executing cost plan):
that doc's shipped-phase status list stays authoritative for what already shipped; THIS doc
governs the batch lane's future (§4.1 there, re-decided here), the free-signal precision work,
and the review-queue exit strategy. Every number below was re-derived from production on
2026-07-12 (~20:45–21:30 UTC) by two independent query paths and spot-verified a third time;
nothing is carried forward from memory unverified.

Method note: all findings come from the production DB (read-only) + code archaeology on
`origin/main` (405fc65). No live LLM calls were needed, so the ~daily Anthropic credit
depletions did not block anything and there is no "verify after top-up" residue for Session 1.

---

## 1. Finding I-1 — the batch warmer and the live engine draw near-disjoint pair sets: CONFIRMED, and worse than reported

Prior session's claims re-measured (window = warmer flip-on 2026-07-09 15:00 UTC → 07-12):

| claim (prior) | re-measured 07-12 | verdict |
|---|---|---|
| ~23 of ~2,391 warmed compare pairs overlap engine visual decisions | **24 of 2,446** warmed vs 2,232 engine visual pairs (~1.0%) | confirmed |
| warm-cache consumption ~$0.34 of $68.82 billed (~0.5%) | **$0.36 of $68.82** (compare 23 reqs ≈ $0.30, floor_plan 6 ≈ $0.06, site_plan **0**) | confirmed |
| warmer ~$23/day, ~32% of call volume | $27.79 / $31.76 / $9.27 on 07-10/11/12; 5,482 of 16,794 calls (32.6%), 19.9% of spend | confirmed |
| queue 27,993 proposed, 86.4% geo | **27,643** proposed: geo 23,846 (86.3%), byt_geo 3,578 (12.9%), street 219 (0.8%) | confirmed |
| 0% warmer coverage of geo/byt_geo backlog | **0/23,846 geo, 0/3,578 byt_geo** ever in any warmed request; street 82/219 (37.4%) | confirmed |

New facts the prior session missed:

- **The warmer is pure additive spend, not partial savings.** Sync dedup-vision spend on 07-12
  ($93.53) matches pre-warmer 07-09 ($93.42); the batch $ sits on top. Total dedup-vision is
  now **~$100/day (~$3,000/mo run-rate)** across the four vision features.
- **site_plan warming has 0% overlap with ANY engine stage** — every one of its 946 warmed
  pairs was wasted.
- Batch attribution method (for future measurement): `llm_calls` has no batch flag; batch rows
  are exactly **`duration_ms = 0 AND error IS NULL`** (validated: 5,482 rows / $68.83 ==
  `SUM(dedup_batches.ingested_count)` / `SUM(total_cost_usd)` to 1¢).
- Engine-side cache hits are structurally invisible (`vision_calls` counts paid calls only;
  cache hits skip the budget decrement), so pair-identity overlap is the only consumption
  metric available — fine, because at 1% overlap the conclusion doesn't hinge on precision.

### 1.1 Root cause: six divergences, one architecture error

The engine has **five** scheduled lanes (`.github/workflows/dedup_engine.yml:42-46`): full
street scan (0 \*/6, compare-budget 300), candidate drain (30 \*/2, budget 100), dirty drain
(45 \*, budget 40), geo scan (0 3,9,15,21, max-vision-calls 300), byt-geo scan (0 1,7,13,19,
max-vision-calls 300; `dedup_byt_geo_enabled=true` since 07-12). All five decide pairs through
the one shared `resolve_pair`; stage='visual' audit rows are written only there.

Ranked divergences (each independently verified in code):

1. **Lane/population mismatch (dominant).** Scheduled `dedup_batches.yml` submits pass no
   inputs → `--lane ${{ inputs.lane || 'street' }}` (line 136) → every scheduled warm walks
   the street funnel. Meanwhile ~74%+ of the engine's paid-visual capacity (geo + byt-geo +
   candidates + dirty lanes) runs over populations (`_load_geo_eligible` cells, the DUE queue,
   last-hour-tagged sets) **the street lane never loads**. The `geo`/`byt_geo`/`candidates`
   warmer lanes exist but nothing schedules them.
2. **No cursor vs persistent cursor.** The warmer restarts at the obec-ASC head every 6h and
   its `--max-pairs 4000` window burns on *cached* pairs too (`pairs_left` decrements on
   skips), so it barely advances; the engine full scan resumes from `dedup_scan_state` deep in
   the market. Even street-vs-street they walk disjoint territory.
3. **Work-list inversion.** Dirty-lane pairs (CLIP-tagged in the last hour) and DUE candidate
   pairs cannot be anticipated by a speculative walk 6h earlier; and the warmer enqueues only
   cache-misses, so anything the engine already decided synchronously is skipped — adverse
   selection pushing the sets apart in every window.
4. **Iteration-order mismatch** even over identical populations (insertion order vs
   `sorted(groups)` vs claim-rank), so bounded runs sample different heads.
5. **Cache-key/model mismatch.** The verdict cache is keyed `(a, b, room, model)`; the warmer
   warms only the default Sonnet compare model, but the engine's cosine router sends
   high-cosine rooms to **Haiku** → different key → miss; `--warm-rooms 1` vs the engine's
   up-to-4-room walk loses the rest.
6. **The shipped thing contradicts the plan.** `dedup-cost-reduction.md` §4.1 is titled
   *"Targeted batch lane (**NOT the old warmer**)"* and specifies: sweep lanes *"submit their
   already-routed vision work as Anthropic batches instead of sync … submit ONLY pairs the
   free funnel has already routed to vision in this run."* What was re-enabled on 07-09 is the
   old speculative pre-warmer architecture. `dedup_engine.yml`'s own comments still assert "no
   batch warmer exists anymore" — the two subsystems were never re-pointed at each other.
   (`roadmap/next.md`'s "DECISION (operator 2026-07-04): NO batch warmer" records the same
   intent; the 07-09 flip contradicted it.)

### 1.2 Decision: retire the speculative pre-warmer; build §4.1 as specced

Fixing the warmer's *targeting* (the prior session's framing) would still leave divergences
2–5: a second process re-deriving the engine's work-list can only ever approximate it. The
robust, no-tech-debt shape is the one the plan already specifies — **the engine defers its own
already-routed vision calls into batches**, so selection identity holds by construction:

- Sweep lanes (full street, geo, byt-geo, candidates) run the free funnel as today; where they
  would make a *cold* vision call, they instead enqueue that exact request
  (pair/room/model/filtered image-ids) into `dedup_batches`/`dedup_batch_requests` and mark
  the pair deferred (the `floor_plan_pending`-style defer already exists for exactly this).
  Next lane pass re-resolves over the now-warm cache. Latency cost: one batch round-trip
  (≤1h typical) on sweep lanes only.
- Dirty/realtime lanes stay sync (latency-critical) — exactly §4.1's split.
- Reuse: `dedup_batches` tables, `ingest_dedup_batch.py` persist path, the submit-retry from
  #744. Retire: `submit_dedup_batch.py`'s independent funnel walk (`collect()` and its lane
  loading) — the submit step becomes "flush the engine's deferred-request spool".
- **Immediate operator action available now, before any code:** flip
  `dedup_batch_warmer_enabled=false`. It stops ~$23/day of ~0.5%-consumed spend the moment
  it's flipped, and nothing regresses (the engine never depended on the warm cache — that is
  precisely the problem). Recommend flipping today; the crons then no-op.

Honest saving estimate (replaces the prior "$1.6–2k/mo recoverable by fixing targeting"):
stop-warmer ≈ **$690/mo** immediately; §4.1 batching ≈ 50% off the sweep-lane share of the
residual sync spend (~60% of ~$78/day ⇒ **~$700/mo**); the Finding-I-2 fix below shrinks the
paid denominator further. The mechanism differs from the prior claim but the magnitude holds.

---

## 2. Finding I-2 — the operator's manual-merge burst: the free arms were right, one guard vetoed them all

The burst: **143 operator merge events / 81 groups / 142 pairs in 23 minutes (2026-07-12
19:50–20:13 UTC)**, all `reason='manual_cluster'`; pair-grain mix **pozemek 93, dům 46,
komerční 3**. This is a gold labeled-positive set for exactly the families the dedup engine
handles worst.

The operator's hypothesis was "these should have auto-merged on pHash if the right images had
been compared." Empirically **confirmed, with a decisive twist** — every cheap explanation is
dead:

- **Coverage was perfect.** 100% of images on both sides of all 142 pairs had `phash`, CLIP
  tags AND embeddings (hashed/tagged days-to-weeks earlier). Zero missing-coverage pairs.
- **Every free arm was enabled** (`dedup_nonbyt_phash_single_enabled=true`,
  `dedup_nonbyt_cosine_merge_min=0.98`, attr arm, facade dismiss, auto-merge — all live).
- **The engine saw the pairs and paid for them.** 140/142 had geo-tier
  `property_identity_candidates` rows created 17:14–17:18 the same day, all decided
  `visual_inconclusive` (confidence 0.6) at 17:22 — ~2.5h before the operator merged them by
  hand. The candidate markers literally record the winning evidence: `phash_pairs: 36`
  against `phash_min_pairs: 2` (pair 370578↔86181, re-verified directly).

**Root cause — the `_both_have_site_plan` step-aside** (`scripts/dedup_engine.py:1201`,
applied to all three free arms at :1957/:2023/:2088): when both sides carry any
`site_plan`-tagged image, the pHash, attr and cosine arms all step aside and the pair goes to
paid forensic vision. CLIP collapses `cadastral_map` / `situation_plan` / `aerial_plot` fine
tags into `site_plan`, and for pozemek a cadastral map *is* the photo set — so the step-aside
fired on **140/142 (98.6%)** of the burst (including 44/46 dům). Forensic vision on
map-dominated image sets then returns `visual_inconclusive`, the pair queues for review, and
the money is spent anyway. Free-signal replay over the 142 pairs:

| free signal (engine-faithful SQL) | would fire | % |
|---|---|---|
| pHash identical ≥2 (standard arm) | 106 | 74.6% |
| pHash identical ≥1 (live non-byt single arm) | 118 | 83.1% |
| max CLIP cosine ≥0.98 (live threshold) | 125 | 88.0% |
| union (pHash ≥1 OR cosine ≥0.98) | 136 | **95.8%** |
| **pHash ≥2 over NON-drawing photos only** (site_plan/floor_plan/property_document excluded from the count) | 89 | 62.7% |
| pHash ≥1 over non-drawing photos only | 103 | 72.5% |

Residue: 2 pairs never enqueued (pairwise-enqueue gap inside a 19-way cross-portal cluster),
1 pair lost to a same-run race (survivor membership changed 2s before evaluation → markers
recorded phash_pairs=0 on a stale listing pair), 1 pair genuinely hard (no free signal).

### 2.1 Fix direction (Session 2 of this program; rule-15 amendment, operator-gated)

**Count, don't step aside:** compute the pHash-identical count (and the cosine-arm max) over
*non-drawing images only* — excluding `site_plan`/`floor_plan`/`property_document`-tagged
images from the *count* — instead of diverting the whole pair whenever plans exist on both
sides. This preserves the guard's actual purpose (a shared development masterplan can no
longer reach the threshold by itself — drawings simply can't contribute) while letting real
photo evidence decide: on this corpus it frees **89/142 (≥2) to 103/142 (single-arm)** merges
at $0.

This is deliberately NOT the withdrawn "site_plan Low auto-dismiss" deviation (architecture.md
records site-plan never-auto-rejects as explicit design; that stands). Plans keep their veto
power via the unchanged site-plan development guard and floor-plan gate on any would-merge;
what changes is only that plan *presence* stops vetoing photo-based free evidence.

Known precision risk to validate before any flip: **shared aerial/drone photos across
neighboring parcels of one development** would count as non-drawing identical pairs. The
validation is the golden set (§4): replay the modified rule against every labeled negative
(operator dismissals, operator unmerges, the 6k coordinate-trap synthetic negatives, engine
dismissals) and report precision per family. Ship default-OFF behind a setting; the flip is
the operator's, with the replay table in hand (same protocol as the §2.2 arms).

Secondary fixes riding along: (a) transitive/cluster-complete enqueue for N-way groups;
(b) re-probe free arms when a same-run merge changes survivor membership (the race);
(c) split the plan taxonomy so the engine can distinguish `cadastral_map` (parcel identity —
merge-supporting evidence for pozemek) from `situation_plan` (development masterplan —
step-aside-worthy); the fine tags are already stored per image, the engine just never reads
them (`image_clip_tags.fine_tag`, only surfaced for frontend display today).

---

## 3. The review queue: what actually blocks the exits

State (07-12): 27,643 proposed (93% both-sides-active; 26,558 = 96% carry
`engine_decision='visual_inconclusive'`); net growth ~2.5–3k/day (inflow ~4k/day since the
byt-geo flip vs ~800/day exits, of which 88.8% are engine self-dismissals, 5.8% auto-merges,
5.5% operator). The engine's ~2.8k terminal decisions/day happen almost entirely on
freshly-scraped pHash-identical pairs — currently-proposed pairs got **zero** terminal
decisions in 7 days (their `last_engine_decision_at` stamps are bulk `visual_inconclusive`
re-evaluations, not exits).

The prior "compare-starved" framing is now only half-true. The geo lanes DO pay vision
(300 calls × 8 runs/day across geo+byt_geo); the problem is the verdicts are structurally
non-terminal for the queue's dominant contents:

- **Merge exit blocked:** the §2 step-aside sends plans-on-both-sides pairs (structural for
  pozemek, most dům) to forensic → inconclusive → re-queue. Fixing it converts the queue's
  free-signal majority into $0 merges — including retroactively: after the fix, a candidates
  re-decide sweep concludes them **without vision** (the evidence is already in the DB).
  **The previously-sized $600–1,000 paid "blitz" is therefore obsolete** — re-size after the
  step-aside fix ships; most of the backlog should drain free.
- **Dismiss exit blocked:** `DISTINCTIVE_DISMISS_ROOMS` = {kitchen, bathroom} (+facade for
  non-byt since mig 285), but pozemek's comparable tags (`LAND_PRIORITY` = site_plan,
  exterior_facade, garden, floor_plan) mostly never qualify → `relevant=[]` → no auto-dismiss,
  ever. There is **no negative free signal at all** (low cosine never dismisses — by design).
  Per-family dismissal policy is its own rule-15 amendment (operator gate + calibration) and
  no amount of vision spend substitutes for it.
- Deliberate residue that stays manual regardless: `site_plan_different_unit` outcomes,
  `no_images` pairs.

Ops health flags observed while investigating (not this program's work, but they gate it):
the **byt-geo lane is currently failing** with a 2-min statement timeout in
`_load_geo_eligible` (run 2026-07-12 20:13Z traceback) — the failure-audit WS3-4
keyset-pagination fix is the root cure; the street full scan had a transient
"connection is closed" death after 74 min the same evening. Both eat scheduled capacity.

---

## 4. One golden set for the whole program

Inventory of ground truth (all re-counted 07-12):

| source | rows | label | caveats |
|---|---|---|---|
| `property_merge_events` source='operator', not undone | 347 all-time / 256 last-30d (incl. the 142-pair burst) | positive | pre-merge pair fully reconstructible (retired vs survivor property; retired kept as `merged_away`) |
| operator dismissals (`reviewed_action='operator'`) | 28 all-time | negative | scarce; geo-tier only 9 |
| operator unmerges of engine merges | 64 events → **32 usable** | negative | exclude the 30/62 later re-merged (contradictions) |
| `dedup_decision_feedback` (is_incorrect) | 11 | both | highest-precision "engine was wrong" labels |
| `dedup_golden_pairs` | 15,888 (9,888 pos / 6,000 neg) | mixed | **stale one-shot snapshot (2026-06-22)**; ~9,663 of the positives trace to AUTO merges → circular for benchmarking engine signals; negatives are synthetic coord-traps, not human |

Design (first consuming session ships the migration):

1. **`dedup_label_events` VIEW** — the always-current union of the four human streams above,
   one row per labeled pair: `(label_id, left/right_property_id, left/right_listing_sreality_id,
   is_same, label_source, labeled_at, category_main, tier)`. Property-grain keys (dedup-stable,
   rule-18 spirit) + listing ids as the media handle for vision benchmarks. Engine-derived
   strata (auto-merge positives, coord-trap negatives) may be included but MUST carry a
   distinguishing `label_source` so benchmarks can exclude them (circularity).
2. **Frozen snapshots** — benchmarks never read the live VIEW: a script materializes a named,
   append-only `dedup_golden_sets(set_name, frozen_at, holdout_floor, …)` fixture per bake-off.
3. **Holdout floor = labeled_at > 2026-07-10** for any threshold benchmarking: the cosine
   bands were calibrated on 273 operator merges and the §2.2 arms were replay-validated on the
   decided corpus through 07-10 — labels at or before that date are training-tainted. The
   07-12 burst (142 pairs) is clean by construction and is the primary Session-2 validation set.
4. Known gap to state honestly: **human negatives are scarce (~70 total)**. Precision claims
   will lean on the synthetic + engine-dismissed strata (flagged as such); growing human
   negatives cheaply = the cost-plan §5.4 queue-triage idea (operator labels where the model
   is uncertain).

---

## 5. Target architecture (end-state after Sessions 2–5)

```
ingest → images (R2) → phash (free) → CLIP tags + embeddings (free, self-hosted)
                                          │
                    ┌─ free tier (per pair, in resolve_pair) ─────────────┐
                    │ classify_pair rejects → prior-dismissal consult     │
                    │ → readiness defers (tags/downloads/embeddings)      │
                    │ → pHash count & cosine over NON-DRAWING images      │
                    │   (plans can't vote, but no longer veto)            │
                    │ → attr arm → per-family dismissal rooms             │
                    │ → plan-type fine-tag logic (cadastral vs masterplan)│
                    └──────────────┬───────────────────────────────────---┘
                                   │ unresolved & routed to vision
                     sweep lanes (full/geo/byt-geo/candidates):
                       DEFER routed calls → Anthropic batch (50% off)
                       → ingest → next pass replays warm  (§4.1 as specced)
                     dirty/realtime lanes: sync (latency-critical)
                                   │
                     forensic High = the only auto-merge gate (rule 15, unchanged)
                     model per lane = harness-measured (Session 4 bake-off)
                     compare budgets ordered by listing recency (Session 5)
```

Invariants preserved: forensic-High-only auto-merge; site-plan guard + floor-plan gate
unchanged on every would-merge; snapshots/append-only history untouched; every conservatism
relaxation ships default-OFF behind a setting with a golden-set replay + operator flip.

---

## 6. Program sequence (operator-set 2026-07-13)

Session 1 recommended reordering to fix the free tier first (it shrinks the paid denominator
every later session prices against). **The operator elected to keep the original order** —
bake-off → free-signals → warmer → priority — so that's the plan of record below. The free-first
consideration is preserved for context but does not govern; the two orderings differ only in
which of Sessions 2/3 lands first, and the golden-set foundation (§4) is a shared prerequisite
that **whichever session goes first must build** — i.e. the bake-off (Session 2) now ships it.

- **Session 2 — vision-model bake-off (point 1).** Benchmark GPT-5-mini, Qwen3-VL-235B-A22B,
  Qwen3-VL-30B-A3B, Gemini-3.1-flash-lite @1568, Gemini-2.5-flash-lite @1568, and current Sonnet
  on the three dedup vision tasks (compare_listings_visually, floor-plan, site-plan) against the
  §4 golden set — precision/recall on the merge decision, latency, $/call on BOTH a normal and a
  batch/prompt-cache basis (be honest about which providers actually support each). Context from
  the already-closed 07-11 harness run: Haiku@768 20% / Haiku@1568 30% / Gemini-3.1-Pro@1568 72%
  recall vs Sonnet's 88.3% self-baseline (Sonnet stayed on forensic lanes; no cascade) — the new
  models are the open question. New providers land as `api/providers/<name>.py` implementing the
  `CompletionProvider` protocol, behind flags, NOT flipped on in prod. Deliverable = a cost×ability
  recommendation. **Ships the golden-set foundation (§4) as its evaluation substrate.**
  **Blocker to clear first: `QWEN_API_KEY`/`OPENAI_API_KEY` exist only on the Railway api service
  — the Actions harness + local benchmark scripts need them as GH secrets / local env too.**
- **Session 3 — free-signal precision (point 4A).** Non-drawing pHash/cosine counting to replace
  the blanket `_both_have_site_plan` step-aside (§2.1; default-OFF + golden replay + operator
  gate); cadastral-vs-masterplan fine-tag split; per-family dismissal rooms (same protocol);
  cluster-complete enqueue; same-run re-probe. Then a candidates re-decide sweep drains the
  backlog FREE. This is where queue growth stops mattering. Plus §6-B: assess moving embeddings
  to a vector DB (below).
- **Session 4 — batch lane rebuild for the backlog (point 3).** GOAL unchanged (warmed verdicts
  actually consumed; the ~$1.6–2k/mo target). **MECHANISM corrected by Finding I-1 (§1.2): do NOT
  "reconcile the warmer's selection query with the engine's"** — a second process re-deriving the
  work-list can only approximate it (six divergences). Instead retire the speculative pre-warmer
  (flip `dedup_batch_warmer_enabled=false`, which the operator can do any time) and build §4.1 as
  specced: the ENGINE defers its own already-routed cold vision calls into `dedup_batches` (sweep
  lanes only; dirty stays sync) so selection identity holds by construction. Measure via the
  `duration_ms=0 AND error IS NULL` batch attribution + pair overlap (must go ~1% → ~100%). What
  spend CANNOT fix: pozemek/komerční are structurally undismissable (no per-family dismissal
  rooms) — that's Session 3's dismissal-side work, don't paper over it here.
- **Session 5 — recency-first compare ordering (point 2).** One shared priority function (newest
  listings first) across candidate drain + sweep compare budgets, so 1d/3d/1w Browse filters
  never show unmerged dups in ANY category. The dirty lane already covers the first hours; this
  fixes the tail. Cleaner after Session 3 (most fresh pairs should then conclude free).
- **Vector-DB question (point 4B, assessed in Session 3):** pgvector already serves the pairwise
  cosine tier server-side; the only case for an ANN index is market-wide visual candidate
  *generation*. Assess pgvector-HNSW-on-a-scoped-subset vs an external service against rule #7;
  no dependency before that memo lands.

## 7. What spend cannot fix (unchanged, restated so nobody re-learns it)

`site_plan_different_unit` queues by design; `no_images` pairs stay manual; operator
throughput (~6.5 actions/day historical) means any plan ending in "the operator reviews a
five-digit queue" is not a plan — the exits must be automatic (Session 2) for the queue to
drain. And the LLM credit pool depletes ~daily at ~$143/day burn with no hard spend cap
(prevention still unbuilt — separate, still CRITICAL).
