# Dedup vision + backlog overhaul — validated findings and program plan

**Status: INVESTIGATION COMPLETE (2026-07-12). This doc is the Session-1 deliverable of the
LLM-cost-reduction + dedup-quality program; §6 is the operator-approved sequence for Sessions 2–5
(free-signal precision first).**
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
  - **SUPERSEDED (operator decision 2026-07-14): the warmer STAYS ON.** After the gpt-5-mini flip
    (PR #787), the warmer became the live proof of the OpenAI Batch round-trip — it submitted 12
    gpt-5-mini batches ($1.17, 11 ingested clean, correct 50% batch pricing) that validated the
    provider path end-to-end. The operator kept it running for that reason. The §4.1 engine-fed
    rebuild below still replaces the warmer's *guessed* work-list (that argument is unchanged) — it
    is a Session 4 build now, not an immediate flip-off. (PR A additionally fixed the OpenAI batch
    poll-counts key mismatch that had been NULLing `dedup_batches.{succeeded,errored}_count`.)

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

Interlock with the geo town-pin false-merge finding (`docs/design/dedup-geo-town-pin-false-merge.md`,
2026-07-13): street-less HTML-portal listings inherit a single **town-level** coordinate for the
whole obec, so a shared pin is NOT evidence of the same property — two genuinely different houses
were queued (and one operator-approved) purely on a coincident town pin. Consequence for THIS fix:
coordinate proximity can never be a positive discriminator on the geo tiers; only a *contradiction*
signal (a different village token, or a large price/area divergence) is trustworthy, and it must
**defer, never dismiss** (killing eligibility would drop the ~77 legit cross-portal merges). So the
non-drawing photo evidence must stand on its own — do not let a shared town pin substitute for it —
and the replay must include the geo town-pin negatives once that design's `coord_precision`
substrate exists. That doc's parallel `dedup_pair_audit` self-paired-id fix (4,539 rows where
`left_sreality_id = right_sreality_id`) also means any golden-set extraction keying on audit
listing ids must resolve through `property_merge_events`, not trust the audit row's listing pair.

Secondary fixes riding along: (a) transitive/cluster-complete enqueue for N-way groups;
(b) re-probe free arms when a same-run merge changes survivor membership (the race);
(c) split the plan taxonomy so the engine can distinguish `cadastral_map` (parcel identity —
merge-supporting evidence for pozemek) from `situation_plan` (development masterplan —
step-aside-worthy); the fine tags are already stored per image, the engine just never reads
them (`image_clip_tags.fine_tag`, only surfaced for frontend display today).

### 2.1a Session 2 correction (2026-07-13): the naive fix FAILS the precision bar — DO NOT SHIP

Session 2 ran the "known precision risk" validation this doc called for (replay against every
labeled negative) *before* writing any code, using the negative population that already existed
in the DB: `property_identity_candidates` rows whose forensic site-plan compare recorded
`markers_matched->>'reason' = 'site_plan_different_unit'` — i.e. Sonnet read the actual
parcel/unit labels on the drawing and confirmed two listings are DIFFERENT real-world units
within one shared development. 141 such pairs exist; 131 reconstruct to an active, listable pair
(now a first-class stratum in `dedup_label_events`, `label_source='engine_site_plan_verdict'`,
migration 300).

**Result: the fix is unsafe at any threshold.** Recomputing "non-drawing pHash-identical count"
(excluding `site_plan`/`floor_plan`/`property_document`) on this confirmed-different population:

| category | negatives (n) | fire at count≥1 | fire at count≥2 | fire at count≥8 |
|---|---|---|---|---|
| dům | 48 | 24 (50%) | 23 (48%) | 15 (31%) |
| pozemek | 79 | 17 (22%) | 13 (16%) | 0 (0%) |
| komerční | 5 | 3 (60%) | 2 (40%) | 1 (20%) |

Combined with the true-positive rate on the 142-pair burst (dům 46/47, pozemek 40/72, komerční
3/3 fire at count≥1), **precision ≈ 67% at count≥1, ≈67% at count≥2, ≈75% even at count≥8** — a
whole order of magnitude below the ≥99% bar every auto-decide rule in this program requires. This
is not a threshold-tuning problem: several confirmed-different pairs share **38–46 non-drawing
image matches at Hamming=0** (a full staged photoshoot — facade, garden, interior, *and* the site
plan — reused byte-for-byte across distinct parcels of one subdivision, e.g. properties
368615/368616/368618/370197-9), which is MORE matching evidence than most true positives carry.
Tightening the Hamming bar (≤6 → 0) does not help either (still 15/24 dům, 11/17 pozemek fire on
exact-duplicate-only matches). A per-image "shared across many properties" pre-filter (the §2.4
shared-render-blacklist idea, extended to non-byt) also does not generalize: at least one
confirmed false-positive pair's driving image is shared across exactly 2 properties — indistinguishable
from a genuine repost by any population-frequency signal.

