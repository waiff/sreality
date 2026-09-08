# The tagging bake-off lane

`tagging_bakeoff.yml` — a three-stage EXPERIMENT, not a steady-state producer. **Manual
`workflow_dispatch` only, no schedule.** It answers one question with measurement instead
of argument: *which image encoder should the per-tag yes/no heads run on?*

The method: embed the same ~10.8k pictures (every admitted training image, plus the 250
sealed-exam images) under several encoder configurations — **arms** — then train one head
per tag on each arm and compare. Nothing here writes a production signal; every row lands
in `dedup_sim`, the simulation schema.

**Two PRs, one lane.** This half owns the manifest, the pod-side embedder, the dispatcher
and this document. The sibling half owns the `dedup_sim.tag_head_bakeoff_*` migration,
the trainer (`toolkit/tag_heads*.py`), the CPU runner (`scripts/tag_head_bakeoff.py`) and
the admin API. They meet at the table contract and nowhere else.

## Shape

| Stage | Where | What it does | Cost |
| --- | --- | --- | --- |
| `manifest` | plain runner | run + arm rows, head selection, presigned URLs, the free `clip-b32-stored` arm | $0 |
| `embed` | rented GPU pod | every pending arm's vectors | the only stage that spends |
| `train` | plain runner (`training` extra) | the sibling's `scripts.tag_head_bakeoff` | $0 |

```
tagging_bakeoff.yml
  └── scripts/tagging_bakeoff_dispatch.py   --stage manifest|embed|train
        ├── manifest → scripts/tagging_bakeoff_manifest.py   (DB + R2, no torch)
        ├── embed    → scripts/runpod_client.py → a pod running
        │              scripts/pod_bootstrap.py's script, then
        │              scripts/tagging_bakeoff_embed.py      (torch, `clip` extra)
        │              …watched from the runner by scripts/pod_watchdog.py
        └── train    → python -m scripts.tag_head_bakeoff    (sibling PR)
```

The arm list lives in **`scripts/tagging_bakeoff_arms.py`** and nowhere else — both the
manifest (which writes the rows) and the embedder (which loads the encoders) read it, so
"what did run 7 measure?" has one answer in code and one in the arms table.

## The arms

An arm is the same **seven facts** that identify a production DINOv3 vector — model,
revision, library, pooling, resolution, preprocessing, dtype (`scraper/dinov3_config.py`).
That is deliberate: the winner's seven facts are copied into `data/dinov3_config.json`
verbatim, with no translation step where a mistake could hide.

| Arm | Model | Res | dtype | Why it is in the run |
| --- | --- | --- | --- | --- |
| `dinov3-b16@512/{fp32,bf16}` | DINOv3 ViT-B/16 (gated) | 512 | both | the operator's floor: at least 512, bars not crops |
| `dinov3-b16@768/{fp32,bf16}` | " | 768 | both | above the largest portal's stored width — does upsampling buy anything? |
| `dinov3-b16@1024/{fp32,bf16}` | " | 1024 | both | "what about even larger?" |
| `dinov3-l16@512/bf16` | DINOv3 ViT-L/16 (gated) | 512 | bf16 | is the bigger model worth its cost? |
| `dinov2-l14-reg@504/bf16` | DINOv2-L/14-with-registers | **504** | bf16 | Apache-2.0 — the licence fallback. Patch 14 does not divide 512 |
| `siglip2-b16@{512,256}/bf16` | SigLIP2 B/16 | largest that exists | bf16 | the language-supervised control (its own attention-pool head) |
| `clip-b32-laion@224/fp32` | LAION CLIP B/32 | 224 | fp32 | tells "the CLIP family is weak" apart from "the 2021 checkpoint is weak" |
| `clip-b32-stored` | `openai/clip-vit-base-patch32` | — | — | **zero GPU** — the incumbent's live vectors, copied by SQL |

All six DINOv3 B/16 arms use `letterbox_pad`: the operator asked for bars rather than
crops, and centre-cropping discards the left/right bands where portal watermarks live.

