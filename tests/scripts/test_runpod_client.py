"""scripts/runpod_client.py — GPU selection, pod CRUD request shapes, the
wait_for_exit poll/timeout split, and (the point of this module) that run_job
ALWAYS terminates the pod, including when a step in between raises. Hermetic:
a fake requests.Session, no network."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.runpod_client import GpuOption, NoCapacityError, RunPodClient, RunPodError


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_body: Any = None,
        text: str = "",
        lines: list[str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self._lines = lines or []

    def json(self) -> Any:
        return self._json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self, decode_unicode: bool = True):
        yield from self._lines

    def close(self) -> None:
        pass


class _FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.post_response: _FakeResponse | None = None  # GraphQL catalog query
        self.launch_responses: list[_FakeResponse] = []  # one per POST /pods call, in order
        self.get_responses: list[_FakeResponse] = []
        self.delete_response: _FakeResponse = _FakeResponse(200)
        self.get_logs_response: _FakeResponse | None = None
        self.get_logs_raises: Exception | None = None

    def post(self, url: str, json: Any = None, timeout: float = 30) -> _FakeResponse:
        self.calls.append(("POST", url, json))
        if url.endswith("/pods"):
            return self.launch_responses.pop(0)
        return self.post_response

    def get(self, url: str, timeout: float = 30, stream: bool = False) -> _FakeResponse:
        self.calls.append(("GET", url, {"stream": stream}))
        if stream:
            if self.get_logs_raises:
                raise self.get_logs_raises
            return self.get_logs_response
        return self.get_responses.pop(0)

    def delete(self, url: str, timeout: float = 30) -> _FakeResponse:
        self.calls.append(("DELETE", url, {}))
        return self.delete_response


def _client(session: _FakeSession) -> RunPodClient:
    return RunPodClient("test-key", session=session)


def test_session_carries_bearer_auth_header():
    session = _FakeSession()
    _client(session)
    assert session.headers["Authorization"] == "Bearer test-key"


# --- cheapest_gpu -----------------------------------------------------------


def test_cheapest_gpu_picks_lowest_community_price():
    session = _FakeSession()
    session.post_response = _FakeResponse(
        200,
        {
            "data": {
                "gpuTypes": [
                    {"id": "rtx4090", "displayName": "RTX 4090", "memoryInGb": 24, "communityPrice": 0.34},
                    {"id": "rtxa2000", "displayName": "RTX A2000", "memoryInGb": 6, "communityPrice": 0.11},
                    {"id": "unpriced", "displayName": "Unpriced", "memoryInGb": 8, "communityPrice": None},
                    # A real live run (2026-08-06) hit exactly this: a placeholder/
                    # unavailable catalog entry priced at 0, which "wins" as cheapest
                    # under a naive `is not None` filter since 0 beats every real price.
                    {"id": "unknown", "displayName": "unknown", "memoryInGb": 0, "communityPrice": 0},
                ]
            }
        },
    )
    gpu = _client(session).cheapest_gpu()
    assert gpu == GpuOption("rtxa2000", "RTX A2000", 6, 0.11)


def test_cheapest_gpu_respects_price_cap():
    session = _FakeSession()
    session.post_response = _FakeResponse(
        200,
        {
            "data": {
                "gpuTypes": [
                    {"id": "rtx4090", "displayName": "RTX 4090", "memoryInGb": 24, "communityPrice": 0.34},
                    {"id": "h100", "displayName": "H100", "memoryInGb": 80, "communityPrice": 2.5},
                ]
            }
        },
    )
    gpu = _client(session).cheapest_gpu(max_price_per_hr=1.0)
    assert gpu.id == "rtx4090"


def test_cheapest_gpu_raises_when_nothing_qualifies():
    session = _FakeSession()
    session.post_response = _FakeResponse(
        200, {"data": {"gpuTypes": [{"id": "h100", "displayName": "H100", "memoryInGb": 80, "communityPrice": 2.5}]}}
    )
    with pytest.raises(RunPodError):
        _client(session).cheapest_gpu(max_price_per_hr=1.0)


def test_cheapest_gpu_raises_on_graphql_error():
    session = _FakeSession()
    session.post_response = _FakeResponse(200, {"errors": [{"message": "bad query"}]})
    with pytest.raises(RunPodError):
        _client(session).cheapest_gpu()


# --- launch_pod / get_pod / terminate_pod ------------------------------------


def test_launch_pod_sends_expected_body():
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(201, {"id": "pod123", "costPerHr": 0.11})]
    pod = _client(session).launch_pod(
        name="smoke", image="runpod/pytorch:x", gpu_type_id="rtxa2000", start_cmd=["bash", "-c", "true"],
    )
    assert pod["id"] == "pod123"
    method, url, body = session.calls[0]
    assert method == "POST" and url.endswith("/pods")
    assert body["gpuTypeIds"] == ["rtxa2000"]
    assert body["dockerStartCmd"] == ["bash", "-c", "true"]
    assert body["interruptible"] is False


# A real batch job needs credentials INSIDE the pod (SUPABASE_DB_URL, R2_*, HF_TOKEN)
# and until the DINOv3 embedding lane there was no channel for them: launch_pod's body
# had no `env` field at all. The REST API (rest.runpod.io/v1, what this client uses for
# pod CRUD) takes `env` as a JSON object — not the [{key, value}] list the older GraphQL
# API used.


def test_launch_pod_sends_env_as_a_rest_object():
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(201, {"id": "pod123"})]
    _client(session).launch_pod(
        name="job", image="x", gpu_type_id="rtxa2000", start_cmd=["true"],
        env={"SUPABASE_DB_URL": "postgres://x", "HF_TOKEN": "hf_secret"},
    )
    body = session.calls[0][2]
    assert body["env"] == {"SUPABASE_DB_URL": "postgres://x", "HF_TOKEN": "hf_secret"}


def test_launch_pod_omits_env_entirely_when_none_is_given():
    # An env-less launch must send the exact body it always did.
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(201, {"id": "pod123"})]
    _client(session).launch_pod(
        name="smoke", image="x", gpu_type_id="rtxa2000", start_cmd=["true"],
    )
    assert "env" not in session.calls[0][2]


def test_launch_pod_omits_an_empty_env():
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(201, {"id": "pod123"})]
    _client(session).launch_pod(
        name="smoke", image="x", gpu_type_id="rtxa2000", start_cmd=["true"], env={},
    )
    assert "env" not in session.calls[0][2]


def test_launch_pod_stringifies_env_values():
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(201, {"id": "pod123"})]
    _client(session).launch_pod(
        name="job", image="x", gpu_type_id="rtxa2000", start_cmd=["true"],
        env={"WORKERS": 16},
    )
    assert session.calls[0][2]["env"] == {"WORKERS": "16"}


def test_run_job_passes_env_through_to_the_launch():
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(201, {"id": "pod123", "costPerHr": 0.11})]
    session.get_responses = [_FakeResponse(200, {"desiredStatus": "EXITED"})]
    session.get_logs_response = _FakeResponse(200, lines=[])
    _client(session).run_job(
        name="job", image="x", gpu_type_id="rtxa2000", start_cmd=["true"],
        env={"HF_TOKEN": "hf_secret"}, max_wait_s=5, poll_interval_s=0,
    )
    assert session.calls[0][2]["env"] == {"HF_TOKEN": "hf_secret"}


def test_run_job_with_fallback_passes_env_to_every_attempt():
    session = _FakeSession()
    session.launch_responses = [
        _FakeResponse(500, text="There are no instances currently available"),
        _FakeResponse(201, {"id": "pod123", "costPerHr": 0.22}),
    ]
    session.get_responses = [_FakeResponse(200, {"desiredStatus": "EXITED"})]
    session.get_logs_response = _FakeResponse(200, lines=[])
    gpus = [GpuOption("rtxa5000", "RTX A5000", 24, 0.16), GpuOption("rtx3090", "RTX 3090", 24, 0.22)]
    _client(session).run_job_with_fallback(
        name="job", image="x", gpu_options=gpus, start_cmd=["true"],
        env={"HF_TOKEN": "hf_secret"}, max_wait_s=5, poll_interval_s=0,
    )
    launches = [c for c in session.calls if c[0] == "POST" and c[1].endswith("/pods")]
    assert len(launches) == 2
    assert all(c[2]["env"] == {"HF_TOKEN": "hf_secret"} for c in launches)


def test_launch_pod_raises_on_error_status():
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(400, text="bad gpu type")]
    with pytest.raises(RunPodError):
        _client(session).launch_pod(
            name="smoke", image="x", gpu_type_id="bogus", start_cmd=["true"],
        )


def test_terminate_pod_treats_404_as_success():
    session = _FakeSession()
    session.delete_response = _FakeResponse(404)
    _client(session).terminate_pod("gone-already")  # must not raise


def test_terminate_pod_raises_on_real_error():
    session = _FakeSession()
    session.delete_response = _FakeResponse(500, text="internal error")
    with pytest.raises(RunPodError):
        _client(session).terminate_pod("pod123")


# --- wait_for_exit ------------------------------------------------------------


def test_wait_for_exit_returns_on_terminal_status(monkeypatch):
    monkeypatch.setattr("scripts.runpod_client.time.sleep", lambda *_: None)
    session = _FakeSession()
    session.get_responses = [
        _FakeResponse(200, {"desiredStatus": "RUNNING"}),
        _FakeResponse(200, {"desiredStatus": "EXITED"}),
    ]
    status, timed_out = _client(session).wait_for_exit("pod123", max_wait_s=5, poll_interval_s=0)
    assert status == "EXITED"
    assert timed_out is False


def test_wait_for_exit_times_out():
    session = _FakeSession()
    session.get_responses = [_FakeResponse(200, {"desiredStatus": "RUNNING"})] * 10
    status, timed_out = _client(session).wait_for_exit("pod123", max_wait_s=0, poll_interval_s=0)
    assert timed_out is True
    assert status == "UNKNOWN"


# --- fetch_logs ----------------------------------------------------------------


def test_fetch_logs_parses_sse_data_lines():
    session = _FakeSession()
    session.get_logs_response = _FakeResponse(
        200, lines=["data: hello", "", ": comment", "data: SMOKE_TEST_OK 4.0"]
    )
    logs = _client(session).fetch_logs("pod123")
    assert logs == "hello\nSMOKE_TEST_OK 4.0"


def test_fetch_logs_swallows_request_errors():
    import requests as requests_module

    session = _FakeSession()
    session.get_logs_raises = requests_module.RequestException("boom")
    logs = _client(session).fetch_logs("pod123")  # must not raise
    assert logs == ""


# --- run_job: the cost-safety guarantee ---------------------------------------


def test_run_job_terminates_pod_on_success():
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(201, {"id": "pod123", "costPerHr": 0.11})]
    session.get_responses = [_FakeResponse(200, {"desiredStatus": "EXITED"})]
    session.get_logs_response = _FakeResponse(200, lines=["data: SMOKE_TEST_OK 1.0"])

    result = _client(session).run_job(
        name="smoke", image="x", gpu_type_id="rtxa2000", start_cmd=["true"],
        max_wait_s=5, poll_interval_s=0,
    )
    assert result.final_status == "EXITED"
    assert result.timed_out is False
    assert "SMOKE_TEST_OK" in result.logs
    assert session.calls[-1][0] == "DELETE"


def test_run_job_still_terminates_when_wait_for_exit_raises(monkeypatch):
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(201, {"id": "pod123", "costPerHr": 0.11})]

    def _boom(*args: Any, **kwargs: Any):
        raise RuntimeError("network blip")

    client = _client(session)
    monkeypatch.setattr(client, "wait_for_exit", _boom)

    with pytest.raises(RuntimeError):
        client.run_job(
            name="smoke", image="x", gpu_type_id="rtxa2000", start_cmd=["true"],
            max_wait_s=5, poll_interval_s=0,
        )
    assert session.calls[-1][0] == "DELETE"
    assert session.calls[-1][1].endswith("/pods/pod123")


def test_run_job_still_terminates_when_fetch_logs_raises(monkeypatch):
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(201, {"id": "pod123", "costPerHr": 0.11})]
    session.get_responses = [_FakeResponse(200, {"desiredStatus": "EXITED"})]

    client = _client(session)

    def _boom(*args: Any, **kwargs: Any):
        raise RuntimeError("log parse blew up")

    monkeypatch.setattr(client, "fetch_logs", _boom)

    with pytest.raises(RuntimeError):
        client.run_job(
            name="smoke", image="x", gpu_type_id="rtxa2000", start_cmd=["true"],
            max_wait_s=5, poll_interval_s=0,
        )
    assert session.calls[-1][0] == "DELETE"


# --- capacity fallback: real live condition, 2026-08-06 ----------------------
# The first live smoke-test run picked a GPU type with zero community capacity
# right now (peer-hosted, availability fluctuates) — RunPod's own 500 body says
# "There are no instances currently available". launch_pod must turn that into
# NoCapacityError (not RunPodError), and run_job_with_fallback must move on to
# the next-cheapest option rather than failing the whole job.


def test_launch_pod_raises_no_capacity_error_on_that_specific_message():
    session = _FakeSession()
    session.launch_responses = [
        _FakeResponse(500, text='{"error":"create pod: There are no instances currently available","status":500}')
    ]
    with pytest.raises(NoCapacityError):
        _client(session).launch_pod(
            name="smoke", image="x", gpu_type_id="rtxa2000", start_cmd=["true"],
        )


def test_launch_pod_raises_plain_error_for_other_4xx():
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(400, text="bad image name")]
    with pytest.raises(RunPodError) as exc_info:
        _client(session).launch_pod(
            name="smoke", image="x", gpu_type_id="rtxa2000", start_cmd=["true"],
        )
    assert not isinstance(exc_info.value, NoCapacityError)


def test_run_job_with_fallback_tries_next_gpu_on_no_capacity():
    session = _FakeSession()
    session.launch_responses = [
        _FakeResponse(500, text="There are no instances currently available"),
        _FakeResponse(201, {"id": "pod123", "costPerHr": 0.34}),
    ]
    session.get_responses = [_FakeResponse(200, {"desiredStatus": "EXITED"})]
    session.get_logs_response = _FakeResponse(200, lines=["data: SMOKE_TEST_OK 1.0"])

    gpus = [GpuOption("rtxa2000", "RTX A2000", 6, 0.11), GpuOption("rtx3070", "RTX 3070", 8, 0.34)]
    result = _client(session).run_job_with_fallback(
        name="smoke", image="x", gpu_options=gpus, start_cmd=["true"],
        max_wait_s=5, poll_interval_s=0,
    )
    assert result.pod_id == "pod123"
    launch_calls = [c for c in session.calls if c[0] == "POST" and c[1].endswith("/pods")]
    assert len(launch_calls) == 2
    assert launch_calls[0][2]["gpuTypeIds"] == ["rtxa2000"]
    assert launch_calls[1][2]["gpuTypeIds"] == ["rtx3070"]


def test_run_job_with_fallback_does_not_retry_a_non_capacity_error():
    session = _FakeSession()
    session.launch_responses = [_FakeResponse(400, text="bad image name")]
    gpus = [GpuOption("rtxa2000", "RTX A2000", 6, 0.11), GpuOption("rtx3070", "RTX 3070", 8, 0.34)]
    with pytest.raises(RunPodError):
        _client(session).run_job_with_fallback(
            name="smoke", image="x", gpu_options=gpus, start_cmd=["true"],
            max_wait_s=5, poll_interval_s=0,
        )
    launch_calls = [c for c in session.calls if c[0] == "POST" and c[1].endswith("/pods")]
    assert len(launch_calls) == 1  # never tried the second GPU for a non-capacity error


def test_run_job_with_fallback_raises_after_exhausting_every_option():
    session = _FakeSession()
    session.launch_responses = [
        _FakeResponse(500, text="There are no instances currently available"),
        _FakeResponse(500, text="There are no instances currently available"),
    ]
    gpus = [GpuOption("rtxa2000", "RTX A2000", 6, 0.11), GpuOption("rtx3070", "RTX 3070", 8, 0.34)]
    with pytest.raises(NoCapacityError):
        _client(session).run_job_with_fallback(
            name="smoke", image="x", gpu_options=gpus, start_cmd=["true"],
            max_wait_s=5, poll_interval_s=0,
        )


def test_run_job_with_fallback_raises_on_empty_gpu_list():
    session = _FakeSession()
    with pytest.raises(RunPodError):
        _client(session).run_job_with_fallback(
            name="smoke", image="x", gpu_options=[], start_cmd=["true"],
        )


def test_eligible_gpus_returns_cheapest_first():
    session = _FakeSession()
    session.post_response = _FakeResponse(
        200,
        {
            "data": {
                "gpuTypes": [
                    {"id": "rtx4090", "displayName": "RTX 4090", "memoryInGb": 24, "communityPrice": 0.34},
                    {"id": "rtxa2000", "displayName": "RTX A2000", "memoryInGb": 6, "communityPrice": 0.11},
                ]
            }
        },
    )
    gpus = _client(session).eligible_gpus()
    assert [g.id for g in gpus] == ["rtxa2000", "rtx4090"]
