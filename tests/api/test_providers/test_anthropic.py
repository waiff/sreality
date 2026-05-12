"""Tests for api.providers.anthropic — block translation + usage extraction.

Hermetic. Mocks `anthropic.Anthropic` via `sys.modules` and asserts:
- text + tool_use blocks come back in the right neutral shape,
- tool_result blocks are encoded to the SDK's expected form on the
  way in,
- cache token fields are pulled from usage when present.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from api.providers import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)
from api.providers.anthropic import AnthropicProvider


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _RawResponse:
    def __init__(
        self,
        *,
        text: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, int] | None = None,
        stop_reason: str = "end_turn",
        model: str = "claude-sonnet-4-5",
    ) -> None:
        blocks: list[_Block] = []
        if text:
            blocks.append(_Block(type="text", text=text))
        for tc in tool_calls or []:
            blocks.append(_Block(
                type="tool_use",
                id=tc.get("id", "tu_1"),
                name=tc["name"],
                input=tc.get("input", {}),
            ))
        self.content = blocks
        self.usage = usage or {}
        self.stop_reason = stop_reason
        self.model = model


class _FakeAnthropicSDK:
    """Captures the kwargs the SDK was called with."""

    def __init__(self, raw: _RawResponse) -> None:
        self._raw = raw
        self.calls: list[dict[str, Any]] = []
        self.messages = self  # messages.create(...) form

    def create(self, **kwargs: Any) -> _RawResponse:
        self.calls.append(kwargs)
        return self._raw


@pytest.fixture
def patch_anthropic(monkeypatch):
    """Install a fake anthropic module that returns the given _RawResponse."""

    def _install(raw: _RawResponse) -> _FakeAnthropicSDK:
        fake_sdk = _FakeAnthropicSDK(raw)
        fake_module = type("FakeAnthropicModule", (), {})()
        fake_module.Anthropic = lambda api_key=None: fake_sdk
        monkeypatch.setitem(sys.modules, "anthropic", fake_module)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        return fake_sdk

    return _install


def test_text_block_round_trips(patch_anthropic):
    sdk = patch_anthropic(_RawResponse(
        text="hello",
        usage={"input_tokens": 10, "output_tokens": 5},
    ))
    p = AnthropicProvider()
    out = p.complete(
        system="be terse",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        model="claude-sonnet-4-5",
    )
    assert out.text_blocks == ["hello"]
    assert out.tool_calls == []
    assert out.stop_reason == "end_turn"
    assert out.usage.input_tokens == 10
    assert out.usage.output_tokens == 5
    assert sdk.calls[0]["model"] == "claude-sonnet-4-5"
    assert sdk.calls[0]["system"] == "be terse"
    assert sdk.calls[0]["messages"][0]["content"][0] == {
        "type": "text", "text": "hi",
    }


def test_tool_use_blocks_extracted(patch_anthropic):
    patch_anthropic(_RawResponse(
        tool_calls=[{
            "id": "tu_42", "name": "find_things",
            "input": {"radius_m": 1500},
        }],
        usage={"input_tokens": 1, "output_tokens": 1},
        stop_reason="tool_use",
    ))
    p = AnthropicProvider()
    out = p.complete(
        system="",
        messages=[Message(role="user", content=[TextBlock(text="go")])],
        tools=[ToolSchema(
            name="find_things", description="d", input_schema={},
        )],
        model="claude-sonnet-4-5",
    )
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].id == "tu_42"
    assert out.tool_calls[0].name == "find_things"
    assert out.tool_calls[0].input == {"radius_m": 1500}
    assert out.stop_reason == "tool_use"


def test_tool_result_blocks_encoded_for_anthropic(patch_anthropic):
    sdk = patch_anthropic(_RawResponse(text="ok"))
    p = AnthropicProvider()
    p.complete(
        system="",
        messages=[
            Message(role="user", content=[TextBlock(text="x")]),
            Message(role="assistant", content=[
                ToolUseBlock(id="tu_1", name="find_things", input={}),
            ]),
            Message(role="user", content=[
                ToolResultBlock(tool_use_id="tu_1", content="[]"),
            ]),
        ],
        tools=[],
        model="claude-sonnet-4-5",
    )
    last = sdk.calls[0]["messages"][-1]["content"][0]
    assert last["type"] == "tool_result"
    assert last["tool_use_id"] == "tu_1"
    assert last["content"] == "[]"


def test_cache_tokens_extracted(patch_anthropic):
    patch_anthropic(_RawResponse(
        text="x",
        usage={
            "input_tokens": 100, "output_tokens": 10,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 5,
        },
    ))
    p = AnthropicProvider()
    out = p.complete(
        system="", messages=[
            Message(role="user", content=[TextBlock(text="x")]),
        ], tools=[], model="claude-sonnet-4-5",
    )
    assert out.usage.cache_read_tokens == 80
    assert out.usage.cache_write_tokens == 5


def test_cache_control_on_last_tool(patch_anthropic):
    sdk = patch_anthropic(_RawResponse(text="ok"))
    p = AnthropicProvider()
    p.complete(
        system="be terse",
        messages=[Message(role="user", content=[TextBlock(text="x")])],
        tools=[
            ToolSchema(name="first", description="d1", input_schema={}),
            ToolSchema(name="second", description="d2", input_schema={}),
        ],
        model="claude-sonnet-4-5",
    )
    sent_tools = sdk.calls[0]["tools"]
    assert "cache_control" not in sent_tools[0]
    assert sent_tools[-1]["cache_control"] == {"type": "ephemeral"}


def test_no_cache_control_when_tools_empty(patch_anthropic):
    sdk = patch_anthropic(_RawResponse(text="ok"))
    p = AnthropicProvider()
    p.complete(
        system="be terse",
        messages=[Message(role="user", content=[TextBlock(text="x")])],
        tools=[],
        model="claude-sonnet-4-5",
    )
    assert "tools" not in sdk.calls[0]


def test_missing_api_key_raises():
    p = AnthropicProvider(api_key=None)
    with pytest.raises(Exception, match="ANTHROPIC_API_KEY"):
        p.complete(
            system="", messages=[Message(role="user", content=[TextBlock(text="x")])],
            tools=[], model="claude-sonnet-4-5",
        )
