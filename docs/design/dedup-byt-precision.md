# Apartment dedup precision: area, renders, floor plans, and the cleanup sweep

Context: the "Rezidence Na Bradle" false-merge collapsed four distinct units (73, 87,
and two × 99 m²) of one development into property 392 across sreality + idnes. This doc
records the shipped fix and proposes the two deeper investigations the operator asked
for, plus the sweep to clean up existing bad merges.

## Why it happened (root cause, validated on live data)

Two mechanisms compounded:

1. **Loose area gate + transitivity.** Rule C rejected only at a >20% area gap. Adjacent
   bands slipped through pairwise (73→87 = 16%, 87→99 = 12%) and transitivity chained
   73-87-99 into one property.
2. **pHash trusted shared renders.** The pHash fast-path auto-merges on ≥2 near-identical
   image pairs over *any* image, with no area check of its own. A development reuses the
   same exterior facade + kitchen **render** across its units → ≥2 matches → auto-merge
   (`reason='image_phash'`, fired live on 2026-06-24).

pHash carries **~94% of all auto-merges** (18,926 / 14 days), so any change here is
high-stakes — the fix must improve precision without a recall cliff.

## Shipped (this PR): area 10% + interior-only image matching for byt

Both changes are **per-category** (apartments only; house/land/commercial unchanged),
expressed as data on `MatchProfile` / small helpers — no `if category ==` branches.

- **`BYT_CANDIDATE_AREA_MAX_PCT = 0.10`.** A byt pair >10% apart is a hard
  `area_contradiction` reject on both the exact-address and candidate paths, *before*
  pHash. Breaks the 73/87/99 chain (16% and 12% now both reject). Single-dwelling
  families keep 0.20 (cross-portal built-up/usable/plot area varies more).
- **Interior-only images for byt.** `_phash_identical_pairs` excludes any pair touching a
  KNOWN-exterior/shared tag (`NON_INTERIOR_TAGS`: exterior_facade, balcony_terrace,
  garden, site_plan, floor_plan — from CLIP `image_clip_tags`); `rooms_in_priority`
  drops exterior rooms for byt (`BYT_ROOM_PRIORITY`). **Untagged images still count**, so
  recall holds for the not-yet-CLIP-tagged majority (CLIP ≈ 27% coverage, rolling out)
  and tightens toward interior-only as coverage fills in. This is the faithful reading of
  "never use exterior images for byt" with no recall cliff.

**Boundary (validated):** the two identical-area 99 m² units still share 6 interior/
untagged near-identical photos after exterior exclusion (8 → 6) — so area and room-type
*cannot* separate them. That is the render-vs-photo ambiguity below.

## Investigation #3 — render / visualization detection

**Goal.** Confidently tag an image as a CGI render (visualization) vs a real photograph,
so a development's reused renders stop driving byt merges (the residual two-99s case).

**Which signal.** Reuse the asset we already have: every active image already gets a
512-d **CLIP embedding** (`image_clip_embeddings`, ~1.15M rows) and a `logical_tag`. Three
options, cheapest first:

| Option | Cost | Accuracy | Verdict |
|---|---|---|---|
| Zero-shot CLIP anchors ("a 3D architectural rendering" vs "a real photograph") → `render_score` | ~0 (compare stored embedding to new text vectors) | moderate; weak on photorealistic renders | a baseline |
| **Logistic-regression head on the stored 512-d embedding**, supervised | ~0 at inference (linear head) | best for our domain; calibratable | **recommended** |
| Dedicated CV forensics (EXIF presence, sensor-noise residual, FFT slope) | low–medium | EXIF stripped by portals → low recall; noise model needs training | supplementary |

**Weak supervision is free here.** Czech developer listings routinely caption renders
("vizualizace", "ilustrační foto", "3D vizualizace", "počítačová vizualizace"), and
new-builds (`condition='novostavba'`) are render-heavy. Mine those for a training set,
add a few hundred hand-labeled images for calibration, train a logistic head on the
existing embeddings. Inference is a dot product over vectors we already store.

**Plan.**
- *Phase 1 (model):* additive column `image_clip_tags.render_score float` (+ `is_render`
  at a calibrated threshold). Train the head in the existing `clip_tag` job; target ≥90%
  **precision** on "render" so we only act on confident renders.
