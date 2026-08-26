# Linear probe on frozen CLIP embeddings — design plan

Status: **proposed / not built**. Produced 2026-07-22 from a grounded investigation (5 parallel
code+DB+literature deep-dives) plus a 2-agent adversarial review of the draft; all live numbers
verified against production that day. Supersedes the tagging half of
`clip-visual-embeddings.md` (whose ONNX/torch-free and pgvector-wall assumptions did not survive
the build — see "Corrected assumptions").

**2026-08-26 update — the label store this doc assumes has moved, and gained a capability this
doc doesn't yet account for.** `image_training_examples` (one free-text label per image, cited
throughout below) is superseded by `tag_taxonomy` + `image_tag_labels` (migration 442,
docs/design/tag-annotation-matrix.md): a permanent, per-(image, tag) tri-state fact —
positive/negative/excluded — that a real per-tag head can train on directly (excluded rows drop
out of that head's set; negative is an explicit, informative example, not just "not the labeled
class"). This doc's v1 (below) still assumes ONE multinomial head over ~10–11 logical classes
with multi-head "deferred" — the new table is exactly that deferred step's data substrate, not a
competing design. Anything below that reads counts off `image_training_examples` (the 2026-07-22
ground truth section, the corpus-skew numbers) is a historical snapshot of the OLD store, not
live. Reconciling this doc's trainer plan with the new table (v1 scope, multi-head timing, how
`excluded` interacts with the "OOD sink" idea) is follow-up work, not done here.

## Verdict

- **Viable, entirely on GitHub Actions. Railway has no role.** Training is seconds of CPU;
  the 8.27M back-catalogue re-score is a retag-shaped campaign (a few free self-chained ticks,
  ~40 GB one-off egress inside the 250 GB/mo Pro quota). The realtime dedup lane ships dark and
  dedup drains hourly, so there is no latency case for an always-on host.
- **The CLIP model needs no adjustment.** The probe is a head over the frozen stored embeddings.
  The only encoder work is *pinning* (HF `revision=`, transformers/torch versions, a score-time
  revision assertion) — currently unpinned, which would silently invalidate any trained probe.
  Encoder upgrade (SigLIP-class, 768-d) is deferred until the probe plateaus; the
  `(image_id, model)` store already supports a later side-by-side A/B.
- **Compute-free but label-expensive.** The binding resource is operator labeling time, not
  runner minutes: a defensible merge-gate cutover needs ~300 targeted blind labels *per gate
  class per probe version* plus one-off canary/negative collection. The probe math is the easy 10%.

## Ground truth (live 2026-07-22)

- `image_clip_embeddings`: 8,266,118 rows, one model key `openai/clip-vit-base-patch32`,
  vector(512), ~100% of stored images. `image_clip_tags`: same count (zero-shot argmax).
- Training set `image_training_examples`: 1,076 rows / 52 free-text labels (grew 782→1,076 in one
  day mid-audit — actively curated). 473 listings / 468 properties → **71.5% of images share a
  listing** with another training image (largest group 27). Exact-pHash dups: 12 images, 0
  label conflicts. Single annotator; 6 border cases.
- Category skew: training is 88.8% byt vs a 40.7%-byt corpus; dum/komercni/pozemek = 58% of the
  corpus but ~11% of the set. komercni: 3 images.
- Operator-vs-CLIP agreement (override-enriched, anchor-biased — ranking diagnostic only):
  other 24.7%, exterior_facade 48.6%, balcony_terrace 50%, bedroom 52%, property_document 52%,
  living_room 60%, site_plan 64%, hallway 65.5% … floor_plan 95%, garden 95.5%. Corpus red flag:
  CLIP calls 17.9% of ALL images `hallway` (largest class — an ambiguity dumping ground).

## Corrected assumptions (validated per the operator's instruction; fix, don't inherit)

