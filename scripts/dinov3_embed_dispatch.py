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
  * THE WINDOW IS A CEILING, NOT A PLAN. The sibling bake-off lane rented a 3090 for its
    full 8,115 s window on 2026-09-08 and wrote nothing — the pod had died in its first
    seconds and the blind wait could not tell. The watchdog
    (`scripts/pod_watchdog.py`) polls the rows this job writes and tears the pod down
    when they never start, or stop.

Completion is NOT read from the pod: it self-reports into Postgres, because the rows it
writes ARE the progress record (count for this config / count of stored images).

Usage:  python -m scripts.dinov3_embed_dispatch --max-write-mb-per-hour 500 --dry-run
Required: RUNPOD_API_KEY (+ SUPABASE_DB_URL, R2_*, HF_TOKEN to hand to the pod).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from scraper.dinov3_config import IDENTITY_FIELDS, encoder_identity
from scripts import pod_bootstrap
from scripts.pod_watchdog import (
    DEFAULT_BOOTSTRAP_DEADLINE_S,
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_STALL_DEADLINE_S,
    PodWatchdog,
    Progress,
)
from scripts.runpod_client import NoCapacityError, RunPodClient, RunPodError

LOG = logging.getLogger("dinov3_embed_dispatch")

REPO_URL = pod_bootstrap.REPO_URL
# The image supplies git, pip and a CUDA driver. It does NOT supply a usable Python:
# its 3.10 is below this project's >=3.12 floor, so the pod bootstraps its own
# interpreter and torch (scripts/pod_bootstrap.py). Chasing a newer image tag is not
# the fix — RunPod's newer tags do not state a Python version at all.
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
_REF_RE = pod_bootstrap._REF_RE

# Pod startup: fetch + interpreter + torch install before the first image is embedded.
# The wait window must cover it on top of the payload's own budget or the teardown lands
# mid-job. (The watchdog is what handles a startup that never finishes.)
STARTUP_GRACE_S = 900

# The watchdog's own read budget. Deliberately far below the payload's 300s: this
# question is asked once a minute beside a live production database and its answer is
# only worth having if it is prompt.
WATCHDOG_QUERY_TIMEOUT_MS = 30_000


def build_start_cmd(*, ref: str, backfill_args: list[str]) -> list[str]:
    """The pod's argv. Carries no secrets — those travel in the REST body's `env`.

    The script (fetch by sha, bootstrap a 3.12, cu118 torch) is shared with the bake-off
    lane in `scripts/pod_bootstrap.py`: both lanes were broken the same way on
    2026-09-08 and must stay fixed the same way."""
    return pod_bootstrap.build_start_cmd(ref=ref,
                                         module="scripts.dinov3_embed_backfill",
                                         payload_args=backfill_args, extra="clip")


def pod_env() -> dict[str, str]:
    """The credentials present in this process's environment, forwarded to the pod.

    NOT WIRED HERE (deliberate, 2026-09-08 (g)): the bootstrap's step reporter needs a
    `HEARTBEAT_SQL` + `HEARTBEAT_RUN_ID` pair naming a row it may UPDATE. This lane has
    no run row — its progress record IS the vector table — so the reporter prints its
    steps and exits 0, and this lane's watchdog still reads the vectors it always did.
    Giving it one means choosing a durable row first (a `dinov3_embed_runs`-shaped
    thing), which is a schema decision, not a bug fix.
    A missing one is reported by NAME so the operator can fix the secret binding —
    values are never logged, and never put in the start command."""
    return {k: os.environ[k] for k in POD_ENV_KEYS if os.environ.get(k)}


def _connect(db_url: str) -> Any:
    import psycopg

    # A bounded read: this runs beside a live production database once a minute, and a
    # progress question that queues behind the bulk write is not worth asking.
    return psycopg.connect(db_url, autocommit=True, prepare_threshold=None,
                           connect_timeout=15,
                           options="-c statement_timeout=30000")


def read_backfill_progress(conn: Any, *, identity: dict[str, Any],
                           baseline: int) -> Progress:
    """One watchdog reading: how many vectors exist under this exact identity.

    BOOTED means the pod wrote at least one vector this run — this lane has no separate
    heartbeat row and gets none, because adding one would be a migration for a job that
    already publishes its progress. The consequence is honest and worth stating: the
    bootstrap deadline here must cover clone + install + weights + the FIRST chunk, not
    just the boot.

    NEVER TERMINAL. Completion would be `pending == 0`, and that anti-join over ~10.4M
    images is far too expensive to ask every minute; the stall deadline is what ends a
    finished pod, one stall window later.
    """
    from scripts.dinov3_embed_backfill import embedded_count

    count = embedded_count(conn, identity, timeout_ms=WATCHDOG_QUERY_TIMEOUT_MS)
    return Progress(booted=count > baseline, marker=str(count), terminal=False,
                    detail=f"{count} vectors under this identity (baseline {baseline})")


