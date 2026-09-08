"""One entry point for all three tagging bake-off stages: manifest, embed, train.

The lane is three stages because they need three different machines, and pretending
otherwise would either put a GPU under a SQL query or a database URL inside a rented pod
that does not need one:

  * `manifest` — a plain runner with the DB and R2. Builds the run, the arms, the image
    manifest and the free `clip-b32-stored` baseline (`tagging_bakeoff_manifest.py`).
  * `embed`    — rents a GPU pod through `scripts/runpod_client.py`, which clones this
    repo, installs the `clip` extra and runs `tagging_bakeoff_embed.py`. THE ONLY STAGE
    THAT SPENDS MONEY.
  * `train`    — back on the runner with the `training` extra, running the results half
    of the experiment (`python -m scripts.tag_head_bakeoff`).

Everything the RunPod half gets right is inherited from `dinov3_embed_dispatch.py` and
worth restating, because each was learned the expensive way:

  * CREDENTIALS TRAVEL IN THE REST BODY'S `env`, never in the start command — argv is
    visible in the pod record.
  * THE WAIT WINDOW IS THE POD'S LIFETIME, not a poll timeout. On-demand Pods never flip
    `desiredStatus`, so `run_job` ALWAYS times out and tears the pod down in its
    `finally` — which is exactly what guarantees the bill stops. The window is therefore
    derived from the payload's own `--max-seconds` plus a startup grace, so teardown
    lands just after a clean stop rather than mid-batch.
  * THE GPU IS NOT PICKED ON PRICE ALONE. JPEG decode is CPU-side and
    `eligible_gpus` ranks on price knowing nothing about vCPU count.

Completion is NOT read from the pod. Progress is a SQL question:
`select arm, status, dim from dedup_sim.tag_head_bakeoff_arms where run_id = N`.

Usage:  python -m scripts.tagging_bakeoff_dispatch --stage embed --run-id 7 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

from scripts.runpod_client import NoCapacityError, RunPodClient, RunPodError

LOG = logging.getLogger("tagging_bakeoff_dispatch")

STAGES = ("manifest", "embed", "train")

REPO_URL = "https://github.com/waiff/sreality"
DEFAULT_IMAGE = "runpod/pytorch:2.1.0-py3.10-cuda11.8.0-devel-ubuntu22.04"
# ENCODER-DECISION §5.3's CPU-adequate boxes, cheapest-first within the allowlist. The
# 4090 is deliberately absent: fastest card, fewest vCPUs, and decode is CPU-side.
DEFAULT_GPU_ALLOWLIST = ("3090", "a5000")
MAX_PRICE_PER_HR = 1.00
# Clone + `pip install -e .[clip]` + the first weight download, before image one is
# embedded. The wait window must cover it on top of the payload's own budget.
STARTUP_GRACE_S = 900

# Credentials the pod payload needs. Names only ever appear in logs; values go in the
# REST body.
POD_ENV_KEYS = (
    "SUPABASE_DB_URL",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
    "HF_TOKEN",
)

# The sibling PR's CPU runner: the trainer + scorer half of the experiment. Invoked as a
# subprocess deliberately — this module must not IMPORT it, so the two halves of the
# bake-off can be built, reviewed and merged in either order.
TRAIN_MODULE = "scripts.tag_head_bakeoff"

_REF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}$")
# What may be interpolated into the pod's shell command. Arm names carry `@` and `/`
# (`dinov3-b16@768/bf16`), so those are in; a space, quote, semicolon or backtick is not.
_ARG_RE = re.compile(r"[A-Za-z0-9._=/@,:-]+")


@dataclass(frozen=True)
class Plan:
    """What a stage will do, decided before anything is executed or launched.

    `where` is "runner" (a subprocess here) or "pod" (a rented GPU). Keeping the decision
    in a pure function is what makes stage routing testable without a RunPod key, a
    database, or a dollar.
    """

    stage: str
    where: str
    argv: list[str] = field(default_factory=list)
    start_cmd: list[str] = field(default_factory=list)
    payload_args: list[str] = field(default_factory=list)
    max_wait_s: float = 0.0
    execute: bool = True


def build_start_cmd(*, ref: str, module: str, payload_args: Sequence[str]) -> list[str]:
    """The pod's argv. Carries no secrets — those travel in the REST body's `env`."""
    if not _REF_RE.match(ref):
        raise ValueError(
            f"refusing to interpolate an unsafe git ref into a shell command: {ref!r}")
    for arg in payload_args:
        if not _ARG_RE.fullmatch(arg):
            raise ValueError(f"refusing to interpolate an unsafe payload arg: {arg!r}")
    script = (
        "set -euo pipefail; "
        f"git clone --depth 1 --branch {ref} {REPO_URL} /workspace/sreality; "
        "cd /workspace/sreality; "
        "pip install -e '.[clip]'; "
        f"python -m {module} " + " ".join(payload_args)
    )
    return ["bash", "-c", script]


def pod_env() -> dict[str, str]:
    """The credentials present in this process's environment, forwarded to the pod."""
    return {k: os.environ[k] for k in POD_ENV_KEYS if os.environ.get(k)}