1. `docs/design/clip-visual-embeddings.md` mandates ONNX-int8/torch-free — production ships
   transformers + CPU torch. Its "pgvector wall" was resolved by never building an ANN index.
2. **Nothing pins the encoder**: no `revision=` in `from_pretrained`, `transformers>=4.40` floats,
   torch unpinned; the DB `model` key is a string, not a content hash. The `_project()` shim is
   proof version drift already bit once.
3. **The 6 merge-gate queries read `image_clip_tags` with NO model filter** (`_both_have_site_plan`,
   `_floor_plan_image_ids`, pHash count/distinctive, `_high_render_image_ids`,
   `render_exclusion_clause` + 2 API readers); `images_public` resolves latest-`tagged_at`-wins.
   Writing a second row per image under a new model key would silently flip galleries and mix
   opinions inside auto-merge gates. (Cosine path IS model-scoped — asymmetric.)
4. **The training store is mutable with no history** (upsert-overwrite + hard deletes, no
   snapshots) — contradicts platform rules 8/12 discipline; results are unreproducible as-is.
5. **Anchor bias**: the Train CTA pre-fills CLIP's argmax, so labels partly echo the model being
   corrected — worst on exactly the low-agreement classes the probe should improve.
6. CI **does** replay pgvector (`migrations.yml` builds `postgresql-17-pgvector`); migration 226's
   comment is stale. The CI gap is *performance* (PREPARE-only), not schema.
7. Smaller drift: `retag_from_embeddings.py` "active-only" docstring stale; dead `--categories`
   in `clip_tag.yml`; `ImageRenderBadge` 0.65 vs engine 0.95; `imageTags.ts` "12 canonical" (=15);
   `queries.ts` `.limit(2000)` silent-undercount cliff; a residual tagged-but-vectorless tail
   (historical ~19%, closing) that any embeddings-JOIN campaign silently skips.

## Architecture

### D1 — Freeze and pin the encoder (prerequisite)
Pin HF `revision=` (commit SHA) + transformers + torch in the clip lane. The probe artifact
records encoder revision + processor-config hash; **scoring hard-fails on mismatch**. Add a
canary-embedding checksum (a few committed test images; assert stored vs fresh vectors match to
6 decimals) as a pre-score/CI guard. Train and score only from **stored** (6-decimal-rounded)
vectors — exact train/infer parity, no R2 reads.

### D2 — Label-space contract: versioned map; single coarse head in v1
`data/clip_labels.json`: canonical machine keys, collapse map → the 15-logical engine contract,
Czech display strings (one source, shared with `imageTags.ts`). Canonicalization of the 52
free-text labels (typos, synonyms, EN/CZ merge) happens **at training time via this map** — no
curation-UI rework needed for v1. Drop non-image classes (`mezonet`); furnishing and
render-vs-photo are orthogonal axes, **not** classes.
**v1 trains ONE multinomial head over the ~10–11 logical classes with ≥16 grouped examples,
plus an explicit `other`/OOD sink.** The multi-head shape (per-parent fine sub-heads like
bathroom shower/tub, sigmoid axes) is the documented target, deferred until a class hits
≥40 images across ≥25 properties AND a consumer actually reads it. `render_score` is untouched.

