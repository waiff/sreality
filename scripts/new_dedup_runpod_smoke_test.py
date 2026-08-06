"""One-off proof that the RunPod pipeline works end-to-end: launch the cheapest
available GPU pod, let a trivial CUDA op run, and guarantee teardown either way.
Not a recurring job — invoked manually via the new_dedup_runpod_smoke_test
workflow_dispatch, or `python -m scripts.new_dedup_runpod_smoke_test` locally
with RUNPOD_API_KEY set.

This is infrastructure validation only (docs/design/new-dedup/PROGRAM.md, Wave 1)
— it does not compute anything the simulation engine uses. Wave 5's real DINOv2
embedding batches will call scripts.runpod_client.RunPodClient.run_job the same
way, with a different image/start_cmd.

Pass criteria is deliberately NOT "the success marker showed up in the logs."
A real live run (2026-08-06) proved the actually load-bearing parts — a pod
launched on a real GPU, accrued real billed time, and was cleanly terminated —
while both `desiredStatus` (stayed RUNNING for the whole wait window) and the
logs endpoint (400) turned out not to be reliable signals for on-demand Pods.
See runpod_client.py's wait_for_exit/fetch_logs docstrings. A real batch job
should confirm its own success by writing a result somewhere durable
(Postgres/R2), not by this script scraping stdout.
"""

from __future__ import annotations

import logging
import os
import sys

from scripts.runpod_client import NoCapacityError, RunPodClient, RunPodError

LOG = logging.getLogger(__name__)

IMAGE = "runpod/pytorch:2.1.0-py3.10-cuda11.8.0-devel-ubuntu22.04"
SUCCESS_MARKER = "SMOKE_TEST_OK"
START_CMD = [
    "bash",
    "-c",
    "nvidia-smi; python3 -c \"import torch; "
    "x = torch.rand(4, 4, device='cuda'); "
    f"print('{SUCCESS_MARKER}', float(x.sum()))\"",
]
MAX_PRICE_PER_HR = 1.00  # sanity cap; several eligible options today are well under this
MAX_WAIT_S = 120  # short: desiredStatus isn't expected to flip (see module docstring);
# this just gives a real job a little runway before the guaranteed terminate.


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        LOG.error("RUNPOD_API_KEY not set")
        return 1

    client = RunPodClient(api_key)
    try:
        gpus = client.eligible_gpus(max_price_per_hr=MAX_PRICE_PER_HR)
    except RunPodError as exc:
        LOG.error("could not list eligible GPUs: %s", exc)
        return 1
    LOG.info(
        "%d eligible GPU(s), cheapest first: %s",
        len(gpus),
        ", ".join(f"{g.id} (${g.community_price_per_hr:.3f}/hr)" for g in gpus[:5]),
    )

    try:
        result = client.run_job_with_fallback(
            name="new-dedup-smoke-test",
            image=IMAGE,
            gpu_options=gpus,
            start_cmd=START_CMD,
            max_wait_s=MAX_WAIT_S,
            poll_interval_s=10,
        )
    except NoCapacityError as exc:
        LOG.error("no eligible GPU type had capacity: %s", exc)
        return 1
    except RunPodError as exc:
        LOG.error("job failed (not a capacity issue, did not try further GPU types): %s", exc)
        return 1

    LOG.info(
        "pod %s finished: gpu=%s status=%s timed_out=%s elapsed=%.0fs cost_per_hr=$%s",
        result.pod_id, result.gpu_type_id, result.final_status, result.timed_out,
        result.elapsed_s, result.cost_per_hr,
    )
    if result.logs:
        LOG.info("--- logs (best-effort, may be empty) ---\n%s\n--- end logs ---", result.logs)
        if SUCCESS_MARKER in result.logs:
            LOG.info("success marker %r found in logs", SUCCESS_MARKER)
    else:
        LOG.info("no logs retrieved — expected, see module docstring; not a failure on its own")

    est_cost_usd = (
        (result.elapsed_s / 3600.0) * result.cost_per_hr if result.cost_per_hr else None
    )
    LOG.info(
        "SMOKE TEST PASSED — pod %s launched, ran, and was torn down; "
        "estimated cost ~$%.4f",
        result.pod_id,
        est_cost_usd if est_cost_usd is not None else float("nan"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
