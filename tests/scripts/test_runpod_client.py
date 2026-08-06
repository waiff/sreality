"""scripts/runpod_client.py — GPU selection, pod CRUD request shapes, the
wait_for_exit poll/timeout split, and (the point of this module) that run_job
ALWAYS terminates the pod, including when a step in between raises. Hermetic:
a fake requests.Session, no network."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.runpod_client import GpuOption, RunPodClient, RunPodError


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
        self.post_response: _FakeResponse | RunPodError | None = None
        self.get_responses: list[_FakeResponse] = []
        self.delete_response: _FakeResponse = _FakeResponse(200)
        self.get_logs_response: _FakeResponse | None = None
        self.get_logs_raises: Exception | None = None

    def post(self, url: str, json: Any = None, timeout: float = 30) -> _FakeResponse:
        self.calls.append(("POST", url, json))
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
    session.post_response = _FakeResponse(201, {"id": "pod123", "costPerHr": 0.11})
    pod = _client(session).launch_pod(
        name="smoke", image="runpod/pytorch:x", gpu_type_id="rtxa2000", start_cmd=["bash", "-c", "true"],
    )
    assert pod["id"] == "pod123"
    method, url, body = session.calls[0]
    assert method == "POST" and url.endswith("/pods")
    assert body["gpuTypeIds"] == ["rtxa2000"]
    assert body["dockerStartCmd"] == ["bash", "-c", "true"]
    assert body["interruptible"] is False


def test_launch_pod_raises_on_error_status():
    session = _FakeSession()
    session.post_response = _FakeResponse(400, text="bad gpu type")
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
    session.post_response = _FakeResponse(201, {"id": "pod123", "costPerHr": 0.11})
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
    session.post_response = _FakeResponse(201, {"id": "pod123", "costPerHr": 0.11})

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
    session.post_response = _FakeResponse(201, {"id": "pod123", "costPerHr": 0.11})
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
