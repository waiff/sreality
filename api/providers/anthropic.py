"""Anthropic backend for the CompletionProvider protocol.

Wraps the official `anthropic` Python SDK. Owns the model-pricing
dict and the block-shape translation between Anthropic content
blocks and the neutral types in `api.providers.base`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from collections.abc import Iterator

from api.providers.base import (
    BatchResultItem,
    BatchStatus,
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
    "claude-haiku-4-5":  ModelPrice(1.0, 5.0, 0.10, 1.25),
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
        client = self._sdk_client()
        kwargs = self._request_kwargs(
            system=system,
            messages=[_msg_to_anthropic(m) for m in messages],
            tools=[_tool_to_anthropic(t) for t in tools],
            model=model,
            max_tokens=max_tokens,
        )
        try:
            raw = client.messages.create(**kwargs)
        except Exception as exc:
            raise ProviderError(f"anthropic call failed: {exc}") from exc
        return self._completion_from_raw(raw, fallback_model=model)

    def price_for(self, model: str) -> ModelPrice | None:
        return PRICES.get(model)

    # --- Message Batches API ----------------------------------------------

    def build_batch_request_params(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Build one batch request's `params` from Anthropic-shaped dicts.

        `messages` / `tools` are already in Anthropic content-block /
        tool-schema form (what `toolkit.condition_scoring.build_scoring_request`
        emits), so no neutral-type round-trip is needed. The cache_control
        placement matches `complete()` exactly, so the shared system+tools
        prefix is a cache hit across every request in the batch.
        """
        return self._request_kwargs(
            system=system,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
        )

    def submit_batch(self, items: list[tuple[str, dict[str, Any]]]) -> str:
        """Create a Message Batch. `items` is [(custom_id, params), ...]."""
        if not items:
            raise ProviderError("submit_batch called with no requests")
        client = self._sdk_client()
        requests = [
            {"custom_id": custom_id, "params": params}
            for custom_id, params in items
        ]
        try:
            batch = client.messages.batches.create(requests=requests)
        except Exception as exc:
            raise ProviderError(f"anthropic batch create failed: {exc}") from exc
        return str(batch.id)

    def poll_batch(self, provider_batch_id: str) -> BatchStatus:
        client = self._sdk_client()
        try:
            batch = client.messages.batches.retrieve(provider_batch_id)
        except Exception as exc:
            raise ProviderError(f"anthropic batch retrieve failed: {exc}") from exc
        raw_status = str(getattr(batch, "processing_status", "") or "")
        rc = getattr(batch, "request_counts", None)
        counts: dict[str, int] = {}
        if rc is not None:
            for key in ("processing", "succeeded", "errored", "canceled", "expired"):
                counts[key] = int(_attr(rc, key) or 0)
        return BatchStatus(
            provider_batch_id=provider_batch_id,
            ended=(raw_status == "ended"),
            raw_status=raw_status,
            counts=counts,
        )

    def iter_batch_results(
        self, provider_batch_id: str
    ) -> Iterator[BatchResultItem]:
        client = self._sdk_client()
        try:
            results = client.messages.batches.results(provider_batch_id)
        except Exception as exc:
            raise ProviderError(f"anthropic batch results failed: {exc}") from exc
        for result in results:
            custom_id = str(getattr(result, "custom_id", "") or "")
            outcome = getattr(result, "result", None)
            rtype = str(_attr(outcome, "type") or "")
            if rtype == "succeeded":
                yield BatchResultItem(
                    custom_id=custom_id,
                    status="succeeded",
                    completion=self._completion_from_raw(
                        _attr(outcome, "message"), fallback_model="",
                    ),
                )
            elif rtype == "errored":
                yield BatchResultItem(
                    custom_id=custom_id,
                    status="errored",
                    error=str(_attr(outcome, "error") or "errored"),
                )
            else:
                yield BatchResultItem(
                    custom_id=custom_id,
                    status=rtype if rtype in ("canceled", "expired") else "errored",
                    error=rtype or "unknown",
                )

    # --- internals --------------------------------------------------------

    def _request_kwargs(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            # Wrap as a list-of-blocks with cache_control so the system
            # prompt becomes part of Anthropic's cached prefix. String
            # form is NOT cache-eligible. Anthropic silently no-ops
            # cache_control when the prefix is below the model's minimum,
            # so this is safe to set unconditionally.
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            tool_dicts = [dict(t) for t in tools]
            # One cache breakpoint at the end of tools captures
            # system + tools as a single cached prefix.
            tool_dicts[-1] = {**tool_dicts[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = tool_dicts
        return kwargs

    def _completion_from_raw(self, raw: Any, *, fallback_model: str) -> Completion:
        text_blocks, tool_calls = _split_anthropic_content(raw)
        usage = _extract_anthropic_usage(raw)
        stop_reason = _normalise_stop_reason(getattr(raw, "stop_reason", None))
        return Completion(
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            model=getattr(raw, "model", fallback_model) or fallback_model,
            raw=raw,
        )

    def _sdk_client(self) -> Any:
        if not self._api_key:
            raise ProviderError(
                "ANTHROPIC_API_KEY is not set; cannot call Anthropic"
            )
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
