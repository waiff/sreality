"""Tests for api.providers.openai_compatible — block translation + usage extraction.

Hermetic. A FakeSession stands in for `requests` (mirrors
tests/scraper/test_price_stats_client.py's FakeSession pattern) — no network,
no `requests` monkeypatching.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from api.providers import (
    ImageBlock,
    Message,
    ProviderError,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)
from api.providers.openai_compatible import OpenAICompatibleProvider


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: int) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self._responses.pop(0)


def _provider(session: FakeSession, **kwargs: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="testprov",
        base_url="https://example.test/v1",
        api_key_env="TESTPROV_API_KEY",
        prices={},
        api_key="test-key",
        session=session,
        **kwargs,
    )


def _text_response(text: str, **usage_kw: int) -> FakeResponse:
    return FakeResponse({
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": usage_kw.get("input_tokens", 10),
            "completion_tokens": usage_kw.get("output_tokens", 5),
        },
    })


def _tool_call_response(name: str, args: dict[str, Any]) -> FakeResponse:
    import json as json_mod
    return FakeResponse({
        "model": "test-model",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": name, "arguments": json_mod.dumps(args)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })


def test_text_block_round_trips():
    session = FakeSession([_text_response("hi back", input_tokens=12, output_tokens=4)])
    p = _provider(session)
    out = p.complete(
        system="be terse",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        model="test-model",
    )
    assert out.text_blocks == ["hi back"]
    assert out.usage.input_tokens == 12
    assert out.usage.output_tokens == 4
    body = session.calls[0]["json"]
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert body["messages"][1]["content"] == [{"type": "text", "text": "hi"}]


def test_function_call_extracted():
    session = FakeSession([_tool_call_response("find_things", {"radius_m": 1500})])
    p = _provider(session)
    out = p.complete(
        system="",
        messages=[Message(role="user", content=[TextBlock(text="go")])],
        tools=[ToolSchema(name="find_things", description="d", input_schema={"type": "object"})],
        model="test-model",
    )
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].name == "find_things"
    assert out.tool_calls[0].input == {"radius_m": 1500}
    assert out.stop_reason == "tool_use"


def test_tool_schema_translated_to_openai_function_shape():
    session = FakeSession([_tool_call_response("record_x", {})])
    p = _provider(session)
    p.complete(
        system="",
        messages=[Message(role="user", content=[TextBlock(text="go")])],
        tools=[ToolSchema(name="record_x", description="records x", input_schema={"type": "object", "properties": {}})],
        model="test-model",
    )
    tools = session.calls[0]["json"]["tools"]
    assert tools == [{
        "type": "function",
        "function": {
            "name": "record_x",
            "description": "records x",
            "parameters": {"type": "object", "properties": {}},
        },
    }]


def test_tool_choice_forces_named_function():
    session = FakeSession([_tool_call_response("record_x", {})])
    p = _provider(session)
    p.complete(
        system="",
        messages=[Message(role="user", content=[TextBlock(text="go")])],
        tools=[ToolSchema(name="record_x", description="d", input_schema={})],
        model="test-model",
        tool_choice="record_x",
    )
    assert session.calls[0]["json"]["tool_choice"] == {
        "type": "function",
        "function": {"name": "record_x"},
    }


def test_image_block_becomes_data_uri():
    session = FakeSession([_text_response("ok")])
    p = _provider(session)
    p.complete(
        system="",
        messages=[Message(role="user", content=[
            ImageBlock(media_type="image/jpeg", data="Zm9v"),
        ])],
        tools=[],
        model="test-model",
    )
    parts = session.calls[0]["json"]["messages"][0]["content"]
    assert parts == [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,Zm9v"}}]


def test_tool_result_block_becomes_separate_tool_message():
    session = FakeSession([_text_response("ok")])
    p = _provider(session)
    p.complete(
        system="",
        messages=[
            Message(role="user", content=[TextBlock(text="x")]),
            Message(role="assistant", content=[
                ToolUseBlock(id="call_1", name="find_things", input={}),
            ]),
            Message(role="user", content=[
                ToolResultBlock(tool_use_id="call_1", content="[]"),
            ]),
        ],
        tools=[],
        model="test-model",
    )
    messages = session.calls[0]["json"]["messages"]
    tool_msg = messages[-1]
    assert tool_msg == {"role": "tool", "tool_call_id": "call_1", "content": "[]"}
    assistant_msg = messages[-2]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "find_things"


def test_missing_api_key_raises():
    session = FakeSession([])
    p = OpenAICompatibleProvider(
        name="testprov", base_url="https://example.test/v1",
        api_key_env="TESTPROV_API_KEY", prices={}, api_key=None, session=session,
    )
    with pytest.raises(ProviderError, match="TESTPROV_API_KEY"):
        p.complete(
            system="", messages=[Message(role="user", content=[TextBlock(text="x")])],
            tools=[], model="test-model",
        )


def test_http_error_status_surfaces_code_and_body_for_infra_detection():
    session = FakeSession([FakeResponse({"error": {"message": "insufficient_quota"}}, status_code=429)])
    p = _provider(session)
    with pytest.raises(ProviderError, match="429"):
        p.complete(
            system="", messages=[Message(role="user", content=[TextBlock(text="x")])],
            tools=[], model="test-model",
        )


def test_request_exception_wrapped_as_provider_error():
    class _BoomSession:
        def post(self, *args: Any, **kwargs: Any) -> Any:
            raise requests.ConnectionError("boom")

    p = _provider(_BoomSession())
    with pytest.raises(ProviderError, match="boom"):
        p.complete(
            system="", messages=[Message(role="user", content=[TextBlock(text="x")])],
            tools=[], model="test-model",
        )


def test_max_tokens_param_is_configurable():
    session = FakeSession([_text_response("ok")])
    p = _provider(session, max_tokens_param="max_tokens")
    p.complete(
        system="", messages=[Message(role="user", content=[TextBlock(text="x")])],
        tools=[], model="test-model", max_tokens=256,
    )
    body = session.calls[0]["json"]
    assert body["max_tokens"] == 256
    assert "max_completion_tokens" not in body


def test_default_max_tokens_param_is_max_completion_tokens():
    session = FakeSession([_text_response("ok")])
    p = _provider(session)
    p.complete(
        system="", messages=[Message(role="user", content=[TextBlock(text="x")])],
        tools=[], model="test-model", max_tokens=256,
    )
    body = session.calls[0]["json"]
    assert body["max_completion_tokens"] == 256
    assert "max_tokens" not in body


def test_cached_tokens_extracted_disjoint_from_input_tokens():
    # OpenAI's prompt_tokens (100) INCLUDES the 40 cached ones. The neutral Usage
    # contract keeps input_tokens and cache_read_tokens disjoint, so input_tokens
    # must be the fresh 60 — otherwise compute_cost_usd bills the cached 40 twice.
    session = FakeSession([FakeResponse({
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
    })])
    p = _provider(session)
    out = p.complete(
        system="", messages=[Message(role="user", content=[TextBlock(text="x")])],
        tools=[], model="test-model",
    )
    assert out.usage.cache_read_tokens == 40
    assert out.usage.input_tokens == 60


def test_cached_fraction_not_double_billed_in_cost():
    from api.providers.base import ModelPrice, compute_cost_usd

    session = FakeSession([FakeResponse({
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 1_000_000},
        },
    })])
    out = _provider(session).complete(
        system="", messages=[Message(role="user", content=[TextBlock(text="x")])],
        tools=[], model="test-model",
    )
    # A fully-cached 1M-token prompt should cost the cache_read rate ($0.025), not
    # input+cache_read ($0.275) — the ~11x over-bill this fix removes.
    price = ModelPrice(0.25, 2.00, 0.025, 0.0)
    assert compute_cost_usd(price=price, model="test-model", usage=out.usage) == 0.025


def test_cached_tokens_clamped_when_exceeding_prompt_tokens():
    session = FakeSession([FakeResponse({
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 30,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
    })])
    out = _provider(session).complete(
        system="", messages=[Message(role="user", content=[TextBlock(text="x")])],
        tools=[], model="test-model",
    )
    assert out.usage.input_tokens == 0
    assert out.usage.cache_read_tokens == 40


def test_price_for_looks_up_prices_dict():
    from api.providers.base import ModelPrice

    session = FakeSession([])
    p = OpenAICompatibleProvider(
        name="testprov", base_url="https://example.test/v1",
        api_key_env="TESTPROV_API_KEY", prices={"m1": ModelPrice(1.0, 2.0)},
        api_key="k", session=session,
    )
    assert p.price_for("m1") == ModelPrice(1.0, 2.0)
    assert p.price_for("unknown") is None
