"""Rent a GPU pod that serves an open-source vision model behind an OpenAI-compatible
endpoint, so the judge lane can put a free model on the SAME pairs the paid judge already
answered (the point of the comparison: the paid verdicts yield value twice, and at full
scale the vision tier is the dominant LLM cost).

The pod runs `vllm/vllm-openai`, whose entrypoint IS an OpenAI Chat Completions server —
nothing is installed at boot, no repo is cloned, no payload script exists. That is the whole
reason this lane does not reuse `scripts/pod_bootstrap.py`: there is no bootstrap.

Four things learned the expensive way, encoded here rather than re-learned:

  * A POD NEVER SAYS "I AM DONE" OR "I AM UP". `desiredStatus` stayed RUNNING through every
    live wait window (2026-08-06) and `GET /pods/{id}/logs` 400s, so readiness CANNOT be read
    from the pod record. `wait_ready` polls the SERVER instead — `GET /v1/models` through
    RunPod's HTTP proxy — which is the only signal that means what it says.
  * THE BILL STARTS AT LAUNCH, NOT AT READY. A pod that never serves still bills for every
    second it existed (8,115 s for zero output, 2026-09-08), so `wait_ready` terminates on its
    own deadline and `pod_cost_usd` prices the whole window, not the useful part. Hold a pod
    through `rented_pod` so teardown is a `finally` nobody can forget — and `wait_ready`
    terminates on cancellation too, because a KeyboardInterrupt in the boot window is not an
    `Exception` and would otherwise walk straight past the caller's handler.
  * THE WORKING DISK IS THE CONTAINER DISK. `/workspace` is a separate volume that defaulted to
    1 GB; the model weights (~16 GB for a 7B VL model in bf16, plus the image) land on the
    container disk. Hence 80 GB container / 0 volume — never "fix" a disk-full boot by renting
    volume.
  * THE GPU IS NOT PICKED ON PRICE ALONE. A 7B VL model at bf16 with a 12k context needs 24 GB+,
    and the 4090 is excluded for the same reason ENCODER-DECISION §5.3 excludes it (6 vCPU:
    image decode is CPU-side). The preference list is that judgement, applied cheapest-first.

Nothing here judges a pair or talks to Postgres: it launches, proves the server answers, and
guarantees teardown. `--dry-run` prints the exact launch request and rents nothing.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import requests

from scripts.runpod_client import CLOUD_TYPES, GpuOption, NoCapacityError, RunPodError

LOG = logging.getLogger(__name__)

DEFAULT_MODEL_ID: str = "Qwen/Qwen2.5-VL-7B-Instruct"
DEFAULT_IMAGE: str = "vllm/vllm-openai:latest"
# Ordered preference, matched case-insensitively against the live catalog's id and display
# name. 24 GB+ community boxes, cheapest-first within what is actually available. The 4090 is
# NOT here on purpose (see the module docstring); `EXCLUDED_GPU_PATTERNS` enforces that even
# when the fallback path widens the search.
DEFAULT_GPU_PREFERENCE: tuple[str, ...] = ("a5000", "3090", "a40", "a6000")
EXCLUDED_GPU_PATTERNS: tuple[str, ...] = ("4090",)
MIN_GPU_MEMORY_GB: float = 24.0
MAX_PRICE_PER_HR: float = 1.00
# Where to look, in order. COMMUNITY alone is what this lane shipped with; the big cards a
# 30B VL model needs (H100, A100 80GB) sit mostly in SECURE, so the cloud is now a LIST the
# caller orders — availability first or price first, its choice, never a silent substitution.
DEFAULT_CLOUD_TYPES: tuple[str, ...] = ("COMMUNITY",)

HTTP_PORT: int = 8000
# RunPod fronts an exposed HTTP port at this host; there is no other way in (no public IP on
# community cloud).
PROXY_HOST_TEMPLATE: str = "https://{pod_id}-{port}.proxy.runpod.net"
# Image + weights + HF cache, on ONE disk (volume is 0 and must stay 0 — see the docstring):
# `vllm/vllm-openai` unpacks to ~20-25 GB (CUDA, torch, flash-attn), Qwen2.5-VL-7B bf16
# safetensors are ~16.6 GB, and the HF download writes the shards before it links them. 40 GB
# left no margin, and a disk-full boot is INDISTINGUISHABLE from a hung one: the only detector
# is `wait_ready`'s deadline, i.e. the full rental. Disk is a fraction of a cent per pod-hour.
# Re-do this sum when the model changes; never inherit the number.
CONTAINER_DISK_GB: int = 80
VOLUME_GB: int = 0

READY_DEADLINE_S: float = 1500.0
READY_POLL_INTERVAL_S: float = 15.0
READY_REQUEST_TIMEOUT_S: float = 20.0
SMOKE_TIMEOUT_S: float = 120.0

# Written into the run's artifact directory the instant a pod exists, removed when it is
# confirmed gone. See `write_receipt`.
RECEIPT_FILE: str = "pod.json"

DEFAULT_MAX_MODEL_LEN: int = 12288
DEFAULT_MAX_IMAGES: int = 8
DEFAULT_GPU_MEMORY_UTILIZATION: float = 0.92
DEFAULT_DTYPE: str = "bfloat16"
# vLLM's parser for Qwen's Hermes-style tool-call tags; `--enable-auto-tool-choice` is what
# turns the parser on at all (docs.vllm.ai/en/latest/features/tool_calling: "Hermes Models
# ... Qwen2.5 ... --enable-auto-tool-choice --tool-call-parser hermes").
DEFAULT_TOOL_CALL_PARSER: str = "hermes"

SMOKE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_smoke",
        "description": "Report what the single supplied image looks like. Call exactly once.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "seen": {"type": "string", "description": "One word for the image."},
            },
            "required": ["seen"],
        },
    },
}


class PodBootstrapError(RuntimeError):
    """The pod was rented but never served — the one failure this lane must charge for and
    surface, because the rental already happened."""


@dataclass(frozen=True)
class PodHandle:
    pod_id: str
    base_url: str
    gpu: str
    usd_per_hr: float
    started_at: float
    model_id: str = DEFAULT_MODEL_ID
    dry_run: bool = False
    cloud_type: str = "COMMUNITY"


def build_vllm_args(
    *,
    model_id: str = DEFAULT_MODEL_ID,
    max_model_len: int = DEFAULT_MAX_MODEL_LEN,
    max_images: int = DEFAULT_MAX_IMAGES,
    gpu_memory_utilization: float = DEFAULT_GPU_MEMORY_UTILIZATION,
    dtype: str = DEFAULT_DTYPE,
    tool_call_parser: str = DEFAULT_TOOL_CALL_PARSER,
    command_prefix: Sequence[str] = (),
) -> list[str]:
    """The container's argv tail. The image's ENTRYPOINT is already the API server, so these
    are FLAGS, not a command — `command_prefix` exists for the case where RunPod's
    `dockerStartCmd` turns out to replace the entrypoint rather than its arguments, in which
    case pass ("vllm", "serve", model_id) and drop nothing else.

    `--served-model-name` pins the id clients must send, so the wire id never drifts with the
    weights path. `--limit-mm-per-prompt` must be >= the judge's images-per-call or the server
    rejects the request outright rather than truncating it, and it is spelled as JSON: the
    comma-separated `image=N` form was deprecated and then removed, and the failure mode of the
    wrong spelling is the expensive one — argparse exits, nothing binds, and only `wait_ready`'s
    deadline notices, 25 minutes of rental later. These go over as an argv LIST, so the JSON
    needs no shell quoting.

    `--host`/`--port` are pinned rather than left to the default, because the exposed port, the
    proxy URL and the readiness poll all read `HTTP_PORT` and the server must read the same
    constant."""
    return [
        *command_prefix,
        "--model", model_id,
        "--max-model-len", str(max_model_len),
        "--limit-mm-per-prompt", json.dumps({"image": max_images}),
        "--dtype", dtype,
        "--gpu-memory-utilization", str(gpu_memory_utilization),
        "--enable-auto-tool-choice",
        "--tool-call-parser", tool_call_parser,
        "--served-model-name", model_id,
        "--host", "0.0.0.0",
        "--port", str(HTTP_PORT),
    ]


def select_gpus(
    client: Any,
    preference: Sequence[str] = DEFAULT_GPU_PREFERENCE,
    *,
    strict: bool = False,
    cloud_type: str = "COMMUNITY",
    max_price_per_hr: float = MAX_PRICE_PER_HR,
    min_memory_gb: float = MIN_GPU_MEMORY_GB,
) -> list[GpuOption]:
    """24 GB+ community boxes, preferred types first and cheapest-first inside each rung.

    An unlisted-but-eligible type is kept as a tail fallback rather than failing the launch —
    community supply is peer-hosted and the preferred rung can be empty at any moment — but an
    EXCLUDED type is dropped from both rungs, never fallen back to.

    `strict` turns the preference into a PIN: when the operator names a GPU type, a silent
    substitution can be a 6x price change on an hourly-billed arm whose whole point is cost
    ($0.16/hr A5000 out of capacity -> $0.98/hr on the widened rung), so a pin that cannot be
    filled fails loudly instead."""
    options = [
        g for g in client.eligible_gpus(
            max_price_per_hr=max_price_per_hr, cloud_type=cloud_type
        )
        if g.memory_gb >= min_memory_gb and not _matches(g, EXCLUDED_GPU_PATTERNS)
    ]
    if strict and preference:
        options = [g for g in options if _matches(g, preference)]
        if not options:
            raise RunPodError(
                f"no eligible {cloud_type} GPU matches the pinned preference "
                f"{list(preference)} (strict); drop the pin to allow any "
                f">={min_memory_gb:.0f} GB box"
            )
    if not options:
        raise RunPodError(
            f"no {cloud_type} GPU with >={min_memory_gb:.0f} GB under "
            f"${max_price_per_hr:.2f}/hr is listed right now"
        )
    ranks = {pattern.lower(): i for i, pattern in enumerate(preference)}

    def rank(gpu: GpuOption) -> tuple[int, float]:
        hits = [ranks[p] for p in ranks if _matches(gpu, (p,))]
        return (min(hits) if hits else len(ranks), gpu.price_per_hr(cloud_type))

    return sorted(options, key=rank)


def _matches(gpu: GpuOption, patterns: Sequence[str]) -> bool:
    """Substring match over id AND display name, with `_` read as a space: RunPod's own GPU
    ids are written both ways ("H100_80GB" in the console, "NVIDIA H100 80GB HBM3" in the
    catalog), and an operator who types one and silently matches nothing gets the default
    rung at the default price."""
    haystack = f"{gpu.id} {gpu.display_name}".lower()
    return any(p.lower().replace("_", " ") in haystack for p in patterns)


def build_launch_request(
    *,
    name: str,
    image: str,
    gpu_type_id_preference: Sequence[str],
    start_cmd: list[str],
    env_keys: Sequence[str] = (),
    cloud_types: Sequence[str] = DEFAULT_CLOUD_TYPES,
    container_disk_gb: int = CONTAINER_DISK_GB,
) -> dict[str, Any]:
    """What `launch_pod` will send, minus the secret VALUES — for the dry run and the log
    line. Env names only ever appear here; values never do.

    The GPU field is named `gpu_type_id_preference` because that is what it holds in a dry run:
    the match PATTERNS ("a5000"), not the catalog id the live path sends ("NVIDIA RTX A5000").
    Printing a pattern under the live key would make the dry run advertise a request RunPod
    would reject, and the dry run is the one artifact read before spending."""
    return {
        "name": name,
        "image": image,
        "gpu_type_id_preference": list(gpu_type_id_preference),
        "cloud_types": list(cloud_types),
        "ports": [f"{HTTP_PORT}/http"],
        "container_disk_gb": container_disk_gb,
        "volume_gb": VOLUME_GB,
        "start_cmd": start_cmd,
        "env_keys": list(env_keys),
    }


def launch_vllm_pod(
    client: Any,
    *,
    model_id: str = DEFAULT_MODEL_ID,
    gpu_preference: Sequence[str] = DEFAULT_GPU_PREFERENCE,
    strict_gpu: bool = False,
    cloud_types: Sequence[str] = DEFAULT_CLOUD_TYPES,
    max_price_per_hr: float = MAX_PRICE_PER_HR,
    image: str = DEFAULT_IMAGE,
    max_model_len: int = DEFAULT_MAX_MODEL_LEN,
    max_images: int = DEFAULT_MAX_IMAGES,
    tool_call_parser: str = DEFAULT_TOOL_CALL_PARSER,
    container_disk_gb: int = CONTAINER_DISK_GB,
    hf_token: str | None = None,
    dry_run: bool = False,
    name: str = "autodedup-oss-judge",
    command_prefix: Sequence[str] = (),
    now: Callable[[], float] = time.time,
) -> PodHandle:
    """Rent one pod serving `model_id`, cheapest eligible GPU with capacity first.

    `cloud_types` is walked in the order given, the whole GPU list inside each: community
    supply is peer-hosted and the big cards a 30B model needs are usually only in secure, so
    "secure, then community" and "community, then secure" are both legitimate orders and the
    caller states which it wants. Capacity is the only thing that advances a rung.

    Only `NoCapacityError` advances to the next GPU type; anything else (bad image, auth, a
    balance too low to rent) is not GPU-specific and fails the launch immediately rather than
    burning the whole list on something a retry cannot fix.

    `started_at` is stamped BEFORE the pod can serve, because that is when billing starts.

    `hf_token` falls back to the AMBIENT HF_TOKEN: the default model is public, but a gated one
    (`meta-llama/…`) 401s on the weights, never binds, and is caught only by the readiness
    deadline — a whole rental for a secret that was sitting in the environment. It travels in
    `env`, never in argv (a start_cmd is visible in the pod record)."""
    start_cmd = build_vllm_args(
        model_id=model_id,
        max_model_len=max_model_len,
        max_images=max_images,
        tool_call_parser=tool_call_parser,
        command_prefix=command_prefix,
    )
    clouds = tuple(c.upper() for c in cloud_types) or DEFAULT_CLOUD_TYPES
    for cloud in clouds:
        if cloud not in CLOUD_TYPES:
            raise RunPodError(f"unknown cloud type {cloud!r}; expected {CLOUD_TYPES}")
    token = hf_token or os.environ.get("HF_TOKEN") or ""
    env = {"HF_TOKEN": token} if token else None

    if dry_run:
        request = build_launch_request(
            name=name, image=image, gpu_type_id_preference=gpu_preference,
            start_cmd=start_cmd, env_keys=sorted(env or ()),
            cloud_types=clouds, container_disk_gb=container_disk_gb,
        )
        LOG.info("DRY RUN launch request: %s", json.dumps(request, sort_keys=True))
        return PodHandle(
            pod_id="dry-run", base_url="https://dry-run.invalid", gpu="dry-run",
            usd_per_hr=0.0, started_at=0.0, model_id=model_id, dry_run=True,
            cloud_type=clouds[0],
        )

    last_error: BaseException | None = None
    for cloud in clouds:
        try:
            options = select_gpus(
                client, gpu_preference, strict=strict_gpu, cloud_type=cloud,
                max_price_per_hr=max_price_per_hr,
            )
        except RunPodError as exc:
            # Nothing of this size is even LISTED in this cloud: not a capacity miss, but it
            # must not end the search while another cloud is still to try.
            LOG.warning("no eligible GPU listed in %s: %s", cloud, exc)
            last_error = exc
            continue
        for gpu in options:
            price = gpu.price_per_hr(cloud)
            LOG.info("launching %s on %s/%s ($%.3f/hr)", model_id, cloud, gpu.id, price)
            try:
                pod = client.launch_pod(
                    name=name,
                    image=image,
                    gpu_type_id=gpu.id,
                    start_cmd=start_cmd,
                    container_disk_gb=container_disk_gb,
                    volume_gb=VOLUME_GB,
                    cloud_type=cloud,
                    env=env,
                    ports=[f"{HTTP_PORT}/http"],
                )
            except NoCapacityError as exc:
                LOG.warning("no capacity for %s/%s, trying the next option: %s",
                            cloud, gpu.id, exc)
                last_error = exc
                continue
            pod_id = str(pod["id"])
            handle = PodHandle(
                pod_id=pod_id,
                base_url=PROXY_HOST_TEMPLATE.format(pod_id=pod_id, port=HTTP_PORT),
                gpu=gpu.id,
                usd_per_hr=float(pod.get("costPerHr") or price),
                started_at=now(),
                model_id=model_id,
                cloud_type=cloud,
            )
            LOG.info("pod %s up at %s ($%.3f/hr, %s)",
                     handle.pod_id, handle.base_url, handle.usd_per_hr, cloud)
            return handle
    assert last_error is not None
    raise last_error


@contextlib.contextmanager
def rented_pod(client: Any, **kwargs: Any) -> Iterator[PodHandle]:
    """The only shape in which a caller should hold a pod: the handle exists before any wait,
    and teardown is a `finally` no caller can forget or get wrong.

    `launch_vllm_pod` hands the handle BACK, which means the window between "rented" and
    "assigned to the variable the caller's finally reads" is the caller's problem — and in the
    one incident that matters it was up to 25 minutes of readiness polling with `pod = None` in
    the enclosing scope. Here that window does not exist, and `finally` catches BaseException
    (cancellation) as well as errors."""
    handle = launch_vllm_pod(client, **kwargs)
    try:
        yield handle
    finally:
        terminate(handle, client=client)


def wait_ready(
    handle: PodHandle,
    *,
    client: Any,
    deadline_s: float = READY_DEADLINE_S,
    session: Any = None,
    poll_interval_s: float = READY_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    """Poll `GET {base_url}/v1/models` until it 200s WITH the served model listed; return the
    elapsed seconds. A 200 listing some other id is not readiness — a mismatched
    `--served-model-name` would otherwise pass here and 404 on every judged pair.

    NOTHING LEAVES THIS FUNCTION WITH THE POD ALIVE. The deadline, an unexpected payload shape,
    and a Ctrl-C / Actions cancellation (KeyboardInterrupt is a BaseException — `except
    Exception` in the caller does not see it) all terminate on the way out. This is the longest
    window in the lane by far, and a pod abandoned here bills until somebody notices."""
    http = session or requests
    url = f"{handle.base_url}/v1/models"
    t0 = clock()
    last = "no response yet"
    try:
        while True:
            elapsed = clock() - t0
            if elapsed >= deadline_s:
                raise PodBootstrapError(
                    f"pod {handle.pod_id} did not serve {handle.model_id} within "
                    f"{elapsed:.0f}s (last: {last}); terminated"
                )
            try:
                resp = http.get(url, timeout=READY_REQUEST_TIMEOUT_S)
                if resp.status_code == 200:
                    served = _served_ids(resp.json())
                    if handle.model_id in served:
                        LOG.info("pod %s ready after %.0fs", handle.pod_id, elapsed)
                        return elapsed
                    last = f"HTTP 200 but served={sorted(served)}"
                else:
                    last = f"HTTP {resp.status_code}"
            except requests.RequestException as exc:
                # Expected for most of the boot: the proxy 502s until the server binds.
                # (requests' JSONDecodeError subclasses this, so a non-JSON 200 lands here too.)
                last = f"{type(exc).__name__}: {exc}"
            sleep(poll_interval_s)
    except BaseException:
        terminate(handle, client=client)
        raise


def _served_ids(payload: Any) -> set[str]:
    """`/v1/models` answered 200 — but a half-booted server or a proxy shim can answer 200 with
    a list, a string, or `{"data": ["x"]}`. Coerced rather than indexed, so a surprise shape is
    "not ready yet", not an AttributeError out of a polling loop."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return set()
    return {str(row.get("id")) for row in data if isinstance(row, dict)}


