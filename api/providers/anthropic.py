"""Anthropic backend for the CompletionProvider protocol.

Wraps the official `anthropic` Python SDK. Owns the model-pricing
dict and the block-shape translation between Anthropic content
blocks and the neutral types in `api.providers.base`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from api.providers.base import (
    Completion,
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


# Source: docs.anthropic.com. Update when prices change or when a
# caller adds a new model.
PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-4-5": ModelPrice(3.0, 15.0, 0.30, 3.75),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0, 0.30, 3.75),
    "claude-opus-4-7":   ModelPrice(15.0, 75.0, 1.50, 18.75),
}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client: Any | None = None

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
        model: str,
        max_tokens: int = 4096,
    ) -> Completion:
        if not self._api_key:
            raise ProviderError(
                "ANTHROPIC_API_KEY is not set; cannot call Anthropic"
            )
        client = self._sdk_client()

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [_msg_to_anthropic(m) for m in messages],
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [_tool_to_anthropic(t) for t in tools]

        try:
            raw = client.messages.create(**kwargs)
        except Exception as exc:
            raise ProviderError(f"anthropic call failed: {exc}") from exc

        text_blocks, tool_calls = _split_anthropic_content(raw)
        usage = _extract_anthropic_usage(raw)
        stop_reason = _normalise_stop_reason(getattr(raw, "stop_reason", None))
        return Completion(
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            model=getattr(raw, "model", model),
            raw=raw,
        )

    def price_for(self, model: str) -> ModelPrice | None:
        return PRICES.get(model)

    def _sdk_client(self) -> Any:
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client


# --- block conversions ----------------------------------------------------

def _msg_to_anthropic(msg: Message) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            content.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        elif isinstance(block, ToolResultBlock):
            entry: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
            }
            if block.is_error:
                entry["is_error"] = True
            content.append(entry)
    return {"role": msg.role, "content": content}


def _tool_to_anthropic(tool: ToolSchema) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def _split_anthropic_content(raw: Any) -> tuple[list[str], list[ToolCall]]:
    blocks = getattr(raw, "content", None) or []
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in blocks:
        kind = _attr(block, "type")
        if kind == "text":
            texts.append(_attr(block, "text") or "")
        elif kind == "tool_use":
            tool_calls.append(ToolCall(
                id=str(_attr(block, "id") or ""),
                name=str(_attr(block, "name") or ""),
                input=_attr(block, "input") or {},
            ))
    return texts, tool_calls


def _extract_anthropic_usage(raw: Any) -> Usage:
    usage = getattr(raw, "usage", None)
    if usage is None:
        return Usage()
    def _g(name: str) -> int:
        if isinstance(usage, dict):
            return int(usage.get(name, 0) or 0)
        return int(getattr(usage, name, 0) or 0)
    return Usage(
        input_tokens=_g("input_tokens"),
        output_tokens=_g("output_tokens"),
        cache_read_tokens=_g("cache_read_input_tokens"),
        cache_write_tokens=_g("cache_creation_input_tokens"),
    )


def _normalise_stop_reason(raw: Any) -> str:
    # Anthropic uses: "end_turn" | "tool_use" | "max_tokens" | "stop_sequence"
    if raw == "stop_sequence":
        return "stop"
    if raw in ("end_turn", "tool_use", "max_tokens"):
        return raw
    return "end_turn"


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