### D3 — Provenance by write-time composition (no read-side resolver)
Rejected: per-version model keys (`probe/v1` rows would add 8.27M rows per retrain and force an
unbounded resolver) and a read-side preferred-tagger resolver threaded through 9 hot call sites
(unproven predicate-pushdown under the 3-s anon / 2-min pooler timeouts; CI can't test perf).
**Chosen:** `image_clip_tags` keeps exactly ONE canonical row per image under the existing model
key (`model` truthfully = the *encoder*). Additive migration adds `tagger` ('zeroshot'|'probe')
+ `tagger_version` provenance columns, and a **shadow table** `image_probe_scores` (probe class,
score, top-k, version; RLS-locked like the tag tables; gates never read it).
The **per-class ownership policy lives in the probe artifact**: at write/re-score time the job
composes zero-shot + probe → one canonical row (probe wins only for classes it has cleared).
Consequences: zero reader changes ever; no `tagged_at` ties; no cross-row opinion mixing;
rollback = revert artifact + re-run campaign (zero-shot is always recomputable from embeddings).
Version-skew rule: a policy/probe change is a campaign; it is "live" only when the campaign's
drain-complete no-op fires. Measure and backfill the tagged-but-vectorless tail first.

### D4 — Dataset program (critical path)
1. **Reproducibility**: the training job exports a content-hashed JSONL snapshot (labels +
   group assignment + label-map version) committed/archived per run; every artifact records
   `dataset_hash`. (A snapshot *table* is deferred.)
2. **Grouped splits**: StratifiedGroupKFold; group key = `listing_id` ∪ **pHash Hamming ≤ 6**
   connected components. Cosine links only at the engine-validated **0.98**, within-fine-tag,
   never across tags (0.95 sits *below* the 0.98 auto-merge bar and would chain drawing-heavy
   classes into mega-components → un-cross-validatable folds). Hard acceptance check: any
   component > ~8 images is inspected/rejected. Group assignment is frozen in the snapshot.
3. **Two eval sets, not one**:
   - *Representativeness canary*: ~300–500 images, blind-labeled, corpus-stratified, frozen
     (rolling refresh; track reuse count) — for distributional checks (abstain rate, per-class
     prediction rates, OOD surfacing). Never trains.
   - *Per-gate-class precision audits*: ~300 of the probe's **predicted positives** per gate
     class, corpus-category-stratified, re-drawn per probe version — the only evidence a gate
     cutover may cite. Wilson math: at n=50 a 0.95 floor is uncertifiable even at zero errors;
     n≈300 at observed ~0.98 puts the lower bound comfortably over 0.95.
   - Plus a ~50–100 double-labeled slice to bound single-annotator label noise; report floors
     relative to measured self-agreement (a 0.95 floor sits within one noise-width of the
     ceiling if operator error is ~2–3%).
4. **De-anchoring**: blind-labeling mode (no argmax pre-fill) is load-bearing — required for the
   canary AND for re-labeling/auditing the low-agreement classes (other, exterior_facade,
   balcony, bedroom, property_document) whose current labels partly echo CLIP.
5. **Growth**: category-stratified collection for dum/komercni/pozemek; **komercni/industrial/
   construction negatives are a blocking prerequisite for the site_plan gate** (confident-OOD
   false positives are the incident mode; max-prob abstain cannot catch them). Active-learning
   queue mines zero-shot⊕probe disagreement (free from the shadow table) + low-max-prob +
   embedding-space diversity.

### D5 — Training and thresholds
Multinomial L2 logistic regression (canonical CLIP-paper protocol: lbfgs, class-balanced,
C-sweep) — scikit-learn confined to a **training-only extra**; inference is numpy-only from the
artifact. **No naive WiSE-FT weight blend** — the zero-shot head (`100·T_c`, no bias) and an LR
head are scale-incommensurable; blending them is incoherent. Anchoring = **per-class selection**
(the D3 ownership policy: probe replaces zero-shot per class only where proven; tail classes stay
zero-shot-owned), optionally an L2-SP-toward-zero-shot-init experiment later.
**No calibrated probabilities as gate inputs in v1**: gate thresholds are per-class raw-score
thresholds chosen and CI-validated directly on the precision audits. Global temperature scaling
only for display tooltips. `FLOOR_PLAN_MIN_CONFIDENCE=0.50` is softmax-calibrated — re-derive
against the probe score AND against the gates' CLIP-OR-LLM union (the paid-LLM branch stays).

### D6 — Artifact + deployment
`data/clip_probe.json` (git-versioned in place): weights, bias, ordered classes, ownership
policy, per-class thresholds, label-map version, dataset hash, encoder revision. ~90 KB,
git-diffable, deploys atomically with consuming code, rollback = revert. Back-catalogue
re-score: a `clip_probe_retag_after` campaign clone (4 shards, own concurrency group,
`max-parallel: 4`, never overlapping a taxonomy retag; **batched COPY→staging→UPDATE writes**,
not per-row executemany; no model download — unlike retag, the probe needs no transformers).
Steady state: probe scored inside `clip_tag_backfill`'s existing pass (embedding already in
memory) — no new lane, no new runner.