def smoke_chat(handle: PodHandle, *, session: Any = None) -> dict[str, Any]:
    """One tiny forced-tool vision call. Proves the three things the judge actually depends on
    — the endpoint answers, it accepts an image part, and `--tool-call-parser` turns the reply
    into a parsable tool call — for a fraction of a cent of pod time, BEFORE a pass of real
    pairs discovers it the expensive way. Returns the parsed tool arguments."""
    http = session or requests
    body = {
        "model": handle.model_id,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Call record_smoke with one word for this image."},
                {"type": "image_url", "image_url": {"url": _smoke_image_data_uri()}},
            ],
        }],
        "tools": [SMOKE_TOOL],
        "tool_choice": {"type": "function", "function": {"name": SMOKE_TOOL["function"]["name"]}},
        "max_tokens": 128,
    }
    try:
        resp = http.post(
            f"{handle.base_url}/v1/chat/completions", json=body, timeout=SMOKE_TIMEOUT_S
        )
    except requests.RequestException as exc:
        raise PodBootstrapError(f"smoke call to pod {handle.pod_id} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise PodBootstrapError(
            f"smoke call to pod {handle.pod_id} failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
    args = _first_tool_arguments(resp.json())
    if args is None:
        raise PodBootstrapError(
            f"pod {handle.pod_id} answered the smoke call without a parsable tool call "
            f"(tool-call parser wrong or unsupported): {str(resp.json())[:300]}"
        )
    return args


def _first_tool_arguments(payload: dict[str, Any]) -> dict[str, Any] | None:
    for choice in payload.get("choices") or []:
        for call in (choice.get("message") or {}).get("tool_calls") or []:
            raw = (call.get("function") or {}).get("arguments")
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str) and raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    return None
                if isinstance(parsed, dict):
                    return parsed
    return None


