"""The OSS (self-hosted vLLM) provider: routing, the wire id, and the JSON fallback.

Block translation, tool_choice and usage extraction are covered once for every
OpenAICompatibleProvider subclass in tests/api/test_providers/test_openai_compatible.py —
what is tested here is only what OSS does DIFFERENTLY: the `oss:` namespace (routed on, then
stripped before it goes on the wire), an ephemeral base_url read from the environment, a
cost that is always zero, and a plain-JSON answer promoted to the tool call it meant to be.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from api.llm_client import provider_for_model
from api.providers.base import (
    Completion,
    Message,
    ProviderError,
    TextBlock,
    ToolSchema,
    Usage,
    compute_cost_usd,
)
from api.providers.oss import OssProvider, served_model

MODEL = "oss:Qwen/Qwen2.5-VL-7B-Instruct"

TOOL = ToolSchema(
    name="record_pair_verdict",
    description="Record the verdict.",
    input_schema={
        "type": "object",
        "properties": {"verdict": {"type": "string"}, "confidence": {"type": "number"}},
        "required": ["verdict", "confidence"],
    },
)


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.bodies: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any],
             timeout: float) -> FakeResponse:
        self.urls.append(url)
        self.bodies.append(json)
        return FakeResponse(self._payload)


def answer(*, content: str | None = None, tool_args: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"content": content}
    if tool_args is not None:
        message["tool_calls"] = [{
            "id": "call_1", "type": "function",
            "function": {"name": TOOL.name, "arguments": tool_args},
        }]
    return {
        "model": "Qwen/Qwen2.5-VL-7B-Instruct",
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4000, "completion_tokens": 120},
    }


def call(provider: OssProvider, tools: list[ToolSchema] | None = None) -> Completion:
    return provider.complete(
        system="s",
        messages=[Message(role="user", content=[TextBlock("pair")])],
        tools=[TOOL] if tools is None else tools,
        model=MODEL,
        tool_choice=TOOL.name,
    )


# --- routing + wire id -------------------------------------------------------------------


def test_an_oss_id_routes_to_the_oss_provider():
    assert provider_for_model(MODEL) == "oss"
    assert provider_for_model("oss:meta-llama/Llama-3.2-11B-Vision") == "oss"


def test_a_bare_hf_id_does_not_route_to_oss():
    # The namespace is the whole point: unprefixed `qwen…` is DashScope's PAID API.
    assert provider_for_model("qwen3-vl-30b-a3b-instruct") == "qwen"


def test_the_prefix_is_stripped_before_it_goes_on_the_wire():
    session = FakeSession(answer(tool_args='{"verdict": "same_property", "confidence": 0.9}'))
    provider = OssProvider(base_url="http://pod:8000/v1", session=session)
    call(provider)
    assert session.bodies[0]["model"] == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert session.urls[0] == "http://pod:8000/v1/chat/completions"
    assert session.bodies[0]["max_tokens"] == 4096


def test_served_model_leaves_an_unprefixed_id_alone():
    assert served_model("Qwen/Qwen2.5-VL-7B-Instruct") == "Qwen/Qwen2.5-VL-7B-Instruct"


# --- ephemeral base_url ------------------------------------------------------------------


def test_base_url_comes_from_the_environment_at_construction(monkeypatch):
    monkeypatch.setenv("OSS_LLM_BASE_URL", "https://pod-9-8000.proxy.runpod.net/v1/")
    provider = OssProvider()
    assert provider._base_url == "https://pod-9-8000.proxy.runpod.net/v1"
    assert provider.name == "oss"
    assert provider._api_key == "none"


def test_a_call_with_no_pod_up_fails_with_a_named_env_var(monkeypatch):
    monkeypatch.delenv("OSS_LLM_BASE_URL", raising=False)
    with pytest.raises(ProviderError, match="OSS_LLM_BASE_URL"):
        call(OssProvider(session=FakeSession(answer(content="x"))))


def test_a_pod_rented_after_construction_is_still_reachable(monkeypatch):
    monkeypatch.delenv("OSS_LLM_BASE_URL", raising=False)
    session = FakeSession(answer(tool_args='{"verdict": "different_property", "confidence": 0.5}'))
    provider = OssProvider(session=session)
    monkeypatch.setenv("OSS_LLM_BASE_URL", "http://pod-late:8000/v1")
    assert call(provider).tool_calls[0].input["verdict"] == "different_property"


def test_a_second_pod_is_reached_at_its_own_url(monkeypatch):
    # Pods are ephemeral BY DESIGN: pass 2 rents a new pod_id, hence a new proxy host. A URL
    # latched on first use would keep POSTing at pass 1's terminated host.
    session = FakeSession(answer(tool_args='{"verdict": "same_property", "confidence": 0.8}'))
    monkeypatch.setenv("OSS_LLM_BASE_URL", "http://pod-1:8000/v1")
    provider = OssProvider(session=session)
    call(provider)
    monkeypatch.setenv("OSS_LLM_BASE_URL", "http://pod-2:8000/v1")
    call(provider)
    assert session.urls == [
        "http://pod-1:8000/v1/chat/completions", "http://pod-2:8000/v1/chat/completions",
    ]


def test_an_explicit_base_url_is_never_overwritten_by_the_environment(monkeypatch):
    session = FakeSession(answer(tool_args='{"verdict": "same_property", "confidence": 0.8}'))
    provider = OssProvider(base_url="http://explicit:8000/v1", session=session)
    monkeypatch.setenv("OSS_LLM_BASE_URL", "http://pod-9:8000/v1")
    call(provider)
    assert session.urls == ["http://explicit:8000/v1/chat/completions"]


def test_the_api_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("OSS_LLM_API_KEY", "vllm-key")
    assert OssProvider(base_url="http://pod:8000/v1")._api_key == "vllm-key"


# --- cost --------------------------------------------------------------------------------


def test_cost_is_zero_on_both_paths():
    provider = OssProvider(base_url="http://pod:8000/v1", session=FakeSession(answer()))
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert provider.compute_cost_usd(model=MODEL, usage=usage) == 0.0
    # The recorder's path: llm_client asks for a price, then computes.
    assert compute_cost_usd(price=provider.price_for(MODEL), model=MODEL, usage=usage) == 0.0


# --- JSON fallback -----------------------------------------------------------------------


def test_a_bare_json_answer_is_promoted_to_the_tool_call():
    payload = answer(content='{"verdict": "same_property", "confidence": 0.8}')
    provider = OssProvider(base_url="http://pod:8000/v1", session=FakeSession(payload))
    completion = call(provider)
    assert completion.stop_reason == "tool_use"
    assert completion.tool_calls[0].name == TOOL.name
    assert completion.tool_calls[0].input == {"verdict": "same_property", "confidence": 0.8}


def test_a_fenced_json_answer_with_prose_around_it_is_promoted():
    content = 'Here is my verdict:\n```json\n{"verdict": "insufficient_evidence", ' \
              '"confidence": 0.3}\n```'
    provider = OssProvider(base_url="http://pod:8000/v1",
                           session=FakeSession(answer(content=content)))
    assert call(provider).tool_calls[0].input["verdict"] == "insufficient_evidence"


def test_a_real_tool_call_is_never_second_guessed():
    payload = answer(content="thinking out loud {\"verdict\": \"junk\"}",
                     tool_args='{"verdict": "same_property", "confidence": 0.9}')
    provider = OssProvider(base_url="http://pod:8000/v1", session=FakeSession(payload))
    calls = call(provider).tool_calls
    assert len(calls) == 1 and calls[0].id == "call_1"


def test_prose_that_is_not_the_tool_arguments_is_left_alone():
    provider = OssProvider(base_url="http://pod:8000/v1",
                           session=FakeSession(answer(content="I cannot tell.")))
    completion = call(provider)
    assert completion.tool_calls == [] and completion.stop_reason == "end_turn"


def test_json_that_matches_no_declared_property_is_left_alone():
    payload = answer(content='{"answer": "yes", "why": "looks alike"}')
    provider = OssProvider(base_url="http://pod:8000/v1", session=FakeSession(payload))
    assert call(provider).tool_calls == []
