"""Reasoning agent that produces a defensible rental estimate.

Synchronous tool-use loop. Drives a `CompletionProvider` (Anthropic
or Gemini) against the curated subset of toolkit functions whitelisted
by the active Skill. Stops when the agent calls `record_estimate`,
or short-circuits to `status='failed'` if any guard (max iterations,
max cost, wall clock) trips.

Provider-agnostic by construction: every LLM call goes through
`LLMClient.call(provider=...)`. Adding a third provider means
writing one more `CompletionProvider` impl — no change here.

Per CLAUDE.md trace rule #8: every step is recorded with a bounded
`output_summary`. Full tool outputs live in dedicated columns
(`comparables_used` etc.) or only in memory.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from api.providers import (
    Message,
    ProviderError,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)
from toolkit import (
    analyze_distribution,
    compare_listing_images,
    compute_amenity_supply,
    compute_listing_velocity,
    compute_market_velocity,
    compute_walkability,
    describe_neighborhood,
    find_comparables_along_axis,
    find_comparables_relaxed,
    find_distribution_outliers,
    summarize_listing,
    verify_listing_freshness,
)
from toolkit.comparables import ComparableFilters, TargetSpec

if TYPE_CHECKING:
    import psycopg

    from api.estimation_runs import TraceRecorder
    from api.llm_client import LLMClient
    from api.skills import Skill
    from scraper.sreality_client import SrealityClient

LOG = logging.getLogger(__name__)


# Truncate text-block reasoning before recording into trace.steps.
# Per CLAUDE.md trace rule, steps must never store full outputs.
_REASONING_MAX_CHARS = 800

# Truncate a tool-result summary before passing it back to the LLM.
# (The full result still lives in memory and shapes the cohort.)
_TOOL_RESULT_PREVIEW = 4_000


@dataclass
class AgentResult:
    data: dict[str, Any]
    metadata: dict[str, Any]


# --- tool registry --------------------------------------------------------

@dataclass(frozen=True)
class _ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., dict[str, Any]] | None = None
    is_terminator: bool = False


def _build_tool_registry() -> dict[str, _ToolDef]:
    """The tools the agent can call. Each entry knows how to dispatch.

    The registry is the source of truth; skill rows reference these
    names. `GET /admin/tools` projects (name, description) for the
    Settings page's checkbox list.
    """
    return {
        "find_comparables_relaxed": _ToolDef(
            name="find_comparables_relaxed",
            description=(
                "Find listings comparable to the target, automatically widening "
                "the area / disposition filters until at least min_results are "
                "found (or the relaxation ladder is exhausted). Returns the "
                "cohort + a relaxation_trace showing what was widened."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "radius_m": {"type": "integer", "minimum": 100, "maximum": 5000},
                    "area_band_pct": {"type": "number", "minimum": 0.05, "maximum": 0.6},
                    "disposition_match": {
                        "type": "string",
                        "enum": ["exact", "loose", "any"],
                    },
                    "max_age_days": {"type": "integer", "minimum": 1, "maximum": 90},
                    "min_results": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": [],
            },
            handler=_handle_find_comparables_relaxed,
        ),
        "analyze_distribution": _ToolDef(
            name="analyze_distribution",
            description=(
                "Compute descriptive statistics (p25 / median / p75 / iqr / "
                "mean / stdev) on a numeric field across the most recent cohort "
                "returned by find_comparables_relaxed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "enum": ["price_per_m2", "price_czk", "area_m2"],
                    },
                },
                "required": [],
            },
            handler=_handle_analyze_distribution,
        ),
        "find_distribution_outliers": _ToolDef(
            name="find_distribution_outliers",
            description=(
                "Flag listings in the most recent cohort whose value on `field` "
                "is outside median ± iqr_multiplier × IQR. Use after "
                "analyze_distribution to investigate the tails."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "enum": ["price_per_m2", "price_czk"],
                    },
                    "iqr_multiplier": {"type": "number", "minimum": 0.5, "maximum": 5.0},
                },
                "required": [],
            },
            handler=_handle_find_distribution_outliers,
        ),
        "describe_neighborhood": _ToolDef(
            name="describe_neighborhood",
            description=(
                "Compute the area-wide price level around the target lat/lng. "
                "Use as a sanity check against the cohort median; divergence "
                ">15% deserves a warning in the final estimate."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "radius_m": {"type": "integer", "minimum": 100, "maximum": 5000},
                    "max_age_days": {"type": "integer", "minimum": 1, "maximum": 365},
                },
                "required": [],
            },
            handler=_handle_describe_neighborhood,
        ),
        "verify_listing_freshness": _ToolDef(
            name="verify_listing_freshness",
            description=(
                "Re-fetch one listing from sreality.cz to confirm it's still "
                "live. Use sparingly: stale listings are already filtered out "
                "by the cohort builder; this is for confirming a *specific* "
                "suspicious comparable before relying on it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sreality_id": {"type": "integer"},
                    "max_age_hours": {"type": "integer", "minimum": 1, "maximum": 168},
                },
                "required": ["sreality_id"],
            },
            handler=_handle_verify_listing_freshness,
        ),
        "compute_market_velocity": _ToolDef(
            name="compute_market_velocity",
            description=(
                "TOM (time-on-market) statistics across the target's spatial "
                "+ attribute cohort. Returns median/p25/p75 TOM days, an "
                "active vs delisted split, and a recent-vs-older trend. Use "
                "when the cohort price spread is wide enough to suspect "
                "demand is doing the work — slow markets justify lower "
                "confidence."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "radius_m": {"type": "integer", "minimum": 100, "maximum": 5000},
                    "population": {
                        "type": "string",
                        "enum": ["active", "delisted", "all"],
                    },
                    "trend_split_days": {
                        "type": "integer", "minimum": 1, "maximum": 90,
                    },
                },
                "required": [],
            },
            handler=_handle_compute_market_velocity,
        ),
        "compute_listing_velocity": _ToolDef(
            name="compute_listing_velocity",
            description=(
                "Percentile-rank one listing's TOM within its peer cohort and "
                "classify it (fast/typical/slow/stuck). Use on a specific "
                "comparable when its price looks anomalous — a 'stuck' "
                "listing pulling the upper tail up is a candidate to set "
                "aside before quoting p75."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sreality_id": {"type": "integer"},
                    "radius_m": {"type": "integer", "minimum": 100, "maximum": 5000},
                    "disposition_match": {
                        "type": "string",
                        "enum": ["exact", "loose", "any"],
                    },
                    "population": {
                        "type": "string",
                        "enum": ["active", "delisted", "all"],
                    },
                },
                "required": ["sreality_id"],
            },
            handler=_handle_compute_listing_velocity,
        ),
        "compute_walkability": _ToolDef(
            name="compute_walkability",
            description=(
                "Weighted 0-100 walkability score from nearest-POI distances "
                "to transit, supermarkets, pharmacies, schools, parks. Use "
                "once per estimate to contextualise location quality — a "
                "below-50 score in a same-radius cohort warrants a warning."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "radius_m": {"type": "integer", "minimum": 100, "maximum": 2000},
                },
                "required": [],
            },
            handler=_handle_compute_walkability,
        ),
        "compute_amenity_supply": _ToolDef(
            name="compute_amenity_supply",
            description=(
                "Per-category POI count vs target counts (transit, food, "
                "health, education, parks), bucketed scarce/adequate/"
                "abundant. Complementary to compute_walkability — use when "
                "the score is mid-range and you want to know *what* is "
                "missing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "radius_m": {"type": "integer", "minimum": 100, "maximum": 2000},
                },
                "required": [],
            },
            handler=_handle_compute_amenity_supply,
        ),
        "find_comparables_along_axis": _ToolDef(
            name="find_comparables_along_axis",
            description=(
                "Comparables in a corridor along tram / subway / bus routes "
                "passing near the target. Listings get merged into the "
                "active cohort (deduped by sreality_id) so subsequent "
                "analyze_distribution / find_distribution_outliers see them. "
                "Use when the target is on a strong transit axis and "
                "circle-radius cohorts under-represent peers down the line."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "transport_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["tram", "subway", "bus"],
                        },
                    },
                    "anchor_radius_m": {
                        "type": "integer", "minimum": 100, "maximum": 2000,
                    },
                    "corridor_m": {
                        "type": "integer", "minimum": 100, "maximum": 1000,
                    },
                },
                "required": [],
            },
            handler=_handle_find_comparables_along_axis,
        ),
        "summarize_listing": _ToolDef(
            name="summarize_listing",
            description=(
                "Structured Claude summary of one listing snapshot — "
                "headline, key_highlights, concerns, condition_assessment, "
                "target_audience. Cached per (sreality_id, snapshot_id); "
                "repeat calls within a run are free. Use to triage a "
                "specific comparable before deciding to keep/drop it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sreality_id": {"type": "integer"},
                },
                "required": ["sreality_id"],
            },
            handler=_handle_summarize_listing,
        ),
        "compare_listing_images": _ToolDef(
            name="compare_listing_images",
            description=(
                "Claude vision pairwise comparison of two cohort listings "
                "across six tenant-relevant dimensions (exterior, kitchen, "
                "windows_and_light, floor_finish, lighting, styling). Both "
                "ids must already be in the current cohort. Vision is "
                "~$0.05/pair — call sparingly (typically once or twice when "
                "two comparables price-diverge sharply and the gap might "
                "reflect condition)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sreality_id_a": {"type": "integer"},
                    "sreality_id_b": {"type": "integer"},
                    "n_images": {"type": "integer", "minimum": 1, "maximum": 12},
                },
                "required": ["sreality_id_a", "sreality_id_b"],
            },
            handler=_handle_compare_listing_images,
        ),
        "record_estimate": _ToolDef(
            name="record_estimate",
            description=(
                "Submit the final estimate and END THE RUN. Call exactly once. "
                "After this tool returns, the agent loop exits immediately."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "estimated_monthly_rent_czk": {"type": "integer", "minimum": 0},
                    "rent_p25_czk": {"type": "integer", "minimum": 0},
                    "rent_p75_czk": {"type": "integer", "minimum": 0},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "comparables_used": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "warnings": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "estimated_monthly_rent_czk",
                    "rent_p25_czk",
                    "rent_p75_czk",
                    "confidence",
                    "comparables_used",
                ],
            },
            is_terminator=True,
        ),
    }


def list_agent_tools() -> list[dict[str, str]]:
    """Project (name, description) for the Settings page."""
    return [
        {"name": t.name, "description": t.description}
        for t in AGENT_TOOLS.values()
    ]


# --- loop state -----------------------------------------------------------

@dataclass
class _LoopState:
    """Mutable state carried through the agent loop."""
    conn: "psycopg.Connection"
    sreality_client: "SrealityClient"
    llm_client: "LLMClient"
    target: TargetSpec
    base_filters: ComparableFilters
    last_cohort: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    total_cost_usd: float = 0.0
    final_call: dict[str, Any] | None = None
    # Audit trail: one record per find_comparables_relaxed call (each
    # is a "round" of cohort selection). Surfaces in the trace as the
    # comparable_selection_summary computation step at end-of-run.
    selection_rounds: list[dict[str, Any]] = field(default_factory=list)
    # Most recent reasoning text emitted by the LLM. Captured so each
    # selection round can attribute "why the agent chose these
    # filters this round" to the immediately preceding reasoning turn.
    last_reasoning: str = ""


# --- entrypoint -----------------------------------------------------------

def run_agent_estimation(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    target: TargetSpec,
    filters: ComparableFilters,
    purchase_price_czk: int | None,
    *,
    skill: "Skill",
    provider: str,
    recorder: "TraceRecorder",
    estimation_run_id: int,
) -> AgentResult:
    """Drive the agent loop. Returns AgentResult with `metadata.stop_reason`.

    Exceptions raised by `llm_client.call` (rate limit, API error,
    unknown provider) propagate up — the caller writes status='failed'
    on the run row.
    """
    from api.estimate_yield import _used_entry  # circular dep; lazy import

    state = _LoopState(
        conn=conn,
        sreality_client=sreality_client,
        llm_client=llm_client,
        target=target,
        base_filters=filters,
    )

    # Filter the tool registry to the skill's whitelist.
    schemas = [
        ToolSchema(
            name=t.name,
            description=t.description,
            input_schema=t.input_schema,
        )
        for t in (AGENT_TOOLS[name] for name in skill.allowed_tools)
    ]
    model = skill.preferred_model[provider]

    system_text = skill.system_prompt
    messages: list[Message] = [
        Message(role="user", content=[TextBlock(text=_initial_user_message(
            target, filters, purchase_price_czk,
        ))])
    ]

    wall_deadline = time.monotonic() + skill.limits.wall_clock_timeout_s
    stop_reason: str = "end_turn"

    while True:
        state.iterations += 1
        if state.iterations > skill.limits.max_iterations:
            stop_reason = "max_iterations"
            break
        if state.total_cost_usd >= skill.limits.max_cost_usd:
            stop_reason = "max_cost"
            break
        if time.monotonic() > wall_deadline:
            stop_reason = "timeout"
            break

        completion = llm_client.call(
            called_for="agent_estimation",
            messages=messages,
            system=system_text,
            tools=schemas,
            model=model,
            provider=provider,
            estimation_run_id=estimation_run_id,
        ).completion
        state.total_cost_usd = _running_cost(state, llm_client, estimation_run_id)

        # Record the turn's plain-text reasoning before any tool dispatch.
        text = "".join(completion.text_blocks).strip()
        tool_names = [tc.name for tc in completion.tool_calls]
        if text or tool_names:
            with recorder.reasoning() as h:
                h.set_summary({
                    "text": _truncate(text, _REASONING_MAX_CHARS),
                    "tool_calls_queued": tool_names,
                    "provider": provider,
                })
            state.last_reasoning = _truncate(text, _REASONING_MAX_CHARS)

        if not completion.tool_calls:
            # Provider stopped without invoking a tool. Done — but not
            # via the terminator, so the run is "failed".
            stop_reason = "end_turn"
            break

        # Append the assistant turn (text + tool_use blocks) to history.
        assistant_blocks: list[Any] = []
        for tb in completion.text_blocks:
            if tb:
                assistant_blocks.append(TextBlock(text=tb))
        for tc in completion.tool_calls:
            assistant_blocks.append(ToolUseBlock(
                id=tc.id, name=tc.name, input=tc.input,
            ))
        messages.append(Message(role="assistant", content=assistant_blocks))

        # Dispatch each tool call. If the terminator fires, stop right
        # after — don't bother feeding the result back.
        results: list[ToolResultBlock] = []
        for tc in completion.tool_calls:
            tool_def = AGENT_TOOLS.get(tc.name)
            if tool_def is None or tc.name not in skill.allowed_tools:
                results.append(ToolResultBlock(
                    tool_use_id=tc.id,
                    content=f"tool {tc.name!r} is not available to this skill",
                    is_error=True,
                ))
                with recorder.tool_call(tc.name, tc.input) as h:
                    h.set_summary({"error": "unknown_tool"})
                continue
            if tool_def.is_terminator:
                state.final_call = tc.input
                with recorder.tool_call(tc.name, tc.input) as h:
                    h.set_summary(_terminator_summary(tc.input))
                stop_reason = "record_estimate"
                break
            try:
                with recorder.tool_call(tc.name, tc.input) as h:
                    result = _dispatch_tool(tool_def, tc.input, state)
                    h.set_summary(_tool_summary(tc.name, result))
                results.append(ToolResultBlock(
                    tool_use_id=tc.id,
                    content=_format_tool_result(tc.name, result),
                ))
            except Exception as exc:
                LOG.warning("agent tool %s raised: %s", tc.name, exc)
                results.append(ToolResultBlock(
                    tool_use_id=tc.id,
                    content=f"{type(exc).__name__}: {exc}",
                    is_error=True,
                ))

        if stop_reason == "record_estimate":
            break

        messages.append(Message(role="user", content=list(results)))

    result = _finalise(
        state,
        skill=skill,
        provider=provider,
        stop_reason=stop_reason,
        purchase_price_czk=purchase_price_czk,
        used_entry=_used_entry,
    )

    _record_selection_summary(recorder, state, result, stop_reason)
    return result


def _record_selection_summary(
    recorder: "TraceRecorder",
    state: _LoopState,
    result: AgentResult,
    stop_reason: str,
) -> None:
    """Emit the v2-trace `comparable_selection_summary` computation step.

    Captures the agent's per-round filter ladder, cohort diffs, and the
    set of comparables it ultimately committed to. The frontend reads
    this step to render the top-of-page strategy panel and the
    per-iteration cohort-diff sub-panels.
    """
    final_ids = sorted(
        int(c["sreality_id"]) for c in (result.data.get("comparables_used") or [])
    )
    final_filters = state.selection_rounds[-1]["filters"] if state.selection_rounds else None
    with recorder.computation("comparable_selection_summary") as h:
        h.set_summary({
            "n_rounds": len(state.selection_rounds),
            "rounds": state.selection_rounds,
            "final_filters": final_filters,
            "final_comparable_ids": final_ids,
            "stop_reason": stop_reason,
        })


# --- tool dispatchers -----------------------------------------------------

def _dispatch_tool(
    tool_def: _ToolDef, args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    if tool_def.handler is None:
        raise RuntimeError(f"tool {tool_def.name} has no handler")
    return tool_def.handler(args, state)


def _handle_find_comparables_relaxed(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    from dataclasses import replace
    filters = state.base_filters
    if "radius_m" in args:
        filters = replace(filters, radius_m=int(args["radius_m"]))
    if "area_band_pct" in args:
        filters = replace(filters, area_band_pct=float(args["area_band_pct"]))
    if "disposition_match" in args:
        filters = replace(filters, disposition_match=args["disposition_match"])
    if "max_age_days" in args:
        filters = replace(filters, max_age_days=int(args["max_age_days"]))

    min_results = int(args.get("min_results", 5))
    result = find_comparables_relaxed(
        state.conn, state.target, filters, min_results=min_results,
    )
    listings = result.get("data", {}).get("listings") or []

    prev_ids = {int(l["sreality_id"]) for l in state.last_cohort}
    new_ids = {int(l["sreality_id"]) for l in listings}
    state.selection_rounds.append({
        "n": len(state.selection_rounds) + 1,
        "filters": {
            "radius_m": filters.radius_m,
            "area_band_pct": filters.area_band_pct,
            "disposition_match": filters.disposition_match,
            "max_age_days": filters.max_age_days,
            "min_results": min_results,
        },
        "cohort_size": len(listings),
        "cohort_ids": sorted(new_ids),
        "added_ids": sorted(new_ids - prev_ids),
        "removed_ids": sorted(prev_ids - new_ids),
        "n_relaxations": len(result.get("data", {}).get("relaxation_trace") or []),
        "reasoning": state.last_reasoning,
    })

    state.last_cohort = listings
    return result


def _handle_analyze_distribution(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    field_name = args.get("field", "price_per_m2")
    return analyze_distribution(state.last_cohort, field=field_name)


def _handle_find_distribution_outliers(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    field_name = args.get("field", "price_per_m2")
    iqr = float(args.get("iqr_multiplier", 1.5))
    return find_distribution_outliers(
        state.conn, state.last_cohort,
        field=field_name, iqr_multiplier=iqr,
    )


def _handle_describe_neighborhood(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    return describe_neighborhood(
        state.conn,
        lat=state.target.lat,
        lng=state.target.lng,
        radius_m=int(args.get("radius_m", state.base_filters.radius_m)),
        max_age_days=int(args.get("max_age_days", 30)),
        category_main=state.base_filters.category_main,
        category_type=state.base_filters.category_type,
    )


def _handle_verify_listing_freshness(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    return verify_listing_freshness(
        state.conn,
        state.sreality_client,
        int(args["sreality_id"]),
        int(args.get("max_age_hours", 24)),
    )


def _handle_compute_market_velocity(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    from dataclasses import replace
    filters = state.base_filters
    if "radius_m" in args:
        filters = replace(filters, radius_m=int(args["radius_m"]))
    return compute_market_velocity(
        state.conn, state.target, filters,
        population=args.get("population", "all"),
        trend_split_days=int(args.get("trend_split_days", 7)),
    )


def _handle_compute_listing_velocity(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    return compute_listing_velocity(
        state.conn,
        int(args["sreality_id"]),
        radius_m=int(args.get("radius_m", state.base_filters.radius_m)),
        disposition_match=args.get(
            "disposition_match", state.base_filters.disposition_match,
        ),
        population=args.get("population", "all"),
    )


def _handle_compute_walkability(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    return compute_walkability(
        state.conn,
        lat=state.target.lat,
        lng=state.target.lng,
        radius_m=int(args.get("radius_m", 1000)),
    )


def _handle_compute_amenity_supply(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    return compute_amenity_supply(
        state.conn,
        lat=state.target.lat,
        lng=state.target.lng,
        radius_m=int(args.get("radius_m", 1000)),
    )


def _handle_find_comparables_along_axis(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    result = find_comparables_along_axis(
        state.conn, state.target, state.base_filters,
        transport_types=args.get("transport_types"),
        anchor_radius_m=int(args.get("anchor_radius_m", 800)),
        corridor_m=int(args.get("corridor_m", 300)),
    )
    new_listings = result.get("data", {}).get("listings") or []

    # Merge into the active cohort, deduped by sreality_id. Existing
    # entries win — they came from find_comparables_relaxed and carry
    # the canonical numeric fields (distance_m to the anchor, etc).
    existing_ids = {int(l["sreality_id"]) for l in state.last_cohort}
    added = 0
    for listing in new_listings:
        sid = int(listing["sreality_id"])
        if sid not in existing_ids:
            state.last_cohort.append(listing)
            existing_ids.add(sid)
            added += 1
    result["data"]["cohort_added"] = added
    result["data"]["cohort_size_after_merge"] = len(state.last_cohort)
    return result


def _handle_summarize_listing(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    return summarize_listing(
        state.conn, state.llm_client,
        sreality_id=int(args["sreality_id"]),
    )


def _handle_compare_listing_images(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    a = int(args["sreality_id_a"])
    b = int(args["sreality_id_b"])
    cohort_ids = {int(l["sreality_id"]) for l in state.last_cohort}
    missing = [sid for sid in (a, b) if sid not in cohort_ids]
    if missing:
        raise ValueError(
            f"compare_listing_images: id(s) {missing} are not in the current "
            f"cohort. Build the cohort with find_comparables_relaxed first, "
            f"then compare two ids from the result."
        )
    return compare_listing_images(
        state.conn, state.llm_client,
        sreality_id_a=a,
        sreality_id_b=b,
        n_images=int(args.get("n_images", 6)),
    )


# --- result summaries -----------------------------------------------------

def _tool_summary(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded `output_summary` for the trace step."""
    data = result.get("data") or {}
    md = result.get("metadata") or {}
    if name == "find_comparables_relaxed":
        listings = data.get("listings") or []
        return {
            "result_count": md.get("result_count") or len(listings),
            "n_relaxations": len(data.get("relaxation_trace") or []),
            "final_filters": (data.get("relaxation_trace") or [{}])[-1].get(
                "filters_applied"
            ) if data.get("relaxation_trace") else None,
        }
    if name == "analyze_distribution":
        return {
            "field": md.get("filters_used", {}).get("field"),
            "n": data.get("n"),
            "median": data.get("median"),
            "p25": data.get("p25"),
            "p75": data.get("p75"),
        }
    if name == "find_distribution_outliers":
        return {
            "n_outliers": len(data.get("outliers") or []),
            "n_total": data.get("n"),
        }
    if name == "describe_neighborhood":
        return {
            "n": data.get("active_listings"),
            "median_price_per_m2": data.get("median_price_per_m2"),
        }
    if name == "verify_listing_freshness":
        return {
            "is_live": data.get("is_live"),
            "from_cache": data.get("from_cache"),
        }
    if name == "compute_market_velocity":
        tom = data.get("tom_stats") or {}
        return {
            "cohort_size": data.get("cohort_size"),
            "active_count": data.get("active_count"),
            "delisted_count": data.get("delisted_count"),
            "median_tom_days": tom.get("median_days"),
            "p75_tom_days": tom.get("p75_days"),
        }
    if name == "compute_listing_velocity":
        return {
            "sreality_id": data.get("sreality_id"),
            "tom_days": data.get("tom_days"),
            "tom_percentile": data.get("tom_percentile"),
            "classification": data.get("classification"),
            "cohort_size": data.get("cohort_size"),
        }
    if name == "compute_walkability":
        return {
            "walkability_score": data.get("walkability_score"),
            "n_categories_with_data": md.get("result_count"),
            "missing_categories": data.get("missing_categories") or [],
        }
    if name == "compute_amenity_supply":
        summary = data.get("summary") or {}
        return {
            "n_scarce": len(summary.get("scarce") or []),
            "n_adequate": len(summary.get("adequate") or []),
            "n_abundant": len(summary.get("abundant") or []),
            "scarce_categories": (summary.get("scarce") or [])[:5],
        }
    if name == "find_comparables_along_axis":
        return {
            "axis_listings": md.get("result_count"),
            "lines_considered": md.get("lines_considered"),
            "cohort_added": data.get("cohort_added"),
            "cohort_size_after_merge": data.get("cohort_size_after_merge"),
        }
    if name == "summarize_listing":
        summary = data.get("summary") or {}
        highlights = summary.get("key_highlights") or []
        return {
            "sreality_id": data.get("sreality_id"),
            "headline": summary.get("headline"),
            "condition_assessment": summary.get("condition_assessment"),
            "n_highlights": len(highlights),
            "n_concerns": len(summary.get("concerns") or []),
            "cache_hit": data.get("cache_hit"),
        }
    if name == "compare_listing_images":
        comp = data.get("comparison") or {}
        return {
            "sreality_id_a": data.get("sreality_id_a"),
            "sreality_id_b": data.get("sreality_id_b"),
            "overall_similarity": comp.get("overall_similarity"),
            "cache_hit": data.get("cache_hit"),
        }
    return {"keys": list(data.keys())[:6]}