**Root cause:** real-estate developers/agents routinely reuse one staged photo set — sometimes the
*entire* set, drawings included — across every parcel/unit in a subdivision or a "build this house
model" catalog listing. Only reading the parcel/unit label ON the drawing (a vision-dependent,
text-reading task) discriminates these from a genuine repost; no perceptual-hash or embedding
signal over the photos themselves can, because the photos are often literally identical files by
design, not just similar.

**Consequence for the program:** the step-aside is not a coarse guard hiding free evidence — for
dům/pozemek/komerční it is protecting against a real, high-volume failure mode, and it should
**stay as designed**. This PR does NOT ship the non-drawing-count relaxation for the pHash/cosine
arms (§2.2 fields `dedup_nonbyt_phash_single_enabled`/`dedup_nonbyt_cosine_merge_min` keep their
existing `_both_have_site_plan` step-aside, unchanged). The queue-exit problem (§3) is therefore
**not** solved by a free-signal relaxation this session; it still needs either (a) a cheaper/faster
paid forensic path (Session 3's model bake-off, Session 4's batch-lane rebuild), or (b) narrowing
what counts as a candidate in the first place. The "$600–1,000 paid blitz is obsolete" claim in §3
is **retracted** — the blitz (rebuilt per Session 4's §4.1 engine-side batching, not the old
warmer) is still the live lever for this backlog.

**The cadastral-vs-masterplan fine-tag split (secondary fix (c) above) does NOT rescue this
either.** Spot-checked directly on a confirmed different_unit pair (properties 388399/398398,
parcels 2699/37 vs 2699/36): the two matching drawing images carry fine_tag `aerial_plot` AND
`cadastral_map` respectively — BOTH already collapse to `logical_tag='site_plan'` per
`data/clip_taxonomy.json`, and BOTH are the literal same development-overview page (pHash
Hamming 0/2) with only the highlighted parcel differing, which pHash cannot see. A cadastral map
in this corpus is not a per-parcel document; it is the same shared development-overview drawing
every listing in the subdivision reuses. Distinguishing the fine tags would not have changed the
count either way. Not shipped; not planned further without a different signal (e.g. OCR the
parcel label itself — out of scope here).

What DOES still ship this session: the golden-set foundation (§4, now including this new negative
stratum — the exact counter-evidence any future proposal on this guard must replay against first),
the cluster-complete-enqueue and same-run re-probe correctness fixes (unaffected by this finding,
narrow and independently safe), and the §6-B vector-DB memo.

**The two secondary correctness fixes, root-caused precisely:**

1. **Cluster-complete enqueue.** `property_identity_candidates` is keyed at the PROPERTY grain
   (`ON CONFLICT (left_property_id, right_property_id)`), but `resolve_pair` marked a property
   pair "seen" (`ctx.seen_property_pairs.add(cp)`, `scripts/dedup_engine.py`) unconditionally as
   soon as its canonical pair was computed — including on a DEFER outcome (clip/download
   readiness not warmed, floor-plan verdict not warmed). In an N-way cluster where several
   listing pairs collapse onto the same property pair (a multi-portal cluster with cross-portal
   duplicates already sharing a property), if the FIRST-tried listing-pair representative
   deferred, no OTHER representative — possibly the one with the decisive photo evidence — got a
   second look this run. Fixed: the property pair is discarded from `seen_property_pairs` on every
   DEFER branch (6 sites), so a later listing-pair representative gets a fresh chance within the
   same run; a TERMINAL outcome (merge/dismiss/enqueue/reject) still blocks re-evaluation as
   before. Covered by `tests/test_dedup_engine.py::test_seen_property_pairs_discarded_on_defer_allows_retry`.
2. **Same-run re-probe / property-id staleness.** A `ListingKey.property_id` is a run-start
   snapshot; when an earlier merge THIS run retires one side's property, `_merge_pair` already
   caught the resulting `MergeError` and skipped (converging on the NEXT run) — but a pair
   evaluated in between still computed its canonical property pair against the now-stale id, so
   its recorded verdict/markers (e.g. a race-condition `phash_pairs: 0`) attached to an already-
   obsolete pairing. Fixed: `_RunContext.retired_to_survivor` records every successful merge's
   retired→survivor mapping; `resolve_pair` resolves both `ListingKey`s through the (possibly
   multi-hop) chain via `_resolve_retired` before computing anything property-id-dependent. The
   pHash/floor-plan probe caches themselves stay correct unchanged (keyed by `sreality_id`, not
   `property_id`) — only the property-pair resolution needed to become merge-aware mid-run.
   Covered by `test_resolve_retired_follows_chain_within_one_run` +
   `test_merge_pair_records_retired_to_survivor_on_ctx`.

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

