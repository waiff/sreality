# Self-hosted CLIP visual embeddings + tagging — build spec

Status: **proposed / not built**. This is a build plan handed off for another session to
execute. It was produced from a grounded research pass (current pipeline + production DB
numbers + GitHub Actions 2026 limits + CLIP CPU footprint), 2026-06-23.

The goal: run our **own** CLIP model (no per-call LLM fee) over listing photos to produce
(a) a per-image **zero-shot type tag** and (b) a **512-dim embedding**, to feed dedup
**candidate generation** upstream of the existing LLM-vision forensic compare — and to
cheapen / replace the per-image `classify_listing_images` spend.

CLIP is a **recall funnel**, not a replacement for the reasoning layer. It proposes
candidate pairs and coarse tags; the LLM-vision tools keep the precision decisions
(`compare_listings_visually`, `compare_listing_site_plans`, the "same physical unit?"
verdict). Do **not** try to make CLIP decide merges.

---

## 0. Scope decision (read first — the "1M / 5M" framing was imprecise)

Production DB query results (project `erlvtprrmrylhznfyaih`, 2026-06-23):

| Set | Images | Definition |
|---|---|---|
| Currently-**queued** dedup candidates | **~260k** | images under listings in `property_identity_candidates` / `listing_visual_matches` (13,108 pairs, 0 pending) |
| Dedup-**eligible** active listings | **~870k–1M** | 62,201 active listings with street+disposition × ~14.1 imgs |
| **Full stored corpus** | **~5.05M** | `images.storage_path IS NOT NULL` (5,055,369 of 5,130,954 rows) |
| Steady-state inflow | **~46k/day** (~1,900/hr, ~0.53 img/s) | ~322,893 images over the last 7 days; ~14.1 imgs/listing |

**The "~1M" is the eligible-active subset, not the queued pairs (which are only ~260k).**

Recommended scope ladder: **start at 260k queued → then ~1M eligible → defer the full 5M**
until dedup proves it needs corpus-wide ANN *and* the storage question (§4) is sized.

---

## 1. Where this runs: GitHub Actions, not Railway

All bulk visual work already lives in GitHub Actions (free, unmetered standard runners on a
public repo), next to the R2 bytes. Railway is CPU-only (no GPU), would be a new always-on
box *away* from the data, and is the wrong host. The one place Railway touches vision is the
on-demand `compare_listing_images` agent tool — out of scope here.

### GitHub Actions 2026 limits that shape the design

| Limit | Value | Consequence |
|---|---|---|
| Standard Linux runner (public repo) | **4 vCPU / 16 GB / 14 GB SSD**, free + unmetered | the compute we get; upgraded from old 2-vCPU |
| Larger runners (8–96 vCPU) | **always billed, even public repos**; free minutes can't be used | **no free path past 4 vCPU** → scale horizontally only |
| Job timeout | **6 h hard** | shards must be bounded + resumable (claim-a-slice + `--max-seconds`) |
| Max concurrent jobs (Free plan, account-wide) | **20** | realistic CLIP headroom 8–16 shards → ~80–160 img/s aggregate |
| Max jobs / workflow run | 256 | not the binding cap; the 20-concurrent account cap is |
| Scheduled cron | **unreliable** (15–60 min jitter, dropped ticks, Jan-2026 incident) | self-chain via `SCRAPE_CHAIN_TOKEN`; treat cron as best-effort kickstart |
| Egress | uncapped/free on runners; R2 has zero egress fee | bottleneck is R2-side 429s, not bandwidth |
| Private-repo contrast | 2,000 min/month | **keeping the repo public is the entire economic basis** |

---

## 2. Throughput model

- **Binding constraint = CPU inference at ~10 img/s/runner** (ONNX int8 ViT-B/32, 4 vCPU;
  band 6–16). This *inverts* the existing image jobs: `compute_image_phash.py` is I/O-bound
  at ~8 img/s because dHash is microseconds of CPU; a CLIP forward pass is ~100 ms, so the
  encoder becomes the floor and R2 download (~1 MB/s at 10 img/s) stops mattering.
- R2 download (~16–32 img/s with 32-worker pool) and **batched** DB writes (thousands/s) are
  *not* binding. The per-row autocommit write pattern **is** a self-inflicted bottleneck — see §4.2.

**Runner-hours (all free) and wall-clock:**

| Target | Runner-hours @10 img/s | Wall-clock @ 8–16 shards | Ticks |
|---|---|---|---|
| 260k queued | ~7 | ~0.5–1 h | 1 |
| **1M eligible** | **~28** | **~1.7–3.5 h** | 1–2 |
| **5M full** | **~139** | **~9–17 h** | ~2 passes/shard (a weekend of self-chained ticks) |
| Steady-state 46k/day | ~1.3/day | ~3–6 min/run | hourly, trivial |

