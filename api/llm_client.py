"""Anthropic API wrapper that audits every call to llm_calls.

Single responsibility: call Anthropic, compute the USD cost from token
counts, INSERT a row into llm_calls, return a typed result. Used today
by the URL-parsing dispatcher; reused later by summarize_listing.

Pricing source: docs.anthropic.com (Sonnet 4.5 today is $3 / $15 /
$0.30 / $3.75 per MTok for input / output / cache-read / 5-min cache-
write). Update PRICES when Anthropic changes them or when callers add
a new model. If a model is missing from PRICES, the call still runs —
cost_usd is recorded as 0 and a warning logged. We do NOT silently
estimate, because a wrong cost is worse than a missing one.

The DB-backed system prompt and model lookups read from app_settings
(seeded by migration 020). If the row is missing, we fall back to the
constants here so a partial DB never breaks a request.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import psycopg

LOG = logging.getLogger(__name__)


CalledFor = Literal["parse_url", "summarize_listing"]


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-4-5": ModelPrice(3.0, 15.0, 0.30, 3.75),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0, 0.30, 3.75),
}

DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_SYSTEM_PROMPT_FALLBACK = (
    "You are a helpful assistant. The operator has not yet seeded a "
    "system prompt in app_settings. Refuse to answer until configured."
)

# Soft warning threshold for daily LLM spend. Override via env var
# LLM_DAILY_COST_WARN_USD. Anthropic's own spend cap at console.anthropic.com
# is the hard guard; this is just an early-warning log line that shows up
# in Railway when the URL parser is being hit harder than expected.
DEFAULT_DAILY_COST_WARN_USD = 5.0


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[dict[str, Any]]
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    duration_ms: int
    llm_call_id: int
    raw: Any = field(repr=False, default=None)


class LLMClient:
    def __init__(
        self,
        conn: "psycopg.Connection",
        api_key: str | None = None,
    ) -> None:
        self._conn = conn
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._anthropic: Any | None = None

    def call(
        self,
        *,
        called_for: CalledFor,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        estimation_run_id: int | None = None,
    ) -> LLMResponse:
        if not self._api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set; cannot call the LLM"
            )
        resolved_model = model or self.resolve_model()
        client = self._client()

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        mono_start = time.monotonic()
        raw = client.messages.create(**kwargs)
        duration_ms = int((time.monotonic() - mono_start) * 1000)

        text, tool_calls = _split_content(raw)
        usage = _extract_usage(raw)
        cost = compute_cost_usd(
            model=resolved_model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_tokens=usage["cache_read_tokens"],
            cache_write_tokens=usage["cache_write_tokens"],
        )
        llm_call_id = self._record_call(
            called_for=called_for,
            model=resolved_model,
            usage=usage,
            cost_usd=cost,
            duration_ms=duration_ms,
            estimation_run_id=estimation_run_id,
        )
        self._check_daily_cost(just_recorded=cost)
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            model=resolved_model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_tokens=usage["cache_read_tokens"],
            cache_write_tokens=usage["cache_write_tokens"],
            cost_usd=cost,
            duration_ms=duration_ms,
            llm_call_id=llm_call_id,
            raw=raw,
        )

    def resolve_model(self) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s",
                ("llm_parse_model",),
            )
            row = cur.fetchone()
        if row is None:
            return DEFAULT_MODEL
        value = row[0]
        if isinstance(value, str):
            return value
        return DEFAULT_MODEL

    def resolve_system_prompt(self, key: str = "llm_parse_system_prompt") -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s",
                (key,),
            )
            row = cur.fetchone()
        if row is None:
            LOG.warning("app_settings.%s missing; using fallback", key)
            return DEFAULT_SYSTEM_PROMPT_FALLBACK
        value = row[0]
        if isinstance(value, str):
            return value
        return DEFAULT_SYSTEM_PROMPT_FALLBACK

    def _client(self) -> Any:
        if self._anthropic is None:
            import anthropic
            self._anthropic = anthropic.Anthropic(api_key=self._api_key)
        return self._anthropic

    def _check_daily_cost(self, *, just_recorded: float) -> None:
        """Log a WARNING once when today's spend crosses the soft threshold.

        Fires only on the call that pushed us over — re-warning every
        subsequent call would be log spam. Failures here are non-fatal:
        any error querying the running total is swallowed, since the
        guardrail is observability, not correctness.
        """
        threshold = _resolve_threshold()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls "
                    "WHERE called_at::date = CURRENT_DATE"
                )
                row = cur.fetchone()
        except Exception as exc:
            LOG.debug("daily cost check failed: %s", exc)
            return
        total = float(row[0]) if row and row[0] is not None else 0.0
        prior = total - just_recorded
        if total >= threshold and prior < threshold:
            LOG.warning(
                "LLM_COST daily total $%.4f crossed soft threshold "
                "$%.2f (this call $%.4f)",
                total, threshold, just_recorded,
            )

    def _record_call(
        self,
        *,
        called_for: CalledFor,
        model: str,
        usage: dict[str, int],
        cost_usd: float,
        duration_ms: int,
        estimation_run_id: int | None,
    ) -> int:
        sql = (
            "INSERT INTO llm_calls "
            "(called_for, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, cost_usd, "
            "duration_ms, estimation_run_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id"
        )
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    called_for,
                    model,
                    usage["input_tokens"],
                    usage["output_tokens"],
                    usage["cache_read_tokens"],
                    usage["cache_write_tokens"],
                    cost_usd,
                    duration_ms,
                    estimation_run_id,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT into llm_calls returned no id")
            return int(row[0])


def _resolve_threshold() -> float:
    raw = os.environ.get("LLM_DAILY_COST_WARN_USD")
    if not raw:
        return DEFAULT_DAILY_COST_WARN_USD
    try:
        return float(raw)
    except ValueError:
        LOG.warning(
            "invalid LLM_DAILY_COST_WARN_USD=%r; using default $%.2f",
            raw, DEFAULT_DAILY_COST_WARN_USD,
        )
        return DEFAULT_DAILY_COST_WARN_USD


def compute_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    price = PRICES.get(model)
    if price is None:
        LOG.warning(
            "no price configured for model %r; recording cost_usd=0", model
        )
        return 0.0
    cost = (
        input_tokens * price.input_per_mtok
        + output_tokens * price.output_per_mtok
        + cache_read_tokens * price.cache_read_per_mtok
        + cache_write_tokens * price.cache_write_per_mtok
    ) / 1_000_000
    return round(cost, 6)


def _extract_usage(raw: Any) -> dict[str, int]:
    usage = getattr(raw, "usage", None) or {}
    if not isinstance(usage, dict):
        usage = {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "cache_read_input_tokens": getattr(
                usage, "cache_read_input_tokens", 0
            ),
            "cache_creation_input_tokens": getattr(
                usage, "cache_creation_input_tokens", 0
            ),
        }
    return {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_tokens": int(
            usage.get("cache_read_input_tokens", 0) or 0
        ),
        "cache_write_tokens": int(
            usage.get("cache_creation_input_tokens", 0) or 0
        ),
    }


def _split_content(raw: Any) -> tuple[str, list[dict[str, Any]]]:
    """Separate Anthropic's content blocks into plain text and tool-use calls."""
    blocks = getattr(raw, "content", None) or []
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        kind = _attr(block, "type")
        if kind == "text":
            texts.append(_attr(block, "text") or "")
        elif kind == "tool_use":
            tool_calls.append({
                "id": _attr(block, "id"),
                "name": _attr(block, "name"),
                "input": _attr(block, "input") or {},
            })
    return "".join(texts), tool_calls


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def parse_tool_input_json(tool_input: Any) -> dict[str, Any]:
    """Anthropic returns tool inputs as dicts; tolerate stringified JSON too."""
    if isinstance(tool_input, dict):
        return tool_input
    if isinstance(tool_input, str):
        return json.loads(tool_input)
    raise ValueError(f"unexpected tool input type: {type(tool_input).__name__}")
