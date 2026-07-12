"""Gemini backend for the CompletionProvider protocol.

Wraps the current Google Gen AI SDK (`google-genai`). The older
`google-generativeai` package is deprecated; do not use it.

Translation between neutral blocks and Gemini's typed parts is the
bulk of this file. A few quirks worth knowing:

- Gemini uses "user" and "model" for roles; we map "assistant" ->
  "model" on the way out, and "model" -> "assistant" on the way in.
- Tool results are encoded as `function_response` parts on a `user`
  turn (not on a separate "tool" role). The tool's `name` must match
  the original function_call's name, so we carry it through.
- Cache-write isn't a public metric on Gemini's response yet; we
  always report `cache_write_tokens=0`. Cache-read maps to
  `cached_content_token_count`.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from api.providers.base import (
    Completion,
    ImageBlock,
    Message,
    ModelPrice,
    ProviderError,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)

LOG = logging.getLogger(__name__)


# Source: ai.google.dev/gemini-api/docs/pricing (verified 2026-07-11). Prices are the
# <=200K-context tier; >200K bills higher (our vision payloads never approach 200K).
# The 2.5 series is documented but CLOSED TO NEW PROJECTS — the API returns 404
# "no longer available to new users" (measured on the freshly-billed sreality key),
# so current work targets the 3.x ids. Any model an app_settings row or harness
# dispatch names MUST be here or its llm_calls rows record cost_usd=0.
PRICES: dict[str, ModelPrice] = {
    "gemini-3.1-pro-preview": ModelPrice(2.00, 12.0, 0.20, 0.0),
    "gemini-3.5-flash":       ModelPrice(1.50, 9.0,  0.15, 0.0),
    "gemini-2.5-pro":         ModelPrice(1.25, 10.0, 0.31, 0.0),
    "gemini-2.5-flash":       ModelPrice(0.30, 2.5,  0.075, 0.0),
}


class GeminiProvider:
    name = "gemini"

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self._client: Any | None = None
        self._types: Any | None = None

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
        model: str,
        max_tokens: int = 4096,
        tool_choice: str | None = None,
    ) -> Completion:
        if not self._api_key:
            raise ProviderError(
                "GEMINI_API_KEY is not set; cannot call Gemini"
            )
        client = self._sdk_client()
        types = self._sdk_types()

        # Build contents (Gemini's term for the message list).
        contents = [_msg_to_gemini(m, types) for m in messages]

        # Tools become a single Tool with N function_declarations.
        cfg_kwargs: dict[str, Any] = {
            "max_output_tokens": max_tokens,
        }
        if system:
            cfg_kwargs["system_instruction"] = system
        if tools:
            cfg_kwargs["tools"] = [
                types.Tool(
                    function_declarations=[_tool_to_gemini(t) for t in tools]
                )
            ]
        if tool_choice and tools:
            # Force the named function: "ANY" mode restricted to one name.
            # Plain dict — GenerateContentConfig coerces it to ToolConfig.
            cfg_kwargs["tool_config"] = {
                "function_calling_config": {
                    "mode": "ANY",
                    "allowed_function_names": [tool_choice],
                }
            }

        try:
            raw = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
        except Exception as exc:
            raise ProviderError(f"gemini call failed: {exc}") from exc

        text_blocks, tool_calls = _split_gemini_content(raw)
        usage = _extract_gemini_usage(raw)
        stop_reason = _normalise_gemini_finish(raw, tool_calls)
        return Completion(
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            model=model,
            raw=raw,
        )

    def price_for(self, model: str) -> ModelPrice | None:
        return PRICES.get(model)

    def _sdk_client(self) -> Any:
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _sdk_types(self) -> Any:
        if self._types is None:
            from google.genai import types  # noqa: I001
            self._types = types
        return self._types


# --- block conversions ----------------------------------------------------

def _msg_to_gemini(msg: Message, types: Any) -> Any:
    parts: list[Any] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            parts.append(types.Part.from_text(text=block.text))
        elif isinstance(block, ToolUseBlock):
            parts.append(types.Part(
                function_call=types.FunctionCall(
                    name=block.name,
                    args=block.input,
                ),
            ))
        elif isinstance(block, ToolResultBlock):
            # Gemini expects the function NAME, not the call id.
            # We stash it in tool_use_id (the agent loop uses the
            # tool name as the id, so this is consistent).
            parts.append(types.Part(
                function_response=types.FunctionResponse(
                    name=block.tool_use_id,
                    response={
                        "error": block.content if block.is_error else None,
                        "result": None if block.is_error else block.content,
                    },
                ),
            ))
        elif isinstance(block, ImageBlock):
            parts.append(types.Part.from_bytes(
                data=base64.b64decode(block.data),
                mime_type=block.media_type,
            ))
    role = "model" if msg.role == "assistant" else "user"
    return types.Content(role=role, parts=parts)


# JSON-Schema keys Gemini's FunctionDeclaration.parameters rejects with a 400
# INVALID_ARGUMENT ("Unknown name additional_properties ... Cannot find field").
# Our Anthropic-shaped tool schemas set additionalProperties: false throughout;
# Gemini's OpenAPI-subset schema has no such field, so it is dropped (Gemini is
# lenient about extra keys in tool ARGUMENTS anyway — the extractors ignore
# unknown keys). Found by the first live Gemini harness smoke run (2026-07-11:
# every call 400'd before the model was ever reached).
_GEMINI_UNSUPPORTED_SCHEMA_KEYS = frozenset({"additionalProperties", "$schema"})


def _schema_for_gemini(node: Any) -> Any:
    """Recursively strip JSON-Schema keys Gemini's schema parser rejects."""
    if isinstance(node, dict):
        return {
            k: _schema_for_gemini(v)
            for k, v in node.items()
            if k not in _GEMINI_UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(node, list):
        return [_schema_for_gemini(x) for x in node]
    return node


def _tool_to_gemini(tool: ToolSchema) -> Any:
    # google.genai.types.FunctionDeclaration accepts a JSON Schema dict
    # via `parameters` — same shape Anthropic uses as `input_schema`, minus
    # the keys Gemini's OpenAPI subset rejects (_schema_for_gemini).
    # Late import to avoid module-level dependency.
    from google.genai import types
    return types.FunctionDeclaration(
        name=tool.name,
        description=tool.description,
        parameters=_schema_for_gemini(tool.input_schema),
    )


def _split_gemini_content(raw: Any) -> tuple[list[str], list[ToolCall]]:
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    candidates = getattr(raw, "candidates", None) or []
    if not candidates:
        return texts, tool_calls
    parts = getattr(candidates[0].content, "parts", None) or []
    for part in parts:
        text = getattr(part, "text", None)
        if text:
            texts.append(text)
        fcall = getattr(part, "function_call", None)
        if fcall is not None:
            name = getattr(fcall, "name", "") or ""
            args = getattr(fcall, "args", None) or {}
            # SDK returns args as a dict-like; coerce to plain dict.
            if not isinstance(args, dict):
                args = dict(args)
            tool_calls.append(ToolCall(
                # Gemini doesn't issue ids; the tool name is unique
                # within one turn (parallel calls aren't supported in
                # slice 1), so use it as the id too.
                id=name,
                name=name,
                input=args,
            ))
    return texts, tool_calls


def _extract_gemini_usage(raw: Any) -> Usage:
    meta = getattr(raw, "usage_metadata", None)
    if meta is None:
        return Usage()
    def _g(name: str) -> int:
        return int(getattr(meta, name, 0) or 0)
    return Usage(
        input_tokens=_g("prompt_token_count"),
        output_tokens=_g("candidates_token_count"),
        cache_read_tokens=_g("cached_content_token_count"),
        cache_write_tokens=0,
    )


def _normalise_gemini_finish(raw: Any, tool_calls: list[ToolCall]) -> str:
    if tool_calls:
        return "tool_use"
    candidates = getattr(raw, "candidates", None) or []
    if not candidates:
        return "end_turn"
    finish = getattr(candidates[0], "finish_reason", None)
    finish_str = str(finish) if finish is not None else ""
    if "MAX_TOKENS" in finish_str:
        return "max_tokens"
    if "STOP" in finish_str:
        return "end_turn"
    return "end_turn"
