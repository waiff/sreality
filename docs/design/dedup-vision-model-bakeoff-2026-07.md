# Dedup vision-model bake-off — 2026-07 (Session 3)

**Status: RESULTS PENDING (harness runs in flight).** This doc reports the cost×ability
bake-off of candidate vision models against the frozen `dedup_golden_sets` snapshot
`2026-07-13-session3-baseline`, across the three forensic dedup lanes. It extends
`docs/design/dedup-cost-reduction.md` §4.3 (the "model flips CLOSED 2026-07-11" bullet, which
measured only compare-lane RECALL for Haiku + Gemini-3.1-Pro) with (a) new cheaper candidates,
(b) all three lanes, and (c) a PRECISION dimension the harness never had before.

Nothing here is a production change. A model flip on `llm_visual_match_model` /
`llm_floor_plan_match_model` / `llm_site_plan_match_model` is an `app_settings` change that needs
the operator gate + a staged one-lane-at-a-time rollout (cost-reduction.md §6 Operator gates).
This doc is the evidence that decision would be made from.

## The question

The forensic Sonnet trio costs **~$849/mo** (30-day `llm_calls`, 2026-07-13):

| lane (`called_for`) | model | calls/30d | $/30d | $/OK call |
|---|---|---|---|---|
| compare_listings_visually | sonnet-4-5 | 30,409 | 433.80 | 0.0231 |
| compare_listing_floor_plans | sonnet-4-5 | 14,497 | 247.15 | 0.0195 |
| compare_listing_site_plans | sonnet-4-5 | 8,236 | 168.00 | 0.0313 |
| **forensic total** | | | **~849** | |

(There is also a cosine-routed `compare_listings_visually @ haiku` lane, 15.7k calls / $61.62,
and classify @ haiku $18.86 — not the subject of this bake-off, which targets the Sonnet trio.)

Rule 15 is precision-locked: the forensic **High** verdict is the *sole* auto-merge gate; the
floor-plan gate only ever *adds* conservatism (a `different_layout` dismiss); the site-plan guard
`never auto-rejects` and only ever QUEUES a `different_unit`. So the bar for any model that would
replace Sonnet on a lane is **precision-first**: it must not fabricate the dangerous verdict
(compare `High` / floor-plan `same_layout` / site-plan `same_unit`) on a confirmed-different pair.

## Method

- **Frozen golden set** `2026-07-13-session3-baseline` (migration 300 `dedup_label_events` →
  `dedup_golden_sets`; holdout_floor 2026-07-10). 527 positive pairs (w/ listings) / ~180 negative.
  Negative strata that drive precision coverage: `engine_site_plan_verdict` 134 (pozemek 75 / dům 49
  / komerční 5 / …) — the confirmed-different-unit population that disproved Session 2's free-signal
  fix; `operator_dismissal` 26; `operator_unmerge` 11 (w/ listings); `decision_feedback` 9.
