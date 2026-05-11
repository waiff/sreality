"""Tests for api.providers.gemini — block translation + usage extraction.

Hermetic. Mocks `google.genai` via `sys.modules`. The fake types
module is intentionally permissive — we capture the kwargs and the
constructed Content/Part shapes so we can assert role mapping,
function_call extraction, and usage_metadata translation.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from api.providers import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)
from api.providers.gemini import GeminiProvider


# Minimal fakes mimicking google.genai.types shapes.

class _FakePart:
    @staticmethod
    def from_text(*, text: str) -> "_FakePart":
        p = _FakePart()
        p.text = text
        return p

    def __init__(self, function_call: Any = None, function_response: Any = None) -> None:
        self.text = None
        self.function_call = function_call
        self.function_response = function_response


class _FakeFunctionCall:
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.name = name
        self.args = args


class _FakeFunctionResponse:
    def __init__(self, name: str, response: dict[str, Any]) -> None:
        self.name = name
        self.response = response


class _FakeFunctionDeclaration:
    def __init__(self, name: str, description: str, parameters: dict[str, Any]) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters


class _FakeTool:
    def __init__(self, function_declarations: list[_FakeFunctionDeclaration]) -> None:
        self.function_declarations = function_declarations


class _FakeContent:
    def __init__(self, role: str, parts: list[_FakePart]) -> None:
        self.role = role
        self.parts = parts


class _FakeGenerateConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeUsageMeta:
    def __init__(self, prompt_token_count: int, candidates_token_count: int, cached_content_token_count: int = 0) -> None:
        self.prompt_token_count = prompt_token_count
        self.candidates_token_count = candidates_token_count
        self.cached_content_token_count = cached_content_token_count


class _FakeCandidate:
    def __init__(self, content: _FakeContent, finish_reason: str = "STOP") -> None:
        self.content = content
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, candidates: list[_FakeCandidate], usage: _FakeUsageMeta) -> None:
        self.candidates = candidates
        self.usage_metadata = usage


class _FakeModels:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: list[Any], config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.models = _FakeModels(response)


@pytest.fixture
def patch_gemini(monkeypatch):
    """Install fake google.genai + google.genai.types modules."""

    def _install(response: _FakeResponse) -> _FakeClient:
        client = _FakeClient(response)

        types_mod = types.ModuleType("google.genai.types")
        types_mod.Part = _FakePart
        types_mod.FunctionCall = _FakeFunctionCall
        types_mod.FunctionResponse = _FakeFunctionResponse
        types_mod.FunctionDeclaration = _FakeFunctionDeclaration
        types_mod.Tool = _FakeTool
        types_mod.Content = _FakeContent
        types_mod.GenerateContentConfig = _FakeGenerateConfig

        genai_mod = types.ModuleType("google.genai")
        genai_mod.types = types_mod
        genai_mod.Client = lambda api_key=None: client

        google_mod = types.ModuleType("google")
        google_mod.genai = genai_mod

        monkeypatch.setitem(sys.modules, "google", google_mod)
        monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
        monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        return client

    return _install


def _text_response(text: str, **usage_kw: int) -> _FakeResponse:
    return _FakeResponse(
        candidates=[_FakeCandidate(
            content=_FakeContent("model", [_FakePart.from_text(text=text)]),
        )],
        usage=_FakeUsageMeta(
            prompt_token_count=usage_kw.get("input_tokens", 10),
            candidates_token_count=usage_kw.get("output_tokens", 5),
            cached_content_token_count=usage_kw.get("cache_read_tokens", 0),
        ),
    )


def _tool_call_response(name: str, args: dict[str, Any]) -> _FakeResponse:
    parts = [_FakePart(function_call=_FakeFunctionCall(name, args))]
    return _FakeResponse(
        candidates=[_FakeCandidate(
            content=_FakeContent("model", parts),
            finish_reason="TOOL_USE",
        )],
        usage=_FakeUsageMeta(prompt_token_count=1, candidates_token_count=1),
    )


def test_text_block_round_trips(patch_gemini):
    client = patch_gemini(_text_response("hi back", input_tokens=12, output_tokens=4))
    p = GeminiProvider()
    out = p.complete(
        system="be terse",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        model="gemini-2.5-pro",
    )
    assert out.text_blocks == ["hi back"]
    assert out.usage.input_tokens == 12
    assert out.usage.output_tokens == 4
    assert client.models.calls[0]["config"].kwargs["system_instruction"] == "be terse"


def test_role_assistant_is_mapped_to_model(patch_gemini):
    client = patch_gemini(_text_response("ok"))
    p = GeminiProvider()
    p.complete(
        system="",
        messages=[
            Message(role="user", content=[TextBlock(text="hi")]),
            Message(role="assistant", content=[TextBlock(text="response")]),
        ],
        tools=[],
        model="gemini-2.5-pro",
    )
    contents = client.models.calls[0]["contents"]
    assert contents[0].role == "user"
    assert contents[1].role == "model"


def test_function_call_extracted(patch_gemini):
    patch_gemini(_tool_call_response("find_things", {"radius_m": 1500}))
    p = GeminiProvider()
    out = p.complete(
        system="",
        messages=[Message(role="user", content=[TextBlock(text="go")])],
        tools=[ToolSchema(
            name="find_things", description="d", input_schema={},
        )],
        model="gemini-2.5-pro",
    )
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].name == "find_things"
    assert out.tool_calls[0].input == {"radius_m": 1500}
    assert out.stop_reason == "tool_use"


def test_tool_result_block_emits_function_response(patch_gemini):
    client = patch_gemini(_text_response("ok"))
    p = GeminiProvider()
    p.complete(
        system="",
        messages=[
            Message(role="user", content=[TextBlock(text="x")]),
            Message(role="assistant", content=[
                ToolUseBlock(id="find_things", name="find_things", input={}),
            ]),
            Message(role="user", content=[
                ToolResultBlock(tool_use_id="find_things", content="[]"),
            ]),
        ],
        tools=[],
        model="gemini-2.5-pro",
    )
    last_content = client.models.calls[0]["contents"][-1]
    assert last_content.parts[0].function_response is not None
    assert last_content.parts[0].function_response.name == "find_things"


def test_missing_api_key_raises():
    p = GeminiProvider(api_key=None)
    with pytest.raises(Exception, match="GEMINI_API_KEY"):
        p.complete(
            system="", messages=[
                Message(role="user", content=[TextBlock(text="x")]),
            ], tools=[], model="gemini-2.5-pro",
        )