**Resolution snaps to the model's patch grid and the snapped value is what gets
recorded.** A ViT cannot take a resolution off its grid, so a requested 512 on patch-14
DINOv2 *is* 504 whatever anyone writes down; `renamed_to_effective` renames the arm too,
so no arm can be called by a resolution it did not run at. (With today's preset nothing
snaps — 512/768/1024 are multiples of 16, 504 of 14, 224 of 32 — but the machinery is
what makes that a checked fact rather than a lucky one.)

**Revisions are resolved at run time, never hardcoded.** `hub_sha` asks the public
model-metadata endpoint with `requests` — not `huggingface_hub`, because the manifest
stage runs on a plain runner that installs neither torch nor the `clip` extra. An arm
whose sha will not resolve (gated weights, no `HF_TOKEN`, licence not accepted) is
**skipped with the reason recorded**, never loaded from an unpinned `main`.

## Cost

Roughly **10.8k images x 9 GPU arms ≈ 97k forward passes**, most at 512, a pair at 1024.
On an RTX 3090 that is on the order of an hour wall-clock including the one-time image
download and per-arm weight loads — call it **well under a dollar** at community pricing
($0.20–0.30/hr). The wait window (`job_max_seconds` + a 900 s startup grace) is the hard
ceiling: the pod is torn down when it expires, whatever the job is doing.

**The ceiling is not the plan — the watchdog is** (`scripts/pod_watchdog.py`, since
2026-09-08). It polls this run's own rows every 60 s from the runner and terminates the pod
when: no boot heartbeat arrives within `bootstrap_deadline_seconds` (default 1200), a booted
pod stops progressing for `stall_deadline_seconds` (default 900), or **every arm is
terminal** (`ok`/`failed`/`skipped`) — the last one being the ordinary happy path, which
now stops paying the moment the work is done rather than at the end of the window. It logs
the case and a spend estimate; a bootstrap or stall teardown **fails the workflow**, because
a green run that embedded nothing is precisely what the incident looked like in Actions.

The tenth arm, `clip-b32-stored`, costs nothing at all — it is a `SELECT … INSERT` out of
the live `image_clip_embeddings`, done in the manifest stage. It is the baseline every
other arm is measured against, so a run without it measures nothing useful.

## Running it

Always start with `dry_run: true` (the default).

1. **manifest** — `stage: manifest`, optionally `label`, `heads` (explicit tag ids), `arms`
   (narrow the preset). **Head selection is the operator's ready flag** — the Ready / Not
   ready / Skip toggle on `/new-dedup/training-set` (`tag_taxonomy.review_state = 'ready'`),
   read through the one shared selector `toolkit.tag_head_bakeoff.ready_heads`, which the
   CPU runner uses too; `heads` is the only override, and the admitted positive/negative
   counts are reported but never filter (ruling 2026-09-08, replacing the old
   `min_train_positives` floor). The dry run prints the selected heads with their counts,
   the distinct-image census, and how many of those images we actually hold bytes for.
   Then re-run with `dry_run: false`
   and **note the `run_id` it prints** — the other two stages need it.
2. **embed** — `stage: embed`, `run_id: N`. The dry run prints the pod plan (image, ref,
   wait window, GPU allowlist, which credential names are present) and launches nothing.
   `arms` narrows the pass; `batch_size` comes down if a 1024 arm runs out of GPU memory.
3. **train** — `stage: train`, `run_id: N`. `dry_run` here only *prints* the command:
   the trainer is the sibling's CLI and this lane does not assume what its flags mean.

## Reading the readouts

Progress and outcome are SQL questions — never read off the pod:

```sql
select arm, status, dim, note from dedup_sim.tag_head_bakeoff_arms where run_id = N order by id;
select arm_id, count(*) from dedup_sim.tag_head_bakeoff_vectors group by 1 order by 1;
```

`status` per arm: `pending` → `running` → `ok` | `failed` | `skipped`. `note` carries the
resolved revision, the end-to-end throughput (`img/s` including JPEG decode, which is the
number that decides whether a future full-corpus pass is affordable) and **the resolved
`py…/torch…/transformers…` versions** — the pod bootstraps its own interpreter, so the row
is the only record of what actually ran. A `skipped` arm names why in `note` — usually a
gated checkpoint the token could not read.