- *Phase 2 (use):* add high-`render_score` images to the byt pHash exclusion (same
  mechanism as `NON_INTERIOR_TAGS`) and down-weight render rooms in the forensic compare.
  This drops the two-99s pHash count below 2 → routes to the operator queue instead of
  auto-merging.

This adds one head to a tagger we already run — no new heavy model, no new pipeline.

## Investigation #4 — floor-plan comparison

**Goal.** When both listings carry a floor plan, USE it to tell units apart — as a
*floor-plan-to-floor-plan* comparison only, never to override listing data.

**Which tool can tell when a floor plan differs?**

- **pHash — NO, actively misleading.** Floor plans are line-art on white; pHash conflates
  different layouts as "near-identical", and a development's shared plan template hashes
  identical. (This is *why* floor_plan is already excluded from the byt pHash count — the
  shipped change reinforces it.)
- **CLIP cosine — WEAK.** CLIP embeds "this is a floor plan" semantically; two different
  layouts both score high. Not reliable for layout difference.
- **Vision LLM (Sonnet @ 1568px) — YES.** Floor plans are document-like; the
  `DOCUMENT_MAX_EDGE = 1568` tier exists for exactly this. Sonnet reads wall arrangement
  AND the embedded text. Haiku is materially weaker on fine diagram/OCR — use Sonnet.

**Plan (mirror the site-plan guard).** New write-allowed toolkit function
`compare_listing_floor_plans(a, b)` + cache `listing_floor_plan_matches` (migration),
run only when both listings have a `floor_plan` image, batch-warmable via `dedup_batches`:

- **A) layout (basic):** verdict `same_layout | different_layout | inconclusive`.
  `different_layout` → strong "distinct unit" → QUEUE the pair (conservative; never
  auto-merge), like the site-plan `different_unit` guard.
- **B) OCR (advanced):** extract `{disposition, unit_number, floor, total_area,
  rooms:[{name, area}], balcony, terrace}` into the cache row. Use ONLY plan-to-plan: if
  plan A is "byt č. 5, 3.NP, 73 m²" and plan B is "byt č. 8, 4.NP, 99 m²", the differing
  unit number / floor / area is the strongest same-development discriminator → reject /
  queue. Per the operator: these never write into `listings.*` — they feed the dedup
  decision only.

Slots into `_resolve_visual` as a development guard alongside the site-plan guard. It is
the precise fix for distinct units that share an identical *photo set* but carry
different per-unit plans.

## The cleanup sweep (plan — not yet built)

**Goal.** Unmerge existing properties the new rules would not have merged — the
chain-merge victims like 392.

1. **Scope (read-only).** Auto-merged (`source='auto'`) byt properties whose active member
   listings span >10% area (pairwise) — the chain-merge class. Measured (2026-06-25): of
   16,983 multi-listing byt properties, **517** span >10%, **154** >20%, **36** >30% (392
   was a 35% span). Exclude any property with an operator merge in its history — never undo
   a human decision; some single-listing-mis-area cases will be self-correcting (split →
   the engine re-forms the right group), so the dry-run count is the working scope.
2. **Re-evaluate via the engine's own reversible machinery.** For each in-scope property,
   `split_property_to_singletons` (the `resplit_mixed.yml` primitive) detaches members
   back to singletons; the next daily `dedup_engine` run re-forms only the correct groups
   under the new rules (73-unit cross-portal, 87-unit, 99-unit(s)). Idempotent, uses the
   new rules automatically, fully logged via `property_merge_events`.
3. **Delivery.** `scripts/resweep_dedup.py` + a dispatch workflow (mirrors
   `resplit_mixed.yml`), `--dry-run` first (count splits, write nothing).
4. **Sequencing.** Merge this PR first (engine stops creating new chain-merges), *then*
   run the sweep — otherwise the sweep races fresh bad merges.
5. **Validation.** Re-run the area-spread query → expect ~0 incoherent auto-merged byt
   properties >10% spread; spot-check 392 decomposed into its units.

The two same-area 99 m² units are deliberately left to investigation #3 + the operator
queue — area and room-type cannot separate them, and the operator themselves flagged that
pair as a genuine human-judgement call.
