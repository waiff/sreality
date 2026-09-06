# The DINOv3 corpus embedding lane

`dinov3_embed_backfill.yml` — the fourth visual-signal producer, and the only GPU one.
**Manual `workflow_dispatch` only, no schedule, and it has never been run against real
data.** Design + rationale: `docs/design/new-dedup/ENCODER-DECISION.md`. Table:
`image_dinov3_embeddings`, migration 480 (PR #1296).

## Shape

Runner → `scripts/dinov3_embed_dispatch.py` → rents a GPU pod through
`scripts/runpod_client.py` → the pod clones the repo at `GITHUB_SHA`, installs the `clip`
extra, and runs `scripts/dinov3_embed_backfill.py`, which streams images out of R2, embeds
them with `scraper/dinov3_tagger.py`, and writes one L2-normalized 768-d `halfvec` per image.

The runner never installs torch — the model lives in the pod. Credentials
(`SUPABASE_DB_URL`, `R2_*`, `HF_TOKEN`) reach the pod through the RunPod REST body's `env`
object, never through the start command (argv is visible in the pod record).

## Four things to know before touching it

**1. It is INERT until the bake-off completes.** `data/dinov3_config.json` ships with
`revision`, `resolution`, `preprocessing` and `dtype` null, and `scraper/dinov3_config.py`
refuses every load while any of the six identity facts (model, revision, library, pooling,
resolution, preprocessing, dtype) is unset. That refusal is the design, not a bug: those six
are the target table's primary key because **any one of them changing means a new population,
not a new value**. Filling them in needs both the bake-off (ENCODER-DECISION §5) and the
operator's DINOv3 licence acceptance — neither is an agent's call.

**2. `max_write_mb_per_hour` is a required input with NO default.** A full pass writes ~18 GB
into the live production database. Supabase gp3 disk auto-expands at 90% of allocated disk and
the project goes **read-only at 95%** with the quota exhausted — which takes down the scrapers,
the API's writes, the SPA and the pipeline, not just this job. Disk also cannot shrink. The
safe rate therefore depends on the dashboard's live utilisation reading at run time, so the
flag is required and the operator must look before dispatching. `WriteThrottle` paces batches
against it (~1,552 B per row, a floor — the identity index costs more on top).

**3. Resume is free and needs no marker column.** The target table IS the checkpoint: pending
= a stored image with no row under this exact six-fact config (a `NOT EXISTS` anti-join), so a
pod dying at 60% costs minutes and a re-run is a no-op. An image embedded under a *different*
config is still pending under this one — that is the six-fact key doing its job. Progress is a
SQL question anyone can ask at any time:
`count(image_dinov3_embeddings for this config) / count(images where storage_path is not null)`.
An in-run `id >` cursor stops a chunk whose downloads all failed from being re-selected
forever; it resets each run, so transient failures retry.

**4. The pod never self-reports completion.** On-demand Pods never flip `desiredStatus` (proved
live, 2026-08-06), so `run_job` always times out and tears the pod down in its `finally` —
that teardown is the cost guarantee. It also means the wait window *is* the pod's lifetime, so
the dispatcher derives it from the payload's own `--max-seconds` plus a startup grace. A
`timed_out=True` in the log is the expected path, not a failure.

## Gotchas

- **GPU selection is not price-only here.** `RunPodClient.eligible_gpus` ranks on price and
  knows nothing about vCPU or system RAM, and JPEG decode is CPU-side. The dispatcher's
  `--gpu-allowlist` (default: RTX 3090 / A5000, per ENCODER-DECISION §5.3) filters first and
  falls back to the price-ranked catalog with a warning. Avoid the 4090 — fastest card, fewest
  vCPUs.
- **`pooler_output`, not `last_hidden_state[:, 0]`.** The first is the post-LayerNorm CLS
  token DINOv3's retrieval protocol uses; the second is the raw pre-LN CLS. Same shape, same
  dtype, different population. Guarded by an AST test.
- **`model.eval()` is mandatory.** DINOv3 applies positional augmentations in train mode, so a
  module left in train mode returns a different vector per forward pass for one image.
- **fp16 is out** (documented NaN risk); only `bf16` and `fp32` are accepted, and an unknown
  value raises rather than falling back.
- **The geometry transform is ours, not the processor's.** All three arms
  (`square_squash` / `resize_center_crop` / `letterbox_pad`) are implemented, and the processor
  runs with `do_resize=False, do_center_crop=False` so its 224-square default cannot override
  the configured resolution. `letterbox_pad` is the one that keeps the left/right edge bands
  where portal watermarks live.
- **`HF_TOKEN` is required at real run time**: `facebook/dinov3-*` is `gated: manual`, so even
  `config.json` 401s without it.
- **The CLIP lane keeps running in parallel.** Nothing has been retired;
  `image_clip_embeddings` is still path B's substrate and the bake-off's baseline.
