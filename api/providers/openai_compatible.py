"""Shared backend for any OpenAI Chat-Completions-shaped API.

OpenAI itself and Alibaba DashScope's "OpenAI-compatible mode" (Qwen) speak the
same wire format: POST {base_url}/chat/completions with Bearer auth, `tools` +
`tool_choice` function-calling, and `image_url` vision content parts. One class
parameterized by base_url/api_key/prices covers both — api/providers/openai.py
and api/providers/qwen.py are thin configuration, not near-duplicate files.

Built on `requests` (already a base dependency — rule #7 needs no new SDK for
this; the `anthropic` / `google-genai` packages earn their keep on richer
non-REST surfaces, like Anthropic's Batches API, that these two providers don't
use here).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import requests

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

_TIMEOUT_S = 120


class OpenAICompatibleProvider:
    """One backend for any OpenAI Chat-Completions-shaped endpoint.

    `max_tokens_param` differs by target and is NOT interchangeable: OpenAI's
    GPT-5 generation rejects `max_tokens` with a 400 ("use max_completion_tokens")
    per developers.openai.com/api/docs/models/gpt-5-mini; DashScope's compatible
    mode documents `max_tokens` for non-thinking (Instruct) models and reserves
    `max_completion_tokens` for thinking-mode chain-of-thought budgets. Neither
    claim has been exercised against a live key from this repo — the harness's
    first real call per provider is the actual verification (mirrors the Gemini
    `additionalProperties` lesson documented in gemini.py: the first live smoke
    call is what actually proves a wire-format assumption, not another doc read).
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key_env: str,
        prices: dict[str, ModelPrice],
        max_tokens_param: str = "max_completion_tokens",
        api_key: str | None = None,
        session: Any = None,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key_env = api_key_env
        self._api_key = api_key or os.environ.get(api_key_env)
        self._prices = prices
        self._max_tokens_param = max_tokens_param
        self._session = session or requests

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
                f"{self._api_key_env} is not set; cannot call {self.name}"
            )
        body: dict[str, Any] = {
            "model": model,
            "messages": _messages_to_openai(system, messages),
            self._max_tokens_param: max_tokens,
        }
        if tools:
            body["tools"] = [_tool_to_openai(t) for t in tools]
            if tool_choice:
                body["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tool_choice},
                }

        try:
            resp = self._session.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"{self.name} call failed: {exc}") from exc

        if resp.status_code >= 400:
            # Keep the status code + body IN the message text (not just logged) —
            # _is_infra_error (scripts/validate_vision_models.py) keyword-matches the
            # exception string to tell a dead key / exhausted quota apart from a real
            # verdict miss, and it needs "429" / "401" / etc. to actually appear here.
            raise ProviderError(
                f"{self.name} call failed: HTTP {resp.status_code} {resp.text[:500]}"
            )
        return _completion_from_raw(resp.json(), model=model)

    def price_for(self, model: str) -> ModelPrice | None:
        return self._prices.get(model)


# --- block conversions ----------------------------------------------------

def _messages_to_openai(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for msg in messages:
        out.extend(_msg_to_openai(msg))
    return out


def _msg_to_openai(msg: Message) -> list[dict[str, Any]]:
    """One neutral Message can expand to >1 OpenAI message: a tool result is its
    own `role: "tool"` message, not a content part on the user/assistant turn."""
    role = "assistant" if msg.role == "assistant" else "user"
    content_parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_result_msgs: list[dict[str, Any]] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            content_parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{block.media_type};base64,{block.data}"},
            })
        elif isinstance(block, ToolUseBlock):
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {"name": block.name, "arguments": json.dumps(block.input)},
            })
        elif isinstance(block, ToolResultBlock):
            tool_result_msgs.append({
                "role": "tool",
                "tool_call_id": block.tool_use_id,
                "content": block.content,
            })
    out: list[dict[str, Any]] = []
    if content_parts or tool_calls:
        entry: dict[str, Any] = {"role": role, "content": content_parts or None}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        out.append(entry)
    out.extend(tool_result_msgs)
    return out


def _tool_to_openai(tool: ToolSchema) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _completion_from_raw(raw: dict[str, Any], *, model: str) -> Completion:
    choices = raw.get("choices") or []
    if not choices:
        raise ProviderError(f"{model}: no choices in response: {raw}")
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    text_blocks = [text] if text else []

    tool_calls: list[ToolCall] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args_raw = fn.get("arguments")
        args: dict[str, Any] = {}
        if isinstance(args_raw, dict):
            args = args_raw
        elif isinstance(args_raw, str) and args_raw:
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                LOG.warning("%s: tool call arguments not valid JSON: %r", model, args_raw)
        tool_calls.append(ToolCall(
            id=str(tc.get("id") or fn.get("name") or ""),
            name=str(fn.get("name") or ""),
            input=args,
        ))

    usage_raw = raw.get("usage") or {}
    cached = (usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    usage = Usage(
        input_tokens=int(usage_raw.get("prompt_tokens") or 0),
        output_tokens=int(usage_raw.get("completion_tokens") or 0),
        cache_read_tokens=int(cached),
        cache_write_tokens=0,
    )
    finish_reason = str(choices[0].get("finish_reason") or "")
    if tool_calls:
        stop_reason = "tool_use"
    elif finish_reason == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"

    return Completion(
        text_blocks=text_blocks,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        usage=usage,
        model=str(raw.get("model") or model),
        raw=raw,
    )
