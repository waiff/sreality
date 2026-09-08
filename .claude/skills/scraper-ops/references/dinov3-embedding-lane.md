# The DINOv3 corpus embedding lane

`dinov3_embed_backfill.yml` — the fourth visual-signal producer, and the only GPU one.
**Manual `workflow_dispatch` only, no schedule, and it has never been run against real
data.** Design + rationale: `docs/design/new-dedup/ENCODER-DECISION.md`. Table:
`image_dinov3_embeddings`, migration 480 (PR #1296).

## Shape

Runner → `scripts/dinov3_embed_dispatch.py` → rents a GPU pod through
`scripts/runpod_client.py` → the pod runs `scripts/pod_bootstrap.py`'s script (fetch the
repo BY SHA, stand up a Python 3.12, install cu118 torch, install the `clip` extra) and
then `scripts/dinov3_embed_backfill.py`, which streams images out of R2, embeds them with
`scraper/dinov3_tagger.py`, and writes one L2-normalized 768-d `halfvec` per image.

The runner never installs torch — the model lives in the pod. The dispatcher passes
`--device cuda` explicitly (a pod always has a card); run by hand, `--device` defaults to
`cuda` when torch reports a GPU and `cpu` otherwise, and the resolved value is logged as
`DINOV3 device=…`. Check that line first on a slow pod run: before 2026-09-08 the payload
moved nothing to CUDA at all, so it would have rented a GPU and computed on the CPU.
Credentials
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

**5. The window is the ceiling; the WATCHDOG is what makes the usual case cheaper.**
`scripts/pod_watchdog.py` polls the rows this job writes (count for the six-fact identity)
every 60 s from the runner and terminates the pod in four cases: no vector at all within
`bootstrap_deadline_seconds` (default 1800 — fetch + interpreter + torch + weights + the
first chunk), no new vector for `stall_deadline_seconds` (default 900), two or more `exit=`
reports from the pod's bootstrap (`case=crash-loop`, which needs the step heartbeats this
lane does not wire — see below), or — in the bake-off lane — every arm terminal. It logs
which case fired plus a spend estimate, and a bootstrap/stall/crash-loop teardown **fails the
workflow on purpose**: a green run that embedded nothing is exactly what 2026-09-08 looked
like. Without `SUPABASE_DB_URL` on the runner there is no watchdog and the dispatcher says so
loudly.

**6. The pod works on the CONTAINER disk, and rents no volume.** `container_disk_gb`
(default **60**, floor 40) has to hold the devel base image, ~5 GB of installed torch and
the DINOv3 weights. `/workspace` is RunPod's mount point for the pod VOLUME, which used to
default to 1 GB while the bootstrap installed torch under it — the likeliest cause of the
sibling lane's third dead pod (2026-09-08 (h)). The bootstrap works in `/opt/podboot`, the
launch asks for `volumeInGb: 0`, and a `step=disk <N>GB free` heartbeat lands right before
the torch step. **The pod also never exits**: RunPod re-runs the start command when it does,
so the bootstrap is idempotent and ends in a sleep; the watchdog ends the pod.

**THE STEP HEARTBEATS ARE NOT WIRED IN THIS LANE (yet), and that is a decision.** Since
2026-09-08 (g) the shared bootstrap reports every step (`deps`/`fetch`/`uv`/`venv`/`disk`/
`torch`/`repo`/`payload`/`idle`) and ships `pass=<n> exit=<code> step=<name>` plus a log tail
from an EXIT trap, as a bounded history a restart loop cannot overwrite (h) —
but only into the row a lane names for it, through the `HEARTBEAT_SQL` + `HEARTBEAT_RUN_ID`
pair the dispatcher puts in the pod's env. The bake-off has such a row
(`dedup_sim.tag_head_bakeoff_runs.note`); **this lane has none** — its progress record is
the vector table itself, which the payload only reaches after the bootstrap has succeeded.
So here `scripts/pod_report.py` prints its steps into the (unreadable) pod console and exits
0, and the watchdog's only proof of life is still the first vector. Giving this lane step
heartbeats means minting a durable run row first (a `dinov3_embed_runs`-shaped table): a
schema decision, not part of the fix. Until then keep `bootstrap_deadline_seconds` generous,
and if a pod dies silently, reproduce it on the bake-off lane, which can see.

## Gotchas

- **The pod bootstraps its own interpreter — do not "fix" the image tag.** The RunPod image
  (`runpod/pytorch:2.1.0-py3.10-…`) ships **Python 3.10** and `pyproject.toml` requires
  `>=3.12`, so `pip install -e '.[clip]'` under the image's python refuses. RunPod's newer
  tags do not name a Python version at all, so `scripts/pod_bootstrap.py` installs `uv` and
  builds a 3.12 venv (uv downloads a managed CPython), then takes torch from the **cu118**
  index — the image is CUDA 11.8-era and cu118 is the flavour with cp312 wheels furthest up
  the torch series (through 2.6.0; cu121 stops at 2.5.1). Versions are recorded at run time in
  the progress rows, not pinned.
- **Never `git clone --branch <sha>`.** `--branch` resolves a branch or a tag only; the ref
  here is `GITHUB_SHA`, so the clone fails (`fatal: Remote branch … not found in upstream
  origin`, exit 128) and, under `set -euo pipefail`, so does the whole pod — which then idles
  on the meter. The bootstrap uses `git init` + `git fetch --depth 1 origin <sha>` +
  `git checkout FETCH_HEAD`. A test asserts `--branch` never comes back.
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
