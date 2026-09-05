"""Qwen backend for the CompletionProvider protocol (Session-3 vision bake-off).

Alibaba DashScope's "OpenAI-compatible mode" speaks the same Chat Completions
wire format as OpenAI (alibabacloud.com/help/en/model-studio/qwen-api-via-dashscope
confirms `tools`/`tool_choice` function-calling and image content parts) —
`OpenAICompatibleProvider` covers it with a different base_url/key/prices, no
new translation code. Two vision-capable MoE sizes are benchmarked: 235B-A22B
(larger) and 30B-A3B (cheaper/faster). Both are the Instruct variant, not
Thinking — this repo's dedup verdicts are forced single-tool-call decisions,
not chain-of-thought tasks, and Thinking pricing/latency would be a worse fit.

Candidate provider only; nothing routes to it today. The vision route that
would use it is a parked item of the dedup rebuild (docs/design/new-dedup/
PROGRAM.md, W6) — register a production model only on the operator's flip.
"""

from __future__ import annotations

from typing import Any

from api.providers.base import ModelPrice
from api.providers.openai_compatible import OpenAICompatibleProvider

# Source: alibabacloud.com/help/en/model-studio/model-pricing, INTERNATIONAL
# (Singapore) tier, checked 2026-07-13 — the tier a non-mainland QWEN_API_KEY
# actually bills against. Third-party resellers (OpenRouter, CloudPrice) quote
# different, sometimes cheaper, numbers for the same model ids; this repo talks
# to DashScope directly (QWEN_API_KEY, not an OpenRouter key), so Alibaba's own
# price is the one that matters. cache_read left at 0.0: DashScope's context
# caching and its batch discount are documented as MUTUALLY EXCLUSIVE ("these two
# discounts cannot apply simultaneously") — unlike Anthropic, where prompt caching
# applies on the sync path independent of batch. Don't assume Anthropic's
# combination rule carries over.
PRICES: dict[str, ModelPrice] = {
    "qwen3-vl-235b-a22b-instruct": ModelPrice(0.40, 1.60, 0.0, 0.0),
    "qwen3-vl-30b-a3b-instruct": ModelPrice(0.20, 0.80, 0.0, 0.0),
    # W2-10's third bake-off candidate (the bazos free-text location lane). Same
    # INTERNATIONAL (Singapore) tier and the same cache_read=0.0 rule as the two rows
    # above. UNVERIFIED against a live QWEN_API_KEY call as of 2026-09-05: nothing in
    # this repo has ever dispatched to DashScope, so the bake-off's first successful
    # call is the actual verification — reconcile the recorded llm_calls.cost_usd
    # against the DashScope billing dashboard afterwards and correct this row.
    "qwen3.7-flash": ModelPrice(0.03, 0.13, 0.0, 0.0),
}


class QwenProvider(OpenAICompatibleProvider):
    name = "qwen"

    def __init__(self, *, api_key: str | None = None, session: Any = None) -> None:
        super().__init__(
            name="qwen",
            # International endpoint — NOT dashscope.aliyuncs.com (mainland-China
            # accounts, CNY billing). Unverified against a live QWEN_API_KEY call as
            # of this file's authorship; the harness's first real call is the actual
            # check (see OpenAICompatibleProvider's docstring for why that's the plan).
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            api_key_env="QWEN_API_KEY",
            prices=PRICES,
            # DashScope docs: max_tokens = answer length only (what we want for a
            # forced-tool-call verdict); max_completion_tokens is for Thinking models'
            # chain-of-thought budget, which the Instruct variants here don't use.
            max_tokens_param="max_tokens",
            api_key=api_key,
            session=session,
        )