def make_watchdog(args: argparse.Namespace, identity: dict[str, Any]) -> PodWatchdog | None:
    """The watchdog for this dispatch, or None when the runner cannot read the database
    (in which case the wait window is the only protection there is, loudly)."""
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        LOG.warning("SUPABASE_DB_URL is not set on the RUNNER — no watchdog, so a pod "
                    "that dies on boot bills the whole window (2026-09-08)")
        return None
    baseline = 0
    try:
        with _connect(db_url) as conn:
            baseline = read_backfill_progress(conn, identity=identity,
                                              baseline=-1).marker
        baseline = int(baseline)
    except Exception as exc:  # noqa: BLE001 - a baseline we cannot read is 0
        LOG.warning("could not read the vector baseline (assuming 0): %s", exc)
        baseline = 0

    def poll() -> Progress:
        with _connect(db_url) as conn:
            return read_backfill_progress(conn, identity=identity, baseline=baseline)

    LOG.info("watchdog: bootstrap_deadline=%.0fs stall_deadline=%.0fs poll=%.0fs "
             "baseline_vectors=%d", args.bootstrap_deadline_s, args.stall_deadline_s,
             DEFAULT_POLL_INTERVAL_S, baseline)
    return PodWatchdog(poll, bootstrap_deadline_s=args.bootstrap_deadline_s,
                       stall_deadline_s=args.stall_deadline_s,
                       poll_interval_s=DEFAULT_POLL_INTERVAL_S)


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
    p.add_argument("--bootstrap-deadline-s", type=float,
                   default=DEFAULT_BOOTSTRAP_DEADLINE_S,
                   help="Tear the pod down if no vector reaches the database within "
                        "this long (fetch + interpreter + torch + weights + the first "
                        "chunk). The 2026-09-08 failure mode.")
    p.add_argument("--stall-deadline-s", type=float, default=DEFAULT_STALL_DEADLINE_S,
                   help="Tear the pod down if a working pod stops writing for this "
                        "long.")
    p.add_argument("--ref", default=os.environ.get("GITHUB_SHA") or "main",
                   help="Git ref the pod fetches BY SHA. Defaults to GITHUB_SHA in "
                        "Actions.")
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
        # Explicit, not left to the payload's probe: this command only ever runs on a
        # rented GPU pod, so anything but cuda there is a fault worth a loud fallback.
        "--device=cuda",
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
    LOG.info("start_cmd:\n%s", start_cmd[-1])

    if args.dry_run:
        # Execute that exact script offline with every real step stubbed — once clean,
        # once with a forced failure — so a syntax error or a dead EXIT trap is found
        # here rather than by renting a GPU and waiting out a deadline.
        ok, lines = pod_bootstrap.preflight(start_cmd[-1])
        for line in lines:
            LOG.info("%s", line)
        if not ok:
            LOG.error("the generated bootstrap script FAILED its offline self-check — "
                      "do not launch a pod with it")
            return 1
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

    watchdog = make_watchdog(args, identity)
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
            progress=watchdog,
        )
    except NoCapacityError as exc:
        LOG.error("no candidate GPU type had capacity: %s", exc)
        return 1
    except RunPodError as exc:
        LOG.error("dispatch failed (not a capacity issue): %s", exc)
        return 1

    spent = ("" if result.cost_per_hr is None
             else f" spent≈${float(result.cost_per_hr) * result.elapsed_s / 3600.0:.2f}")
    LOG.info("pod %s torn down: gpu=%s status=%s timed_out=%s elapsed=%.0fs cost_per_hr=$%s%s",
             result.pod_id, result.gpu_type_id, result.final_status, result.timed_out,
             result.elapsed_s, result.cost_per_hr, spent)
    if result.stop_reason:
        LOG.warning("the WATCHDOG ended this run: %s", result.stop_reason)
    LOG.info("A timed_out=True here is EXPECTED (on-demand Pods do not flip desiredStatus) "
             "— read progress from Postgres instead: count(image_dinov3_embeddings for this "
             "config) / count(images where storage_path is not null).")
    # A watchdog teardown means the pod was not working: fail the lane rather than leave
    # a green run that embedded nothing (which is what 2026-09-08 looked like).
    if result.stop_reason and not result.stop_reason.startswith("all-terminal"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