def plan_stage(args: argparse.Namespace) -> Plan:
    """Pure stage routing: what runs, where, and whether a dry run should execute it."""
    if args.stage == "manifest":
        argv = [sys.executable, "-m", "scripts.tagging_bakeoff_manifest",
                f"--min-train-positives={args.min_train_positives}"]
        if args.label:
            argv.append(f"--label={args.label}")
        if args.note:
            argv.append(f"--note={args.note}")
        if args.heads:
            argv.append(f"--heads={args.heads}")
        if args.arms:
            argv.append(f"--arms={args.arms}")
        if args.dry_run:
            # OUR script, whose --dry-run is a reporting mode that writes nothing — so a
            # dry run still gets the head counts and the image census, which is the
            # whole reason to run this stage dry.
            argv.append("--dry-run")
        return Plan(stage="manifest", where="runner", argv=argv)

    if args.stage == "embed":
        if not args.run_id:
            raise ValueError("--run-id is required for stage=embed")
        payload = [
            f"--run-id={args.run_id}",
            f"--batch-size={args.batch_size}",
            f"--workers={args.workers}",
            f"--max-seconds={args.job_max_seconds}",
            "--device=cuda",
        ]
        if args.arms:
            payload.append(f"--arms={args.arms}")
        return Plan(
            stage="embed", where="pod", payload_args=payload,
            start_cmd=build_start_cmd(ref=args.ref,
                                      module="scripts.tagging_bakeoff_embed",
                                      payload_args=payload),
            max_wait_s=args.job_max_seconds + STARTUP_GRACE_S,
            execute=not args.dry_run,
        )

    if not args.run_id:
        raise ValueError("--run-id is required for stage=train")
    argv = [sys.executable, "-m", TRAIN_MODULE, "--run-id", str(args.run_id)]
    if args.arms:
        argv.extend(["--arms", args.arms])
    # NOT executed under --dry-run, unlike the manifest stage: the trainer is the
    # sibling's CLI and this module refuses to assume what its flags mean. A dry run
    # prints the exact command instead, which is the honest thing it can offer.
    return Plan(stage="train", where="runner", argv=argv, execute=not args.dry_run)


def _run_pod(plan: Plan, args: argparse.Namespace) -> int:
    env = pod_env()
    missing = [k for k in POD_ENV_KEYS if k not in env]
    allowlist = tuple(s.strip().lower() for s in args.gpu_allowlist.split(",")
                      if s.strip())
    LOG.info("image=%s ref=%s max_wait_s=%.0f gpu_allowlist=%s",
             args.image, args.ref, plan.max_wait_s, ",".join(allowlist) or "(none)")
    LOG.info("pod env keys present: %s", ",".join(sorted(env)) or "(none)")
    if missing:
        LOG.warning("pod env keys MISSING (the pod will no-op or fail): %s",
                    ",".join(missing))
    LOG.info("start_cmd: %s", plan.start_cmd[-1])

    if not plan.execute:
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
            name=f"tagging-bakeoff-{args.run_id}",
            image=args.image,
            gpu_options=gpus,
            start_cmd=plan.start_cmd,
            env=env,
            max_wait_s=plan.max_wait_s,
            poll_interval_s=30,
            container_disk_gb=60,
        )
    except NoCapacityError as exc:
        LOG.error("no candidate GPU type had capacity: %s", exc)
        return 1
    except RunPodError as exc:
        LOG.error("dispatch failed (not a capacity issue): %s", exc)
        return 1

    LOG.info("pod %s torn down: gpu=%s status=%s timed_out=%s elapsed=%.0fs "
             "cost_per_hr=$%s", result.pod_id, result.gpu_type_id, result.final_status,
             result.timed_out, result.elapsed_s, result.cost_per_hr)
    LOG.info("A timed_out=True here is EXPECTED (on-demand Pods do not flip "
             "desiredStatus) — read progress from Postgres: select arm, status, dim "
             "from dedup_sim.tag_head_bakeoff_arms where run_id = %s.", args.run_id)
    return 0


def select_gpus(client: RunPodClient, allowlist: tuple[str, ...]):
    """Cheapest-first, restricted to the CPU-adequate boxes when any are available."""
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


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stage", choices=STAGES, required=True)
    p.add_argument("--run-id", type=int, default=0,
                   help="The bake-off run. Required for embed and train; the manifest "
                        "stage MINTS one.")
    p.add_argument("--label", default="", help="manifest: run label.")
    p.add_argument("--note", default="", help="manifest: free-text note on the run.")
    p.add_argument("--min-train-positives", type=int, default=100,
                   help="manifest: a head's floor for entering the run.")
    p.add_argument("--heads", default="", help="manifest: explicit tag ids.")
    p.add_argument("--arms", default="",
                   help="Comma-separated arm names, to narrow any stage to a subset.")
    p.add_argument("--batch-size", type=int, default=32, help="embed: forward batch.")
    p.add_argument("--workers", type=int, default=16,
                   help="embed: parallel image downloads inside the pod.")
    p.add_argument("--job-max-seconds", type=float, default=7200,
                   help="embed: the payload's own budget. The pod's wait window is "
                        "this plus a 900s startup grace, so teardown lands after a "
                        "clean stop rather than mid-batch.")
    p.add_argument("--ref", default=os.environ.get("GITHUB_SHA") or "main",
                   help="Git ref the pod clones. Defaults to GITHUB_SHA in Actions.")
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--gpu-allowlist", default=",".join(DEFAULT_GPU_ALLOWLIST))
    p.add_argument("--dry-run", action="store_true",
                   help="Print the resolved plan. The manifest stage still runs (its "
                        "own --dry-run reports counts and writes nothing); embed "
                        "launches no pod and train executes nothing.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        plan = plan_stage(args)
    except ValueError as exc:
        LOG.error("%s", exc)
        return 2

    LOG.info("stage=%s where=%s execute=%s", plan.stage, plan.where, plan.execute)
    if plan.where == "pod":
        return _run_pod(plan, args)

    LOG.info("command: %s", " ".join(plan.argv))
    if not plan.execute:
        LOG.info("DRY RUN — command printed, not executed.")
        return 0
    return subprocess.run(plan.argv, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
