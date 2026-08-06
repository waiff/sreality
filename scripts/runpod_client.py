"""Thin RunPod API client for the NEW DEDUP program's on-demand GPU jobs
(docs/design/new-dedup/PROGRAM.md, Wave 1: "RunPod account (operator) + serverless
workflow (me)"; Wave 5 will run real DINOv2 embedding batches through this same
client). Reusable across whatever job a wave needs — this module only knows how
to launch a pod, wait for it, collect its logs, and guarantee teardown; it has no
opinion on what the pod actually computes.

Two APIs: the REST API (https://rest.runpod.io/v1) for pod CRUD, and the GraphQL
API (https://api.runpod.io/graphql) for the read-only GPU catalog — RunPod splits
these itself, this client mirrors the split rather than hiding it.

Cost safety is the point of this module, not an afterthought: `run_job` launches
in a `try` and terminates in `finally`, so a pod is torn down whether the job
succeeds, its own request fails, or it times out waiting. RunPod's pod API has no
documented "run once and stop" flag, so a job that errors after start would
otherwise keep billing indefinitely — the bounded `wait_for_exit` timeout plus the
unconditional `finally` terminate is the actual guarantee, not the container's own
exit behavior.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

LOG = logging.getLogger(__name__)

REST_BASE = "https://rest.runpod.io/v1"
GRAPHQL_URL = "https://api.runpod.io/graphql"
_TERMINAL_STATUSES = {"EXITED", "TERMINATED"}


class RunPodError(RuntimeError):
    pass


@dataclass(frozen=True)
class GpuOption:
    id: str
    display_name: str
    memory_gb: float
    community_price_per_hr: float


@dataclass(frozen=True)
class JobResult:
    pod_id: str
    gpu_type_id: str
    final_status: str
    timed_out: bool
    logs: str
    elapsed_s: float
    cost_per_hr: float | None


class RunPodClient:
    def __init__(self, api_key: str, *, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"

    def cheapest_gpu(self, *, max_price_per_hr: float | None = None) -> GpuOption:
        """The lowest community-cloud-priced GPU type, optionally capped by price.
        Queries live rather than hardcoding an ID — RunPod's catalog and pricing
        both shift with supply/demand. `communityPrice <= 0` is excluded, not just
        `None` — a real live run (2026-08-06) hit a catalog entry with id
        "unknown" and `communityPrice: 0`, a placeholder/unavailable listing that
        a `None`-only filter let through and that then always "won" as cheapest
        since 0 beats every real price."""
        query = (
            "query { gpuTypes { id displayName memoryInGb communityPrice } }"
        )
        resp = self._session.post(GRAPHQL_URL, json={"query": query}, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RunPodError(f"gpuTypes query failed: {body['errors']}")
        options = [
            GpuOption(
                id=g["id"],
                display_name=g["displayName"],
                memory_gb=g["memoryInGb"],
                community_price_per_hr=g["communityPrice"],
            )
            for g in body["data"]["gpuTypes"]
            if (g.get("communityPrice") or 0) > 0
        ]
        if max_price_per_hr is not None:
            options = [o for o in options if o.community_price_per_hr <= max_price_per_hr]
        if not options:
            raise RunPodError("no GPU type available under the given price cap")
        return min(options, key=lambda o: o.community_price_per_hr)

    def launch_pod(
        self,
        *,
        name: str,
        image: str,
        gpu_type_id: str,
        start_cmd: list[str],
        container_disk_gb: int = 10,
        volume_gb: int = 1,
        cloud_type: str = "COMMUNITY",
    ) -> dict[str, Any]:
        body = {
            "name": name,
            "imageName": image,
            "gpuTypeIds": [gpu_type_id],
            "gpuCount": 1,
            "cloudType": cloud_type,
            "computeType": "GPU",
            "containerDiskInGb": container_disk_gb,
            "volumeInGb": volume_gb,
            "dockerStartCmd": start_cmd,
            "interruptible": False,
        }
        resp = self._session.post(f"{REST_BASE}/pods", json=body, timeout=30)
        if resp.status_code >= 400:
            raise RunPodError(f"pod launch failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def get_pod(self, pod_id: str) -> dict[str, Any]:
        resp = self._session.get(f"{REST_BASE}/pods/{pod_id}", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def terminate_pod(self, pod_id: str) -> None:
        resp = self._session.delete(f"{REST_BASE}/pods/{pod_id}", timeout=30)
        if resp.status_code not in (200, 202, 204, 404):
            # 404 = already gone (e.g. RunPod's own cleanup beat us to it) — not an error.
            raise RunPodError(f"pod terminate failed ({resp.status_code}): {resp.text}")

    def wait_for_exit(
        self, pod_id: str, *, max_wait_s: float, poll_interval_s: float = 10.0
    ) -> tuple[str, bool]:
        """(final desiredStatus, timed_out). Polls until the pod reports EXITED/
        TERMINATED or max_wait_s elapses — never raises on timeout, the caller
        decides whether a timeout is a failure."""
        deadline = time.monotonic() + max_wait_s
        status = "UNKNOWN"
        while time.monotonic() < deadline:
            pod = self.get_pod(pod_id)
            status = pod.get("desiredStatus", "UNKNOWN")
            if status in _TERMINAL_STATUSES:
                return status, False
            time.sleep(poll_interval_s)
        return status, True

    def fetch_logs(self, pod_id: str, *, max_lines: int = 500, read_timeout_s: float = 10.0) -> str:
        """Best-effort log fetch over the SSE logs endpoint. Logs are diagnostic,
        not load-bearing — any read/parse failure returns what was collected
        rather than raising, so a logging hiccup never masks whether the job
        itself (tracked via desiredStatus) actually finished."""
        lines: list[str] = []
        try:
            resp = self._session.get(
                f"{REST_BASE}/pods/{pod_id}/logs", stream=True, timeout=read_timeout_s
            )
            resp.raise_for_status()
            deadline = time.monotonic() + read_timeout_s
            for raw in resp.iter_lines(decode_unicode=True):
                if time.monotonic() > deadline or len(lines) >= max_lines:
                    break
                if not raw or not raw.startswith("data:"):
                    continue
                lines.append(raw[len("data:"):].strip())
        except requests.RequestException as exc:
            LOG.warning("log fetch for pod %s failed (non-fatal): %s", pod_id, exc)
        finally:
            try:
                resp.close()  # type: ignore[possibly-undefined]
            except Exception:  # noqa: BLE001 - best-effort cleanup of the stream
                pass
        return "\n".join(lines)

    def run_job(
        self,
        *,
        name: str,
        image: str,
        gpu_type_id: str,
        start_cmd: list[str],
        max_wait_s: float = 600.0,
        poll_interval_s: float = 10.0,
        container_disk_gb: int = 10,
        volume_gb: int = 1,
    ) -> JobResult:
        """Launch → wait → collect logs → ALWAYS terminate, even if a step above
        raises. This is the one entry point every wave's RunPod usage should go
        through, so the teardown guarantee is enforced once, not re-implemented
        per caller."""
        t0 = time.monotonic()
        pod = self.launch_pod(
            name=name,
            image=image,
            gpu_type_id=gpu_type_id,
            start_cmd=start_cmd,
            container_disk_gb=container_disk_gb,
            volume_gb=volume_gb,
        )
        pod_id = pod["id"]
        cost_per_hr = pod.get("costPerHr")
        LOG.info("launched pod %s (%s, $%s/hr)", pod_id, gpu_type_id, cost_per_hr)
        try:
            status, timed_out = self.wait_for_exit(
                pod_id, max_wait_s=max_wait_s, poll_interval_s=poll_interval_s
            )
            logs = self.fetch_logs(pod_id)
            return JobResult(
                pod_id=pod_id,
                gpu_type_id=gpu_type_id,
                final_status=status,
                timed_out=timed_out,
                logs=logs,
                elapsed_s=time.monotonic() - t0,
                cost_per_hr=cost_per_hr,
            )
        finally:
            LOG.info("terminating pod %s", pod_id)
            self.terminate_pod(pod_id)
