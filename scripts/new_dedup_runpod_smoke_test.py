"""One-off proof that the RunPod pipeline works end-to-end: launch the cheapest
available GPU pod, run a trivial CUDA op, confirm it printed the expected marker,
and guarantee teardown either way. Not a recurring job — invoked manually via the
new_dedup_runpod_smoke_test workflow_dispatch, or `python -m scripts.new_dedup_runpod_smoke_test`
locally with RUNPOD_API_KEY set.

This is infrastructure validation only (docs/design/new-dedup/PROGRAM.md, Wave 1)
— it does not compute anything the simulation engine uses. Wave 5's real DINOv2
embedding batches will call scripts.runpod_client.RunPodClient.run_job the same
way, with a different image/start_cmd.
"""

from __future__ import annotations

import logging
import os
import sys

from scripts.runpod_client import RunPodClient, RunPodError

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
MAX_PRICE_PER_HR = 0.50  # sanity cap; the cheapest option today is well under this


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        LOG.error("RUNPOD_API_KEY not set")
        return 1

    client = RunPodClient(api_key)
    try:
        gpu = client.cheapest_gpu(max_price_per_hr=MAX_PRICE_PER_HR)
    except RunPodError as exc:
        LOG.error("could not pick a GPU: %s", exc)
        return 1
    LOG.info(
        "cheapest eligible GPU: %s (%s, %.2f GB, $%.3f/hr community)",
        gpu.id, gpu.display_name, gpu.memory_gb, gpu.community_price_per_hr,
    )

    try:
        result = client.run_job(
            name="new-dedup-smoke-test",
            image=IMAGE,
            gpu_type_id=gpu.id,
            start_cmd=START_CMD,
            max_wait_s=480,
            poll_interval_s=10,
        )
    except RunPodError as exc:
        LOG.error("job failed: %s", exc)
        return 1

    LOG.info(
        "pod %s finished: status=%s timed_out=%s elapsed=%.0fs cost_per_hr=%s",
        result.pod_id, result.final_status, result.timed_out,
        result.elapsed_s, result.cost_per_hr,
    )
    LOG.info("--- logs ---\n%s\n--- end logs ---", result.logs)

    if result.timed_out:
        LOG.error("pod never exited within the wait window (pod was still terminated)")
        return 1
    if SUCCESS_MARKER not in result.logs:
        LOG.error("success marker %r not found in logs", SUCCESS_MARKER)
        return 1

    est_cost_usd = (
        (result.elapsed_s / 3600.0) * result.cost_per_hr if result.cost_per_hr else None
    )
    LOG.info(
        "SMOKE TEST PASSED — estimated cost ~$%.4f",
        est_cost_usd if est_cost_usd is not None else float("nan"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
