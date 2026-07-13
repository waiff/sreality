"""Tests for api.llm_client — the provider-agnostic orchestrator.

The Anthropic-specific block translation moved to
api/providers/anthropic.py; tests for that live in
tests/api/test_providers/test_anthropic.py. Here we cover:

- The DB-backed model + system-prompt lookup helpers.
- One call() end-to-end via a _ScriptedProvider, asserting the
  llm_calls row shape (provider, called_for, model, tokens, cost).
- The daily-cost soft warning behaviour.
- The legacy-shape -> neutral block translation that lets the URL
  parser / summarize / image-compare callers keep their dict shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from api import llm_client as lc
from api.providers import (
    Completion,
    ModelPrice,
    TextBlock,
    ToolCall,
)
from tests.api._fakes import _FakeConn, _ScriptedProvider, usage


# ----------------------------------------------------------------------
# resolve_model / resolve_system_prompt
# ----------------------------------------------------------------------

def test_resolve_model_reads_app_settings():
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-6"})
    client = lc.LLMClient(conn)
    assert client.resolve_model() == "claude-sonnet-4-6"


def test_resolve_model_falls_back_when_row_missing():
    conn = _FakeConn(app_settings={})
    client = lc.LLMClient(conn)
    assert client.resolve_model() == lc.DEFAULT_MODEL


def test_resolve_system_prompt_reads_app_settings():
    conn = _FakeConn(app_settings={"llm_parse_system_prompt": "Be excellent."})
    client = lc.LLMClient(conn)
    assert client.resolve_system_prompt() == "Be excellent."


def test_resolve_system_prompt_fallback_when_missing(caplog):
    conn = _FakeConn(app_settings={})
    client = lc.LLMClient(conn)
    with caplog.at_level("WARNING"):
        prompt = client.resolve_system_prompt()
    assert prompt == lc.DEFAULT_SYSTEM_PROMPT_FALLBACK
    assert any("missing" in m for m in caplog.messages)


# ----------------------------------------------------------------------
# call() end-to-end
# ----------------------------------------------------------------------

def _completion(
    *,
    text: str = "",
    tool_calls: list[ToolCall] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    model: str = "claude-sonnet-4-5",
) -> Completion:
    return Completion(
        text_blocks=[text] if text else [],
        tool_calls=tool_calls or [],
        stop_reason="end_turn" if not tool_calls else "tool_use",
        usage=usage(input_tokens, output_tokens),
        model=model,
    )


def _client_with(provider: _ScriptedProvider, conn: _FakeConn) -> lc.LLMClient:
    return lc.LLMClient(conn, providers={provider.name: provider})


def test_call_records_llm_calls_row():
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    prov = _ScriptedProvider(
        "anthropic",
        [_completion(text="Hello")],
        prices={"claude-sonnet-4-5": ModelPrice(3.0, 15.0, 0.30, 3.75)},
    )
    client = _client_with(prov, conn)

    resp = client.call(
        called_for="parse_url",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert resp.text == "Hello"
    assert resp.input_tokens == 100
    assert resp.output_tokens == 50
    assert resp.model == "claude-sonnet-4-5"
    assert resp.provider == "anthropic"
    # cost: 100*3/1e6 + 50*15/1e6 = 0.0003 + 0.00075 = 0.00105
    assert resp.cost_usd == pytest.approx(0.00105)
    assert resp.llm_call_id == 1
    assert len(conn.llm_calls_rows) == 1
    params = conn.llm_calls_rows[0]["params"]
    # New INSERT order: called_for, provider, model, in, out, cache_r, cache_w,
    # cost_usd, duration_ms, estimation_run_id.
    assert params[0] == "parse_url"
    assert params[1] == "anthropic"
    assert params[2] == "claude-sonnet-4-5"
    assert params[3] == 100
    assert params[4] == 50
    assert params[9] is None


def test_call_extracts_tool_use_blocks():
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    prov = _ScriptedProvider(
        "anthropic",
        [_completion(tool_calls=[ToolCall(
            id="tu_1", name="record_listing", input={"area_m2": 65},
        )])],
        prices={"claude-sonnet-4-5": ModelPrice(3.0, 15.0)},
    )
    client = _client_with(prov, conn)

    resp = client.call(
        called_for="parse_url",
        messages=[{"role": "user", "content": "x"}],
        tools=[{"name": "record_listing", "description": "", "input_schema": {}}],
    )
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["name"] == "record_listing"
    assert resp.tool_calls[0]["input"] == {"area_m2": 65}


# ----------------------------------------------------------------------
# provider_for_model + provider derivation in call()
# ----------------------------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("gpt-5-mini", "openai"),
    ("gpt-5.4-mini", "openai"),
    ("o3", "openai"),
    ("o4-mini", "openai"),
    ("qwen3-vl-235b-a22b-instruct", "qwen"),
    ("gemini-3.1-flash-lite", "gemini"),
    ("claude-sonnet-4-5", "anthropic"),
    ("claude-haiku-4-5", "anthropic"),
    ("", "anthropic"),
])
def test_provider_for_model(model, expected):
    assert lc.provider_for_model(model) == expected


def test_call_derives_openai_provider_from_gpt_model():
    """A caller that passes only a gpt-* model (a dedup lane reading its
    app_settings value) is routed to OpenAI without threading a provider arg."""
    conn = _FakeConn(app_settings={})
    prov_a = _ScriptedProvider(
        "anthropic", [_completion(text="A")],
        prices={"claude-sonnet-4-5": ModelPrice(3.0, 15.0)},
    )
    prov_o = _ScriptedProvider(
        "openai", [_completion(text="O", model="gpt-5-mini")],
        prices={"gpt-5-mini": ModelPrice(0.25, 2.0)},
    )
    client = lc.LLMClient(conn, providers={"anthropic": prov_a, "openai": prov_o})

    resp = client.call(
        called_for="compare_listings_visually",
        messages=[{"role": "user", "content": "x"}],
        model="gpt-5-mini",
    )
    assert resp.provider == "openai"
    assert resp.text == "O"
    assert conn.llm_calls_rows[0]["params"][1] == "openai"


def test_call_explicit_provider_overrides_derivation():
    """An explicit provider wins over the model-id heuristic."""
    conn = _FakeConn(app_settings={})
    prov_a = _ScriptedProvider(
        "anthropic", [_completion(text="A", model="gpt-5-mini")],
        prices={"gpt-5-mini": ModelPrice(3.0, 15.0)},
    )
    client = lc.LLMClient(conn, providers={"anthropic": prov_a})
    resp = client.call(
        called_for="parse_url",
        messages=[{"role": "user", "content": "x"}],
        model="gpt-5-mini",
        provider="anthropic",
    )
    assert resp.provider == "anthropic"


def test_call_propagates_estimation_run_id():
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    prov = _ScriptedProvider("anthropic", [_completion(text="ok", input_tokens=1, output_tokens=1)])
    client = _client_with(prov, conn)
    client.call(
        called_for="parse_url",
        messages=[{"role": "user", "content": "x"}],
        estimation_run_id=42,
    )
    assert conn.llm_calls_rows[0]["params"][9] == 42


def test_call_uses_explicit_provider_kwarg():
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    g_prov = _ScriptedProvider("gemini", [_completion(text="g")])
    client = lc.LLMClient(conn, providers={"gemini": g_prov})
    client.call(
        called_for="agent_estimation",
        messages=[{"role": "user", "content": "x"}],
        provider="gemini",
    )
    assert conn.llm_calls_rows[0]["params"][1] == "gemini"


def test_call_forwards_tool_choice_when_set():
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    prov = _ScriptedProvider("anthropic", [_completion(text="ok")])
    client = _client_with(prov, conn)
    client.call(
        called_for="enrich_listing_description",
        messages=[{"role": "user", "content": "x"}],
        tools=[{"name": "record_listing", "description": "", "input_schema": {}}],
        tool_choice="record_listing",
    )
    assert prov.calls[0]["tool_choice"] == "record_listing"


def test_call_omits_tool_choice_for_providers_without_support():
    """A provider (or fake) whose complete() predates the tool_choice
    parameter must keep working for callers that don't use it — the
    kwarg is only forwarded when set."""
    from api.providers import Usage

    class _Legacy:
        name = "legacy"

        def complete(self, *, system, messages, tools, model, max_tokens=4096):
            return Completion(
                text_blocks=["ok"], tool_calls=[], stop_reason="end_turn",
                usage=Usage(input_tokens=1, output_tokens=1), model=model,
            )

        def price_for(self, model):
            return None

    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    client = lc.LLMClient(conn, providers={"legacy": _Legacy()})
    resp = client.call(
        called_for="parse_url",
        messages=[{"role": "user", "content": "x"}],
        provider="legacy",
    )
    assert resp.text == "ok"


def test_call_raises_on_unknown_provider():
    from api.providers import ProviderError
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    client = lc.LLMClient(conn, providers={})
    with pytest.raises(ProviderError, match="not configured"):
        client.call(
            called_for="parse_url",
            messages=[{"role": "user", "content": "x"}],
            provider="missing",
        )


# ----------------------------------------------------------------------
# Daily-cost soft guardrail
# ----------------------------------------------------------------------

def _heavy_call(monkeypatch, conn, input_tokens, output_tokens):
    """Run a call producing a known cost via the scripted provider."""
    prov = _ScriptedProvider(
        "anthropic",
        [_completion(input_tokens=input_tokens, output_tokens=output_tokens)],
        prices={"claude-sonnet-4-5": ModelPrice(3.0, 15.0)},
    )
    client = lc.LLMClient(conn, providers={"anthropic": prov})
    return client.call(
        called_for="parse_url",
        messages=[{"role": "user", "content": "x"}],
    )


def test_cost_guard_does_not_warn_under_threshold(monkeypatch, caplog):
    monkeypatch.setenv("LLM_DAILY_COST_WARN_USD", "5.0")
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    with caplog.at_level("WARNING"):
        _heavy_call(monkeypatch, conn, 1_000_000, 100_000)  # $4.50
    assert not any("crossed soft threshold" in m for m in caplog.messages)


def test_cost_guard_warns_on_threshold_crossing(monkeypatch, caplog):
    monkeypatch.setenv("LLM_DAILY_COST_WARN_USD", "5.0")
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    with caplog.at_level("WARNING"):
        _heavy_call(monkeypatch, conn, 2_000_000, 1_000_000)  # $21
    assert any("crossed soft threshold" in m for m in caplog.messages)


def test_cost_guard_uses_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("LLM_DAILY_COST_WARN_USD", raising=False)
    assert lc._resolve_threshold() == lc.DEFAULT_DAILY_COST_WARN_USD


def test_cost_guard_falls_back_on_invalid_env(monkeypatch, caplog):
    monkeypatch.setenv("LLM_DAILY_COST_WARN_USD", "not a number")
    with caplog.at_level("WARNING"):
        assert lc._resolve_threshold() == lc.DEFAULT_DAILY_COST_WARN_USD
    assert any("invalid" in m for m in caplog.messages)


# ----------------------------------------------------------------------
# Legacy block translation
# ----------------------------------------------------------------------

def test_legacy_string_message_becomes_text_block():
    m = lc._to_neutral_message({"role": "user", "content": "hello"})
    assert m.role == "user"
    assert len(m.content) == 1
    assert isinstance(m.content[0], TextBlock)
    assert m.content[0].text == "hello"


def test_legacy_tool_result_block_round_trips():
    raw: dict[str, Any] = {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "tu_1",
            "content": "x",
            "is_error": True,
        }],
    }
    m = lc._to_neutral_message(raw)
    assert m.content[0].tool_use_id == "tu_1"
    assert m.content[0].is_error is True


def test_legacy_image_block_becomes_image_block_not_text():
    # Regression: a vision image dict must convert to ImageBlock, NOT fall
    # through to the str() fallback (which sent the base64 as TEXT, blowing the
    # token limit and sending no actual image to the model).
    from api.providers import ImageBlock
    from api.providers.anthropic import _msg_to_anthropic

    raw: dict[str, Any] = {
        "role": "user",
        "content": [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"},
        }],
    }
    m = lc._to_neutral_message(raw)
    assert isinstance(m.content[0], ImageBlock)
    assert m.content[0].data == "QUJD"
    assert m.content[0].media_type == "image/jpeg"

    # round-trips back to a real Anthropic image block, not a text block
    out = _msg_to_anthropic(m)
    block = out["content"][0]
    assert block["type"] == "image"
    assert block["source"]["data"] == "QUJD"


def test_parse_tool_input_json_handles_dict_and_string():
    assert lc.parse_tool_input_json({"a": 1}) == {"a": 1}
    assert lc.parse_tool_input_json('{"a": 2}') == {"a": 2}
    with pytest.raises(ValueError):
        lc.parse_tool_input_json(123)
