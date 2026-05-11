"""Neutral types every CompletionProvider produces and consumes.

The agent loop drives the conversation in these neutral types; each
provider implementation converts to/from its native SDK shapes inside
`complete()`. Keeping the loop body provider-agnostic is what lets us
swap Anthropic for Gemini (or add a third provider later) without
touching `api/agent.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

LOG = logging.getLogger(__name__)


# --- Tool schema ----------------------------------------------------------

@dataclass(frozen=True)
class ToolSchema:
    """Provider-agnostic tool declaration.

    `input_schema` is a JSON Schema object (the same shape Anthropic
    accepts as `input_schema`). Each provider translates it to its
    native function-declaration form.
    """
    name: str
    description: str
    input_schema: dict[str, Any]


# --- Conversation blocks --------------------------------------------------

@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: list[Block]


# --- Completion result ----------------------------------------------------

@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


StopReason = Literal["tool_use", "end_turn", "max_tokens", "stop"]


@dataclass(frozen=True)
class Completion:
    """One turn's worth of provider output, in neutral form."""
    text_blocks: list[str]
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usage: Usage
    model: str
    raw: Any = field(repr=False, default=None)


# --- Pricing --------------------------------------------------------------

@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0


def compute_cost_usd(
    *,
    price: ModelPrice | None,
    model: str,
    usage: Usage,
) -> float:
    """Compute USD cost from a Usage tuple and a price row.

    If `price` is None the cost is recorded as 0 and a warning is
    logged. Wrong cost is worse than a missing one — the operator
    needs to add the model to the provider's PRICES dict.
    """
    if price is None:
        LOG.warning(
            "no price configured for model %r; recording cost_usd=0", model
        )
        return 0.0
    cost = (
        usage.input_tokens * price.input_per_mtok
        + usage.output_tokens * price.output_per_mtok
        + usage.cache_read_tokens * price.cache_read_per_mtok
        + usage.cache_write_tokens * price.cache_write_per_mtok
    ) / 1_000_000
    return round(cost, 6)


# --- Provider protocol ----------------------------------------------------

class ProviderError(Exception):
    """Raised when a provider fails to produce a Completion."""


class CompletionProvider(Protocol):
    """One backend (Anthropic, Gemini, ...).

    Implementations are stateless aside from a constructed SDK client.
    `complete()` is called once per agent turn; the agent loop owns
    the message history.
    """

    name: str

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
        model: str,
        max_tokens: int = 4096,
    ) -> Completion: ...

    def price_for(self, model: str) -> ModelPrice | None: ...