### D7 — Eval harness (build before any cutover; nothing measures this today)
`scripts/probe_eval.py`: probe vs zero-shot on the canary + grouped CV; per-class
precision/recall with Wilson bounds; pooled McNemar reserved for the *display* cutover decision
(per-class McNemar is underpowered — single-digit discordants); pre-registered gate classes.
Results persisted with dataset hash. (`embedding_ab.py` measures cosine separation,
`clip_trial.py` compares vs Haiku — both the wrong axis.)

### D8 — Staged cutover, easiest-first
Stage 0 *shadow*: probe → shadow table only; disagreement feed lights up /clip-audit.
Stage 1 *display*: pooled-McNemar-gated; start with **kitchen/bathroom** (well-populated,
in-distribution) to prove the whole eval→threshold→audit machine on an easy class.
Stage 2 *engine, per-class*: precision-floor CI on that class's audit; **site_plan LAST**, not
first — it is simultaneously the rarest, most OOD-exposed, most mega-group-prone, and most
annotator-ambiguous class; its cutover is contingent on the negative collection and a banked
~300-label audit. Each promotion edits the artifact's ownership policy → campaign → live at
drain-complete.

## Where it runs; cost

GH Actions for everything (train = dispatch job, seconds; backfill = campaign ticks; steady
state rides the hourly clip lane). In-DB `<#>` scoring only for bounded incremental top-ups —
never the backfill (no ANN index, shared CPU, pooler timeout). Railway: nothing.
Compute cost ≈ $0; egress ~40 GB/campaign shares the 250 GB quota with SPA reads and taxonomy
retags — schedule campaigns apart. **Real cost = operator label-days**: canary ~1–2 days;
each gate-class audit ~1–2 days per probe version; negatives collection ~1 day. No gate-cutover
PR opens before its label budget is banked.

## Phasing (each PR one purpose)

- **P0a** pin encoder (revision, deps, score-time assertion, embedding checksum).
- **P0b** provenance columns + shadow table (additive migration).
- **P0c** label map v1 + snapshot export in the training job.
- **P0d** blind-labeling mode in the Train CTA.
- **Cleanup PRs** (independent, any time): render badge 0.65→0.95, `imageTags.ts` comment, retag
  docstring, dead `--categories`, `.limit(2000)`, update/retire `clip-visual-embeddings.md`,
  stale migration-226 CI comment.
- **P1** trainer + eval harness + representativeness canary; shadow campaign.
- **P2** disagreement/audit UI + growth program (stratified sampling, negatives, de-anchoring
  re-labels). Label rename/merge tooling lands here, not P0.
- **P3** display cutover → per-class gate cutovers (thresholds re-derived vs the LLM union).
- **P4** fine sub-heads (bathroom split), furnishing axis, snapshot table, encoder A/B (768-d
  SigLIP-class) only on demonstrated plateau.

Same-PR doc rule: P0b/P1/P3 touch behavior documented in `llm-pipelines` + `scraper-ops` skills
and this file — update in the same PRs.

## Open decisions for the operator

1. Confirm **write-time composition** (D3) over a read-side resolver — this is the load-bearing
   architecture choice; everything else follows.
2. Commit to the label budget/pace (the actual critical path) — canary first, then which class
   family to grow (recommend: dum/pozemek exteriors + site-plan negatives).
3. OK to add scikit-learn as a training-only extra (never in runtime images)?
4. First display-cutover targets (recommend kitchen + bathroom).