def _smoke_image_data_uri(size: int = 64) -> str:
    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (size, size), (130, 140, 150)).save(buf, format="JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def terminate(handle: PodHandle, *, client: Any) -> None:
    """Always called from a `finally`, so it NEVER raises: a teardown error must not mask the
    error that sent us here, and `terminate_pod` already treats an already-gone pod as success.
    A failure here is logged at ERROR because it is the one case that keeps billing."""
    if handle.dry_run:
        return
    try:
        client.terminate_pod(handle.pod_id)
        LOG.info("terminated pod %s", handle.pod_id)
    except Exception as exc:  # noqa: BLE001 - see docstring: teardown never raises
        LOG.error("POD %s MAY STILL BE BILLING — terminate failed: %s", handle.pod_id, exc)


def pod_cost_usd(handle: PodHandle, now: float) -> float:
    """Wall-clock rental cost so far. Prices the WHOLE window — boot, idle and judged pairs
    alike — because that is what RunPod bills; a per-judgement cost that only counted serving
    time would understate the comparison against the paid judge, which is the whole point."""
    return round(max(now - handle.started_at, 0.0) / 3600.0 * handle.usd_per_hr, 6)


# --- the receipt: what survives the process being killed -----------------------------------


def write_receipt(handle: PodHandle, out_dir: Path) -> Path:
    """Name the rented pod in the run's ARTIFACT, immediately, before the long wait.

    Every teardown in this module hangs off a `finally`, and a `finally` is exactly what a
    cancelled Actions job or a `timeout-minutes` kill does not run: the process dies, the pod
    keeps billing, and the only record of its id is a log line in a run nobody re-reads. The
    artifact upload step runs `if: always()`, so a file dropped here reaches the operator even
    from a killed job — and `reap_receipt` turns it back into a DELETE.

    Written at LAUNCH, not at ready: the readiness wait is up to 25 minutes and is therefore
    the likeliest window to be cancelled in."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / RECEIPT_FILE
    path.write_text(json.dumps({
        "pod_id": handle.pod_id,
        "gpu": handle.gpu,
        "usd_per_hr": handle.usd_per_hr,
        "base_url": handle.base_url,
        "model_id": handle.model_id,
        "cloud_type": handle.cloud_type,
        "started_at": handle.started_at,
    }, indent=2, sort_keys=True), encoding="utf-8")
    return path


def clear_receipt(out_dir: Path) -> None:
    """The pod is confirmed gone — so the receipt must go too, or the reaper reports a leak on
    every clean run and the operator learns to ignore it."""
    (Path(out_dir) / RECEIPT_FILE).unlink(missing_ok=True)


def reap_receipt(path: Path, *, client: Any) -> str | None:
    """Terminate whatever a receipt names. Returns the pod id it killed, or None if there was
    nothing to kill — an absent receipt is the NORMAL outcome (clean run) and must not fail the
    step. Uses only DELETE /pods/{id}, which `terminate_pod` already treats as idempotent
    (404 = already gone), so this can run after a successful teardown and say nothing."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        pod_id = str(json.loads(path.read_text(encoding="utf-8")).get("pod_id") or "")
    except ValueError:
        return None
    if not pod_id:
        return None
    client.terminate_pod(pod_id)
    path.unlink(missing_ok=True)
    return pod_id


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m autodedup.oss_pod",
        description="Terminate a pod a killed judge run left behind (reads its receipt).",
    )
    parser.add_argument("--reap", required=True, type=Path,
                        help=f"path to the run's {RECEIPT_FILE}")
    args = parser.parse_args(argv)

    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        # No key, no rental to reap: never fail a cleanup step over it.
        print("RUNPOD_API_KEY is not set; nothing to reap")
        return 0
    from scripts.runpod_client import RunPodClient

    pod_id = reap_receipt(args.reap, client=RunPodClient(key))
    # Not phrased as "leaked": a receipt also survives the boot-failure path, where the lane
    # DID terminate and this DELETE merely 404s. Say what was done, not what it implies.
    print(f"reaped pod {pod_id} named by the receipt (already gone counts as success)"
          if pod_id else "no pod receipt; nothing to reap")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through `main`
    raise SystemExit(main())