The one-time backfill is the entire cost; the daily drip is rounding error.

> **The 10 img/s figure is MODELED, not measured on these runners.** The 6–16 band swings
> 5M between ~87 h and ~232 h. **Step 1 of the build is a 20k-image trial to pin the real
> rate before committing to anything bigger.** See §5.

---

## 3. Model + runtime

- **Model:** ViT-B/32 (DataComp-1B or LAION-2B weights via open_clip), 512-dim. Plenty for
  coarse room-type / interior-vs-exterior / floor-plan tagging and dedup similarity. ViT-L/14
  is 3–6× slower on CPU — don't, unless B/32 tagging proves too weak.
- **Runtime:** export once to **ONNX, int8-quantized**, serve with `onnxruntime`
  (CPUExecutionProvider, intra-op threads = 4). Keep the runtime **torch-free** so the image
  stays in the hundreds-of-MB range (a torch image is 2–4 GB and risks Railway/Actions build
  pain). `clip.cpp`/GGUF q8_0 (~85 MB) is the absolute-minimum-footprint alternative.
- **Model delivery:** fetch the ~90–350 MB ONNX model from R2 (same zero-egress bucket) at
  job start. Don't rely on the Actions cache (10 GB/repo, LRU-evicted >7 days). Ephemeral
  re-provisioning costs ~10–30 s/job — negligible over a handful of ticks.
- **New deps** (`onnxruntime`, a CLIP weights source, transitive `numpy`) are **not** in
  `pyproject.toml` today (only Pillow + boto3 are). Adding them is a justified-dependency
  decision per architectural rule #7; add to the base install used by the image workflows.

---

## 4. The two real walls (neither is GitHub)

### 4.1 pgvector storage at 5M — the actual blocker

512-dim float32 = 2,048 B/row.

| | 1M | 5M |
|---|---|---|
| Raw vector heap | ~2.05 GB | **~10.5 GB** |
| HNSW index (1.5–3×) | ~3–6 GB | **~16–30 GB** |
| Added DB footprint | ~5–8 GB | **~26–40 GB** |
| Resulting DB (current 25 GB) | ~30–33 GB | **~51–65 GB (doubles–triples)** |

The HNSW graph must mostly fit RAM to build; a default Supabase Pro instance (likely 1–4 GB
RAM unless a compute add-on is bought) **cannot build a 16–30 GB graph** without thrashing.
For scale: the existing 64-bit `phash` btree is **46 MB**; a 512-dim+HNSW set is ~300–600× that.

`pgvector` 0.8.0 is **available but not installed** — one `CREATE EXTENSION vector` (supports
`hnsw` + `ivfflat` + `halfvec`).

**Mitigations (pick in the build):**
1. **Scope to ~1M** (or 260k) — ~5–8 GB, fits a modest compute add-on.
2. **`halfvec` (float16)** — halves raw to ~5.25 GB at 5M; minimal recall loss for CLIP.
3. **256-dim** (PCA/projection) — ~2.6–5 GB raw at 5M.
4. **Externalize** — compute embeddings in Actions, store only the **top-K neighbor pairs**
   dedup consumes back into Postgres, keep raw vectors in R2/parquet. This makes the pgvector
   wall **disappear**; cleanest fit for a free-Actions producer feeding the existing consumer.
5. If keeping vectors in PG at scale: **≥8 GB compute add-on** + raised `maintenance_work_mem`
   + `max_parallel_maintenance_workers`, or `ivfflat` (cheaper RAM, lower recall — fine for
   *candidate generation* upstream of the precise LLM compare). Build the index **once after
   backfill**, not incrementally.

### 4.2 DB-write pattern — do NOT copy the pHash per-row autocommit

`compute_image_phash.py` writes per-image `UPDATE … WHERE id=…` autocommit (~8 rows/s serial
floor). Copy that unchanged and writing embeddings *alone* adds ~28 h (1M) / ~140 h (5M) of
pure round-trips — co-equal with compute.

**Fix:** buffer 200–500 vectors/shard → `COPY` into a staging temp table → single set-based
upsert (the `write_detail_batch` / `jsonb_to_recordset` pattern the drains already use), over
`connect_session()` (session pooler) for prepared-statement reuse on the hot write loop. Lifts
writes to thousands/s; removes it as a constraint.

---

## 5. Recommended build shape (the actual work)