While a pod is up, the same two columns are the heartbeat: the arm note is rewritten at
every batch (`<iso> running 3000/10800 vectors dim=768 …`) and the RUN row's note carries
two payload-owned lines, `pod booted <iso> <versions>` (its very first DB write, before the
manifest and any weight download) and `pod alive <iso> <phase>` (rewritten through the
phases that write no vector — fetching the manifest, filling the image cache). A run note
with no fresh `pod booted` line means the clone or the install failed; that distinction is
the whole reason the boot stamp is written before anything expensive:

```sql
select note from dedup_sim.tag_head_bakeoff_runs where id = N;
```

## Gotchas

- **`timed_out=True` in the dispatch log is EXPECTED.** On-demand Pods never flip
  `desiredStatus`, so `run_job` always times out and tears the pod down in its `finally`
  — that teardown *is* the cost guarantee. Judge the run by the arms table.
- **The pod bootstraps its own Python; never `git clone --branch <sha>`.** Both halves of
  the 2026-09-08 incident (run 1: 8,115 s on a 3090, ~$0.50, zero vectors). `--branch`
  takes a branch or a tag, so cloning at `GITHUB_SHA` fails outright — the bootstrap now
  does `git init` + `git fetch --depth 1 origin <sha>` + `git checkout FETCH_HEAD`. And the
  image ships **Python 3.10** while `pyproject.toml` requires `>=3.12`, so the pod installs
  `uv`, builds a 3.12 venv and takes torch from the **cu118** index (widest cp312 coverage,
  and the image is CUDA 11.8-era). One module, `scripts/pod_bootstrap.py`, shared with the
  production lane; tests assert `--branch` never returns.
- **Re-dispatching a half-finished run needs no cleanup.** An arm a killed pod left
  `running` is picked up again — `pending_arms` treats `pending`/`running`/`failed` alike,
  and the per-image skip means the work already committed is not repeated. A `running`
  status has never meant "someone is on it"; the timestamp in the note is what says that.
- **Presigned URLs live 7 days.** An `embed` run against a stale manifest fails with
  every download erroring; re-run the manifest stage (it mints a new run) rather than
  hunting the pod.
- **Download once, embed N times.** The pod caches every image to local disk before the
  first arm runs (~1 GB), because ten arms re-downloading the corpus would spend the
  pod's life on R2 egress. The *production* corpus job does the opposite and must —
  10.4M images do not fit on a pod's disk. Different problem, different answer.
- **Resume is free and needs no marker column**, the same way the production lane works:
  an arm's already-written `image_id`s are read into a set and skipped. A pod dying at
  arm 6 of 10 costs the arms it had not started.
- **Naming an arm in `arms` overrides its status.** That is how a `skipped` arm is
  retried once the token is fixed or the licence accepted; an already-finished arm named
  this way is a no-op, because the per-image skip still applies. Leaving `arms` empty
  works only on unfinished arms.
- **Pooling is per family, not one hardcoded slice.** DINO arms use the post-LayerNorm
  CLS (`pooler_output`) through `scraper/dinov3_tagger.py`; SigLIP2 has **no CLS token at
  all** (a learned attention-pooling head); CLIP's comparable vector is the projected
  `image_embeds`, not any hidden state. Getting this wrong is silent — same shape,
  different population.
- **`Dinov3Tagger` now takes a `device`** (default `"cpu"`, i.e. unchanged for every
  existing caller). Without it the model loads onto the CPU even on a rented GPU, which
  is why the production `dinov3_embed_backfill.py` should be given the same flag before
  its first real corpus run.
- **The sealed exam is embedded but never read.** Its images have to carry vectors —
  grading the heads is the point — but no exam *answer* is read anywhere in this lane.
  Training images arrive only through `toolkit.machine_labeling.training_rows`, the one
  holdout-excluded door, and `tests/test_holdout_exclusion_census.py` is the rail. The
  manifest module contains no SQL naming `image_tag_labels`, and a test asserts that.
