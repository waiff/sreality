> **Measured 2026-09-05, after this document was drafted** (its own §5.0 pre-flight, run live):
> `image_clip_embeddings` = **11,301,885** rows — 10,489,289 with `revision NULL` (written before
> the pin) + 812,596 pinned at `3d74acf…` — one model, two provenance states; table size
> **31 GB**; whole database **150 GB**; images stored in R2 = 11,303,863; velocity ≈ 55–60k new
> images/day (39,794 listings first seen in the last 7 days). Every "10.36M" figure below scales
> by ×1.09 (a `halfvec(768)` store ≈ 17.5 GB payload, ~19–20 GB on disk). Provisioned disk size
> and utilisation % still need the Supabase dashboard readout. Status unchanged: **DRAFT,
> proposed, operator-owned; nothing here is decided.**

# Encoder decision — which model produces the embeddings for the NEW DEDUP program

**Date:** 2026-09-05 (rev. 2, after three adversarial reviews) · **Status:** decision proposed, gated on a bake-off, a licence review, two dependency approvals and one disk-headroom check · **Owner:** operator

---

## 0. What changed in this revision, in one breath

Three independent reviews went at v1 with the papers and the repo open. The **recommendation survives** — one encoder, chosen on near-duplicate terms — but a lot of the evidence I used to argue for it did not. The honest summary:

- **Every number in v1 was computed on a corpus size I never measured** (8.45M vectors / 9.6M images). The repo says **10.36M** in three places. All costs and GB figures below are recomputed on 10.36M and marked as provisional until the pre-flight query in §5.0 is actually run.
- **The "lock 512×512" decision had no evidence behind it.** DINOv3's published retrieval numbers are measured at **224 px**, not 512. Resolution is now a measured arm of the bake-off, not a locked knob. So are precision and preprocessing.
- **The bake-off as specified would have produced zero positives** — its positive population and its contamination filter were byte-identical rules. Redesigned in §5.
- **The escalation ladder was aimed at the wrong heads.** The repo's own data says floor plans are among our *strongest* heads; the weak ones are semantic/scene heads, which is exactly where DINO trails text-supervised models.
- **The licence scores were inverted.** The incumbent has no licence grant at all; DINOv3 has a real grant plus three commercial terms I never mentioned (uncapped indemnity, California forum, termination-with-delete).
- **"Storage goes net negative after we drop the old table" was a billing fiction** — Supabase disk cannot shrink.
- **Two arguments were strawmen**: the program ledger already specifies off-DB FAISS for path B, and it never proposed a full-corpus pgvector index. I was arguing against a plan nobody wrote.

Everything below is the corrected document. Where a v1 claim was refuted I say so in place rather than quietly deleting it, so you can see what moved.

---

## 1. The question

Today one model — OpenAI's `openai/clip-vit-base-patch32`, pinned at commit `3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268` in `/home/hejtm/dev/sreality/data/clip_taxonomy.json` — produces every image *embedding* in the platform (an **embedding** is a list of numbers, here 512 of them, that stands in for a photo; two photos that "look alike to the model" get similar lists, and "similar" is measured by **cosine similarity**, a number from −1 to 1).

The repo says that store holds **10.36M vectors** — stated in `migrations/456_clip_embeddings_revision.sql` ("10.36M vectors and every per-tag centroid built on them depend on this being one coherent population"), in `data/clip_taxonomy.json`'s `revision_note`, and in `tests/test_clip_encoder_pin.py`'s docstring. An older live readout in `docs/design/clip-linear-probe.md` (dated 2026-07-22) says 8,266,118. **Neither has been re-measured for this decision**, and v1 of this document used a third number (8.45M) that appears nowhere. §5.0 is the query that settles it; every figure below scales linearly with it.

Those vectors are about to be asked to do two very different jobs at once:

- **(a)** be the input on which per-tag classifiers ("heads" — one small yes/no model per tag: *is this a kitchen? is this a floor plan?*) are trained;
- **(b)** be the signal for Level-3 near-duplicate similarity, and for **path B**, a batch nearest-neighbour search that proposes *which listings might be the same property* by finding images that look like the same photograph.

One correction of fact about (b): **path B is not a corpus-wide search.** The ledger scopes it as "batch k-NN over per-type priority **same-tag** image embeddings" (`docs/design/new-dedup/PROGRAM.md`, ledger line 40 and W3). That matters twice: it shrinks the index-sizing argument, and it means "two different properties carrying the same tag" is not an incidental failure mode of path B — it is the entire search space by design.

Job (a) asks "what **kind** of thing is this?" Job (b) asks "is this the **same** thing?" The published evidence says the CLIP/SigLIP family is stronger at the first and much weaker at the second, and the DINO family the reverse. So: **which encoder, which variant, and does one model serve both jobs or do we need two?**

> **PM framing (unchanged, and none of the reviews dented it).** The two jobs have wildly different *reversal costs*. A tag head is a handful of logistic regressions over cached vectors — retraining them takes minutes. The similarity store is a full-corpus re-embed plus a search index plus the invalidation of every number you have ever calibrated against, on a database whose disk cannot be shrunk afterwards. **Cheap-and-reversible should never dictate the choice of expensive-and-sticky.** The encoder is therefore chosen on job (b)'s terms, and job (a) lives with the consequence — unless job (a)'s loss turns out to be large, which the bake-off in §5 measures.

---

## 2. Options matrix

**Scoring:** 1 = bad / 5 = excellent, from the platform's point of view. Steady-state cost assumes **50,000 new images/day**. GPU costs are RunPod **community cloud**, per-second billing ([runpod.io/pricing](https://www.runpod.io/pricing)); R2 egress is $0 and Class B (`GetObject`) has a 10M/month free tier, $0.36/M after ([R2 pricing](https://developers.cloudflare.com/r2/pricing/), verified 2026-09-05).

Two new columns in this revision:

- **A′ — `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`**, the same architecture as the incumbent but **MIT-licensed, ungated, shipping safetensors** (verified via the HF model API, 2026-09-05). Its presence answers a question v1 never asked: *is the incumbent's problem the CLIP family, or is it this specific 2021 88M-parameter 224-px checkpoint with no licence?*
- **TOAST / read cost** — a Postgres storage detail with real consequences, see below.

### 2.1 Summary scores

| | **A** CLIP B/32 (today) | **A′** LAION B/32 (MIT) | **B** DINOv3 ViT-B/16 | **C** SigLIP2 So400m/16 | **D** SigLIP2 + DINOv3 | **E** DINOv2 ViT-L/14-reg |
|---|---|---|---|---|---|---|
| Tag-head accuracy | 3 | 3 | 4 | 5 | 5 | 4 |
| Near-duplicate accuracy | 2 (unmeasured) | 2 (unmeasured) | **5** | 1 | 5 | 4 |
| One-off corpus cost | 5 ($0) | 4 (~$1-2) | 4 (~$3-12) | 4 (~$2-4) | 3 (~$4-14) | 3 (~$9-13) |
| Steady-state $/day @50k | 5 ($0) | 5 ($0) | 4 ($0.05-0.50) | 4 (similar) | 3 (2 lanes) | 3 (~2× B) |
| Supabase storage (peak, **non-reclaimable**) | 5 (0) | 5 (0) | 4 (+~18 GB) | 2 (+~27 GB) | 1 (+~36 GB) | 3 (+~24 GB) |
| TOAST / read cost | 2 | 2 | **5** | 2 | 3 | 2 |
| Operational simplicity | 5 | 4 | 3 | 3 | 1 | 4 |
| Licence / longevity | **2** | **5** | 2 | 5 | 2 | 5 |
| Migration effort | 5 (none) | 3 | 2 | 2 | 1 | 2 |
| **Verdict** | unmeasured on (b); worst licence | honest baseline + licence-clean drop-in | **recommended, gated** | fails job (b) | over-engineered | licence-clean near-equal |