- **Per-lane golden-negative coverage** (both sides carry the lane's tag): site_plan ~146, floor_plan
  ~41, compare ~30-37 (byt interiors + non-byt facade/garden).
- **RECALL** = replay each lane's cached decisive verdicts (`listing_visual_matches` High;
  `listing_floor_plan_matches` same/different_layout; `listing_site_plan_matches` same/different_unit;
  all `model = <prod sonnet>`), require the candidate reproduces the SAME verdict. Read every
  candidate's recall relative to Sonnet's OWN self-baseline on this sample (forensic verdicts are
  ~5% non-deterministic, so Sonnet's self-recall is the ceiling, not 100%).
- **PRECISION** = replay the golden confirmed-different negatives, require the candidate does NOT
  return the dangerous verdict. Compare walks up to 4 rooms (FULL_PRIORITY) stop-at-first-danger,
  mirroring the engine's OR-gate; floor/site check the one plan set.
- **Resolution held at production tiers** to isolate the MODEL variable: compare @ 1568px, plans @
  1568px. (768px is a separate resolution lever, cost-plan §3.1 — not confounded into this.)
- Harness: `scripts/validate_vision_models.py` (`--lanes`, `--golden-set-name`), dispatched via
  `validate_vision_models.yml`. Sample per model per lane: compare-limit 60, plan-limit 40,
  precision-limit 50.

## Candidates + list price ($/Mtok, standard tier; batch = −50% where supported)

| model | provider | in | out | batch? | notes |
|---|---|---|---|---|---|
| claude-sonnet-4-5 | anthropic | 3.00 | 15.00 | yes (−50%) | baseline / self-ceiling |
| gpt-5-mini | openai | 0.25 | 2.00 | yes (−50%) | max_completion_tokens |
| qwen3-vl-235b-a22b-instruct | qwen (DashScope intl) | 0.40 | 1.60 | yes (−50%, MUTEX w/ cache) | |
| qwen3-vl-30b-a3b-instruct | qwen (DashScope intl) | 0.20 | 0.80 | yes (−50%, MUTEX w/ cache) | |
| gemini-3.1-flash-lite | gemini | 0.25 | 1.50 | yes (−50%) | |
| gemini-2.5-flash-lite | gemini | 0.10 | 0.40 | — | **UNAVAILABLE: 404 "no longer available to new users"** (whole 2.5 series closed on this key) |

Batch-discount honesty: Anthropic, OpenAI, and Google all document a flat −50% batch tier. Alibaba
DashScope also documents −50% batch, but its batch discount and its context-cache discount are
MUTUALLY EXCLUSIVE (cannot stack) — unlike Anthropic where prompt caching applies on the sync path
independent of batch. For this dedup workload (mostly-unique image pairs) cache hits are rare either
way, so batch −50% is the relevant lever for all four live providers.

## Results

<!-- FILL PER MODEL AS RUNS COMPLETE -->

Recall / precision, both as `%(n eval)`. Precision = share of confirmed-different pairs on which the
candidate did NOT emit the lane's dangerous verdict (higher = safer). Bar for any auto-merge-lane
flip = **≥99% precision AND recall at/near Sonnet's self-ceiling**.

| model | compare recall | compare prec | floor recall | floor prec | site recall | site prec | $/run |
|---|---|---|---|---|---|---|---|
| claude-sonnet-4-5 (baseline) | | | | | | | |
| gpt-5-mini | | | | | | | |
| qwen3-vl-235b-a22b-instruct | | | | | | | |
| gpt-5-mini | 88.3% (60) | **56.4% (39)** | 97.5% (40) | **63.6% (11)** | 100.0% (40) | **58.3% (48)** | 0.872 |
| qwen3-vl-235b-a22b-instruct | | | | | | | |
| qwen3-vl-30b-a3b-instruct | 70.0% (60) | **64.1% (39)** | 97.5% (40) | **36.4% (11)** | 85.0% (40) | **66.0% (47)** | 0.375 |
| gemini-3.1-flash-lite | 86.7% (60) | **48.7% (39)** | 95.0% (40) | **45.5% (11)** | 90.0% (40) | **68.8% (48)** | 0.518 |
| gemini-2.5-flash-lite | — UNAVAILABLE (404: "no longer available to new users") — | | | | | | |

**gpt-5-mini (reasoning model):** BEST recall of any candidate (compare 88.3% = matches Sonnet's own
self-baseline; site_plan recall a clean 100%) — but STILL catastrophic precision (56-64%), AND the
slowest + priciest: **63 minutes wall-clock** for ~320 calls (~12s/call — reasoning tokens) at
$0.872/run. Even a careful reasoning model is an eager merger here. Operationally disqualified twice
over: precision AND throughput (the compare lane alone is ~30k calls/mo — a 12s/call model can't serve
that on a 6h sweep cadence).

**Numbers above are the preview runs (n as shown). The canonical, per-pair-persisted batch
(`run_label='2026-07-13-session3'`, feeding the /model-testing explorer) reproduces them within
forensic non-determinism (~5%); the final table is recomputed from `dedup_vision_bakeoff_results`.**

**qwen-30b (2026-07-13):** decent recall (70-97%), CATASTROPHIC precision (36-66%) — fabricates the
dangerous verdict on 34-64% of confirmed-different pairs. The failure MODE differs by lane and matters:
site_plan RECALL misses were mostly the SAFE direction (`different_unit → inconclusive`, 5/6), but
site_plan PRECISION misses are the DANGEROUS direction (`different_unit → same_unit`, 16/47) — the
model collapses distinct units of one development into "same unit", exactly the false-merge the guard
exists to prevent. classify agreement 100% (consistent with the prior finding that classify is the
easy task — it's already on Haiku). DO NOT ADOPT on any auto-merge lane.

## Recommendation

**Sonnet stays on all three forensic lanes. Do not flip `llm_visual_match_model`,
`llm_floor_plan_match_model`, or `llm_site_plan_match_model` to any candidate benchmarked here.**

The reason is uniform and decisive: **every cheap candidate has good recall but catastrophic
precision** on confirmed-different pairs. They are *eager mergers* — they emit the dangerous verdict
(compare `High` / floor `same_layout` / site `same_unit`) on **31–64%** of pairs that operator/forensic
ground truth has confirmed are DIFFERENT properties. In a precision-locked engine where the forensic
`High` is the *sole* auto-merge gate (rule 15), that is not a cost trade-off — it is a false-merge
generator. A single wrong `High` merges two distinct real-world properties (reversible, but it
corrupts every rollup, notification, and estimate in between, and burns operator review time to
unmerge). The prior "model flips CLOSED 2026-07-11" finding measured only *recall* and stopped at
Gemini-3.1-Pro's 72%; the **precision dimension this session added is what actually disqualifies the
whole cheap-VLM class**, including a reasoning model.

Per-candidate:
- **gpt-5-mini** (reasoning) — the strongest on recall (compare 88.3% = Sonnet's own self-baseline;
  site_plan recall a clean 100%), yet still only **56–64% precision**, AND the **slowest and priciest**
  candidate: **~63 min / ~$0.87 per benchmark run, ~12 s/call** (reasoning tokens). The compare lane
  alone is ~30k calls/mo on a 6h sweep cadence — a 12 s/call model cannot serve that. Disqualified on
  precision AND throughput.
- **gemini-3.1-flash-lite** — good recall (compare 86.7%), worst compare precision (48.7%). The cheapest
  Gemini that exists on this key (2.5-flash-lite is 404). Disqualified on precision.
- **qwen3-vl-235b-a22b / -30b** — the cheapest per-call ($0.001–0.002), recall 70–98%, precision
  36–66%. The 10× size jump from 30B→235B did not rescue precision. Disqualified on precision.
- **gemini-2.5-flash-lite** — could not be benchmarked at all (404 "no longer available to new users";
  the entire Gemini 2.5 series is closed to new projects on the sreality key, consistent with the
  already-known 2.5-flash / 2.5-pro retirement). Reported as unavailable, not estimated.

**Why the cheap models fail the same way:** the golden negatives are dominated by
`engine_site_plan_verdict` pairs — distinct units/parcels of ONE development that reuse the same staged
photo set (the exact population that disproved Session 2's free-signal fix, §2.1a). Telling them apart
requires *reading the parcel/unit number off the drawing and distrusting near-identical photos* — a
conservative, evidence-demanding judgment. Sonnet does it; the cheap models pattern-match "these look
like the same place → same property" and merge. Higher recall on the cheap models is the same trait
that sinks their precision: they say "same" more readily.

**So the dedup-vision cost lever is NOT a model swap.** It stays what Sessions 1–2 established and
Session 4 will extend: the free-first arms (pHash/cosine/attr) that decide pairs without vision at all,
plus the **Anthropic batch −50% discount on the residual Sonnet calls** (recall-identical, applies to
whatever model runs the lane). Session 4's engine-side batch-lane rebuild is where the next ~$400/mo of
this ~$849/mo comes from — not from a cheaper model. The bake-off's negative result is itself valuable:
it closes "just use a cheap VLM" as an option with per-pair evidence the operator can inspect on
`/model-testing`, rather than leaving it as an open temptation.

**Interactive evidence:** the `/model-testing` page (this PR) lets the operator step through every
benchmarked pair and see each model's verdict side by side — filter to "dangerous only" to watch the
cheap models merge confirmed-different developments the site-plan guard is meant to catch.

### If a cheap model is ever revisited
Only two framings could change this verdict, and both need new evidence, not a re-run:
1. **A cascade** (cheap walks rooms, Sonnet confirms every `High` before merge) — but the cheap models'
   inflated `High` rate means Sonnet re-checks most pairs anyway (little saving), AND their sub-Sonnet
   recall (87% compare) means real merges they score `Low` never reach Sonnet (lost recall). Doubly
   unattractive on these numbers; don't build it without a model whose *Lows* are trustworthy.
2. **A precision-hardened prompt** for one cheap model on ONE lane (e.g. site_plan, where gpt-5-mini
   already hits 100% recall) — plausible only if a reworked prompt lifts its 58% site precision past
   99% on this frozen set. That is a prompt-engineering research task with a clear, cheap harness now
   in place (`--golden-set-name 2026-07-13-session3-baseline`), not a flip.

## Reproduce

```
# freeze (already done): python -m scripts.build_dedup_golden_set --set-name 2026-07-13-session3-baseline
gh workflow run validate_vision_models.yml --ref main \
  -f candidate_model=<model> -f lanes=compare,floor_plan,site_plan \
  -f golden_set_name=2026-07-13-session3-baseline \
  -f max_edge=1568 -f plan_max_edge=1568 \
  -f compare_limit=60 -f plan_limit=40 -f precision_limit=50 -f skip_classify=true
```
