"""The OSS pod lane against fakes — no RunPod account, no GPU, no HTTP, no spend.

The seams worth pinning are the ones a live run cannot cheaply re-test: the vLLM flag
string, the GPU choice (24 GB+, never a 4090, capacity fallback), readiness read from the
SERVER rather than the pod record, and the two paths that must tear a pod down — the
readiness deadline and a teardown that itself fails.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from autodedup import oss_pod
from scripts.runpod_client import GpuOption, NoCapacityError


# --- fakes -------------------------------------------------------------------------------


def gpu(gpu_id: str, memory_gb: float, price: float) -> GpuOption:
    return GpuOption(
        id=gpu_id, display_name=gpu_id, memory_gb=memory_gb, community_price_per_hr=price
    )


CATALOG = [
    gpu("NVIDIA GeForce RTX 4090", 24, 0.34),
    gpu("NVIDIA RTX A5000", 24, 0.16),
    gpu("NVIDIA GeForce RTX 3090", 24, 0.22),
    gpu("NVIDIA A40", 48, 0.39),
    gpu("NVIDIA RTX A6000", 48, 0.49),
    gpu("NVIDIA GeForce RTX 3070", 8, 0.11),
]


class FakeRunPod:
    """Records launches; `no_capacity` names the gpu ids that refuse to rent."""

    def __init__(self, *, catalog: list[GpuOption] | None = None,
                 no_capacity: tuple[str, ...] = ()) -> None:
        self._catalog = catalog if catalog is not None else CATALOG
        self._no_capacity = no_capacity
        self.launches: list[dict[str, Any]] = []
        self.terminated: list[str] = []

    def eligible_gpus(self, *, max_price_per_hr: float | None = None) -> list[GpuOption]:
        options = [g for g in self._catalog
                   if max_price_per_hr is None or g.community_price_per_hr <= max_price_per_hr]
        return sorted(options, key=lambda g: g.community_price_per_hr)

    def launch_pod(self, **kwargs: Any) -> dict[str, Any]:
        self.launches.append(kwargs)
        if kwargs["gpu_type_id"] in self._no_capacity:
            raise NoCapacityError(f"no instances currently available for {kwargs['gpu_type_id']}")
        return {"id": f"pod-{len(self.launches)}", "costPerHr": 0.19}

    def terminate_pod(self, pod_id: str) -> None:
        self.terminated.append(pod_id)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """`gets` is a script consumed in order; the last entry repeats forever."""

    def __init__(self, gets: list[Any] | None = None, post: Any = None) -> None:
        self._gets = list(gets or [])
        self._post = post
        self.get_urls: list[str] = []
        self.post_bodies: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_urls.append(url)
        step = self._gets.pop(0) if len(self._gets) > 1 else (self._gets[0] if self._gets else None)
        if isinstance(step, Exception):
            raise step
        return step

    def post(self, url: str, *, json: dict[str, Any], **kwargs: Any) -> FakeResponse:
        self.post_bodies.append(json)
        if isinstance(self._post, Exception):
            raise self._post
        return self._post


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def models_ok(model_id: str = oss_pod.DEFAULT_MODEL_ID) -> FakeResponse:
    return FakeResponse(200, {"object": "list", "data": [{"id": model_id}]})


def handle(**over: Any) -> oss_pod.PodHandle:
    base = dict(pod_id="pod-1", base_url="https://pod-1-8000.proxy.runpod.net",
                gpu="NVIDIA RTX A5000", usd_per_hr=0.16, started_at=0.0)
    base.update(over)
    return oss_pod.PodHandle(**base)  # type: ignore[arg-type]


# --- the vLLM command line ---------------------------------------------------------------


def test_vllm_args_are_the_documented_qwen_vl_tool_calling_flags():
    args = oss_pod.build_vllm_args(model_id="Qwen/Qwen2.5-VL-7B-Instruct",
                                   max_model_len=12288, max_images=8)
    assert args == [
        "--model", "Qwen/Qwen2.5-VL-7B-Instruct",
        "--max-model-len", "12288",
        # JSON, not the removed `image=8` spelling: a CLI parse error boots a server that
        # never binds, and the only detector is a 25-minute readiness deadline.
        "--limit-mm-per-prompt", '{"image": 8}',
        "--dtype", "bfloat16",
        "--gpu-memory-utilization", "0.92",
        "--enable-auto-tool-choice",
        "--tool-call-parser", "hermes",
        "--served-model-name", "Qwen/Qwen2.5-VL-7B-Instruct",
        "--host", "0.0.0.0",
        # The exposed port, the proxy URL and the readiness poll all read one constant.
        "--port", "8000",
    ]
    assert json.loads(args[args.index("--limit-mm-per-prompt") + 1]) == {"image": 8}


def test_command_prefix_prepends_without_dropping_flags():
    args = oss_pod.build_vllm_args(command_prefix=("vllm", "serve", "m"))
    assert args[:3] == ["vllm", "serve", "m"]
    assert "--enable-auto-tool-choice" in args


# --- GPU selection -----------------------------------------------------------------------


def test_select_gpus_prefers_the_listed_order_and_never_the_4090():
    picked = [g.id for g in oss_pod.select_gpus(FakeRunPod())]
    assert picked == [
        "NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090", "NVIDIA A40", "NVIDIA RTX A6000",
    ]


def test_select_gpus_drops_boxes_under_24gb():
    assert all(g.memory_gb >= 24 for g in oss_pod.select_gpus(FakeRunPod()))


def test_select_gpus_keeps_an_unlisted_big_card_as_a_tail_fallback():
    catalog = [gpu("NVIDIA L40S", 48, 0.86), gpu("NVIDIA RTX A5000", 24, 0.16)]
    assert [g.id for g in oss_pod.select_gpus(FakeRunPod(catalog=catalog))] == [
        "NVIDIA RTX A5000", "NVIDIA L40S",
    ]


def test_a_strict_preference_never_substitutes_a_pricier_card():
    # An operator who pins a $0.16/hr A5000 must not silently rent a $0.49/hr A6000 on an
    # hourly-billed arm whose whole point is the cost comparison.
    catalog = [gpu("NVIDIA RTX A6000", 48, 0.49)]
    with pytest.raises(Exception, match="strict"):
        oss_pod.select_gpus(FakeRunPod(catalog=catalog), ("a5000",), strict=True)


def test_a_strict_preference_still_ranks_within_the_pinned_type():
    catalog = [gpu("NVIDIA RTX A5000", 24, 0.16), gpu("NVIDIA RTX A6000", 48, 0.49)]
    picked = oss_pod.select_gpus(FakeRunPod(catalog=catalog), ("a5000",), strict=True)
    assert [g.id for g in picked] == ["NVIDIA RTX A5000"]


def test_launch_propagates_strict_gpu():
    client = FakeRunPod()
    with pytest.raises(Exception, match="strict"):
        oss_pod.launch_vllm_pod(client, gpu_preference=("l40s",), strict_gpu=True)
    assert client.launches == []


def test_select_gpus_raises_when_nothing_is_big_enough():
    catalog = [gpu("NVIDIA GeForce RTX 3070", 8, 0.11)]
    with pytest.raises(Exception, match="24 GB"):
        oss_pod.select_gpus(FakeRunPod(catalog=catalog))


# --- launch ------------------------------------------------------------------------------


def test_launch_exposes_the_http_port_and_sizes_the_container_disk():
    client = FakeRunPod()
    pod = oss_pod.launch_vllm_pod(client, now=lambda: 1000.0)
    launch = client.launches[0]
    assert launch["ports"] == ["8000/http"]
    # Image (~20-25 GB) + bf16 weights (~16.6 GB) + the HF cache, all on the container disk.
    assert launch["container_disk_gb"] >= 60 and launch["volume_gb"] == 0
    assert launch["gpu_type_id"] == "NVIDIA RTX A5000"
    assert pod.base_url == "https://pod-1-8000.proxy.runpod.net"
    assert pod.usd_per_hr == 0.19 and pod.started_at == 1000.0


def test_launch_passes_hf_token_through_env_never_argv():
    client = FakeRunPod()
    oss_pod.launch_vllm_pod(client, hf_token="hf-secret")
    launch = client.launches[0]
    assert launch["env"] == {"HF_TOKEN": "hf-secret"}
    assert "hf-secret" not in " ".join(launch["start_cmd"])


def test_launch_falls_through_to_the_next_gpu_on_no_capacity():
    client = FakeRunPod(no_capacity=("NVIDIA RTX A5000",))
    pod = oss_pod.launch_vllm_pod(client)
    assert [c["gpu_type_id"] for c in client.launches] == [
        "NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090",
    ]
    assert pod.gpu == "NVIDIA GeForce RTX 3090"


def test_launch_raises_when_no_gpu_type_has_capacity():
    client = FakeRunPod(no_capacity=tuple(g.id for g in CATALOG))
    with pytest.raises(NoCapacityError):
        oss_pod.launch_vllm_pod(client)


def test_dry_run_rents_nothing_and_is_deterministic():
    client = FakeRunPod()
    a = oss_pod.launch_vllm_pod(client, dry_run=True)
    b = oss_pod.launch_vllm_pod(client, dry_run=True)
    assert a == b
    assert a.dry_run and a.usd_per_hr == 0.0
    assert client.launches == []


def test_dry_run_request_names_env_keys_never_values():
    request = oss_pod.build_launch_request(
        name="n", image=oss_pod.DEFAULT_IMAGE, gpu_type_id_preference=("a5000",),
        start_cmd=oss_pod.build_vllm_args(), env_keys=("HF_TOKEN",),
    )
    assert request["env_keys"] == ["HF_TOKEN"]
    assert request["ports"] == ["8000/http"]


def test_dry_run_calls_the_gpu_field_a_preference_not_an_id():
    # "a5000" is a match PATTERN; the live path sends the catalog id "NVIDIA RTX A5000". The
    # dry run is what an operator reads before spending, so it must not advertise a field
    # value RunPod would reject under the key it would reject it in.
    request = oss_pod.build_launch_request(
        name="n", image=oss_pod.DEFAULT_IMAGE, gpu_type_id_preference=("a5000", "3090"),
        start_cmd=[],
    )
    assert "gpu_type_id" not in request
    assert request["gpu_type_id_preference"] == ["a5000", "3090"]


def test_an_ambient_hf_token_reaches_a_gated_model_without_being_passed(monkeypatch):
    # Otherwise a gated `oss_model=` 401s on the weights, never binds, and costs a full
    # rental for a secret that was already in the environment.
    monkeypatch.setenv("HF_TOKEN", "hf-from-env")
    client = FakeRunPod()
    oss_pod.launch_vllm_pod(client)
    assert client.launches[0]["env"] == {"HF_TOKEN": "hf-from-env"}


def test_no_hf_token_sends_no_env(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    client = FakeRunPod()
    oss_pod.launch_vllm_pod(client)
    assert client.launches[0]["env"] is None


# --- readiness ---------------------------------------------------------------------------


def test_wait_ready_polls_the_server_until_the_model_is_listed():
    clock = Clock()
    session = FakeSession([
        requests.ConnectionError("proxy 502"),
        FakeResponse(503, {}),
        FakeResponse(200, {"data": [{"id": "some-other-model"}]}),
        models_ok(),
    ])
    client = FakeRunPod()
    elapsed = oss_pod.wait_ready(handle(), client=client, session=session,
                                 clock=clock.now, sleep=clock.sleep)
    assert elapsed == pytest.approx(45.0)
    assert session.get_urls[0] == "https://pod-1-8000.proxy.runpod.net/v1/models"
    assert client.terminated == []


def test_wait_ready_rejects_a_server_that_lists_a_different_model():
    clock = Clock()
    session = FakeSession([FakeResponse(200, {"data": [{"id": "mistral"}]})])
    client = FakeRunPod()
    with pytest.raises(oss_pod.PodBootstrapError, match="mistral"):
        oss_pod.wait_ready(handle(), client=client, session=session, deadline_s=60,
                           clock=clock.now, sleep=clock.sleep)
    assert client.terminated == ["pod-1"]


def test_wait_ready_terminates_the_pod_on_its_deadline():
    clock = Clock()
    session = FakeSession([FakeResponse(502, {})])
    client = FakeRunPod()
    with pytest.raises(oss_pod.PodBootstrapError, match="did not serve"):
        oss_pod.wait_ready(handle(), client=client, session=session, deadline_s=45,
                           clock=clock.now, sleep=clock.sleep)
    assert client.terminated == ["pod-1"]


def test_wait_ready_terminates_the_pod_when_the_wait_is_CANCELLED():
    # Ctrl-C / an Actions cancellation raises KeyboardInterrupt, which is NOT an Exception:
    # a caller's `except Exception` teardown never sees it, and the pod bills on alone.
    class Cancel(Clock):
        def sleep(self, seconds: float) -> None:
            raise KeyboardInterrupt()

    clock = Cancel()
    client = FakeRunPod()
    with pytest.raises(KeyboardInterrupt):
        oss_pod.wait_ready(handle(), client=client, session=FakeSession([FakeResponse(502, {})]),
                           clock=clock.now, sleep=clock.sleep)
    assert client.terminated == ["pod-1"]


@pytest.mark.parametrize("payload", [[], "booting", {"data": ["a-model"]}, {"data": None}])
def test_wait_ready_treats_a_surprise_200_body_as_not_ready(payload):
    # A half-booted server or a proxy shim can answer 200 with anything; an AttributeError out
    # of the polling loop would escape the terminate-on-the-way-out contract.
    clock = Clock()
    client = FakeRunPod()
    with pytest.raises(oss_pod.PodBootstrapError, match="did not serve"):
        oss_pod.wait_ready(handle(), client=client, session=FakeSession([
            FakeResponse(200, payload)]), deadline_s=30, clock=clock.now, sleep=clock.sleep)
    assert client.terminated == ["pod-1"]


# --- rented_pod ---------------------------------------------------------------------------


def test_rented_pod_terminates_on_a_clean_exit():
    client = FakeRunPod()
    with oss_pod.rented_pod(client) as pod:
        assert pod.pod_id == "pod-1"
    assert client.terminated == ["pod-1"]


def test_rented_pod_terminates_on_cancellation():
    # The handle exists BEFORE any wait, so there is no window in which a caller holds a
    # rented pod in a variable its `finally` cannot see.
    client = FakeRunPod()
    with pytest.raises(KeyboardInterrupt):
        with oss_pod.rented_pod(client):
            raise KeyboardInterrupt()
    assert client.terminated == ["pod-1"]


# --- smoke call --------------------------------------------------------------------------


def test_smoke_chat_sends_one_image_and_parses_the_forced_tool_call():
    session = FakeSession(post=FakeResponse(200, {
        "choices": [{"message": {"tool_calls": [
            {"function": {"name": "record_smoke", "arguments": '{"seen": "grey"}'}}
        ]}}],
    }))
    assert oss_pod.smoke_chat(handle(), session=session) == {"seen": "grey"}
    body = session.post_bodies[0]
    parts = body["messages"][0]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert body["tool_choice"]["function"]["name"] == "record_smoke"
    assert body["model"] == oss_pod.DEFAULT_MODEL_ID


def test_smoke_chat_raises_when_the_tool_call_parser_produced_prose():
    session = FakeSession(post=FakeResponse(200, {
        "choices": [{"message": {"content": "<tool_call>{\"seen\": \"grey\"}</tool_call>"}}],
    }))
    with pytest.raises(oss_pod.PodBootstrapError, match="tool-call parser"):
        oss_pod.smoke_chat(handle(), session=session)


def test_smoke_chat_raises_on_an_http_error():
    session = FakeSession(post=FakeResponse(500, {}, text="boom"))
    with pytest.raises(oss_pod.PodBootstrapError, match="HTTP 500"):
        oss_pod.smoke_chat(handle(), session=session)


# --- teardown + cost ---------------------------------------------------------------------


def test_terminate_never_raises_from_a_finally():
    class Angry(FakeRunPod):
        def terminate_pod(self, pod_id: str) -> None:
            raise RuntimeError("runpod 500")

    oss_pod.terminate(handle(), client=Angry())


def test_terminate_is_a_no_op_for_a_dry_run_handle():
    client = FakeRunPod()
    oss_pod.terminate(handle(pod_id="dry-run", dry_run=True), client=client)
    assert client.terminated == []


def test_pod_cost_prices_the_whole_rental_window():
    assert oss_pod.pod_cost_usd(handle(usd_per_hr=0.24), now=1800.0) == pytest.approx(0.12)
    assert oss_pod.pod_cost_usd(handle(started_at=100.0), now=50.0) == 0.0


# --- the receipt: the leak a `finally` cannot close ---------------------------------------


def test_the_receipt_names_the_pod_for_a_process_that_never_gets_to_its_finally(tmp_path):
    """A cancelled Actions job runs NO `finally`. The receipt is the only thing that crosses
    that boundary, because the artifact upload step runs `if: always()`."""
    path = oss_pod.write_receipt(handle(pod_id="pod-7", usd_per_hr=0.16), tmp_path)
    assert path.name == oss_pod.RECEIPT_FILE
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["pod_id"] == "pod-7" and written["usd_per_hr"] == 0.16
    assert {"gpu", "base_url", "model_id", "started_at"} <= set(written)


def test_reaping_a_receipt_terminates_what_it_names_and_then_forgets_it(tmp_path):
    client = FakeRunPod()
    oss_pod.write_receipt(handle(pod_id="pod-7"), tmp_path)
    assert oss_pod.reap_receipt(tmp_path / oss_pod.RECEIPT_FILE, client=client) == "pod-7"
    assert client.terminated == ["pod-7"]
    # Reaped once: a second pass has nothing to kill and must not report a leak.
    assert oss_pod.reap_receipt(tmp_path / oss_pod.RECEIPT_FILE, client=client) is None
    assert client.terminated == ["pod-7"]


def test_a_clean_run_leaves_the_reaper_nothing_to_do(tmp_path):
    """The step runs on EVERY outcome, so the normal case — no receipt, or an unreadable one —
    has to be silent success; a cleanup that fails green runs is a cleanup people switch off."""
    client = FakeRunPod()
    assert oss_pod.reap_receipt(tmp_path / "pod.json", client=client) is None
    oss_pod.write_receipt(handle(), tmp_path)
    oss_pod.clear_receipt(tmp_path)
    assert oss_pod.reap_receipt(tmp_path / oss_pod.RECEIPT_FILE, client=client) is None
    (tmp_path / oss_pod.RECEIPT_FILE).write_text("{not json", encoding="utf-8")
    assert oss_pod.reap_receipt(tmp_path / oss_pod.RECEIPT_FILE, client=client) is None
    assert client.terminated == []


def test_the_reap_cli_is_silent_success_without_a_runpod_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    assert oss_pod.main(["--reap", str(tmp_path / "pod.json")]) == 0
    assert "nothing to reap" in capsys.readouterr().out