**On the TOAST row** (this is new, and it is a genuine differentiator no one had noticed). Postgres moves a row's wide columns out of the main table into a side table ("TOAST") once the row crosses ~2 KB, which turns one read into two. `toolkit/tag_candidates.py` builds its whole draw-pool budget on this: *"a vector(512) is 2,056 bytes — over TOAST_TUPLE_THRESHOLD — so every vector is a heap fetch plus a TOAST fetch."* Using pgvector's published formulas — `vector` = 4×dims+8, `halfvec` = 2×dims+8 ([pgvector README](https://github.com/pgvector/pgvector)):

| Vector | Bytes/row | Above the ~2 KB threshold? |
|---|---|---|
| `vector(512)` — today | 2,056 | **yes** — every read is heap + TOAST |
| `halfvec(768)` — option B | **1,544** | **no** — stored inline, the TOAST fetch disappears |
| `halfvec(1024)` — option E | 2,056 | yes |
| `halfvec(1152)` — option C | 2,312 | yes |

So option B doesn't merely shift that comment's arithmetic (as v1 said) — it **inverts its conclusion**, and every drawn queue and outlier ranking gets cheaper to read. Options C and E do not.

### 2.2 Option A — keep `openai/clip-vit-base-patch32` (512-d, 224 px, 88M params)

| Criterion | Score | Justification |
|---|---|---|
| Tag-head accuracy | **3** (was 2) | **v1's main evidence here was withdrawn.** I cited the repo's operator-vs-CLIP agreement figures (24.7% `other`, 48.6% `exterior_facade`, …) as proof the encoder is weak. `docs/design/clip-linear-probe.md` labels that exact line *"(override-enriched, anchor-biased — ranking diagnostic only)"* and explains why in its own corrected-assumption 5: *"the Train CTA pre-fills CLIP's argmax, so labels partly echo the model being corrected."* Those numbers say which **tags are ambiguous**, not how good the encoder is, and using them as encoder evidence is the one use the source forbids. What honestly stands: at 224 px with patch 32 an image is a 7×7 grid of 49 tokens, each covering a 32×32-px block; and in our few-labels regime a frozen B/32 linear probe scores **59.16** at 10-shot ImageNet vs **73.94** for a LAION-2B L/14 — a 14.8-point gap, roughly double the full-data gap ([OpenCLIP scaling laws, Table 5](https://ar5iv.labs.arxiv.org/html/2212.07143)). Few labels per head is an argument *for* a stronger encoder. That is a real deficit, not a catastrophe. |
| Near-dup accuracy | **2** (was 1) | Still the weakest axis, but v1 overclaimed on three counts and I withdraw all three. **(1)** AnyPattern's "<10% µAP" is a *zero-shot, not-trained-for-the-task* CLIP measured against detectors trained on DISC21, on deliberately novel tampering patterns, and the paper attributes it to training-data domain (*"CLIP being predominantly trained on natural images"*) — not to the architectural "no image-image objective" story I told ([arXiv:2404.13788](https://arxiv.org/html/2404.13788v1)); the paper reports **no DINO row at all**, so it cannot establish an ordering, and v1's companion "32.2 µAP for DINO ViT-B/16" had no citation and is dropped. **(2)** The MMVP "CLIP-blind pairs" argument is circular: *"less than 0.6 for DINOv2 embeddings"* is half of the **selection rule**, so DINOv2's advantage on that set is true by construction; and it is 150 designed pairs on CLIP-**L/14**, not an industrial-scale yield on our checkpoint ([arXiv:2401.06209](https://arxiv.org/html/2401.06209v1) §2.1-2.2). It remains a valid *existence proof* that a high CLIP cosine can sit on two visually different images — nothing more. **(3)** I misread the DINOv2 table: OpenCLIP ViT-G/14's **AmsterTime mAP is 24.6**, not 23.9 (23.9 is the Met GAP- column) ([DINOv2 Table 9](https://ar5iv.labs.arxiv.org/html/2304.07193)). What stands, same-paper and matched-size: OpenCLIP ViT-G/14 Oxford-Hard **19.7** vs DINOv2 ViT-L **54.0**. **The decisive fact remains that nobody has measured this checkpoint on our images.** Score 2, not 1, and the bake-off is what settles it. |
| One-off / steady-state | **5 / 5** | $0. Vectors exist; the lane is CPU torch on free GitHub runners, hourly (`.github/workflows/clip_tag.yml`, `cron: "40 * * * *"`). |
| Storage | **5** | Already provisioned. At 10.36M rows × 2,056 B ≈ 21.3 GB of payload, ~23-25 GB with heap headers, the `(image_id, model)` index and TOAST overhead — which is where v1's "~25 GB" came from and *only* reconciles at ~10.36M rows, not at 8.45M. |
| TOAST / read cost | **2** | Every vector read is a heap fetch plus a TOAST fetch (see §2.1). |
| Operational simplicity | **5** | Nothing to learn, no second identity concept. |
| **Licence / longevity** | **2** (was 4) | **This score was inverted in v1 and the reviews are right.** The HF weight repo carries **no licence field at all** (cardData `license: None`; README front-matter has only `tags`/`widget`), and the model card states verbatim: *"**Any** deployed use case of the model - whether commercial or not - is currently out of scope"*, with image search called out as not recommended without in-domain testing ([model card](https://huggingface.co/openai/clip-vit-base-patch32)). The code is MIT ([openai/CLIP LICENSE](https://github.com/openai/CLIP/blob/main/LICENSE)) but the code is not what we run. A no-grant artifact whose own card forbids exactly our deployment cannot score double an express-grant artifact. **Separately, I withdraw the "forced migration is queued anyway" argument entirely.** The pinned revision genuinely ships no `.safetensors` (verified at that sha: `pytorch_model.bin`, `flax_model.msgpack`, `tf_model.h5`) — but `pyproject.toml` says in terms why the `transformers>=4.40,<5` bound exists: *"the same weights give the same numbers across 4.x. The <5 guard is against a major release changing the from_pretrained/CLIPModel API … which is a breakage risk, not a drift risk."* transformers 5 removed unsafe `.bin` *saving*, not loading, and CLIP was not removed from the library ([transformers v5 notes](https://github.com/huggingface/blog/blob/main/transformers-v5.md)). Nothing forces a move. That was the only "you have to move anyway" argument against the incumbent and it does not hold. |
| Migration effort | **5** | None. |

**Reading:** A is free and is the only option with zero work. Its real weaknesses, honestly stated, are that it has **no licence grant** and that **nobody has measured it on job (b) with our images**.

### 2.3 Option A′ — `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` (MIT, ungated, 512-d)

Same architecture, same dimensionality, same everything the pipeline touches — but a **real licence** (`license: mit`, `gated: false`, ships `model.safetensors`; verified via the HF model API 2026-09-05) and, per the OpenCLIP scaling-laws table this document already cites, a stronger LAION-2B checkpoint than the OpenAI original.

It is not a serious candidate for job (b). It is here for two reasons, both of which v1 missed:

1. **It is the fair CLIP baseline.** With only the incumbent in the run, a bad CLIP result cannot distinguish "the CLIP family fails at near-duplicate" from "this particular 2021 checkpoint fails". That distinction decides whether §3.3's stick-with-the-plan branch is live.
2. **If we stick, it dissolves the licence problem** for the cost of one re-embed (~$1-2 of GPU) and no architectural change at all.

### 2.4 Option B — single **DINOv3 ViT-B/16** (`facebook/dinov3-vitb16-pretrain-lvd1689m`, 85.6M params, **768-d**, patch 16)

| Criterion | Score | Justification |
|---|---|---|
| Tag-head accuracy | **4** | DINOv3 is trained with no labels and no text, purely on images. **Correction to v1:** its published probe protocol is *not* "exactly ours". Only the 12 small Fine-S datasets use sklearn logistic regression; for ImageNet, **Places205** and iNaturalist — including my own nominated proxy for "which room is this" — App. D.7 trains a linear layer with SGD, momentum 0.9, 10 epochs, batch 1024, a 15-point learning-rate sweep and random-resize-crop augmentation, selecting on a validation set ([arXiv:2508.10104](https://arxiv.org/html/2508.10104v1)). A tuned, augmented probe flatters frozen features relative to the plain L2 logistic regressions we will actually fit. Per-size and token-matched (Table 14), DINOv3-B beats DINOv2-B by +8.3 on ImageNet-R (76.7 vs 68.4) and +6.8 on ObjectNet (64.1 vs 57.3). On *scene* recognition it is competitive but behind text-supervised models (SUN397 81.1 for DINOv3-7B vs 85.1 SigLIP-g; Places205 70.0 vs SigLIP2-g's 70.5). |
| **Near-dup accuracy** | **5** | **The reason to choose it — and after correction the evidence is narrower but cleaner.** Token-matched, same table, same protocol, per size: **Oxford-Hard — DINOv3-B 58.5 vs DINOv2-B 51.0; DINOv3-L 63.1 vs DINOv2-L 55.7; SigLIP2-L 21.4; SigLIP2-B and PEcore-B both 20.2** (Table 14). That is the load-bearing evidence and it survived every review intact. **Withdrawn from v1:** (i) *"matches DINOv2's 1.1B flagship (58.2) at 3.5× less compute"* — 58.2 is from Tables 9/23 under a **different protocol** (Oxford evaluated at 224 with a full centre crop, App. D.8) and Table 14 contains no DINOv2-g row at all; the compute ratio is ~13×, not 3.5× (3.5× is L-vs-g). (ii) Every **Met GAP** number I quoted (55.4 vs 0.0; 40.0 vs 6.5) is measured after **PCA whitening fitted on Met's own training set plus a k/τ grid search on Met's validation set** (App. D.8) — our pipeline has neither, so Met measures a capability we will not have. I still cite AmsterTime (DINOv3-7B 56.5 vs SigLIP2-g 15.5, Table 23) because matching a modern street photo to an archival photo of the same building is the most transferable public analogue we have — but as an *ordering*, not a magnitude. (iii) The ImageNet-C robustness argument is gone: 19.6 vs 24.1 is **DINOv3-7B vs DINOv2-g**, not ViT-B, and mCE is the corruption error *of a linear classifier*, which can stay accurate while the embedding itself moves. Nothing in it measures cosine(x, JPEG(x)). §5's synthetic-transform arm now measures that directly instead. |
| One-off corpus cost | **4** | **Widened, and now stated as a band.** v1's throughput numbers came from timm's benchmark CSVs, which measure a **pure GPU forward pass over synthetic tensors at batch 1024** — no JPEG decode, no resize, no network — while §5 simultaneously argues (correctly) that decode is CPU-side and vCPU count is the binding constraint. Both cannot be true. Treating the timm figure as a **ceiling**: `vit_base_patch16_dinov3` @256 = 1,340 img/s on a 4090 ([timm CSV](https://raw.githubusercontent.com/huggingface/pytorch-image-models/main/results/benchmark-infer-amp-nchw-pt291-cu128-4090.csv)), scaling to ~290 img/s at 512 on FLOPs. At 10.36M images: **GPU-only ≈ $3.4 at 512 px, ≈ $0.75 at 256 px**; decode-limited, plausibly 2-4× that. **Band: $3-12 at 512, $1-2 at 256.** (v1's "12→63 GFLOPs" citation was also wrong — timm reports 23.6 GMACs ≈ 47 GFLOPs at 256. The ratio survives; the cited numbers didn't.) |
| Steady-state @50k/day | **4** (was 5) | **v1 compared unlike things.** Today's lane runs **hourly** on free runners, so a new image is embedded within the hour. The $0.023/day figure was a **once-daily** figure — i.e. up to 24 hours of embedding latency, a product regression presented as a cost win. Priced honestly: one daily batch ≈ $0.04-0.11/day; **keeping the hourly cadence ≈ $0.30-0.50/day**, because RunPod's billed rental is dominated by start-up (our own live runs billed **126 s** and **482 s** for jobs whose inner command finished in seconds — `PROGRAM.md`, 2026-08-06 parts 4-5). **The cadence is an operator decision, and it belongs in the ledger, not in a footnote.** |
| Storage (peak) | **4** | 10.36M × `halfvec(768)` (1,544 B) ≈ **16 GB payload, ~17-18 GB with heap and the `(image_id, encoder_id)` index** ≈ $2.25/mo at $0.125/GB. **The "+15 GB → net −10 GB" framing in v1 is withdrawn: Supabase disk cannot be shrunk.** You can increase disk size but not decrease it; reclaiming allocation requires a Postgres version upgrade with downtime ([compute & disk](https://supabase.com/docs/guides/platform/compute-and-disk), [database size](https://supabase.com/docs/guides/platform/database-size)). So the honest number is the **peak**: ~25 GB (old) + ~18 GB (new) ≈ **43 GB held indefinitely**, of which ~$3.10/mo is stranded allocation after the old table is dropped. |
| TOAST / read cost | **5** | 1,544 B/row falls **below** the TOAST threshold — the extra fetch per vector disappears (§2.1). |
| Operational simplicity | **3** (was 4) | One model, one vector, one table — but three things to hold, not two. **Gated weights:** `gated: manual`; even `config.json` returns 401 unauthenticated (verified: `x-error-code: GatedRepo`). Approval time is anecdotal and **bimodal** — the same thread reports "~15 minutes" and "waiting since March 25"; v1's "a few days to a few weeks" was one unaffiliated commenter's guess presented as a reported distribution ([issue #332](https://github.com/facebookresearch/dinov3/issues/332)). **Precision is unsettled** (see §6). **`model.eval()` is mandatory** — the config carries `pos_embed_shift`/`jitter`/`rescale`, positional augmentations applied only in training mode. |
| Licence / longevity | **2** | Still 2 — but for materially different and worse reasons than v1 gave. Full treatment in §2.8. |
| Migration effort | **2** (was 3) | **v1 undercounted.** 32 files reference `image_clip_embeddings` (`git grep -l`), and the categories I missed carry the most risk — an RLS security posture and a CI-replay guard, neither of which is "mechanical". Full surface in §4.3. |

### 2.5 Option C — single **SigLIP2 So400m/16 @256** (`google/siglip2-so400m-patch16-256`, 427.9M vision tower, **1152-d**)

| Criterion | Score | Justification |
|---|---|---|
| Tag-head accuracy | **5** | The best of the field on this job. On DINOv3's own matched frozen-probe tables, SigLIP2-g beats both DINOv2-g and DINOv3-7B on Places205 (70.5 / 68.2 / 70.0) and leads the 12-dataset Fine-S average (93.7 vs 92.6 / 93.0). It keeps a **working text tower** (Czech is among the 36 XM3600 languages), so zero-shot bootstrapping of a brand-new tag survives. **Softened from v1:** I attributed the PASCAL 72.0→77.1 jump to the self-distillation + masked-prediction losses specifically — Table 2 is a whole-model comparison, not an ablation, and the delta also bundles LocCa decoder pretraining, data curation, multilinguality and distillation; and mIoU measures *dense un-pooled patch* quality, which does not transfer to the single pooled vector we would store ([arXiv:2502.14786](https://arxiv.org/html/2502.14786v1)). |
| Near-dup accuracy | **1** | Disqualifying, and the *empirical* case is unchanged: matched-size, same protocol, **SigLIP2-L Oxford-Hard 21.4 against DINOv3-L's 63.1; SigLIP2-B 20.2** (Table 14). **My mechanism story was wrong, though, and its being wrong makes the result more striking, not less.** I claimed the sigmoid image-text loss "contains no image-image objective at all" — but SigLIP **2** explicitly adds self-distillation and masked prediction, which *are* image-image objectives, and it still lands at 20-21 on Oxford-Hard. So this is an empirical fact about text-supervised training at scale, not a clean architectural argument, and I should not dress it up as one. Also withdrawn: the GLDv2 74.1→65.6 "regression" — that is **0-shot classification** at So400m/14 only; at B/16 and L/16 SigLIP2 improves at every resolution. Honest counterweight retained: on [ILIAS](https://vrg.fel.cvut.cz/ilias/) (1,000 everyday-object instances, 100M distractors) SigLIP2-L@512 scores 25.3 vs DINOv2-L's 18.5 — cluttered everyday objects where semantics help. |
| One-off / steady | **4 / 4** | 480 img/s GPU-only @256 on a 4090 → **$2-4** for the corpus. |
| Storage (peak) | **2** | 1152-d halfvec × 10.36M = **24 GB payload, ~26-28 GB on disk** ≈ $3.40/mo, non-reclaimable; and 2,312 B/row stays above the TOAST threshold. |
| Operational simplicity | **3** | Three sharp edges: **no CLS token at all** (pooling is a learned attention head; `last_hidden_state[:, 0]` is the top-left *patch*, not a summary — and the existing bench harness hardcodes exactly that slice, see §5.1); normalization is 0.5/0.5/0.5, not CLIP's; and FixRes preprocessing **squashes to a square**, distorting a 4:3 listing photo differently depending on the portal's crop. |
| Licence / longevity | **5** | Apache-2.0, ungated, ships safetensors, no acceptable-use policy. The best licence position in the field, alongside E. |
| Migration effort | **2** | Same surface as B, plus rewriting the prompt anchors for a new text tower. |

### 2.6 Option D — two encoders (SigLIP2-B/16 for tags + DINOv3-B/16 for similarity)

Best-in-class on each job by construction; ~$4-14 one-off; **~36 GB of non-reclaimable storage**; and the real cost is conceptual. Two vector spaces means every surface must state *which* space its number came from; every centroid, drawn queue and "nearest tags" reading exists twice; the operator's mental model becomes "which distance am I looking at?" — which directly contradicts the program's stated instruction that the operator must be able to hold every step in their head. **And it must never be collapsed into one concatenated vector** — half the numbers would measure "same photo" and half "same kind of room", their sum measures neither.

**When D becomes right:** only after measurement, and only in one narrow shape — if the bake-off shows that specific heads materially underperform under DINOv3 *after training on real labels*, add a small text-aligned encoder **for those heads only**: a second store for *classification*, which is cheap and reversible (no index, no calibrated number, no migration).

**Which heads, though — corrected.** v1 pointed this ladder at the document-like heads (floor plan, cadastral map, certificate) on the strength of DINOv3's OCR-benchmark deficit. That was wrong on both ends:

- The benchmark spread is **−1.6 to +15.5**, not "5-15 points" — DINOv3-7B actually *beats* PE-core-G on RP2K (94.7 vs 93.1) — it is 7B-vs-G with SigLIP2 absent from the table entirely, and the tasks are street signs, brand logos and retail products, i.e. **glyph association**, which the paper itself names as the cause (*"our model does not leverage pair image-text data … a much harder time learning glyph associations"*, Table 25 discussion). "Is this a floor plan?" is line-art-vs-photograph style discrimination, not glyph reading.
- **The repo's own data points the other way.** Under the *weakest* CLIP checkpoint, `floor_plan` and `garden` are the **strongest** heads (95%, 95.5%); the weak ones are `other` (24.7%), `exterior_facade` (48.6%), `balcony_terrace` (50%), `bedroom` (52%) — semantic/scene heads, which is precisely where DINO trails SigLIP (SUN397 81.1 vs 85.1; Places205 70.0 vs 70.5). Those agreement numbers are anchor-biased and can't score an encoder, but they are perfectly good evidence about *which heads are hard*.

**So the escalation ladder now points at the semantic heads** — `other`, `exterior_facade`, `balcony_terrace`, `bedroom`, `hallway` — and the order of attempts is **more labels → higher input resolution → a second encoder for those heads only**, with the second encoder last.

### 2.7 Option E — single **DINOv2 ViT-L/14 with registers** (`facebook/dinov2-with-registers-large`, rev `e4c89a4e05589de9b3e188688a303d0f3c04d0f3`, 304M params, **1024-d**)

| Criterion | Score | Justification |
|---|---|---|
| Tag-head accuracy | **4** | Places205 67.3, SUN397 78.7, 12-benchmark average 91.2 ([DINOv2 Tables 7/8](https://ar5iv.labs.arxiv.org/html/2304.07193)); registers additionally move the ImageNet linear probe 86.3 → 86.7. **Caveat added:** DINOv2's linear-probe protocol grid-searches *"whether we concatenate the average-pooled patch token features with the class token (or use only the class token)"* and reports the best (App. B.3) — so these are **max-over-a-pooling-sweep** numbers, not guaranteed CLS-only. |
| Near-dup accuracy | **4** | Genuinely strong and independently corroborated: Oxford-H 54.0, AmsterTime 50.0 (Table 9), and the ILIAS paper singles it out as the model that **degrades least as distractors grow** — the exact regime a batch k-NN lives in. Token-matched it sits one rung below DINOv3 (Oxford-H 55.7 vs 63.1 at L). Sobering detail worth keeping: DINOv2's own authors de-duplicated their training data with SSCD, a purpose-built copy detector, not with DINOv2. |
| One-off corpus cost | **3** | 113.9 img/s GPU-only @518 on a 4090 → ~25 h → **~$8.6, band $9-13** with decode. At 224 px, ~$2. |
| Steady-state | **3** | Roughly 2× option B for the same cadence. |
| Storage (peak) | **3** | 1024-d halfvec × 10.36M = 21.3 GB payload, **~23-25 GB on disk**, non-reclaimable, and 2,056 B/row keeps the TOAST fetch. |
| Operational simplicity | **4** | Ungated, no approval queue, ships safetensors, `AutoImageProcessor` just works. Two traps: input size must be a **multiple of 14** (512 px is silently treated as 504), and with registers the token layout is `[CLS, reg×4, patch…]`, so any patch-mean must slice past 5 — HuggingFace shipped exactly this bug ([transformers #37817](https://github.com/huggingface/transformers/issues/37817)). |
| Licence / longevity | **5** | **Apache-2.0 for code *and* weights**, ungated, no attribution string, no approval, no acceptable-use policy, **no unilateral-amendment clause and no termination-with-delete**. Historical note: at the April 2023 release DINOv2 was CC-BY-NC; Meta relicensed on 2023-08-31 (commit *"Update license everywhere (#182)"*) — verify the copy you deploy. |
| Migration effort | **2** | Identical to B. |

**Standing raised.** v1 filed E as "safe fallback". On licence-adjusted terms it is closer to a co-equal: token-matched, **DINOv3-B (58.5 Oxford-H) buys roughly DINOv2-L-class retrieval (55.7) at one-third the parameters and ~6 GB less storage — in exchange for a bespoke, amendable, terminable licence.** That is a real trade and it is the operator's to make, not mine to hide behind a word like "fallback". §3 is explicit about it.

### 2.8 The DINOv3 licence, properly

v1 gave this a paragraph and got the emphasis wrong in both directions. Here it is straight.

**What the grant actually says (good).** §1.a grants *"a non-exclusive, worldwide, non-transferable and royalty-free limited license … to use, reproduce, distribute, copy, create derivative works of, and make modifications to the DINO Materials"* — **no field-of-use limit, and no Llama-style monthly-active-user cap**. Commercial use is permitted. The use restrictions (§1.b.iii comply with law including privacy/data protection; §1.b.iv no reverse engineering; §1.b.v trade controls, military, nuclear, weapons) do not touch a Czech real-estate pipeline ([LICENSE.md](https://raw.githubusercontent.com/facebookresearch/dinov3/main/LICENSE.md)).

**The "Built with DINOv3" clause never fires for us** — v1 stated it as a standing obligation, which was wrong. It is limb (B) of §1.b.i and is **conditional on distributing or making the Materials or derivative works available to a third party**. Server-side inference with vectors in our own database triggers neither limb.

**Three commercial terms v1 never mentioned, which are what a licence review exists to surface:**

| Clause | Text | Why it matters here |
|---|---|---|
| **§5.b indemnity** | *"You will indemnify and hold harmless Meta from and against any claim by any third party arising out of or related to your use or distribution of the DINO Materials."* | Uncapped, no carve-outs. Apache-2.0 has no equivalent. |
| **§7 governing law** | *"governed and construed under the laws of the State of California … The courts of California shall have exclusive jurisdiction"* — while the counterparty for an EEA licensee is **Meta Platforms Ireland Limited** | An Irish counterparty under a Californian forum, for a Czech operating company. |
| **§6 termination** | *"Meta may terminate this Agreement if you are in breach … Upon termination, you shall delete and cease use of the DINO Materials."* | The failure mode below. |

**§8 amendments, corrected in both directions.** v1 said Meta can amend "effective immediately" — true, but I omitted the limiter (*"provided that they are similar in spirit to the current version"*) **and** I overstated the mitigation: §8 also says *"Your continued use of the DINO Materials after any modification to this Agreement constitutes your agreement to such modification."* So archiving the accepted text is **evidence of what you relied on, not protection**. Calling it "mitigation" made a 2/5 read as managed when the exposure is unchanged.

**The failure mode that actually matters, and which v1 never stated.** §6 requires you to stop using the **Materials** (models, weights, code) — it imposes no obligation to delete **outputs**. So if the grant is terminated or amended unacceptably: the ~10M vectors survive, but **no new image can ever be projected into that space**, on a platform ingesting ~50k images/day. Every calibrated number — Level-3 similarity, path-B neighbours, every head's decision boundary, every centroid — stays defined in a space the pipeline can no longer write into. That is a hard ingest stop, and it is the single strongest longevity argument against option B. Options C and E have no equivalent shape.

**Are our embeddings "derivative works" or "outputs"?** The licence draws the distinction repeatedly — §3 and §5.b speak of *"the DINO Materials **and any output and results therefrom**"* as separate things, while §1.b.i binds only *"DINO Materials, and any derivative works thereof"*, and §5.a confirms *"you are and will be the owner of such derivative works and modifications"*. That distinction is what decides whether we could ever expose vectors, centroids or pair-scores to a third party — an acquirer, a data partner, the Chrome extension's backend, a customer-facing similarity score — without dragging the agreement along. **The reading is probably favourable. It is not, today, a reading anyone has made on the record.**

**The two texts, and why the ungated route makes it worse.** Two non-identical licences circulate under one name, and the divergence is wider than v1 found: the [ai.meta.com copy dated 2025-08-14](https://ai.meta.com/resources/models-and-libraries/dinov3-license/) carries the attribution limb and says *"Sections 5, 6 and 9 shall survive"*; the [repo LICENSE.md dated 2025-08-19](https://raw.githubusercontent.com/facebookresearch/dinov3/main/LICENSE.md) drops the attribution limb and says *"Sections 3, 4 and 7 shall survive"*. Meanwhile the **ungated timm mirror** I proposed as the unattended-pipeline path (`timm/vit_base_patch16_dinov3.lvd1689m`, `gated: false`) **bundles the Aug-19 text while its own HF metadata `license_link` points at the Aug-14 text**. And because that route has no click-through, there is **no accepted text and no acceptance date to archive** — my only stated mitigation cannot be executed on my own recommended acquisition path. (§Acceptance also binds you *"by using or distributing any portion or element of the DINO Materials"*, so the mirror binds you without producing any record of to what.)

**Not superseded, and actively maintained** — a longevity point in DINOv3's favour that v1 failed to make: no DINOv4 exists as of 2026-09; `facebookresearch/dinov3` is not archived (pushed 2026-07-15); transformers main ships `dinov3_vit`, `dinov3_convnext` and `eomt_dinov3`.

**Unresolved and owned by the operator, not by me:** whether §1.b.iii's *"applicable privacy and data protection laws"* obligation needs a GDPR position for a 10M-vector store built from Czech listing photos that routinely contain identifiable people, plates, house numbers and door signage; and the **patent asymmetry** — Apache-2.0 grants patent rights expressly with a defined retaliation trigger, whereas the DINOv3 grant covers *"Meta's intellectual property or other rights … embodied in the DINO Materials"* with no express patent licence and a §5.b that terminates all licences on any IP claim brought against Meta **or any entity**.

---

## 3. Recommendation

### 3.1 The call

> **Adopt option B — one encoder: DINOv3 ViT-B/16 (LVD-1689M), 768 dimensions, stored as `halfvec`, one vector per image, serving both the tag heads and Level-3 / path-B similarity — subject to four gates, in this order: (1) the pre-flight ops readout in §5.0; (2) a licence review with a defined pass/fail, completed *before any production vector is written*; (3) two dependency approvals under CLAUDE.md rule 7; (4) the bake-off in §5, which now measures resolution, precision and preprocessing rather than assuming them. Run DINOv2 ViT-L/14-with-registers in the same bake-off — not as a consolation prize but as the licence-clean near-equal it is.**

Four reasons, in the order they matter:

**1. The cost objection dissolves, and that changes the shape of the decision — but the numbers are bands, not points.** The instinct "we cannot afford to recompute 10M images" is an artifact of running CLIP on CPU inside GitHub Actions. On a rented GPU a full corpus pass at ViT-B/16 costs **somewhere between $1 and $12** depending on resolution and how badly JPEG decode binds; even the most expensive configuration anywhere in this analysis is around $13. **Encoder choice should be driven by signal quality, not by the cost of re-embedding.** What v1 got wrong was the precision, not the conclusion: the throughput figures were GPU-only synthetic-tensor benchmarks quoted as end-to-end rates, and §5 now requires one measured end-to-end number before the corpus pass is scheduled.

**2. The asymmetry of the two jobs is not close — on the evidence that survived.** Matched-size, one table, one protocol: DINOv3-B 58.5 vs SigLIP2-B 20.2 on Oxford-Hard, with DINOv2-B at 51.0. DINO's shortfall on classification is *a few points on scene recognition*, on heads that retrain in minutes and will be trained on real labels rather than zero-shot prompts. That asymmetry is what should pick the encoder, and no review dented it.

**3. The store you were going to keep is being rebuilt anyway.** `migrations/226_clip_engine_schema.sql` says it outright: *"Deliberately NO ANN index: dedup computes cosine between TWO specific listings' images … never a global nearest-neighbour search."* Path B's batch k-NN is precisely the access pattern that table was designed not to serve. A new store and a new access pattern are needed regardless of which encoder wins. **The perceived switching cost is largely already sunk.**

**4. The incumbent has no licence grant and no measurement.** Its model card puts *any* deployed use out of scope, and nobody has ever scored it on job (b) with our images. Defending it means defending both of those. (Note what this reason is *not*: v1 also argued the corpus was internally incoherent because "~8.45M rows carry `revision NULL`". Migration 456's own reasoning is that the **whole** population predates the pin — *"NULL means exactly 'written before the pin'"* — so the population is uniform-but-unpinned, not split. That is a different and milder problem, and §5.0 confirms it either way.)

**And the honest counterweight, stated in the recommendation rather than buried:** on token-matched numbers, **option E gets you within ~3 Oxford-Hard points of B, under Apache-2.0, with no gating, no amendable terms, no indemnity and no ingest-stop risk — at ~3.5× the compute and ~+6 GB of permanently allocated disk.** If the licence review in §3.4 comes back anything other than clean, take E and do not look back. The bake-off exists partly to tell you exactly how many points of retrieval quality the DINOv3 licence terms are costing you.

### 3.2 The concrete configuration — what is locked, and what is measured

v1 presented seven locked knobs. Three of them had no evidence and are now **arms of the bake-off**.

| Knob | Status | Detail |
|---|---|---|
| Model | **locked** | `facebook/dinov3-vitb16-pretrain-lvd1689m`. The ungated `timm/…` mirror may be used for *acquisition* only, and only after the canary check below. |
| Revision | **locked** | Pinned commit sha, mandatory, refuse to embed without it — the exact rail `scraper/clip_tagger.py` already enforces for CLIP (it reads `revision` from the taxonomy file, refuses to embed if absent, and passes it to both `from_pretrained` calls). |
| **Resolution** | **MEASURED** (was locked at 512) | **v1's justification collapsed under review.** DINOv3's App. D.8 evaluates *"Oxford and Paris … larger side is 224 pixels … full center crop, yielding 224×224"* and AmsterTime at a 224×224 centre crop; only Met is evaluated near native resolution. **So Oxford-H 58.5/63.1 and AmsterTime 56.5 — my core job-(b) evidence — were produced at 224, not 512.** There is no published DINOv3 retrieval number at 512. The supporting citation was worse: [arXiv:2510.07191](https://arxiv.org/abs/2510.07191) is **chest radiographs** (816k grayscale medical images) under **full fine-tuning**, its 512 advantage is qualified *"especially with ConvNeXt-B"*, it concludes *"ConvNeXt-B remained superior to ViT-B/16"*, and its abstract explicitly warns the 512 benefit *"should not be interpreted simply as superior noise robustness"*. Bake-off arms: **224 / 256 / 512**. |
| **Precision** | **MEASURED** (was locked at bf16) | v1 quoted *"DINOv3 doesn't support FP16"* as a Meta author's statement. The speaker is labelled **CONTRIBUTOR**, not a maintainer, and the immediately preceding comment in the same thread says **bf16 also degrades quality** (*"FP16 directly causing NaN and BF16 causing poor results"*), with the reply asking for bf16-vs-fp32 numbers rather than confirming bf16 is safe ([issue #181](https://github.com/facebookresearch/dinov3/issues/181)). fp16 is out. **bf16 vs fp32 is an arm**, and fp32 is the fallback for the corpus pass if cosines move. |
| **Preprocessing** | **MEASURED** (was "one frozen transform", never chosen) | v1 declared the transform *"part of the vector's identity as surely as the weights are"* and then never picked one. Four defaults circulate (HF processor 224 square-squash bilinear; Meta README 256; paper probes; timm 256 bicubic). Portal photos are ~4:3, and **centre-cropping discards the left and right edges where portal watermarks live** — the exact evidence a near-duplicate check wants. Arms: **square-squash / shortest-side-resize + centre-crop / letterbox-pad**. |
| Pooling | **default CLS, one arm** | For *retrieval*, DINOv3's own protocol is CLS cosine (App. D.8), so CLS is the right default. **Correction:** v1 said there is "no published pooling ablation" — true of DINOv3's retrieval numbers, false as a claim about the family: DINOv2's linear-probe protocol grid-searches CLS vs CLS⊕average-pooled-patches and reports the max (App. B.3), which means option E's quoted classification numbers are pooling-sweep maxima. Adopt a non-CLS pooling only if it wins by a margin the operator judges worth the extra concept. |
| Storage | **locked** | `halfvec(768)`, new table, PK `(image_id, encoder_id)`, RLS + REVOKE posture replayed from migrations 237/447, created behind 226's pgvector-conditional `DO` block so CI replay still passes. ~17-18 GB, and see the disk-headroom gate in §5.0. |

**On where path B's k-NN runs.** v1 framed "run it off-database with FAISS, don't build a pgvector HNSW index" as a correction to the plan. **It is not a correction — the ledger already says this**: W3 specifies *"a batch k-NN job over per-type priority same-tag images … (off-DB, e.g. FAISS on a pod/runner; writes candidate pairs + best-similarity evidence into the sim store)"*. I was arguing against a design nobody held, and then booking the difference as a benefit of switching encoders. Withdrawn.

The sizing evidence I offered for it was also wrong on its face: Supabase's pgvector sizing page's largest documented configuration is **16XL / 256 GB**, not 4XL / 64 GB, and **the page carries no prices at all** — so "$960/mo" and "$410/mo" are from [compute & disk](https://supabase.com/docs/guides/platform/compute-and-disk), not from the page I cited for them. What the page *does* show, and this is the only thing it shows, is that **every tier from Micro to 16XL is benchmarked at 1,000,000 vectors** ([choosing-compute-addon](https://supabase.com/docs/guides/ai/choosing-compute-addon), verified 2026-09-05). It therefore offers no direct evidence about 10M vectors in either direction. The correct statement is the modest one: **in-database ANN at our corpus size is undocumented territory on a database that also serves the SPA, and the ledger already routes around it.**

Three things about the off-DB route that v1 left unpriced and that the operator should see:

- **Supabase egress.** The first k-NN pass is free (the vectors are already in pod memory from the backfill). Every *subsequent* pass must pull ~16 GB of vectors back out — and there will be several, because B's two search parameters are explicitly undecided and operator-tuned. Pro includes 250 GB/mo shared with SPA reads, $0.09/GB after ([egress](https://supabase.com/docs/guides/platform/manage-your-usage/egress)); `clip-linear-probe.md` already warns that *"egress ~40 GB/campaign shares the 250 GB quota with SPA reads and taxonomy retags — schedule campaigns apart."*
- **The box is not guaranteed.** `scripts/runpod_client.py`'s `eligible_gpus` ranks **by price only**, with no vCPU or system-RAM constraint, and our live runs record the requested GPU unavailable twice with a fallback to whatever was cheapest (RTX 3070). If the plan needs a 3090's 125 GB of system RAM and 16 vCPUs, the selector must constrain on those.
- **Output volume is unbounded.** `k` is undecided; at k=10 over ~10.4M images that is up to ~100M candidate rows before filtering, **on a disk that cannot shrink**. The row estimate and what prunes it belong in the W3 spec.

**Do not provision a RunPod network volume for weights** — but not for the reason v1 gave. My "20 GB weight cache is $1.40/mo" was invented and ~40× oversized: DINOv3 ViT-B/16 is 85.6M params ≈ 340 MB in fp32; all five bake-off checkpoints together are ~4.8 GB ≈ $0.34/mo at $0.07/GB-mo. The real reasons to bake weights into the container image are **reproducibility** (the image is the pinned artifact) and **the gated-weights problem** (an unattended pod cannot click through a licence).

### 3.3 The explicit "stick with the current plan" alternative

The plan as written in `PROGRAM.md` is: **train the probe on the stored CLIP vectors; use DINOv2 on RunPod, candidate-scoped, as the Level-3 decision signal; run path B's k-NN over the existing CLIP 512-d vectors because they are already stored and need no new backfill.**

That plan is coherent and it is the low-effort path. Three specific weaknesses, stated fairly:

- **Path B is the weakest link and it is load-bearing.** Its job is to *find* pairs by image similarity — the job at which CLIP is weakest on published matched-size evidence. The ledger's justification is explicitly economic (*"already stored — no new backfill"*, and elsewhere *"no ~35 GB corpus backfill returns"*), and that justification does not survive the cost model: the backfill it avoids costs single-digit dollars of GPU. It does *not* avoid the storage, though — and this is the half of the ledger's reasoning that **holds up**: a new store is ~18 GB of disk you can never give back. That is the real argument for the current plan, and v1 dismissed it by pretending the old table's space could be reclaimed.
- **It already contains a two-encoder architecture, just an implicit one.** CLIP for heads and path B, DINOv2 for L3 — that is option D with the weaker halves. The ledger anticipates the conflict: *"Audit C (W5) shows whether CLIP vectors suffice or B should upgrade to DINOv2 vectors."* The proposal here is to answer that question **before three waves are built on top of the answer**, rather than at W5 after path B's parameters, audits and calibrations have all been set in CLIP space.
- **"DINOv2 on RunPod" names no variant.** Size, resolution, pooling and pin are unspecified — and DINOv2 ViT-L *beats* ViT-g on six of the eight instance-retrieval metrics in Table 9 at ~3.5× less compute, so the intuitive "use the biggest one" would be the wrong call. Whatever is decided, the variant belongs in the ledger.

**When sticking is the right answer:** if the bake-off shows the new encoder's near-duplicate separation is not materially better than stored CLIP **on our own images** — a real possibility no published benchmark can settle for us — the correct move is to keep CLIP for path B (optionally swapping in the MIT-licensed LAION checkpoint to fix the licence for ~$1-2), keep DINOv2 candidate-scoped for L3 as planned, and spend the saved attention on W0's genuine debt and the labeling budget.

### 3.4 The licence review — what v1 made a decision conditional on without ever defining

§3.1 of v1 said "if the DINOv3 licence review fails, the fallback is already measured" and never said what the review is. Here it is, and note the ordering: **the corpus write is the one-way door, so the review gates the write, not the bake-off.**

| # | Question | Pass looks like |
|---|---|---|
| 1 | Are stored embeddings "outputs and results" (§3, §5.b) rather than "derivative works" (§1.b.i)? | A written position, taken once, recorded in the ledger. |
| 2 | Is the uncapped §5.b indemnity acceptable to the operating company? | An explicit yes, or a no that selects option E. |
| 3 | Is California exclusive jurisdiction (§7) against Meta Platforms Ireland acceptable? | Same. |
| 4 | Which of the two licence texts binds us, and by what acquisition route? | Gated route: click-through, archive the text + hash + date. Mirror route: **no click-through exists**, so archive the bundled text, its hash, its fetch date, and record which text the mirror's metadata points at. |
| 5 | §1.b.iii privacy/data-protection: is a GDPR position needed for a 10M-vector store built from photos containing identifiable people? | A position, even a short one. |
| 6 | Who monitors §8 amendments, and how often? | A named owner and a cadence — §8 changes are effective immediately and are not announced. |
| 7 | What is the runbook if the grant terminates or is amended unacceptably? | The vectors survive; new images cannot be embedded. Needs a trigger, a re-embed budget held in reserve, and a decision on whether option E is kept warm or rebuilt cold. |

**Ordering matters and v1 got it wrong in a second way too:** falling back from B to E after adoption is **not** "one meeting". They differ in dimensionality (768 vs 1024), patch multiple (16 vs 14), resolution, storage (~18 vs ~24 GB, both non-reclaimable) and token layout. A post-adoption fallback is a **second full re-embed, a second table, and a second invalidation of every calibrated number** — the exact migration this document argues you should pay once.

---

## 4. What changes in the program plan if the recommendation is taken

### 4.1 Ledger decisions that move

| Ledger entry (PROGRAM.md) | Today | Becomes |
|---|---|---|
| **Embeddings** | "DINOv2 on RunPod, **candidate-scoped** … **unchanged by path B** — B generates candidates from the existing CLIP vectors" | "**One encoder, corpus-wide store**: DINOv3 ViT-B/16, CLS, 768-d halfvec, resolution/precision/preprocessing set by the bake-off. L3 and path B read the same vectors. Candidate-scoping is no longer a cost control (a full pass is single-digit dollars) but remains the right scope for the *decision* tier." |
| **Candidate path B (2026-08-27)** | "using the **existing corpus-wide CLIP 512-d vectors** (already stored — no new backfill)" | "over the new encoder's vectors. The off-DB FAISS placement is **unchanged — it was already the plan**. New: an explicit **recall@k target** for the approximate index, validated against exact search on a sample, sitting upstream of the operator's two search parameters; plus a **bound on B's write volume** (rows at the chosen k) since disk cannot be reclaimed." |
| **Probe scope (2026-08-27)** | "v1 trains exactly the 11 tags…" | Unchanged in substance — but the bake-off reports **every head with its graded-n next to it** (see §5.4), which gives the operator numbers rather than principle for the 11-vs-18-vs-8 question. |
| **W5 audit C** | "also the evidence for whether path B should upgrade to DINOv2 vectors" | **Resolved early by the bake-off.** Audit C reverts to its primary purpose (does the embedding tier add measurable lift over pHash — Gate 5). |
| **RunPod** | "serverless/on-demand only, <$1/day run-rate" | Unchanged as a **RunPod run-rate cap** — and this is a correction: v1 called it "the entire program budget" and computed ratios against $30/mo. It is a run-rate cap in a ledger row whose subject is RunPod; the platform already pays Supabase, Railway, R2 and LLM spend outside it. Now quantified: one-off $1-12; steady state $0.04-0.11/day daily-batch or $0.30-0.50/day hourly; k-NN passes ~$0.20-0.50 each. Weights baked into the image, no network volume. |
| **New: embedding cadence** | — | "New images are embedded **[daily / hourly]**, chosen by the operator. Daily ≈ $0.05/day and up to 24 h of embedding latency; hourly ≈ $0.40/day and parity with today's `clip_tag.yml`. Today's lane is hourly." |
| **New: encoder identity** | — | "A vector's identity is **six** things: model, revision sha, library+pooling, resolution, preprocessing transform, dtype. All six recorded per row. Any of them changing means a new population, not a new value." |

### 4.2 What gets recomputed

Everything that resolves through `toolkit/tag_definitions.embedding_model()` is centroid-based and therefore *encoder-relative*. None of these numbers survive the change; all must be re-taken, and the operator should be told they are **re-taken**, not moved:

- **Outlier-first tag gallery ordering** (`_POSITIVE_IMAGES_OUTLIER_SQL`) — the "farthest from this tag's centroid" ranking read daily.
- **`nearest_tags` centroid-overlap evidence** — the diagnostic that decides whether two tags are really one tag. Any taxonomy decision taken on the old geometry needs re-confirming.
- **`tag_candidates` drawn queues** — *ranked* in the old space, so they need **re-drawing, not re-scoring**. Line ~65's cost comment doesn't just need new arithmetic: at 1,544 B the whole "heap fetch plus a TOAST fetch" rationale **inverts** (§2.1), so `POOL_IMAGES_TARGET` may be able to rise.
- **`--near-tag` targeted draws** (`toolkit/machine_labeling.py`) — the rare-head rescue lane; its concentration is entirely encoder-specific.
- **The `image_clip_tags` zero-shot lane and render/photo scores** (`scripts/retag_from_embeddings.py`, `scripts/backfill_render_score.py`) — derived from stored vectors, so they die with them unless deliberately kept.

**One item recorded rather than recomputed:** the sealed exam. `toolkit/tag_exam.py`'s eligible frame is literally *"an image carrying a vector from the pinned encoder"* — `SELECT count(*) FROM image_clip_embeddings WHERE model = %(model)s`. Re-embedding changes what "the population" meant when the exam was drawn. **The exam must not be re-run** — it is the yardstick every agreement number is quoted against. Good news the reviews surfaced: `tag_exam_cohorts` **already stores `model`, `revision` and `frame_size`** per cohort, so the record largely exists; what is needed is a ledger note saying the exam describes a CLIP-defined population, and the date.

### 4.3 The concrete migration surface — bigger than v1 said

32 files reference `image_clip_embeddings` (`git grep -l image_clip_embeddings origin/main`). The ~12 sites v1 listed are real, but the ones it missed carry the most risk, and "mechanical" is the wrong word for two of them.

**v1's list (still correct):** `data/clip_taxonomy.json` (model + revision + prompt anchors, which do **not** transfer) · `toolkit/tag_definitions.py` (`embedding_model()`/`embedding_revision()` — the good chokepoint; note callers pass only `model` into SQL, never `revision`) · `_POSITIVE_IMAGES_OUTLIER_SQL` + `_NEAREST_TAGS_SQL` · `toolkit/tag_candidates.py` (:65 byte math, `_COUNT_VERIFIED_POSITIVES_SQL`, `_DRAW_POOL_SQL`) · `toolkit/machine_labeling.py` `_NEAR_TAG_SQL` · `scraper/clip_tagger.py` + `scripts/clip_tag_backfill.py` · `toolkit/tag_exam.py`, `scripts/screen_exam_cohort.py`, `scripts/draw_exam_cohort.py` · `scripts/retag_from_embeddings.py`, `scripts/backfill_render_score.py` · `frontend/src/pages/NewDedupLabeling.tsx` + the Taxonomy workbench (which render centroid distance as operator-facing evidence and have **no notion of an encoder version to warn on**) · `tests/test_clip_encoder_pin.py` (extend the AST sweep to the new `from_pretrained` sites).

**Missing from v1, and load-bearing:**

- **The security posture.** Migrations **237** and **447** enable RLS and REVOKE the default anon/authenticated DML grants on the tag tables. 237 exists precisely because *"image_clip_embeddings — 512-d vectors — being anon-truncatable is the worst of the three"*. A new embeddings table created without replaying that posture **reproduces that exact incident**.
- **The CI-replay guard.** 226 creates the table inside a `DO` block gated on pgvector's availability, because the CI migration-replay image (postgis/postgis) has no pgvector. A plain `create table … halfvec(768)` **fails CI replay**.
- **The workflows.** `.github/workflows/clip_tag.yml` and `clip_retag.yml` become GPU lanes, which means `frontend/public/workflow-docs.json` + the `workflowDocs.generated.ts` codegen must be regenerated **or CI fails**.
- **The same-PR documentation rule** (CLAUDE.md): `docs/architecture.md`, `.claude/skills/scraper-ops/SKILL.md`, `roadmap/new-dedup.md`.
- **Two dependency approvals under rule 7** — see §4.5.

### 4.4 What can be dropped, and when — with the storage truth attached

| Object | Size | Drop when |
|---|---|---|
| `image_clip_embeddings` (~10.36M × 512-d) | **~23-25 GB** | **Not before W5.** It is path B's current substrate and the bake-off's baseline. Drop after (i) heads trained and validated on the new vectors, (ii) path B runs on the new vectors, (iii) W5 audit C recorded. **Correction: dropping it does not return the disk.** Supabase disk cannot be shrunk; reclaiming allocation needs a Postgres version upgrade with downtime. The billing consequence of the drop is **zero**; what you get back is free space *inside* the existing allocation. v1's "the storage delta of the whole change goes negative" is withdrawn. |
| `image_clip_tags` zero-shot labels + the render/photo axis | — | When the trained heads replace them on every surface. Watch the hazard already documented in `clip-linear-probe.md`: six merge-gate queries and two API readers read `image_clip_tags` with **no model filter**, and `images_public` resolves latest-`tagged_at`-wins. Decide the write-time composition policy **before** the first new row is written. |
| `scraper/label_proposal_tagger.py` + `scripts/label_proposal_backfill.py` + `dedup_sim.label_proposals` + the `labeling_secondary_model` setting | small | Already superseded by LLM labeling; per the program review it is the only reason `dedup_sim` cannot be dropped, so retiring it unblocks W8's schema cleanup. |
| The `clip` extra's `transformers>=4.40,<5` ceiling | — | Replaced by whatever the new encoder needs (DINOv3 ≥4.56.0; DINOv2-with-registers ≥4.48.0). **This is now the only reason to move the ceiling** — the "transformers 5 forces us anyway" claim is withdrawn (§2.2). |

**Unchanged by any of this:** W0's genuine outstanding debt (the CUTOFF §4 drops, the `property_merge_events.generation` legacy stamp, the `app_settings` sweep, the never-recorded §7 step-8 verification checklist). Encoder-independent; can run in parallel.

### 4.5 Two dependency approvals the plan silently assumed

CLAUDE.md rule 7: *"No new dependencies without justification."* The project's core dependencies are requests / psycopg / boto3 / selectolax / Pillow / pyyaml — with Pillow annotated **"Pillow-only — no numpy/scipy"** — and torch+transformers live only in the `clip` extra installed by the tagging workflows.

- **scikit-learn (or numpy)** to fit the heads. v1 called the heads "logistic regressions over cached vectors — retraining takes minutes and costs nothing". True of compute, false of the decision gate: **there is no approved library to fit them in**, and `clip-linear-probe.md`'s open question 3 — *"OK to add scikit-learn as a training-only extra (never in runtime images)?"* — has been open and unanswered since July. **The bake-off cannot produce its Set-1 table until this is granted.**
- **faiss** for the off-DB k-NN. Not in `pyproject.toml`, no approval on file.

Both are training/analysis-only and would never enter the scraper or API images. Both need a yes.

---

## 5. The bake-off — decisive, ~$2 of GPU and about a day of harness work

### 5.0 Pre-flight: the ops readout, before any number in this document is quoted to anyone

v1 said "do this first, it costs nothing" about **one** query, and then never ran it — while quoting corpus-derived figures to two significant figures throughout. Run these five, in one sitting, before anything else:

```sql
-- 1. Is the store one coherent population, and how big is it really?
select model, revision, count(*) from image_clip_embeddings group by 1,2;

-- 2. What does it actually occupy, TOAST included?
select pg_size_pretty(pg_total_relation_size('image_clip_embeddings'));

-- 3. How many images are there to embed at all?
select count(*) from images where storage_path is not null;

-- 4. How much document-like content is there? (the P4 denominator;
--    image_clip_tags argmax is anchor-biased, so read it as an order of magnitude)
select tag, count(*) from image_clip_tags group by 1 order by 2 desc;
```

Plus, from the Supabase dashboard rather than SQL: **current provisioned disk size and current utilisation %.**

That last one is not bookkeeping. Supabase gp3 disk **auto-expands at 90% of allocated disk** (by +50%, capped +200 GB), auto-scaling is limited to **4 modifications per rolling 24 hours**, and **at 95% utilisation with the quota exhausted the project enters read-only mode** ([database size](https://supabase.com/docs/guides/platform/database-size), [compute & disk](https://supabase.com/docs/guides/platform/compute-and-disk)). Read-only takes down the scrapers, the API's writes, the SPA's writes and the pipeline — not just this program. Writing ~18 GB of new vectors at whatever rate a GPU pod emits them, **while a ~25 GB legacy table is still resident**, is exactly the shape that trips it. The backfill therefore needs a write-rate throttle and a checkpoint/abort plan (§5.5), and those need today's headroom number to be designed against.

### 5.1 What already exists — and what honestly does not

The harness was built once. `git show 74bf82b2:scripts/embedding_gpu_bench.py` is a standalone pod-side bench (no repo imports, no credentials, deps torch/transformers/pillow/requests) that measures pos/neg percentiles, separation, ROC-AUC, recall at ≥100%/99%/95% precision, per-category breakdowns, and throughput + $/1M images.

**Reusable as-is:** `hamming64`, the percentile/AUC/recall-at-precision math, the $/1M accounting, and the *idea* of an encoder-independent shared-photo exclusion — which exists because the previous A/B was confounded by exactly that: *"same-development negatives sharing literal marketing renders sit at cosine 1.0 under ANY encoder."*

**v1 said "reusable as-is" and that was wrong.** Six concrete gaps, all verified in the file:

1. **No bf16 path.** `dtype = torch.float16 if (fp16 and device == "cuda") else None` — the only alternative is fp32. The recommended encoder would either NaN or run at fp32.
2. **No resolution control.** `AutoImageProcessor.from_pretrained(model_id)` with no size override — it uses the checkpoint default (224 square-squash for DINOv3). The resolution arm cannot be run.
3. **Pooling is hardcoded** `res.last_hidden_state[:, 0]` — which for SigLIP2 is **the top-left patch, not a summary**, so the tag-side control would be silently mis-embedded. Per-encoder pooling is required.
4. **The unit of analysis is a labelled *listing pair*, not an image pair.** `labels = {p["pair_id"]: p["is_same"] …}`; `score_pairs` returns per-pair max cosine; every metric consumes listing-pair labels. §5.2's populations are **image-pair** populations, so labelling, aggregation and metrics all get rewritten.
5. **The exclusion is not fully encoder-independent.** `is_shared_photo` also fires on stored `clip_cos >= 0.999`, and `--rmin 0.95` filters on the CLIP-derived `render_score` — so the incumbent's opinion decides which pairs survive. Report both with and without the CLIP-derived limbs.
6. **It downloads every image to local disk before embedding**, and decode is effectively single-threaded (one chunk submitted as one pool task, `proc(images=…)` on the main thread) — fine for 20k, structurally wrong for a 10.4M-image production pass, and it cannot exploit the 9 or 16 vCPUs §5.3 picks the box for.

Its companion `scripts/build_embedding_manifest.py` **must be rewritten**: it reads `dedup_label_events`, a legacy table on the CUTOFF §4 drop list. Nothing else from that era is reused — this is a measurement script, not any part of the removed decision engine.

**So the honest framing is: ~$2 of GPU, and about a day of harness work.** v1's "uses assets that already exist" set the wrong expectation.

### 5.2 The two evaluation sets

**Set 1 — tag heads (job a).** Train on the machine-labeled + human-labeled image sets (each written against the active written definition and stamped with `definition_id` + `model`); test on **`exam_v1`**, the sealed 250-image human-answered holdout. Fit one logistic regression per head on the frozen embeddings, and **grade exactly the cells `scripts/exam_agreement.py` already grades** — *"A cell grades only when both sides said yes or no. A 'left out' on either side is an abstention"*, and *"a declared default is not a judgment"*, so migration 466's untouched `backfill:466` cells abstain.

**What v1 promised and could not deliver.** exam_v1 is **250 images across 18 tags**, and migration 466 backfilled the ten added tags as **declared negatives** across all 250, leaving only the **original routing eight** fully human-answered. So "report all 18 heads" will deliver eight real rows and ten thin ones. Worse, the repo already did the statistics on this and v1 ignored it: `clip-linear-probe.md` D4.3 — *"at n=50 a 0.95 floor is uncertifiable even at zero errors; n≈300 at observed ~0.98 puts the lower bound comfortably over 0.95"* — plus a call for *"a ~50-100 double-labeled slice to bound single-annotator label noise … a 0.95 floor sits within one noise-width of the ceiling if operator error is ~2-3%."*

**Therefore, stated plainly:** exam_v1 can **rank encoders** on the routing eight. It **cannot certify** any head, and a 2-3 point per-head difference is inside single-annotator noise. Every per-head number is reported with its **graded n** beside it — which the existing tool already prints (`graded`, `abst`, `deflt` columns) — and a head below a graded-n the operator names is reported as "not measurable here", not as a number.

**Set 2 — near-duplicate separation (job b).** Five populations, all defined by rules independent of any encoder.

| Population | Definition | Role |
|---|---|---|
| **P1a — same photo, pHash catches it** | `images.phash` Hamming ≤2 between images of **different listings** | The **control**, not the headline. These are the reposts the cheap incumbent signal already finds. |
| **P1b — same photo, pHash MISSES it** ⭐ | **Synthetic transforms applied pod-side to real corpus images**: crop 10%, resize, re-JPEG at q60, watermark overlay in the left/right band, letterbox pad, and combinations | **The population the embedding tier exists for.** Encoder-independent by construction, cheap, and it measures cosine(x, T(x)) directly — the exact quantity §6 says is missing from the literature and that I wrongly tried to substitute ImageNet-C for. |
| **P2 — same property, different photo** | Two images of the **same `sreality_id`**, same tag | Hard negatives for *photo* identity. Will legitimately contain burst shots; inspect, don't assume. |
| **P3 — different property, same tag** | Two images with the same tag, from listings in **different obce** | **The modal failure — and, per §1, path B's entire search space.** Separation between P1 and P3 is the most decision-relevant readout in the exercise. |
| **P4 — document tags, inspection only** | Document-tagged images from different listings | **Not a scored positive set.** `toolkit/tag_candidates.py` records that *"dHash collapses distinct floor plans (mostly-white documents hash alike)"* — which is why it rejects at Hamming <6 rather than the engine's 11. So a Hamming-≤2 pair of floor plans is **not** evidence of a repost; it may be two different units in the same building. Document positives come **only** from P1b's synthetic arm; the pHash-defined document pairs are an eyeball set. |

**Two fixes v1 needed badly.**

1. **As specified, v1's bake-off would have produced zero positives.** Its P1 was "pHash Hamming 0-2 across listings", and it then instructed "apply the harness's shared-photo exclusion" — whose predicate is `hamming64(pa, pb) <= hamming_max` with **`--hamming-max` defaulting to 2**. The positive set and the contamination filter were the same rule. Fixed: the exclusion's Hamming parameter is set **independently** of P1a's definition, it is applied to the P3 side to strip literal shared renders, and its CLIP-derived limbs (`clip_cos ≥ 0.999`, `--rmin` render_score) are reported both on and off.
2. **P1a is not the measurement §6 asks for.** Being pHash-defined, it contains only reposts pHash already catches and **excludes every crop, watermark, resize and re-encode that breaks pHash** — the very population the embedding tier is meant to recover, and the one Gate 5 ("measurable lift over pHash") is about. P1b is that measurement.

### 5.3 How to run it

- **Manifest** (in GitHub Actions, where the secrets live): rewrite the builder to draw the five populations plus Set-1 image ids from `image_tag_labels` / `tag_taxonomy` / `images`, emit **presigned R2 URLs** (7-day expiry) so the pod needs no credentials, carry each image's stored CLIP vector along so the incumbent is scored on byte-identical pairs at zero GPU cost, and carry the **synthetic-transform recipes** so the transforms are applied pod-side rather than re-uploaded. Target ~20,000 distinct images.
- **Pod:** an **RTX 3090, $0.22/hr (16 vCPU, 125 GB RAM)** or an **RTX A5000, $0.16/hr (9 vCPU)**. Avoid the 4090: fastest card, **fewest vCPUs (6)**, and JPEG decode is CPU-side ([RunPod pricing](https://www.runpod.io/pricing), verified). Launch through `scripts/runpod_client.py`, whose teardown lives in a `finally` — and **have the job self-report into Postgres/R2**: our live runs show `desiredStatus` never left `RUNNING` and the logs endpoint 400'd, so pod status is not a completion signal. Constrain the GPU selector on vCPU/RAM, not price alone, and check the account balance first (a live dispatch failed on *"Your account balance is too low to rent a pod"*).
- **Arms in one run:**
  - stored CLIP B/32 (baseline, no GPU) · **LAION CLIP B/32** (the fair CLIP baseline — is it the family or the checkpoint?) · **DINOv3 ViT-B/16** · **DINOv3 ViT-L/16** · **DINOv2 ViT-L/14-reg** · **SigLIP2-B/16** (tag-side control)
  - **resolution** for the leading DINO arm: 224 / 256 / 512
  - **precision**: bf16 vs fp32 on a fixed sample — report max cosine drift and whether any pair's ordering changes
  - **preprocessing**: square-squash / shortest-side + centre-crop / letterbox-pad, scored specifically on P1b's watermark transforms (this is where centre-cropping should hurt)
  - **weights canary**: `facebook/dinov3-vitb16-…` vs `timm/vit_base_patch16_dinov3.lvd1689m` on a fixed handful of images, asserting cosines match to 6 decimals — the pattern `clip-linear-probe.md` D1 already specifies. **This is not optional:** timm's config declares `"global_pool": "avg"`, so a careless mirror swap silently produces a second, incomparable population.
  - optionally a pooling variant (CLS vs CLS⊕patch-mean) on the same forward passes
- **A measured end-to-end throughput number is a deliverable, not a by-product.** Report img/s **including JPEG decode and preprocessing**, on the actual candidate box, at each resolution. It is a 30-minute measurement that decides whether the corpus pass is $3 or $12 and whether the daily lane is 3 minutes or 25.
- **Cost:** ~20k images × ~10 configurations, at a few hundred img/s effective, is well under two hours of pod time. **≈$0.30-1.00 of GPU; call it under $2 with a failed attempt and a re-run.** The manifest build in Actions is free.
- **On R2 free operations:** v1 asserted "a 9.6M-image pass is $0 in object-store fees". The 10M Class B free tier is **per account and already being consumed by production** — listing photos reach the SPA and the extension through the API's `GET /images/{storage_path}` presigned redirect (`frontend/src/lib/imageUrl.ts`), and every one is a `GetObject`. A full corpus pass at 10.36M exhausts the tier by itself. The overage is small ($0.36/M) but **"$0" is unproven**; measure production Class B volume before repeating it. The bake-off's 20k GETs are noise either way.

### 5.4 What the operator looks at — pass criteria are theirs to set

No thresholds are proposed here. Seven readouts, in the order to read them:

1. **P1b (pHash-breaking same-photo) against P3 (different property, same tag), per encoder** — overlapping histograms plus ROC-AUC and recall at the precision points the operator names. *Does the encoder put "the same photograph, cropped and re-compressed and watermarked" clearly to one side of "two different kitchens", and by how much more than stored CLIP?* **This is the headline.**
2. **P1a as the pHash control.** How much of P1b does pHash already catch? That is Gate 5's "lift over pHash" in one picture.
3. **The twenty worst P3 pairs under each encoder, side by side.** If the highest-scoring "different property" pairs are on inspection the same property, the negatives were mislabelled and the number is pessimistic. If they are genuinely different flats, the encoder is confusing them and the number is honest.
4. **The per-head precision/recall table, new encoder vs stored CLIP, on exam_v1's graded cells — with graded n in every row.** Read per head. The decision-relevant question is not "which encoder is better on average" but "**does any head get materially worse, is it one I care about, and is the difference bigger than the noise floor?**"
5. **The semantic-head panel** (`other`, `exterior_facade`, `balcony_terrace`, `bedroom`, `hallway`) **read on its own** — corrected from v1, which pointed this at the document heads. This is where DINO is expected to be weakest and where the §2.6 escalation ladder applies.
6. **The knob arms**: resolution, precision drift, preprocessing on the watermark transforms, and the timm-vs-gated canary. Adopt a non-default only where it wins by a margin the operator judges worth the complexity — and remember that whatever wins becomes **part of the vector's permanent identity**.
7. **Measured end-to-end throughput and $/1M per encoder.** Confirms the cost model against reality rather than against a synthetic-tensor benchmark.

**Decision shape after the bake-off:** if the new encoder clearly separates P1b from P3 where CLIP does not, *and* no head the operator cares about regresses beyond the noise floor → adopt B (or E on licence grounds). If separation is not materially better on our own images → stick with the current plan (§3.3), optionally swapping in the MIT LAION checkpoint. If only specific heads regress → adopt B and add the narrow second store from §2.6, later, as a classification-only addition.

### 5.5 The backfill execution plan — absent from v1 entirely

A 10.4M-image pass is a 3-25 hour job that writes ~18 GB into a live production database. It needs, before it starts:

- **Checkpoint/resume** keyed on image id, so a pod dying at 60% costs minutes, not the whole pass. (The existing harness downloads every image to disk first — structurally impossible here; the production pass must **stream**.)
- **A write-rate throttle** sized against today's disk headroom (§5.0), so the pass never walks the database toward the 90% autoscale trigger or the 95% read-only wall.
- **A statement of what reads the old vectors during the window** — the drawn queues, the outlier ordering and `nearest_tags` all keep working off CLIP until the cutover, and that is fine, but it should be a decision rather than an accident.
- **A GPU capacity and balance policy** for an unattended lane: our live runs hit "no capacity" twice and an empty balance once, and the selector picks by price only.
- **A rollback cost, stated up front.** Because disk cannot shrink and both populations would be resident, reverting to CLIP after adoption means **paying for both stores indefinitely**. That number belongs next to the recommendation, not implied by it.

---

## 6. Risks and unknowns — stated honestly

**Evidence gaps that no amount of reading closes.**

- **There is no published copy-detection number for DINOv3.** No Copydays, no DISC21/ISC — in the paper or the third-party literature. Oxford/Paris/AmsterTime measure *instance recognition* (same object, different photograph), which maps onto "same room, different angle" but is a **different axis** from "same photo, re-encoded and watermarked". **v1 filled that gap with ImageNet-C robustness; I withdraw that** — mCE is the corruption error of a linear classifier, which can stay accurate while the embedding moves, and the number I quoted was 7B-vs-g, not ViT-B. **P1b's synthetic-transform arm is the replacement, and it measures the right thing directly.**
- **There is no same-protocol DINO-vs-CLIP copy-detection head-to-head.** Treat the *ordering* as solid (same-paper, matched-size Oxford-Hard: DINOv3-B 58.5 / DINOv2-B 51.0 / SigLIP2-B 20.2) and any *magnitude* as indicative.
- **Met GAP is not a raw-cosine readout.** DINOv3 App. D.8: for Met the protocol tunes k and τ by grid search on Met's validation set **and whitens the features using a PCA estimated on Met's training set**. Our pipeline has neither. Every Met number in this document is discounted accordingly.
- **DINOv3's retrieval numbers are at 224 px**, not 512 (App. D.8), so the resolution choice has no published retrieval evidence behind it in either direction. Hence the arm.
- **No published DINOv3 pooling ablation for retrieval**, and no few-shot/k-NN table for the distilled models. (DINOv2 *does* publish a pooling sweep for linear probing, which is why option E's classification numbers are sweep maxima.)
- **No public benchmark measures per-tag binary heads on real-estate photos.** SUN397 and Places205 are 397-way and 205-way proxies, not 18 binary ones.
- **DINOv3 is not uniformly better than DINOv2.** A user reports frozen DINOv3-base performing *worse* than frozen DINOv2-base on their dataset ([issue #181](https://github.com/facebookresearch/dinov3/issues/181)); the radiograph study found no advantage at 224. Per-domain variance is real.

**Operational hazards, each with a known mitigation.**

- **Precision is not settled.** fp16 is out (NaNs). bf16 is *reported* to degrade quality by the same thread that condemns fp16, and the "Meta author" who said DINOv3 doesn't support fp16 is labelled CONTRIBUTOR, not a maintainer. → measure bf16 vs fp32; fp32 is the fallback for the corpus pass.
- **Two libraries, two different embeddings from the same weights.** HuggingFace's DINOv3 `pooler_output` is the post-LayerNorm CLS token; **timm's config for the mirror declares `"global_pool": "avg"`**. Prototyping in one and productionising in the other silently produces two incomparable populations, with no error and no shape change. → the canary in §5.3, plus library+pooling in the recorded identity.
- **Register tokens are in the sequence.** DINOv3 ships 4 on every checkpoint (`[CLS, reg×4, patches…]`); any patch-mean must slice past them. HuggingFace shipped exactly this bug for DINOv2-with-registers. Irrelevant if we stay on CLS — one more argument for CLS.
- **Four different "default" resolutions circulate** (HF processor 224 square-squash bilinear, Meta README 256, paper probes, timm 256 bicubic). Whatever we pick becomes part of the vector's identity.
- **`model.eval()` is mandatory.** `pos_embed_shift`/`jitter`/`rescale` are applied only in training mode; leaving the module in train mode makes embeddings non-deterministic.
- **Gated weights break unattended pipelines.** `facebook/dinov3-*` is `gated: manual`; even `config.json` 401s. Mirror once into R2 or use the ungated timm mirror — **and see §2.8 for why that route makes the licence record worse, not better.** Do not rely on Meta's `dl.fbaipublicfiles` URLs. The mirror is a third-party redistribution that HF or Meta can remove at any time → keep our own copy with a recorded sha256.
- **Do not touch the SAT-493M satellite checkpoints.** Different normalization statistics; mixing families silently produces garbage.

**Programme-level risks.**

- **A stronger instance-retrieval encoder makes path B's already-recorded failure mode worse, not better.** `PROGRAM.md`'s parked items say it plainly: *"B has no location anchor, so identical marketing photos (developer catalogs, staged/stock interiors, reused renders across a project's units) can become candidate pairs and would merge at L2 under the operator's current rules."* An encoder that is better at "same photograph" will score those pairs **more** confidently, not less. v1 never mentioned this. No mechanism is proposed here (the operator owns all merge logic) — it is flagged because **the recommended change increases exposure on a risk the program has already written down**, and the W3/W4 audits should be read with that in mind.
- **Every calibrated number is a function of six things**, not one. Store model + revision + library/pooling + resolution + preprocessing + dtype next to every vector, so "were these two vectors made by the same pipeline?" is a `SELECT` rather than an act of faith. **This is the lesson migration 456 already paid for once.**
- **Disk is a one-way ratchet.** ~18 GB written is ~18 GB allocated forever; the transition peak (~43 GB) is what you pay steady-state; a rollback means paying for both populations indefinitely. Design the write rate against today's headroom, not against the final size.
- **Path B's approximate-search recall is an unsurfaced parameter.** FAISS's guideline for 1M-10M vectors is `IVF65536_HNSW32`, and an approximate index has its own recall knob sitting **upstream** of the operator's two search parameters. Set a recall@k target and validate approximate vs exact on a sample; state what a missed neighbour costs.
- **Some image populations are near-identical by nature and no encoder fixes it:** floor plans of different units in the same building; cadastral maps sharing a base tile; aerial base imagery shared between neighbouring properties. This is a *data-scoping* decision, and the tag set is the natural scoping key — itself an argument for one pipeline serving both jobs.
- **The DINOv3 licence can change without our signature and continued use is acceptance** (§8). Archiving the accepted text is evidence, not protection. The termination shape (§2.8) is a **hard ingest stop**, not a redistribution question.
- **Evidence currency is a recurring obligation, not a one-off.** This document's licence reading is of the Aug-2025 texts; §8 lets them change silently. As of 2026-09: no DINOv4 exists, the repo is not archived (pushed 2026-07-15), and both texts still bear their Aug-2025 dates — but nothing makes that true next quarter.
- **This decision is upstream of, but not blocked by, Gate 1.** The bake-off can run today regardless of whether Gate 1 means 11, 18 or 8 tags — and its per-head table (with graded n) is the best evidence the operator will have when ruling on those questions.

---

## 7. What I am confident about / what I am not

**Confident.**

1. **The asymmetry argument.** A tag head is cheap and reversible; a similarity store plus its calibrations plus non-reclaimable disk is expensive and sticky. The encoder should be chosen on job (b)'s terms. No review touched this and I would defend it unchanged.
2. **The matched-size retrieval evidence.** One table, one protocol, per size: DINOv3-B 58.5 vs DINOv2-B 51.0 vs SigLIP2-B 20.2 on Oxford-Hard. This is the load-bearing fact, it survived three adversarial passes verbatim, and it says text-supervised encoders collapse on instance retrieval at every size tested.
3. **That the switching cost is largely sunk.** Migration 226 says in terms that its table was built for pairwise cosine and *"never a global nearest-neighbour search"*. Path B needs a new store and a new access pattern whichever encoder wins.
4. **That cost should not decide this.** Even at the corrected corpus size, on the slower box, with a decode penalty, a full pass is single-digit to low-double-digit dollars. The precision of v1's figures was wrong; the conclusion was not.
5. **The direction of the licence comparison.** The incumbent has no grant and a model card that puts any deployed use out of scope; Apache-2.0 (C and E) has no indemnity, no forum clause, no termination-with-delete and no unilateral amendment. DINOv3 sits between them and closer to the second.
6. **That the bake-off as v1 specified it was broken** — the positive set and the contamination filter were the same rule, and the positives it did define were the ones pHash already catches.

**Not confident.**

1. **The corpus size.** 10.36M is the repo's number in three places, but I did not run the query, and one repo document says 8,266,118 as of 2026-07-22. **Every money and GB figure here is provisional until §5.0 runs.** I quoted numbers to two significant figures in v1 off a base I invented; I will not do it twice.
2. **All throughput figures.** They are GPU-only synthetic-tensor benchmarks used as end-to-end rates, which is internally inconsistent with the (correct) claim that JPEG decode is the binding constraint. The bands are wide on purpose. One measured number replaces all of them.
3. **Whether DINOv3-B actually beats the alternatives on *our* images.** Everything above is benchmark transfer. The published gap between DINOv3-B and DINOv2-L is ~3 Oxford-Hard points — smaller than the licence difference between them is large.
4. **Resolution, precision, preprocessing and pooling.** Four knobs, four arms, zero published evidence at the settings we would actually deploy. Any of them could move cosines more than the encoder choice does.
5. **Whether stored embeddings are "outputs" or "derivative works"** under the DINOv3 licence. The text distinguishes them and the favourable reading looks right, but nobody has taken that position on the record, and it is the clause that governs every future option involving a third party.
6. **Whether exam_v1 can answer the per-head question at all.** 250 images, 18 tags, only eight fully human-answered, single annotator, no measured noise floor. It can rank encoders. It cannot certify a head, and I should not have implied otherwise.
7. **What path B's write volume and repeat-pass egress actually come to.** `k` is undecided; at k=10 over 10.4M images the candidate table could be ~100M rows before filtering, on disk that cannot be reclaimed.
8. **Whether the "$0 in R2 fees" claim holds.** Production already consumes the 10M/month Class B free tier through the SPA and extension; nobody has measured how much.

---

### Sources cited in this document

**Papers / benchmarks:** [DINOv3 arXiv:2508.10104](https://arxiv.org/html/2508.10104v1) (Tables 7/14/22/23/25, App. D.7 probe protocol, App. D.8 retrieval protocol) · [DINOv2 arXiv:2304.07193](https://ar5iv.labs.arxiv.org/html/2304.07193) (Tables 7/8/9, App. B.3 pooling sweep) · [SigLIP 2 arXiv:2502.14786](https://arxiv.org/html/2502.14786v1) (Tables 2/8) · [AnyPattern arXiv:2404.13788](https://arxiv.org/html/2404.13788v1) · [MMVP arXiv:2401.06209](https://arxiv.org/html/2401.06209v1) · [OpenCLIP scaling laws arXiv:2212.07143](https://ar5iv.labs.arxiv.org/html/2212.07143) · [SSCD arXiv:2202.10261](https://arxiv.org/abs/2202.10261) · [ISC2021 arXiv:2202.04007](https://arxiv.org/abs/2202.04007) · [Registers arXiv:2309.16588](https://arxiv.org/abs/2309.16588) · [Cone effect arXiv:2203.02053](https://arxiv.org/abs/2203.02053) · [ILIAS](https://vrg.fel.cvut.cz/ilias/) · [Resolution scaling in chest radiographs arXiv:2510.07191](https://arxiv.org/abs/2510.07191) *(cited now only to withdraw v1's use of it)*

**Model / licence / tooling:** [DINOv3 licence, Meta copy, 2025-08-14](https://ai.meta.com/resources/models-and-libraries/dinov3-license/) · [DINOv3 LICENSE.md, repo copy, 2025-08-19](https://raw.githubusercontent.com/facebookresearch/dinov3/main/LICENSE.md) · [DINOv3 issue #181 (precision)](https://github.com/facebookresearch/dinov3/issues/181) · [issue #332 (gating)](https://github.com/facebookresearch/dinov3/issues/332) · [DINOv2 repo/licence](https://github.com/facebookresearch/dinov2) · [transformers DINOv3 docs](https://huggingface.co/docs/transformers/main/en/model_doc/dinov3) · [transformers #37817 (register slicing)](https://github.com/huggingface/transformers/issues/37817) · [transformers v5 notes](https://github.com/huggingface/blog/blob/main/transformers-v5.md) · [timm 4090 benchmark CSV](https://raw.githubusercontent.com/huggingface/pytorch-image-models/main/results/benchmark-infer-amp-nchw-pt291-cu128-4090.csv) *(note: the 3090 file v1 cited 404s; the newest is `benchmark-infer-amp-nchw-pt240-cu124-rtx3090.csv`)* · [openai/clip-vit-base-patch32](https://huggingface.co/openai/clip-vit-base-patch32) · [openai/CLIP LICENSE](https://github.com/openai/CLIP/blob/main/LICENSE) · [laion/CLIP-ViT-B-32-laion2B-s34B-b79K](https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K) · [google/siglip2-so400m-patch16-256](https://huggingface.co/google/siglip2-so400m-patch16-256) · [facebook/dinov2-with-registers-large](https://huggingface.co/facebook/dinov2-with-registers-large) · [timm/vit_base_patch16_dinov3.lvd1689m](https://huggingface.co/timm/vit_base_patch16_dinov3.lvd1689m)

**Infrastructure pricing:** [RunPod pricing](https://www.runpod.io/pricing) · [pgvector README](https://github.com/pgvector/pgvector) · [Supabase compute sizing for pgvector](https://supabase.com/docs/guides/ai/choosing-compute-addon) *(verified 2026-09-05: tiers Micro→16XL, all benchmarked at 1,000,000 vectors, no prices on the page)* · [Supabase compute & disk](https://supabase.com/docs/guides/platform/compute-and-disk) · [Supabase database size / disk autoscaling](https://supabase.com/docs/guides/platform/database-size) · [Supabase egress](https://supabase.com/docs/guides/platform/manage-your-usage/egress) · [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/) *(verified 2026-09-05: Class B 10M/mo free, $0.36/M after; egress free)* · [FAISS index guidelines](https://github.com/facebookresearch/faiss/wiki/Guidelines-to-choose-an-index)

**Repo files (absolute paths; read from `origin/main` unless noted):** `/home/hejtm/dev/sreality/migrations/226_clip_engine_schema.sql` · `/home/hejtm/dev/sreality/migrations/237_lock_down_image_tag_tables.sql` · `/home/hejtm/dev/sreality/migrations/447_tag_tables_revoke_default_grants.sql` · `/home/hejtm/dev/sreality/migrations/456_clip_embeddings_revision.sql` · `/home/hejtm/dev/sreality/migrations/466_one_set_of_18.sql` · `/home/hejtm/dev/sreality/data/clip_taxonomy.json` · `/home/hejtm/dev/sreality/scraper/clip_tagger.py` · `/home/hejtm/dev/sreality/scraper/image_storage.py` · `/home/hejtm/dev/sreality/toolkit/tag_definitions.py` · `/home/hejtm/dev/sreality/toolkit/tag_candidates.py` · `/home/hejtm/dev/sreality/toolkit/tag_exam.py` · `/home/hejtm/dev/sreality/toolkit/machine_labeling.py` · `/home/hejtm/dev/sreality/scripts/exam_agreement.py` · `/home/hejtm/dev/sreality/scripts/draw_exam_cohort.py` · `/home/hejtm/dev/sreality/scripts/runpod_client.py` · `/home/hejtm/dev/sreality/tests/test_clip_encoder_pin.py` · `/home/hejtm/dev/sreality/pyproject.toml` · `/home/hejtm/dev/sreality/.github/workflows/clip_tag.yml` · `/home/hejtm/dev/sreality/frontend/src/lib/imageUrl.ts` · `/home/hejtm/dev/sreality/docs/design/new-dedup/PROGRAM.md` · `/home/hejtm/dev/sreality/docs/design/clip-linear-probe.md` · `scripts/embedding_gpu_bench.py` and `scripts/build_embedding_manifest.py` at commit `74bf82b2` (deleted from `main`; retrieve with `git show 74bf82b2:<path>`)