"""OpenAI backend for the CompletionProvider protocol (Session-3 vision bake-off).

Wraps the plain Chat Completions REST API via `OpenAICompatibleProvider` — no
`openai` SDK dependency (rule #7): the wire format is JSON over HTTP and the
shared base already speaks it. Bake-off candidate only; NOT registered as a
production dedup-vision model until a green harness run + operator flip
(docs/design/dedup-cost-reduction.md §Operator gates).
"""

from __future__ import annotations

from typing import Any

from api.providers.base import ModelPrice
from api.providers.openai_compatible import OpenAICompatibleProvider

# Source: developers.openai.com/api/docs/pricing, cross-checked against 3
# independent aggregators (openrouter.ai, devtk.ai, pricepertoken.com) 2026-07-13.
# NOT read directly off OpenAI's current pricing table — that page no longer lists
# gpt-5-mini as a row (it shows the later 5.4/5.5/5.6 snapshots only), even though
# gpt-5-mini is still a live, callable model id. Re-verify before any spend beyond
# the bake-off sample. cache_read is a same-generation estimate (gpt-5.4-mini's
# published $0.075 cached rate, scaled by gpt-5-mini's input:cached-input ratio
# elsewhere in the 5.x line), NOT a confirmed gpt-5-mini figure — bake-off payloads
# are mostly-unique image pairs, so cache hits should be rare here regardless.
PRICES: dict[str, ModelPrice] = {
    "gpt-5-mini": ModelPrice(0.25, 2.00, 0.025, 0.0),
}


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"

    def __init__(self, *, api_key: str | None = None, session: Any = None) -> None:
        super().__init__(
            name="openai",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            prices=PRICES,
            # GPT-5-series rejects `max_tokens` with a 400 ("use max_completion_tokens");
            # see the OpenAICompatibleProvider docstring for the source.
            max_tokens_param="max_completion_tokens",
            api_key=api_key,
            session=session,
        )