## 6. Program sequence (operator-approved 2026-07-13)

Order the data argues for, confirmed by the operator: fix the free tier first — it shrinks the
paid denominator every later session prices against, and it's what stops the queue from growing —
then batch what remains, then choose models for the residue, then order by recency. Each session
is its own PR + operator flip.

- **Session 2 — free-signal precision (point 4A) + the golden-set foundation.** Ships the §4
  golden set first (it's this session's validation substrate AND every later session reuses it).
  Then: non-drawing pHash/cosine counting to replace the blanket `_both_have_site_plan` step-aside
  (§2.1; default-OFF + golden replay + operator gate); cadastral-vs-masterplan fine-tag split;
  per-family dismissal rooms (same protocol); cluster-complete enqueue; same-run re-probe. Then a
  candidates re-decide sweep drains the backlog FREE (evidence already in the DB) — the old
  $600–1k paid blitz is obsolete; re-size any residue after. This is where queue growth stops
  mattering. Includes the §6-B vector-DB assessment (below). Dependencies: none.
- **Session 3 — vision-model bake-off (point 1).** Benchmark GPT-5-mini, Qwen3-VL-235B-A22B,
  Qwen3-VL-30B-A3B, Gemini-3.1-flash-lite @1568, Gemini-2.5-flash-lite @1568, and current Sonnet
  on the three dedup vision tasks (compare_listings_visually, floor-plan, site-plan) against the
  §4 golden set — precision/recall on the merge decision, latency, $/call on BOTH a normal and a
  batch/prompt-cache basis (be honest about which providers actually support each). Context from
  the already-closed 07-11 harness run: Haiku@768 20% / Haiku@1568 30% / Gemini-3.1-Pro@1568 72%
  recall vs Sonnet's 88.3% self-baseline (Sonnet stayed on forensic lanes; no cascade) — the new
  models are the open question. New providers land as `api/providers/<name>.py` implementing the
  `CompletionProvider` protocol, behind flags, NOT flipped on in prod. Deliverable = a cost×ability
  recommendation. Cleaner after Session 2 shrinks the paid volume the residue models must cover.
  **Blocker to clear first: `QWEN_API_KEY`/`OPENAI_API_KEY` exist only on the Railway api service
  — the Actions harness + local benchmark scripts need them as GH secrets / local env too.**
- **Session 4 — batch lane rebuild for the backlog (point 3): SHIPPED (2026-07-14).** GOAL
  unchanged (warmed verdicts actually consumed; the ~$1.6–2k/mo target). **MECHANISM corrected by
  Finding I-1 (§1.2): do NOT "reconcile the warmer's selection query with the engine's"** — a
  second process re-deriving the work-list can only approximate it (six divergences). Shipped as
  specced: the ENGINE defers its own already-routed cold classify/compare/site-plan/floor-plan
  calls into `dedup_batch_requests` (`batch_id NULL` — migration 306; sweep lanes only — full
  street, geo, byt-geo, candidates; dirty/realtime stay sync) so selection identity holds by
  construction, gated by `dedup_engine_batch_defer_enabled` (default OFF — flip to activate).
  `scripts/submit_dedup_batch.py`'s old collect() work-list guesswork is retired; its only job now
  is flushing the spool into provider Batch API submissions (unchanged `dedup_batches`/ingest
  plumbing). `dedup_batch_warmer_enabled` is retired (inert). Shared chunk/retry primitives
  extracted to `toolkit/batch_submit.py` (dedup/condition/enrich converge). Provider-agnostic
  naming swept across the batch layer. Verified: dedup batch requests already run at 4096
  max_tokens with no truncation evidence (max observed 3546/4096 on floor_plan) — unlike
  enrichment's 512-token bug (#791), no fix needed. **Not yet measured live** (flag ships OFF):
  next operator flip should watch `duration_ms=0 AND error IS NULL` batch attribution + pair
  overlap (must go ~1% → ~100%) to confirm the fix. **Found, not fixed:** ~0.4-1.2% of
  floor_plan/site_plan gpt-5-mini calls error with an Anthropic-provider 404 for a gpt-5-mini
  model id (pre-existing routing bug, unrelated to this session — flagged for follow-up). What
  spend CANNOT fix: pozemek/komerční are structurally undismissable (no per-family dismissal
  rooms) — that's Session 2's dismissal-side work, don't paper over it here.
