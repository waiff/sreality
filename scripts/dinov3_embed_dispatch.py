"""Launch the DINOv3 corpus embedding pass on a RunPod GPU pod.

The thin half of the lane: this picks a GPU, builds the pod's start command, hands the
pod the credentials it needs, and relies on `scripts/runpod_client.py`'s guaranteed
`finally` teardown. The work itself is `scripts/dinov3_embed_backfill.py`, which knows
nothing about RunPod and runs identically on a plain runner.

Three things this script exists to get right, each from
docs/design/new-dedup/ENCODER-DECISION.md §5.3/§5.5 or a live RunPod run:

  * CREDENTIALS REACH THE POD. `launch_pod` had no `env` field until this lane, so
    nothing could hand a pod SUPABASE_DB_URL / R2_* / HF_TOKEN. They go through the
    REST body's `env` object — never through the start command, which is argv and is
    visible in the pod record.
  * THE WAIT WINDOW IS THE JOB'S BUDGET, not a poll timeout. A live run proved
    `desiredStatus` never leaves RUNNING for on-demand Pods, so `run_job` ALWAYS times
    out and then terminates in its `finally`. That teardown is the cost guarantee — and
    it means the wait window is effectively "how long the pod is allowed to live". It
    is therefore derived from the payload's own --max-seconds plus a startup grace, so
    the pod is torn down shortly AFTER the job stops cleanly, never in the middle of it.
  * THE GPU IS NOT PICKED ON PRICE ALONE. `eligible_gpus` ranks by price and knows
    nothing about vCPU or system RAM, and JPEG decode is CPU-side — §5.3 names the
    RTX 3090 (16 vCPU) and RTX A5000 (9 vCPU) and warns off the 4090 (6 vCPU). The
    allowlist below is that judgement, applied cheapest-first within it.

Completion is NOT read from the pod: it self-reports into Postgres, because the rows it
writes ARE the progress record (count for this config / count of stored images).

Usage:  python -m scripts.dinov3_embed_dispatch --max-write-mb-per-hour 500 --dry-run
Required: RUNPOD_API_KEY (+ SUPABASE_DB_URL, R2_*, HF_TOKEN to hand to the pod).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys

from scraper.dinov3_config import IDENTITY_FIELDS, encoder_identity
from scripts.runpod_client import NoCapacityError, RunPodClient, RunPodError

LOG = logging.getLogger("dinov3_embed_dispatch")

REPO_URL = "https://github.com/waiff/sreality"
# A CUDA image with torch + git + pip already present, so the pod's only setup is a
# shallow clone and `pip install -e .[clip]`.
DEFAULT_IMAGE = "runpod/pytorch:2.1.0-py3.10-cuda11.8.0-devel-ubuntu22.04"
# §5.3's boxes, in the order it prefers them. Matched case-insensitively against the
# catalog's id and display name; an empty match falls back to the price-ranked list
# with a warning rather than failing the dispatch.
DEFAULT_GPU_ALLOWLIST = ("3090", "a5000")
MAX_PRICE_PER_HR = 1.00

# Credentials the payload needs INSIDE the pod. Names only ever appear in logs.
POD_ENV_KEYS = (
    "SUPABASE_DB_URL",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
    "HF_TOKEN",
)

# The ref is interpolated into a shell command, so it is constrained to what a git ref
# can legally contain — no spaces, quotes, semicolons or backticks.
_REF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}$")

# Pod startup: clone + pip install before the first image is embedded. The wait window
# must cover it on top of the payload's own budget or the teardown lands mid-job.
STARTUP_GRACE_S = 900


def build_start_cmd(*, ref: str, backfill_args: list[str]) -> list[str]:
    """The pod's argv. Carries no secrets — those travel in the REST body's `env`."""
    if not _REF_RE.match(ref):
        raise ValueError(f"refusing to interpolate an unsafe git ref into a shell command: {ref!r}")
    for arg in backfill_args:
        if not re.fullmatch(r"[A-Za-z0-9._=/-]+", arg):
            raise ValueError(f"refusing to interpolate an unsafe backfill arg: {arg!r}")
    script = (
        "set -euo pipefail; "
        f"git clone --depth 1 --branch {ref} {REPO_URL} /workspace/sreality; "
        "cd /workspace/sreality; "
        "pip install -e '.[clip]'; "
        "python -m scripts.dinov3_embed_backfill " + " ".join(backfill_args)
    )
    return ["bash", "-c", script]


def pod_env() -> dict[str, str]:
    """The credentials present in this process's environment, forwarded to the pod.
    A missing one is reported by NAME so the operator can fix the secret binding —
    values are never logged, and never put in the start command."""
    return {k: os.environ[k] for k in POD_ENV_KEYS if os.environ.get(k)}


def select_gpus(client: RunPodClient, allowlist: tuple[str, ...]):
    """Cheapest-first, restricted to §5.3's CPU-adequate boxes when any are available."""
    gpus = client.eligible_gpus(max_price_per_hr=MAX_PRICE_PER_HR)
    if not allowlist:
        return gpus
    preferred = [
        g for g in gpus
        if any(pat in g.id.lower() or pat in g.display_name.lower() for pat in allowlist)
    ]
    if preferred:
        return preferred
    LOG.warning(
        "none of the preferred GPU types %s are available — falling back to the "
        "price-ranked catalog, which does NOT constrain vCPU/RAM and may pick a box "
        "whose CPU-side JPEG decode starves the GPU (ENCODER-DECISION §5.3)",
        ",".join(allowlist),
    )
    return gpus


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-write-mb-per-hour", type=float, required=True,
                   help="REQUIRED, no default — passed straight through to the payload. "
                        "The safe value depends on the Supabase dashboard's LIVE disk-"
                        "utilisation reading at run time: gp3 disk auto-expands at 90%% of "
                        "allocated disk and the project goes READ-ONLY at 95%% with the "
                        "quota exhausted, taking the scrapers, the API's writes, the SPA "
                        "and the pipeline down with it. Disk cannot shrink. Look first.")
    p.add_argument("--limit", type=int, default=200_000, help="Max images this pass.")
    p.add_argument("--chunk", type=int, default=256)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--job-max-seconds", type=float, default=3600,
                   help="The payload's own time budget. The pod's wait window is this "
                        "plus a startup grace, so teardown lands after a clean stop.")
    p.add_argument("--ref", default=os.environ.get("GITHUB_SHA") or "main",
                   help="Git ref the pod clones. Defaults to GITHUB_SHA in Actions.")
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--gpu-allowlist", default=",".join(DEFAULT_GPU_ALLOWLIST),
                   help="Comma-separated substrings of preferred GPU ids/names. "
                        "Empty = price-ranked catalog only (not recommended).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the resolved plan and exit. Contacts neither RunPod nor "
                        "the database, launches nothing, spends nothing.")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Refuse before spending a cent if the encoder is still under-specified: a pod that
    # launches only to raise on the config is $0.22/hr of nothing.
    identity = encoder_identity()
    LOG.info("identity %s", " ".join(f"{k}={identity[k]}" for k in IDENTITY_FIELDS))

    backfill_args = [
        f"--max-write-mb-per-hour={args.max_write_mb_per_hour}",
        f"--limit={args.limit}",
        f"--chunk={args.chunk}",
        f"--batch-size={args.batch_size}",
        f"--workers={args.workers}",
        f"--shard={args.shard}",
        f"--shards={args.shards}",
        f"--max-seconds={args.job_max_seconds}",
    ]
    start_cmd = build_start_cmd(ref=args.ref, backfill_args=backfill_args)
    env = pod_env()
    missing = [k for k in POD_ENV_KEYS if k not in env]
    max_wait_s = args.job_max_seconds + STARTUP_GRACE_S
    allowlist = tuple(s.strip().lower() for s in args.gpu_allowlist.split(",") if s.strip())

    LOG.info("image=%s ref=%s max_wait_s=%.0f gpu_allowlist=%s",
             args.image, args.ref, max_wait_s, ",".join(allowlist) or "(none)")
    LOG.info("pod env keys present: %s", ",".join(sorted(env)) or "(none)")
    if missing:
        LOG.warning("pod env keys MISSING (the pod will no-op or fail): %s", ",".join(missing))
    LOG.info("start_cmd: %s", start_cmd[-1])

    if args.dry_run:
        LOG.info("DRY RUN — no pod launched, nothing written, nothing spent.")
        return 0

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        LOG.error("RUNPOD_API_KEY not set")
        return 1

    client = RunPodClient(api_key)
    try:
        gpus = select_gpus(client, allowlist)
    except RunPodError as exc:
        LOG.error("could not list eligible GPUs: %s", exc)
        return 1
    LOG.info("%d candidate GPU(s), cheapest first: %s", len(gpus),
             ", ".join(f"{g.id} (${g.community_price_per_hr:.3f}/hr)" for g in gpus[:5]))

    try:
        result = client.run_job_with_fallback(
            name="dinov3-embed-backfill",
            image=args.image,
            gpu_options=gpus,
            start_cmd=start_cmd,
            env=env,
            max_wait_s=max_wait_s,
            poll_interval_s=30,
            container_disk_gb=40,
        )
    except NoCapacityError as exc:
        LOG.error("no candidate GPU type had capacity: %s", exc)
        return 1
    except RunPodError as exc:
        LOG.error("dispatch failed (not a capacity issue): %s", exc)
        return 1

    LOG.info("pod %s torn down: gpu=%s status=%s timed_out=%s elapsed=%.0fs cost_per_hr=$%s",
             result.pod_id, result.gpu_type_id, result.final_status, result.timed_out,
             result.elapsed_s, result.cost_per_hr)
    LOG.info("A timed_out=True here is EXPECTED (on-demand Pods do not flip desiredStatus) "
             "— read progress from Postgres instead: count(image_dinov3_embeddings for this "
             "config) / count(images where storage_path is not null).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