def _terminator_summary(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "estimated_monthly_rent_czk": args.get("estimated_monthly_rent_czk"),
        "rent_p25_czk": args.get("rent_p25_czk"),
        "rent_p75_czk": args.get("rent_p75_czk"),
        "confidence": args.get("confidence"),
        "n_comparables_used": len(args.get("comparables_used") or []),
        "n_warnings": len(args.get("warnings") or []),
    }


def _format_tool_result(name: str, result: dict[str, Any]) -> str:
    """Render the result as JSON for the LLM, truncated if huge."""
    payload = result.get("data") or result
    text = json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) > _TOOL_RESULT_PREVIEW:
        return text[:_TOOL_RESULT_PREVIEW] + f"\n…(truncated {len(text) - _TOOL_RESULT_PREVIEW} chars)"
    return text


# --- finalisation ---------------------------------------------------------

def _finalise(
    state: _LoopState,
    *,
    skill: "Skill",
    provider: str,
    stop_reason: str,
    purchase_price_czk: int | None,
    used_entry: Callable[[dict[str, Any]], dict[str, Any]],
) -> AgentResult:
    if stop_reason != "record_estimate" or state.final_call is None:
        return AgentResult(
            data={
                "estimated_monthly_rent_czk": None,
                "rent_p25_czk": None,
                "rent_p75_czk": None,
                "gross_yield_pct": None,
                "confidence": None,
                "comparables_used": [],
                "warnings": [f"agent halted: stop_reason={stop_reason}"],
            },
            metadata={
                "stop_reason": stop_reason,
                "iterations": state.iterations,
                "total_cost_usd": round(state.total_cost_usd, 6),
                "provider": provider,
                "skill": skill.name,
            },
        )

    call = state.final_call
    estimate = _round_to_100(call.get("estimated_monthly_rent_czk"))
    p25 = _round_to_100(call.get("rent_p25_czk"))
    p75 = _round_to_100(call.get("rent_p75_czk"))
    confidence = call.get("confidence")
    warnings = list(call.get("warnings") or [])

    declared_ids = set(int(i) for i in call.get("comparables_used") or [])
    cohort_by_id = {l["sreality_id"]: l for l in state.last_cohort}
    valid_ids = sorted(declared_ids & set(cohort_by_id.keys()))
    invented = declared_ids - set(cohort_by_id.keys())
    if invented:
        warnings.append(
            f"agent referenced {len(invented)} sreality_id(s) not in the "
            f"latest cohort: {sorted(invented)[:5]}{'…' if len(invented) > 5 else ''}"
        )
    comparables_used = [used_entry(cohort_by_id[i]) for i in valid_ids]

    yield_pct: float | None = None
    if estimate is not None and purchase_price_czk and purchase_price_czk > 0:
        yield_pct = round((estimate * 12) / purchase_price_czk * 100, 2)

    return AgentResult(
        data={
            "estimated_monthly_rent_czk": estimate,
            "rent_p25_czk": p25,
            "rent_p75_czk": p75,
            "gross_yield_pct": yield_pct,
            "confidence": confidence,
            "comparables_used": comparables_used,
            "warnings": warnings,
        },
        metadata={
            "stop_reason": stop_reason,
            "iterations": state.iterations,
            "total_cost_usd": round(state.total_cost_usd, 6),
            "provider": provider,
            "skill": skill.name,
        },
    )