- **Session 5a — recency-first compare ordering (point 2): SHIPPED (2026-07-14).** One shared
  recency signal (`properties.first_seen_at`) feeds both halves: the candidate drain ranks its
  whole due-set newest-first (`_recency_ranked_property_ids` → `priority_property_order`, the
  mechanism the dirty drain already established); the three cursor-bearing sweep lanes (full
  street, geo, byt-geo) each pull a bounded "recency head" (`_recency_head_candidate_ids`,
  tier + 7-day-window scoped) to the front of the pass. Composed with the `scan_cursor`
  lexicographic frontier, not a re-sort of it: `run_engine`'s `frontier_keys` ensures only
  cursor-ordered tail groups can advance the PERSISTED cursor position, so a deadline-truncated
  run can never regress the frontier even when the head is non-empty (migration 261's coverage
  guarantee is preserved). `dedup_recency_backlog` (migration 307) is the acceptance-metric view
  (per-tier unresolved-and-fresh counts, <1d/<3d/<7d); write-once `first_engine_decision_at`
  (same migration) instruments time-to-first-look separately from `last_engine_decision_at`.
  Live re-verification (2026-07-14) found the geo tier carries ~85% of the fresh backlog (700/
  <1d, 1391/<3d, 5521/<7d of 39,983 total proposed) — expected, since single-dwelling families
  have no free-arm/warmer path (rule #15 (E)); the geo lane's head is where this matters most.
  The dirty lane already covers the first hours; this fixes the tail. Not yet measured for
  actual acceptance-metric movement (needs a few scheduled cycles to run against the new
  ordering) — that's the next session's first check.
- **Vector-DB question (point 4B, assessed in Session 2):** pgvector already serves the pairwise
  cosine tier server-side; the only case for an ANN index is market-wide visual candidate
  *generation*. Assess pgvector-HNSW-on-a-scoped-subset vs an external service against rule #7;
  no dependency before that memo lands.

## 6-B. Vector-DB assessment memo (Session 2, delivered — defer, no dependency added)

`image_clip_embeddings` is now **7.7M rows** (`vector(512)`, one model), past the "5M-row" scale
the original CLIP build spec flagged as a future concern — not from runaway growth but because
this branch's own "embed every tagged image, not active-only" fix widened the embedded set from
active-only to ~the whole tagged corpus (98.7% of 7.8M tagged images now embedded). Marginal
growth post-backfill looks like ~50k/day (ordinary ingest), not the ~640k/day the one-time
backfill briefly produced.

No ANN index exists today (`pg_indexes` shows only the `(image_id, model)` primary key — pgvector
0.8.0 is installed, so adding one is a config/index decision, not a new dependency per rule #7).
No consumer needs one either: every `<=>` call site (`toolkit/clip_dedup.py`'s `pair_max_cosine`/
`room_pair_cosine`, `scripts/embedding_ab.py`'s golden-set A/B) is scoped to a specific listing's
image set — a handful of rows, never `ORDER BY embedding <=> :vec LIMIT k` over the full table.
Migration 226's own comment already states this as deliberate design ("no ANN index... never a
global nearest-neighbour search"); nothing in ROADMAP.md proposes a market-wide visual-similarity
feature.

**Cost if built anyway:** `image_clip_embeddings` is already ~21GB (mostly TOASTed vector data);
HNSW's well-known ~1.5–2x raw-vector RAM/disk overhead would add another ~30–40GB, i.e. a ~50–60GB
working set against a 1GB `shared_buffers`/3GB `effective_cache_size` instance that can't even
cache the raw vectors today. That's a real infrastructure cost (bigger instance + tuning + build
time), not a free flip.

**Recommendation: defer.** No code path or roadmap item creates a present-day use case for
market-wide nearest-neighbor search; building HNSW now would be speculative infrastructure against
a feature nobody has asked for. If/when a "find visually similar listings" feature is proposed,
prefer **pgvector HNSW on a scoped subset** (one canonical embedding per active listing, not all
7.7M raw images) over an external vector service — the vectors already sit beside the price/geo/
category metadata any candidate-generation query must join against, and the table is already
RLS-locked-down (migration 237); an external service would need its own sync pipeline, auth
perimeter, and cost line for no matching benefit.

## 7. What spend cannot fix (unchanged, restated so nobody re-learns it)

`site_plan_different_unit` queues by design; `no_images` pairs stay manual; operator
throughput (~6.5 actions/day historical) means any plan ending in "the operator reviews a
five-digit queue" is not a plan — the exits must be automatic (Session 2) for the queue to
drain. And the LLM credit pool depletes ~daily at ~$143/day burn with no hard spend cap
(prevention still unbuilt — separate, still CRITICAL).
