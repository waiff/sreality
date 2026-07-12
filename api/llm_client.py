"""Provider-agnostic orchestrator that audits every LLM call.

Two responsibilities:

1. Dispatch a `Completion` request through the named
   `CompletionProvider` (Anthropic, Gemini, …). Provider-specific
   SDK code lives in `api/providers/<name>.py`; this module never
   imports those SDKs directly.

2. Record one `llm_calls` row per call — usage, cost, duration,
   provider, optional `estimation_run_id`. Same audit table all
   callers used before the provider abstraction; the new `provider`
   column distinguishes who served the request.

The DB-backed system prompt / model lookups (`app_settings`) are
still here for backwards compatibility with the URL parser and the
summarize / image-compare callers. Agent-mode callers go through
the `skills` table instead.

If a model is missing from a provider's PRICES dict, the call still
runs — cost_usd is recorded as 0 and a warning logged. Wrong cost
is worse than a missing one.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from api.providers import (
    Block,
    Completion,
    CompletionProvider,
    ImageBlock,
    Message,
    ProviderError,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
    compute_cost_usd,
)

if TYPE_CHECKING:
    import psycopg

LOG = logging.getLogger(__name__)


CalledFor = Literal[
    "parse_url",
    "summarize_listing",
    "compare_listing_images",
    "agent_estimation",
    "extract_building_units",
    "read_floor_plan",
    "refine_skill",
    "discover_condition_markers",
    "score_listing_condition",
    "summarize_region_dispositions",
    "enrich_listing_description",
    "classify_listing_images",
    "compare_listings_visually",
    "compare_listing_site_plans",
]


DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_SYSTEM_PROMPT_FALLBACK = (
    "You are a helpful assistant. The operator has not yet seeded a "
    "system prompt in app_settings. Refuse to answer until configured."
)

# Soft warning threshold for daily LLM spend. Override via env var
# LLM_DAILY_COST_WARN_USD. Anthropic's / Google's own spend caps are
# the hard guards; this is just an early-warning log line.
DEFAULT_DAILY_COST_WARN_USD = 5.0


@dataclass
class LLMResponse:
    """Backwards-compatible response for the URL-parser + summary callers.

    Today's non-agent callers expect a plain `text` string and a list
    of `{id, name, input}` tool-call dicts. Keep that shape so this
    refactor doesn't ripple into every caller.
    """
    text: str
    tool_calls: list[dict[str, Any]]
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    duration_ms: int
    llm_call_id: int
    completion: Completion = field(repr=False)


class LLMClient:
    def __init__(
        self,
        conn: "psycopg.Connection",
        providers: dict[str, CompletionProvider] | None = None,
    ) -> None:
        self._conn = conn
        self._providers: dict[str, CompletionProvider] = providers or {}

    def register_providers(
        self, providers: dict[str, CompletionProvider]
    ) -> None:
        """Late-binding registration. Used by api/main.py at startup."""
        self._providers.update(providers)

    def provider(self, name: str) -> CompletionProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ProviderError(
                f"provider {name!r} is not configured; "
                f"available: {sorted(self._providers)}"
            ) from exc

    def call(
        self,
        *,
        called_for: CalledFor,
        messages: list[dict[str, Any]] | list[Message],
        system: str | None = None,
        tools: list[dict[str, Any]] | list[ToolSchema] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        estimation_run_id: int | None = None,
        provider: str = "anthropic",
        tool_choice: str | None = None,
    ) -> LLMResponse:
        """Single API used by every LLM caller in the codebase.

        Accepts either the legacy dict shapes (used by the URL parser
        and the summary/vision tools) or the neutral Message /
        ToolSchema types (used by the agent loop). Translates dicts
        to neutral types before dispatch. `tool_choice` forces the named
        tool; it is forwarded only when set, so providers (and test
        fakes) that predate the parameter keep working for callers that
        don't use it.
        """
        resolved_model = model or self.resolve_model()
        prov = self.provider(provider)
        neutral_messages = [_to_neutral_message(m) for m in messages]
        neutral_tools = [_to_neutral_tool(t) for t in (tools or [])]
        extra_kwargs: dict[str, Any] = {}
        if tool_choice:
            extra_kwargs["tool_choice"] = tool_choice

        mono_start = time.monotonic()
        try:
            completion = prov.complete(
                system=system or "",
                messages=neutral_messages,
                tools=neutral_tools,
                model=resolved_model,
                max_tokens=max_tokens,
                **extra_kwargs,
            )
        except Exception as exc:
            # Record the FAILURE so a provider outage is VISIBLE in the audit trail. On success
            # we write an llm_calls row; on failure the old code wrote nothing, so an exhausted
            # credit balance / dead key left zero trace and the liveness monitor stayed green
            # (it keyed off "no rows"). Best-effort: never let the bookkeeping mask the real
            # error — the original exception always re-raises unchanged.
            self._record_failure(
                called_for=called_for, provider=provider, model=resolved_model,
                duration_ms=int((time.monotonic() - mono_start) * 1000),
                estimation_run_id=estimation_run_id, error=str(exc),
            )
            raise
        duration_ms = int((time.monotonic() - mono_start) * 1000)

        cost = compute_cost_usd(
            price=prov.price_for(resolved_model),
            model=resolved_model,
            usage=completion.usage,
        )
        llm_call_id = self._record_call(
            called_for=called_for,
            provider=provider,
            model=resolved_model,
            usage=completion.usage,
            cost_usd=cost,
            duration_ms=duration_ms,
            estimation_run_id=estimation_run_id,
        )
        self._check_daily_cost(just_recorded=cost)

        return LLMResponse(
            text="".join(completion.text_blocks),
            tool_calls=[
                {"id": tc.id, "name": tc.name, "input": tc.input}
                for tc in completion.tool_calls
            ],
            model=completion.model,
            provider=provider,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_read_tokens=completion.usage.cache_read_tokens,
            cache_write_tokens=completion.usage.cache_write_tokens,
            cost_usd=cost,
            duration_ms=duration_ms,
            llm_call_id=llm_call_id,
            completion=completion,
        )

    def record_external_call(
        self,
        *,
        called_for: CalledFor,
        provider: str,
        model: str,
        usage: Usage,
        cost_usd: float,
        duration_ms: int = 0,
        estimation_run_id: int | None = None,
    ) -> int:
        """Record an `llm_calls` row for a call this client didn't dispatch.

        Used by the async batch ingester: the Message Batches API runs the
        request server-side, so there's no synchronous `complete()` here —
        the ingester computes the (batch-discounted) cost from the returned
        usage and logs it through this method to keep the audit trail and
        daily-spend warning intact.
        """
        call_id = self._record_call(
            called_for=called_for,
            provider=provider,
            model=model,
            usage=usage,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            estimation_run_id=estimation_run_id,
        )
        self._check_daily_cost(just_recorded=cost_usd)
        return call_id

    def resolve_model(self, key: str = "llm_parse_model") -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s",
                (key,),
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

    def _check_daily_cost(self, *, just_recorded: float) -> None:
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
        provider: str,
        model: str,
        usage: Usage,
        cost_usd: float,
        duration_ms: int,
        estimation_run_id: int | None,
        error: str | None = None,
    ) -> int:
        sql = (
            "INSERT INTO llm_calls "
            "(called_for, provider, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, cost_usd, "
            "duration_ms, estimation_run_id, error) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id"
        )
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    called_for,
                    provider,
                    model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    cost_usd,
                    duration_ms,
                    estimation_run_id,
                    error,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT into llm_calls returned no id")
            return int(row[0])

    def _record_failure(
        self,
        *,
        called_for: CalledFor,
        provider: str,
        model: str,
        duration_ms: int,
        estimation_run_id: int | None,
        error: str,
    ) -> None:
        """Best-effort failure row (zero usage/cost, `error` set) so a provider outage is
        auditable. NEVER raises — a bookkeeping failure must not mask the original error."""
        try:
            self._record_call(
                called_for=called_for, provider=provider, model=model,
                usage=Usage(), cost_usd=0.0, duration_ms=duration_ms,
                estimation_run_id=estimation_run_id, error=error[:2000],
            )
        except Exception as rec_exc:  # noqa: BLE001 — audit is best-effort
            LOG.warning("failed to record llm_calls failure row: %s", rec_exc)


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


# --- legacy shape -> neutral conversion -----------------------------------

def _to_neutral_message(msg: Any) -> Message:
    if isinstance(msg, Message):
        return msg
    role = msg.get("role", "user")
    content = msg.get("content")
    blocks: list[Block] = []
    if isinstance(content, str):
        blocks.append(TextBlock(text=content))
    elif isinstance(content, list):
        for entry in content:
            blocks.append(_to_neutral_block(entry))
    return Message(role=role, content=blocks)


def _to_neutral_block(entry: Any) -> Block:
    if isinstance(entry, (TextBlock, ToolUseBlock, ToolResultBlock, ImageBlock)):
        return entry
    if isinstance(entry, str):
        return TextBlock(text=entry)
    kind = entry.get("type")
    if kind == "text":
        return TextBlock(text=entry.get("text") or "")
    if kind == "image":
        # Vision input: an Anthropic-shaped image block
        # {"type":"image","source":{"type":"base64","media_type":...,"data":...}}.
        # Without this case it would fall through to the str() fallback below and
        # the base64 would be sent as TEXT (blowing the token limit; no image seen).
        src = entry.get("source") or {}
        return ImageBlock(
            media_type=str(src.get("media_type") or "image/jpeg"),
            data=str(src.get("data") or ""),
        )
    if kind == "tool_use":
        return ToolUseBlock(
            id=str(entry.get("id") or ""),
            name=str(entry.get("name") or ""),
            input=entry.get("input") or {},
        )
    if kind == "tool_result":
        return ToolResultBlock(
            tool_use_id=str(entry.get("tool_use_id") or ""),
            content=str(entry.get("content") or ""),
            is_error=bool(entry.get("is_error", False)),
        )
    return TextBlock(text=str(entry))


def _to_neutral_tool(tool: Any) -> ToolSchema:
    if isinstance(tool, ToolSchema):
        return tool
    return ToolSchema(
        name=tool["name"],
        description=tool.get("description", ""),
        input_schema=tool.get("input_schema") or {},
    )


def parse_tool_input_json(tool_input: Any) -> dict[str, Any]:
    """Tool inputs may arrive as dicts or stringified JSON; tolerate both."""
    if isinstance(tool_input, dict):
        return tool_input
    if isinstance(tool_input, str):
        return json.loads(tool_input)
    raise ValueError(f"unexpected tool input type: {type(tool_input).__name__}")
