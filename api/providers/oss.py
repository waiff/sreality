"""Open-source vision model served by our own vLLM pod (`autodedup/oss_pod.py`).

vLLM's OpenAI server speaks the same Chat Completions wire format as OpenAI and DashScope,
so this is `OpenAICompatibleProvider` with three differences and no new translation code:

  * THE ENDPOINT IS EPHEMERAL. There is no vendor URL to hardcode — the base_url is a pod
    that exists for the length of one pass, so it is read from OSS_LLM_BASE_URL at
    construction and, when that is where it came from, RE-READ on every call: the provider
    registry is built once at API startup, long before any pod is rented, and the next pass
    rents a different pod at a different proxy host. An explicit `base_url=` is never
    overwritten.
  * THE TOKENS ARE FREE; THE POD IS NOT. Per-token price is 0 by construction — the bill is
    GPU-hours, which only the lane that rented the pod can attribute (`pod_cost_usd`). A
    per-token price here would double-count it, and worse, would look like the real number.
  * A SELF-HOSTED MODEL MISSES THE TOOL CALL SOMETIMES. vLLM's tool-call parser is a text
    parser over the model's output, not a constrained decode: a 7B model that answers with
    the right JSON in plain prose gets NO tool_calls back. That answer is already paid for
    in GPU time and is usually perfectly valid, so a JSON body that matches the declared
    tool is promoted to a tool call rather than discarded (E31's failure shape, applied to
    a free model: never throw away a call you have already paid for).

Model ids are namespaced `oss:<hf id>` so `provider_for_model` can route on the id alone
(`oss:Qwen/Qwen2.5-VL-7B-Instruct`); the prefix is stripped before it goes on the wire,
where vLLM knows only its `--served-model-name`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from typing import Any

from api.providers.base import (
    Completion,
    Message,
    ModelPrice,
    ProviderError,
    ToolCall,
    ToolSchema,
    Usage,
)
from api.providers.openai_compatible import OpenAICompatibleProvider

BASE_URL_ENV: str = "OSS_LLM_BASE_URL"
API_KEY_ENV: str = "OSS_LLM_API_KEY"
# vLLM serves without auth unless `--api-key` is passed, but the shared base class refuses to
# call with no key at all (a real guard for the paid providers). A placeholder keeps that guard
# meaningful there and harmless here.
DEFAULT_API_KEY: str = "none"
MODEL_PREFIX: str = "oss:"

# Deliberately empty: no OSS model is billed per token. `price_for` returns a zero row instead
# of None so `compute_cost_usd` records 0 without the "no price configured" warning, which here
# would be a false alarm on every single call.
PRICES: dict[str, ModelPrice] = {}
ZERO_PRICE: ModelPrice = ModelPrice(0.0, 0.0, 0.0, 0.0)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def served_model(model: str) -> str:
    """`oss:Qwen/Qwen2.5-VL-7B-Instruct` -> `Qwen/Qwen2.5-VL-7B-Instruct`."""
    return model[len(MODEL_PREFIX):] if model.lower().startswith(MODEL_PREFIX) else model


class OssProvider(OpenAICompatibleProvider):
    name = "oss"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        session: Any = None,
    ) -> None:
        super().__init__(
            name="oss",
            base_url=base_url if base_url is not None else os.environ.get(BASE_URL_ENV, ""),
            api_key_env=API_KEY_ENV,
            prices=PRICES,
            # vLLM implements `max_tokens`; `max_completion_tokens` is an OpenAI-platform
            # spelling its server does not document.
            max_tokens_param="max_tokens",
            api_key=api_key or os.environ.get(API_KEY_ENV) or DEFAULT_API_KEY,
            session=session,
        )
        # Only an env-sourced URL tracks the environment; an explicitly passed one is the
        # caller's word and is never overwritten.
        self._url_from_env = base_url is None

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
        if self._url_from_env:
            # RE-READ, never latch: the provider registry is a process singleton built at API
            # startup, and pods are ephemeral BY DESIGN — pass 2 rents a new pod_id and so a new
            # proxy host. A URL frozen on first use would keep POSTing at pass 1's terminated
            # host and fail as a connection error instead of the named-env-var error below.
            self._base_url = os.environ.get(BASE_URL_ENV, "").rstrip("/")
        if not self._base_url:
            raise ProviderError(
                f"{BASE_URL_ENV} is not set; no OSS pod is serving right now "
                "(autodedup.oss_pod rents one and exports its proxy URL)"
            )
        completion = super().complete(
            system=system, messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, tool_choice=tool_choice,
        )
        if completion.tool_calls or not tools:
            return completion
        return _promote_json_content(completion, tools=tools, tool_choice=tool_choice)

    def _chat_body(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
        model: str,
        max_tokens: int = 4096,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        return super()._chat_body(
            system=system, messages=messages, tools=tools, model=served_model(model),
            max_tokens=max_tokens, tool_choice=tool_choice,
        )

    def price_for(self, model: str) -> ModelPrice | None:
        return ZERO_PRICE

    def compute_cost_usd(self, *, model: str, usage: Usage) -> float:
        """Always 0. The pod is billed by the hour to the lane that rented it
        (`autodedup.oss_pod.pod_cost_usd`), never per token here."""
        return 0.0


def _promote_json_content(
    completion: Completion, *, tools: list[ToolSchema], tool_choice: str | None
) -> Completion:
    """Turn a JSON answer that matches the declared tool into the tool call it meant to be.

    Conservative on purpose: only an OBJECT that shares at least one property name with the
    target tool's schema is promoted, so ordinary prose (or a JSON list of something else) is
    left alone to fail loudly rather than becoming a fabricated verdict."""
    target = next((t for t in tools if t.name == tool_choice), tools[0])
    properties = set((target.input_schema or {}).get("properties") or {})
    for text in completion.text_blocks:
        payload = _loads_relaxed(text)
        if not isinstance(payload, dict):
            continue
        if properties and not properties & set(payload):
            continue
        call = ToolCall(id=f"{target.name}-json-fallback", name=target.name, input=payload)
        return replace(completion, tool_calls=[call], stop_reason="tool_use")
    return completion


def _loads_relaxed(text: str) -> Any:
    """JSON out of a model's content block: bare, fenced, or with prose either side."""
    if not text:
        return None
    candidate = text.strip()
    fenced = _FENCE_RE.match(candidate)
    if fenced:
        candidate = fenced.group(1)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(candidate[start:end + 1])
    except json.JSONDecodeError:
        return None