# --- helpers --------------------------------------------------------------

def _initial_user_message(
    target: TargetSpec,
    filters: ComparableFilters,
    purchase_price_czk: int | None,
) -> str:
    payload = {
        "target": {
            "lat": target.lat,
            "lng": target.lng,
            "area_m2": target.area_m2,
            "disposition": target.disposition,
            "floor": target.floor,
        },
        "filters": {
            "radius_m": filters.radius_m,
            "max_age_days": filters.max_age_days,
            "category_main": filters.category_main,
            "category_type": filters.category_type,
        },
        "purchase_price_czk": purchase_price_czk,
    }
    return (
        "Estimate the monthly rent (CZK) for the following target. "
        "Follow your operating principles. The first tool call should "
        "be find_comparables_relaxed.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _round_to_100(value: Any) -> int | None:
    if value is None:
        return None
    try:
        n = int(round(float(value) / 100.0) * 100)
        return n
    except (TypeError, ValueError):
        return None


def _running_cost(
    state: _LoopState, llm_client: "LLMClient", run_id: int,
) -> float:
    """Refresh total cost from llm_calls so the cap honours cache writes etc."""
    try:
        with state.conn.cursor() as cur:
            cur.execute(
                "SELECT coalesce(sum(cost_usd), 0) FROM llm_calls "
                "WHERE estimation_run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else state.total_cost_usd
    except Exception as exc:
        LOG.debug("running cost lookup failed: %s", exc)
        return state.total_cost_usd


# Registry is built at the bottom so the handler functions are already
# in scope. `_ToolDef.handler` holds a callable; building the registry
# before the handler defs would NameError on import.
AGENT_TOOLS: dict[str, _ToolDef] = _build_tool_registry()
