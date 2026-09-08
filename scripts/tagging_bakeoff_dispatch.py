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
  * THE WINDOW IS A CEILING, NOT A PLAN. On 2026-09-08 this lane rented a 3090 for the
    full 8,115 s window and wrote zero vectors: the pod died in its first seconds and
    nothing here noticed, because the wait is blind and RunPod's Pod logs endpoint
    answers 400. A watchdog (`scripts/pod_watchdog.py`) now polls the payload's OWN rows
    every minute and tears the pod down on a missing boot heartbeat, a stalled one, or
    every arm reaching a terminal status.
  * THE BOOTSTRAP REPORTS ITSELF. The second failure (pod bsg9k5ee9y6jcm, 1,245 s,
    ~$0.08) was killed correctly and still could not be diagnosed: the first heartbeat
    was the payload's, written only after fetch + uv + venv + torch + install had all
    succeeded. This dispatch now hands the pod a `HEARTBEAT_SQL` naming THIS run's row
    (`POD_HEARTBEAT_SQL`), which `scripts/pod_report.py` executes after every bootstrap
    step and once more from an EXIT trap carrying `exit=<code> step=<the failing step>`
    and the tail of the pod's own bootstrap log. `select note from
    dedup_sim.tag_head_bakeoff_runs where id = N` is now the pod log RunPod will not
    give us, and the dispatcher prints it after teardown.

Completion is NOT read from the pod. Progress is a SQL question:
`select arm, status, dim, note from dedup_sim.tag_head_bakeoff_arms where run_id = N`.

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
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from scripts import pod_bootstrap
from scripts.pod_watchdog import (
    DEFAULT_BOOTSTRAP_DEADLINE_S,
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_STALL_DEADLINE_S,
    PodWatchdog,
    Progress,
)
from scripts.runpod_client import NoCapacityError, RunPodClient, RunPodError
from scripts.tagging_bakeoff_arms import STORED_CLIP_ARM

LOG = logging.getLogger("tagging_bakeoff_dispatch")

STAGES = ("manifest", "embed", "train")

REPO_URL = pod_bootstrap.REPO_URL
# The image supplies a CUDA driver and nothing else that matters: its Python is 3.10 and
# this project requires >=3.12, so the pod bootstraps its own interpreter
# (scripts/pod_bootstrap.py). Do not "fix" this tag by chasing a Python version.
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

_REF_RE = pod_bootstrap._REF_RE
# What may be interpolated into the pod's shell command. Arm names carry `@` and `/`
# (`dinov3-b16@768/bf16`), so those are in; a space, quote, semicolon or backtick is not.
_ARG_RE = pod_bootstrap._ARG_RE

# An arm nothing will move again. Every arm being one of these is the dispatcher's cue
# that the rest of the wait window is pure rent.
TERMINAL_ARM_STATUSES = frozenset({"ok", "failed", "skipped"})

# The pod's clock is not the runner's. A boot stamp this much older than the launch is
# still accepted as this pod's, and anything older is a previous dispatch's.
CLOCK_SKEW_GRACE_S = 120

_ARM_PROGRESS_SQL = """
    SELECT a.arm, a.status, a.note, count(v.image_id)
    FROM dedup_sim.tag_head_bakeoff_arms a
    LEFT JOIN dedup_sim.tag_head_bakeoff_vectors v ON v.arm_id = a.id
    WHERE a.run_id = %(run_id)s
    GROUP BY a.arm, a.status, a.note
"""

_RUN_NOTE_SQL = """
    SELECT note FROM dedup_sim.tag_head_bakeoff_runs WHERE id = %(run_id)s
"""

# THE BOOTSTRAP'S OWN HEARTBEAT (2026-09-08 (g)). Handed to the pod in the REST body's
# `env` as HEARTBEAT_SQL and executed by `scripts/pod_report.py` on the image's Python
# after every bootstrap step — before this repo, the 3.12 venv or torch exist. It lives
# here, not in the pod script, because the reporter must stay lane-agnostic: it knows a
# note and a run id, never a table.
#
# LATEST-WINS, ONE LINE: the regexp drops any previous `pod step` line, so the note holds
# the newest step (or, from the EXIT trap, the failing step plus a tail of the bootstrap
# log) and never grows. Everything else — the manifest's text, the payload's own
# `pod booted` / `pod alive` lines — is preserved.
POD_HEARTBEAT_SQL = r"""
    UPDATE dedup_sim.tag_head_bakeoff_runs
    SET note = left(
        regexp_replace(coalesce(note, ''), '(^|\n)pod step [^\n]*', '', 'g')
        || E'\n' || %(note)s, 8000)
    WHERE id = %(run_id)s
"""

STEP_PREFIX = "pod step "

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)?")


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
    """The pod's argv. Carries no secrets — those travel in the REST body's `env`.

    The script itself (fetch by sha, bootstrap a 3.12, cu118 torch) is shared with the
    production lane in `scripts/pod_bootstrap.py`; both were broken the same way and
    must stay fixed the same way."""
    return pod_bootstrap.build_start_cmd(ref=ref, module=module,
                                         payload_args=payload_args, extra="clip")


def pod_env(run_id: int = 0) -> dict[str, str]:
    """The credentials present in this process's environment, forwarded to the pod —
    plus the heartbeat wiring the bootstrap reporter needs. That wiring is not a
    credential: an UPDATE and a row id, executed with the SUPABASE_DB_URL the pod
    already has."""
    env = {k: os.environ[k] for k in POD_ENV_KEYS if os.environ.get(k)}
    if run_id:
        env["HEARTBEAT_SQL"] = POD_HEARTBEAT_SQL
        env["HEARTBEAT_RUN_ID"] = str(run_id)
    return env


def _latest_iso(texts: Sequence[str | None]) -> str:
    """The newest ISO timestamp appearing anywhere in these notes, '' if none."""
    stamps = [m for text in texts if text for m in _ISO_RE.findall(text)]
    return max(stamps) if stamps else ""


def _last_step(run_note: str | None) -> str:
    """The bootstrap's newest step line, verbatim (JSON: timestamp, message, pod name,
    python version — and from the EXIT trap the code plus a tail of the bootstrap log).
    Empty before the first one."""
    for line in reversed((run_note or "").splitlines()):
        if line.startswith(STEP_PREFIX):
            return line[len(STEP_PREFIX):]
    return ""


def _boot_at(run_note: str | None) -> datetime | None:
    """When THIS payload said Python was up — the `pod booted <iso>` line the payload
    rewrites (never appends), so at most one can be present."""
    for line in (run_note or "").splitlines():
        if not line.startswith("pod booted "):
            continue
        match = _ISO_RE.search(line)
        if match:
            try:
                return datetime.fromisoformat(match.group(0).replace("Z", "+00:00"))
            except ValueError:
                return None
    return None


def read_bakeoff_progress(conn: Any, *, run_id: int, only: Sequence[str],
                          launched_at: datetime, baseline_vectors: int) -> Progress:
    """One watchdog reading, out of the rows the payload writes anyway.

    BOOTED is the payload's own boot stamp, dated AFTER this dispatch launched — a stale
    stamp from a previous run must not vouch for this pod. Vectors above the count at
    launch are accepted as a second, equally conclusive proof of life.

    The MARKER folds the vector count together with the newest heartbeat timestamp, so
    the phases that write no vectors (fetching the manifest, filling the image cache,
    downloading weights) still count as progress.
    """
    wanted = {a.strip() for a in only if a.strip()}
    with conn.cursor() as cur:
        cur.execute(_ARM_PROGRESS_SQL, {"run_id": run_id})
        rows = cur.fetchall()
        cur.execute(_RUN_NOTE_SQL, {"run_id": run_id})
        run_row = cur.fetchone()
    considered = [r for r in rows
                  if r[0] != STORED_CLIP_ARM and (not wanted or r[0] in wanted)]
    vectors = sum(int(r[3] or 0) for r in considered)
    terminal = bool(considered) and all(r[1] in TERMINAL_ARM_STATUSES
                                        for r in considered)
    run_note = run_row[0] if run_row else None
    heartbeat = _latest_iso([run_note] + [r[2] for r in considered])
    boot_at = _boot_at(run_note)
    fresh_boot = (boot_at is not None
                  and boot_at >= launched_at - timedelta(seconds=CLOCK_SKEW_GRACE_S))
    done = sum(1 for r in considered if r[1] in TERMINAL_ARM_STATUSES)
    # The bootstrap's step line carries its own ISO stamp, so `heartbeat` (and with it
    # the marker) advances through fetch/uv/venv/torch/repo — the phases that used to
    # look exactly like a dead pod from here.
    step = _last_step(run_note)
    return Progress(
        booted=fresh_boot or vectors > baseline_vectors,
        marker=f"{vectors}|{heartbeat}",
        terminal=terminal,
        detail=(f"{vectors} vectors, arms {done}/{len(considered)} terminal, "
                f"heartbeat {heartbeat or 'none'}"),
        step=step[:1500],
    )


def _connect(db_url: str) -> Any:
    import psycopg

    return psycopg.connect(db_url, autocommit=True, prepare_threshold=None,
                           connect_timeout=15)


def make_watchdog(args: argparse.Namespace, *, only: Sequence[str]) -> PodWatchdog | None:
    """The watchdog for this dispatch, or None when the runner cannot read the database
    (in which case the wait window is the only protection there is, loudly)."""
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        LOG.warning("SUPABASE_DB_URL is not set on the RUNNER — no watchdog, so a pod "
                    "that dies on boot bills the whole %.0fs window (2026-09-08)",
                    args.job_max_seconds + STARTUP_GRACE_S)
        return None
    launched_at = datetime.now(timezone.utc)
    baseline = 0
    try:
        with _connect(db_url) as conn:
            before = read_bakeoff_progress(conn, run_id=args.run_id, only=only,
                                           launched_at=launched_at, baseline_vectors=-1)
        baseline = int(before.marker.split("|", 1)[0])
    except Exception as exc:  # noqa: BLE001 - a baseline we cannot read is 0
        LOG.warning("could not read the vector baseline (assuming 0): %s", exc)

    def poll() -> Progress:
        with _connect(db_url) as conn:
            return read_bakeoff_progress(conn, run_id=args.run_id, only=only,
                                         launched_at=launched_at,
                                         baseline_vectors=baseline)

    LOG.info("watchdog: bootstrap_deadline=%.0fs stall_deadline=%.0fs poll=%.0fs "
             "baseline_vectors=%d", args.bootstrap_deadline_s, args.stall_deadline_s,
             DEFAULT_POLL_INTERVAL_S, baseline)
    return PodWatchdog(poll, bootstrap_deadline_s=args.bootstrap_deadline_s,
                       stall_deadline_s=args.stall_deadline_s,
                       poll_interval_s=DEFAULT_POLL_INTERVAL_S)


def plan_stage(args: argparse.Namespace) -> Plan:
    """Pure stage routing: what runs, where, and whether a dry run should execute it."""
    if args.stage == "manifest":
        argv = [sys.executable, "-m", "scripts.tagging_bakeoff_manifest"]
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
    env = pod_env(args.run_id)
    missing = [k for k in POD_ENV_KEYS if k not in env]
    allowlist = tuple(s.strip().lower() for s in args.gpu_allowlist.split(",")
                      if s.strip())
    LOG.info("image=%s ref=%s max_wait_s=%.0f gpu_allowlist=%s",
             args.image, args.ref, plan.max_wait_s, ",".join(allowlist) or "(none)")
    LOG.info("pod env keys present: %s", ",".join(sorted(env)) or "(none)")
    if missing:
        LOG.warning("pod env keys MISSING (the pod will no-op or fail): %s",
                    ",".join(missing))
    LOG.info("start_cmd:\n%s", plan.start_cmd[-1])

    if not plan.execute:
        # The cheap pre-flight: execute that exact script offline with every real step
        # stubbed — once clean, once with a forced failure — and confirm the EXIT trap
        # reports the failing step. A syntax error here would otherwise be discovered by
        # renting a GPU and waiting out a deadline (2026-09-08, twice).
        ok, lines = pod_bootstrap.preflight(plan.start_cmd[-1])
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

    only = [a.strip() for a in args.arms.split(",") if a.strip()]
    watchdog = make_watchdog(args, only=only)
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
            progress=watchdog,
        )
    except NoCapacityError as exc:
        LOG.error("no candidate GPU type had capacity: %s", exc)
        return 1
    except RunPodError as exc:
        LOG.error("dispatch failed (not a capacity issue): %s", exc)
        return 1

    LOG.info("pod %s torn down: gpu=%s status=%s timed_out=%s elapsed=%.0fs "
             "cost_per_hr=$%s%s", result.pod_id, result.gpu_type_id,
             result.final_status, result.timed_out, result.elapsed_s,
             result.cost_per_hr, _spend(result))
    if result.stop_reason:
        LOG.warning("the WATCHDOG ended this run: %s", result.stop_reason)
    if watchdog is not None and watchdog.last_step:
        LOG.info("last bootstrap step heartbeat seen: %s", watchdog.last_step)
    _log_final_note(args.run_id)
    LOG.info("A timed_out=True here is EXPECTED (on-demand Pods do not flip "
             "desiredStatus) — read progress from Postgres: select arm, status, dim, "
             "note from dedup_sim.tag_head_bakeoff_arms where run_id = %s.", args.run_id)
    # A watchdog teardown for a boot or stall case is a FAILED dispatch, and the lane
    # must go red: the alternative is a green run that embedded nothing, which is what
    # 2026-09-08 looked like in Actions.
    if result.stop_reason and not result.stop_reason.startswith("all-terminal"):
        return 1
    return 0


def _log_final_note(run_id: int) -> None:
    """Print the run row's note after teardown — the last step heartbeat and, when the
    pod's EXIT trap got there first, its exit code plus the tail of its bootstrap log.
    RunPod's Pod logs endpoint answers 400, so in a GitHub run this IS the pod's log.
    Best effort: a dispatch is never failed by an unreadable note."""
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        return
    try:
        with _connect(db_url) as conn, conn.cursor() as cur:
            cur.execute(_RUN_NOTE_SQL, {"run_id": run_id})
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - the teardown already happened
        LOG.warning("could not read the run note after teardown: %s", exc)
        return
    note = (row[0] if row else None) or "(empty)"
    LOG.info("run %s note after teardown:\n%s", run_id, note[:6000])


def _spend(result: Any) -> str:
    if result.cost_per_hr is None:
        return ""
    return f" spent≈${float(result.cost_per_hr) * result.elapsed_s / 3600.0:.2f}"


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
    p.add_argument("--heads", default="",
                   help="manifest: explicit tag ids, overriding the ready flag.")
    p.add_argument("--arms", default="",
                   help="Comma-separated arm names, to narrow any stage to a subset.")
    p.add_argument("--batch-size", type=int, default=32, help="embed: forward batch.")
    p.add_argument("--workers", type=int, default=16,
                   help="embed: parallel image downloads inside the pod.")
    p.add_argument("--job-max-seconds", type=float, default=7200,
                   help="embed: the payload's own budget. The pod's wait window is "
                        "this plus a 900s startup grace, so teardown lands after a "
                        "clean stop rather than mid-batch.")
    p.add_argument("--bootstrap-deadline-s", type=float,
                   default=DEFAULT_BOOTSTRAP_DEADLINE_S,
                   help="embed: tear the pod down if no NEW heartbeat reaches the "
                        "database within this long while the payload has yet to start. "
                        "Measured from the last bootstrap STEP heartbeat, so a slow "
                        "torch download keeps buying time and a dead pod does not.")
    p.add_argument("--stall-deadline-s", type=float, default=DEFAULT_STALL_DEADLINE_S,
                   help="embed: tear the pod down if a booted pod stops making "
                        "progress for this long.")
    p.add_argument("--ref", default=os.environ.get("GITHUB_SHA") or "main",
                   help="Git ref the pod fetches BY SHA. Defaults to GITHUB_SHA in "
                        "Actions.")
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