**Step 1 — validation trial (do this first, before any migration):**
- A `scripts/clip_embed.py` + a **one-shard** `clip_embed.yml` workflow (`workflow_dispatch`).
- Process **20k images** from the queued-candidate set: R2 download → decode/resize 224×224 →
  ONNX int8 ViT-B/32 encode → discard.
- **Report measured img/s** on a 4-vCPU public runner (and decode-vs-encode split). This pins
  the real rate (the 6–16 band → decides whether 5M is ~87 h or ~232 h) and validates the
  whole plan for ~7 free runner-hours of risk.
- Don't write embeddings to the DB yet (or write to a throwaway staging table) — the trial is
  about throughput, not storage.

**Step 2 — storage decision (operator input needed):** pick from §4.1 — scope (260k/1M/5M),
precision (`halfvec`/256-dim), and **in-DB vs externalized**. This gates the migration.

**Step 3 — backfill pipeline:**
- Copy `images.yml`'s sharding: `image_id mod 16 == shard`, `matrix: shard [0..15]`,
  `max-parallel: 16` (or 8 to share the 20-concurrent cap nicely with pHash/images/scrapers),
  `fail-fast: false`, `ubuntu-latest`.
- Job budget: `timeout-minutes: 350`, `--max-seconds 18000` (5 h) wall-clock → clean finalize
  under the 6 h cap. Claim a slice (`FOR UPDATE SKIP LOCKED`) of unembedded image_ids, embed
  in batches of 16–32, commit incrementally, exit. A `reclaim_stale_claims`-style recovery for
  SIGKILLed claims (mirror the existing drains).
- Cadence: kickstart by `workflow_dispatch`, **self-chain via `SCRAPE_CHAIN_TOKEN`** while
  unembedded work remains. Don't trust cron timing.
- Writes: batched COPY → staging → upsert (§4.2). Per-image exception catch (like pHash's
  `_hash_one`) so one bad/410 image never kills a shard. Reuse `image_storage.py`'s boto3 pool
  + add explicit 429 backoff (the codebase's `RateLimiter`/penalize discipline).

**Step 4 — steady-state maintenance:** one small recurring job (1–2 shards, hourly or 2-hourly)
absorbs the ~46k/day in ~3–6 min/run — comparable to `images_fresh.yml`.

**Step 5 — wire into dedup:** CLIP cosine similarity (in-DB ANN or external) proposes candidate
pairs → existing street+disposition rules gate them → only survivors hit `compare_listings_visually`.
CLIP slots *upstream* of the forensic compare, shrinking its call volume. Also feed/replace
`classify_listing_images` (zero-shot type tag) — the clearest per-image LLM-spend saving.

---

## 6. Files to copy patterns from

- `.github/workflows/images.yml` — `image_id mod N` sharding, matrix, per-shard cap, runner/timeout.
- `.github/workflows/images_fresh.yml` — self-chaining `SCRAPE_CHAIN_TOKEN` cadence.
- `scripts/compute_image_phash.py` + `.github/workflows/compute_image_phash.yml` — the
  claim/decode loop AND the **per-row write pattern to fix** (batch instead).
- `scraper/image_storage.py` — `R2Client` download path, boto3 pool sizing, sreality 749×562
  gate vs full-res other portals (the real input sizes CLIP reads).
- `scraper/db.py` `write_detail_batch` — the batched set-based upsert pattern for writes.
- Toolkit vision tools that stay LLM (don't replace): `toolkit/visual_match.py`
  (`compare_listings_visually`, `compare_listing_site_plans`), `toolkit/image_classification.py`
  (`classify_listing_images` — the zero-shot replace candidate).

---

## 7. Open decisions for the operator

1. **Scope:** 260k queued / ~1M eligible / 5M full? (Recommend laddering 260k → 1M, defer 5M.)
2. **Vectors in Postgres vs externalized?** In-DB needs `halfvec`/256-dim + likely an ≥8 GB
   compute add-on at scale; externalized (store only top-K pairs) sidesteps the storage wall.
3. **Compute add-on?** Only needed if keeping float-vectors in-DB at 1M+ with HNSW.
4. **Model:** confirm ViT-B/32 int8 ONNX (recommended) vs `clip.cpp` GGUF (smaller footprint).

---

## Bottom line

GitHub Actions on free public-repo standard runners is a **viable, free** host for the 1M
backfill (~28 runner-hours, 1–2 ticks) and compute-viable for 5M (~139 runner-hours). The
binding constraints are **CPU throughput (~10 img/s/runner, horizontal-scale-only under a
20-job cap)** and, at 5M, **pgvector/HNSW storage + index-build RAM** — not money, egress, or
DB-writes (if you batch). Validate the real img/s with a 20k trial first; scope embeddings to
~1M; batch every write; self-chain dispatch.
