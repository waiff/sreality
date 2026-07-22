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
    get_manual_rental_estimates,
    read_floor_plan,
    summarize_listing,
    verify_listing_freshness,
)
from toolkit import filter_registry
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

# Shared descriptions for the dual listing-address inputs the per-listing tools
# accept. The cohort emitted by find_comparables_relaxed carries BOTH ids, so
# the agent can name either; post-Gate-2 a listing with no sreality_id is only
# reachable by the surrogate.
_SREALITY_ID_DESC = (
    "Portal-native listing id. Supply either sreality_id or listing_id; when "
    "both are given, listing_id (the stable surrogate) wins."
)
_LISTING_ID_DESC = (
    "Stable surrogate listing id (listings.id) — preferred, and the only handle "
    "for a listing that has no sreality_id."
)


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
                "cohort + a relaxation_trace showing what was widened.\n\n"
                "Every filter is optional. Omitted filters fall back to the "
                "base filters established for this run (request body + app_settings "
                "defaults). The skill prompt should instruct WHEN and HOW to "
                "tune each one; pass only the filters you want to differ from "
                "the base for this round."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    # Every filter the registry declares for COMPARABLES.
                    # Descriptions come from `filter_registry.REGISTRY[id]
                    # .description` — single source of truth, no more
                    # hand-written agent prose drifting from operator-
                    # facing surfaces.
                    **filter_registry.to_jsonschema_properties(
                        filter_registry.Agenda.COMPARABLES,
                    ),
                    # Knobs specific to the relaxation wrapper — these
                    # don't belong on `ComparableFilters` itself.
                    "min_results": {
                        "type": "integer", "minimum": 1, "maximum": 50,
                        "description": (
                            "Stop relaxing once at least this many "
                            "comparables are found. Default 5."
                        ),
                    },
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
                    # The handler honours only radius_m + max_age_days;
                    # category is taken from the run's base_filters and
                    # cannot be overridden per-call. We pull descriptions
                    # from the registry so the agent reads canonical text.
                    "radius_m": filter_registry.to_jsonschema_property(
                        filter_registry.by_id("radius_m"),
                    ),
                    "max_age_days": filter_registry.to_jsonschema_property(
                        filter_registry.by_id("max_age_days"),
                    ),
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
                    "sreality_id": {"type": "integer", "description": _SREALITY_ID_DESC},
                    "listing_id": {"type": "integer", "description": _LISTING_ID_DESC},
                    "max_age_hours": {"type": "integer", "minimum": 1, "maximum": 168},
                },
                # At-least-one is enforced in the handler, not the JSON schema:
                # a portable XOR isn't expressible across providers, and a stale
                # operator prompt naming only sreality_id must still resolve.
                "required": [],
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
                    # The handler honours radius_m + lifecycle only,
                    # plus the velocity-specific trend_split_days. Other
                    # cohort knobs come from base_filters.
                    "radius_m": filter_registry.to_jsonschema_property(
                        filter_registry.by_id("radius_m"),
                    ),
                    "lifecycle": filter_registry.to_jsonschema_property(
                        filter_registry.by_id("lifecycle"),
                    ),
                    "trend_split_days": {
                        "type": "integer", "minimum": 1, "maximum": 90,
                        "description": (
                            "Split the cohort's delisted listings into "
                            "'recent' and 'older' buckets at N days ago "
                            "for a trend signal. Default 7."
                        ),
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
                    "sreality_id": {"type": "integer", "description": _SREALITY_ID_DESC},
                    "listing_id": {"type": "integer", "description": _LISTING_ID_DESC},
                    "radius_m": {"type": "integer", "minimum": 100, "maximum": 5000},
                    "disposition_match": {
                        "type": "string",
                        "enum": ["exact", "loose", "any"],
                    },
                    "lifecycle": {
                        "type": "string",
                        "enum": ["active", "delisted", "all"],
                    },
                },
                # At-least-one of sreality_id / listing_id enforced in the handler.
                "required": [],
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
                    "sreality_id": {"type": "integer", "description": _SREALITY_ID_DESC},
                    "listing_id": {"type": "integer", "description": _LISTING_ID_DESC},
                },
                # At-least-one of sreality_id / listing_id enforced in the handler.
                "required": [],
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
                    "sreality_id_a": {"type": "integer", "description": _SREALITY_ID_DESC},
                    "sreality_id_b": {"type": "integer", "description": _SREALITY_ID_DESC},
                    "listing_id_a": {"type": "integer", "description": _LISTING_ID_DESC},
                    "listing_id_b": {"type": "integer", "description": _LISTING_ID_DESC},
                    "n_images": {"type": "integer", "minimum": 1, "maximum": 12},
                },
                # Address BOTH sides by the same id-space: sreality_id_a+_b, or
                # listing_id_a+_b. Enforced in the handler.
                "required": [],
            },
            handler=_handle_compare_listing_images,
        ),
        "get_manual_rental_estimates": _ToolDef(
            name="get_manual_rental_estimates",
            description=(
                "Fetch operator-recorded manual rental estimates "
                "attached to a listing. Returns 0+ point estimates; "
                "each row has rent_czk (monthly), author, source_kind "
                "(broker / gut / external_comp / portfolio / other), "
                "optional notes, and timestamps. Manual estimates are "
                "operator judgement, not comparables — use them to "
                "reconcile against your distribution, never to "
                "replace it. Returns an empty list if none exist."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sreality_id": {"type": "integer", "description": _SREALITY_ID_DESC},
                    "listing_id": {"type": "integer", "description": _LISTING_ID_DESC},
                },
                # At-least-one of sreality_id / listing_id enforced in the handler.
                "required": [],
            },
            handler=_handle_get_manual_rental_estimates,
        ),
        "read_floor_plan": _ToolDef(
            name="read_floor_plan",
            description=(
                "Read one operator-supplied attachment (floor plan, "
                "drawing, or photo) for the current building_run via "
                "Claude vision. Returns structured headline + room list "
                "+ layout description so the agent can reason about "
                "unit boundaries the listing didn't disclose. Only "
                "valid in a building flow — apartment estimations have "
                "no attachments. Cached per (attachment_id, model); "
                "repeat calls within a session are free. The available "
                "attachment_ids are listed in the initial user message "
                "under <custom_attachments>."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attachment_id": {"type": "integer"},
                    "force_refresh": {"type": "boolean"},
                },
                "required": ["attachment_id"],
            },
            handler=_handle_read_floor_plan,
        ),
        "record_estimate": _ToolDef(
            name="record_estimate",
            description=(
                "Submit the final estimate and END THE RUN. Call exactly once. "
                "After this tool returns, the agent loop exits immediately.\n\n"
                "You do NOT need to retype sreality_ids. The harness already "
                "knows which listings find_comparables_relaxed returned and "
                "treats every one as INCLUDED by default. If you want to set "
                "a specific listing aside (luxury / furnished outlier / "
                "obviously bad data / etc.), add an entry to "
                "`comparable_decisions` with decision='excluded' and a short "
                "reason. Optional included entries with a reason annotate "
                "*why* you kept a particular listing for the audit trail. "
                "Inclusion is the default; exclusion is the editorial act."
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
                    "warnings": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "comparable_decisions": {
                        "type": "array",
                        "description": (
                            "Curation log. The cohort is server-derived from "
                            "the listings find_comparables_relaxed returned; "
                            "default policy is INCLUDE. Use this field to "
                            "express exclusions (decision='excluded' + "
                            "reason) and, optionally, inclusion reasons "
                            "(decision='included' + reason) for listings "
                            "you want to call out. Entries referencing "
                            "sreality_ids not actually in the cohort are "
                            "ignored and surface as a hallucination warning "
                            "on the run."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "sreality_id": {"type": "integer"},
                                "decision": {
                                    "type": "string",
                                    "enum": ["included", "excluded"],
                                },
                                "reason": {"type": "string"},
                            },
                            "required": ["sreality_id", "decision", "reason"],
                        },
                    },
                    "comparables_used": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "DEPRECATED. The server now derives "
                            "comparables_used from the cohort minus "
                            "exclusions; whatever you pass here is "
                            "validated for hallucinations but no longer "
                            "drives the included set. Omit it."
                        ),
                    },
                },
                "required": [
                    "estimated_monthly_rent_czk",
                    "rent_p25_czk",
                    "rent_p75_czk",
                    "confidence",
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
    # Building-flow context: set when the agent is invoked as part of
    # a per-unit child estimation off a building_runs row. Scopes
    # read_floor_plan to attachments that belong to this run.
    building_run_id: int | None = None
    # The estimation_runs.id currently driving the loop; passed through
    # to vision tools so their llm_calls rows attribute correctly.
    estimation_run_id: int | None = None
    # Operator-tunable filter defaults (app_settings, migration 052).
    # Used to seed the agent's per-round min_results when the LLM
    # omits it. Other filter defaults are already baked into
    # `base_filters` upstream in `_build_filters`.
    filter_defaults: Any = None


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
    special_instructions: str | None = None,
    contextual_text: str | None = None,
    building_run_id: int | None = None,
    attachments: list[dict[str, Any]] | None = None,
    subject_condition: dict[str, Any] | None = None,
) -> AgentResult:
    """Drive the agent loop. Returns AgentResult with `metadata.stop_reason`.

    Exceptions raised by `llm_client.call` (rate limit, API error,
    unknown provider) propagate up — the caller writes status='failed'
    on the run row.
    """
    from api.estimate_yield import _used_entry  # circular dep; lazy import
    from api.estimation_runs import load_filter_defaults

    state = _LoopState(
        conn=conn,
        sreality_client=sreality_client,
        llm_client=llm_client,
        target=target,
        base_filters=filters,
        building_run_id=building_run_id,
        estimation_run_id=estimation_run_id,
        filter_defaults=load_filter_defaults(conn),
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

    # Audit step #0: record which skill was selected and the knobs
    # that came with it, before the loop runs. This answers "why was
    # this skill used on this run?" — the trace itself states the
    # name, description, model, and limits; the caller controls
    # which skill is loaded (CreateEstimationIn.skill, default
    # 'rental_estimator_v1') so the answer to "why this one over
    # the alternatives" lives at the request boundary, but the
    # operator now sees what was actually picked.
    with recorder.computation("skill_choice") as h:
        h.set_summary({
            "skill_name": skill.name,
            "skill_description": skill.description,
            "provider": provider,
            "model": model,
            "max_iterations": skill.limits.max_iterations,
            "max_cost_usd": skill.limits.max_cost_usd,
            "wall_clock_timeout_s": skill.limits.wall_clock_timeout_s,
            "allowed_tools": list(skill.allowed_tools),
            "skill_updated_at": skill.updated_at,
        })

    system_text = skill.system_prompt
    messages: list[Message] = [
        Message(role="user", content=[TextBlock(text=_initial_user_message(
            target, filters, purchase_price_czk,
            special_instructions=special_instructions,
            contextual_text=contextual_text,
            attachments=attachments,
            subject_condition=subject_condition,
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
                    h.set_full_output({"error": "unknown_tool", "input": tc.input})
                continue
            if tool_def.is_terminator:
                state.final_call = tc.input
                with recorder.tool_call(tc.name, tc.input) as h:
                    h.set_summary(_terminator_summary(tc.input))
                    h.set_full_output({"terminator_input": tc.input})
                stop_reason = "record_estimate"
                break
            try:
                with recorder.tool_call(tc.name, tc.input) as h:
                    result = _dispatch_tool(tool_def, tc.input, state)
                    h.set_summary(_tool_summary(tc.name, result))
                    h.set_full_output(result)
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
    used = result.data.get("comparables_used") or []
    # Runs AFTER the estimate is computed and paid for, and outside the per-tool
    # try/except — so an int(None) here would throw away a finished run. A
    # comparable with no sreality_id is simply absent from the legacy list; the
    # surrogate list beside it is complete. Both are emitted rather than
    # switching the existing field's id space, because the two spaces overlap
    # numerically and readers of `final_comparable_ids` expect sreality_ids.
    final_ids = sorted(
        int(c["sreality_id"]) for c in used if c.get("sreality_id") is not None
    )
    final_listing_ids = sorted(
        int(c["listing_id"]) for c in used if c.get("listing_id") is not None
    )
    final_filters = state.selection_rounds[-1]["filters"] if state.selection_rounds else None
    with recorder.computation("comparable_selection_summary") as h:
        h.set_summary({
            "n_rounds": len(state.selection_rounds),
            "rounds": state.selection_rounds,
            "final_filters": final_filters,
            "final_comparable_ids": final_ids,
            "final_comparable_listing_ids": final_listing_ids,
            "stop_reason": stop_reason,
        })


# --- tool dispatchers -----------------------------------------------------

def _dispatch_tool(
    tool_def: _ToolDef, args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    if tool_def.handler is None:
        raise RuntimeError(f"tool {tool_def.name} has no handler")
    return tool_def.handler(args, state)


def _listing_id_kwargs(args: dict[str, Any]) -> dict[str, int]:
    """The single {sreality_id|listing_id: value} kwarg for a per-listing tool.

    The surrogate listing_id wins when the model names both (the id-spaces
    overlap numerically, so we never cross-resolve). Exactly one kwarg is
    returned, so a tool called with only sreality_id dispatches byte-identically
    to before. Raises ValueError — not TypeError — when the model names neither,
    so a stale operator prompt degrades to a clean tool error.
    """
    lid = args.get("listing_id")
    if lid is not None:
        return {"listing_id": int(lid)}
    sid = args.get("sreality_id")
    if sid is not None:
        return {"sreality_id": int(sid)}
    raise ValueError("tool call must include either sreality_id or listing_id")


_FCR_OVERRIDE_FIELDS: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    ("radius_m", int),
    ("area_band_pct", float),
    ("disposition_match", str),
    ("max_age_days", int),
    ("lifecycle", str),
    ("floor_band", int),
    ("portals", list),
    ("condition_match", list),
    ("building_type_match", list),
    ("energy_rating_match", list),
    ("has_balcony", bool),
    ("has_lift", bool),
    ("has_parking", bool),
    ("min_price_czk", int),
    ("max_price_czk", int),
    ("category_main", str),
    ("category_type", str),
    ("category_sub_cb", int),
    ("locality_district_id", int),
    ("locality_region_id", int),
    ("include_unreliable", bool),
    ("furnished", list),
    ("terrace", bool),
    ("cellar", bool),
    ("garage", bool),
    ("ownership", list),
    ("min_estate_area", float),
    ("max_estate_area", float),
    ("min_usable_area", float),
    ("max_usable_area", float),
    ("min_parking_lots", int),
    ("building_condition_level_min", int),
    ("building_condition_level_max", int),
    ("apartment_condition_level_min", int),
    ("apartment_condition_level_max", int),
)


def _filters_snapshot(
    filters: ComparableFilters, *, min_results: int,
) -> dict[str, Any]:
    """Render every ComparableFilters field the agent can tune into a flat
    dict for the selection_rounds audit trail.

    The frontend's Strategy table renders one row per key here, so every
    field listed here gets a column in the UI — including fields the
    agent left at the base value. That's the explicit guarantee from
    the user: "show all filters an agent can use ... even if the
    agent chose to leave the filter blank".
    """
    return {
        "radius_m": filters.radius_m,
        "area_band_pct": filters.area_band_pct,
        "disposition_match": filters.disposition_match,
        "max_age_days": filters.max_age_days,
        "min_results": min_results,
        "lifecycle": filters.lifecycle,
        "floor_band": filters.floor_band,
        "portals": list(filters.portals) if filters.portals else None,
        "condition_match": (
            list(filters.condition_match) if filters.condition_match else None
        ),
        "building_type_match": (
            list(filters.building_type_match)
            if filters.building_type_match else None
        ),
        "energy_rating_match": (
            list(filters.energy_rating_match)
            if filters.energy_rating_match else None
        ),
        "has_balcony": filters.has_balcony,
        "has_lift": filters.has_lift,
        "has_parking": filters.has_parking,
        "min_price_czk": filters.min_price_czk,
        "max_price_czk": filters.max_price_czk,
        "category_main": filters.category_main,
        "category_type": filters.category_type,
        "category_sub_cb": filters.category_sub_cb,
        "locality_district_id": filters.locality_district_id,
        "locality_region_id": filters.locality_region_id,
        "include_unreliable": filters.include_unreliable,
        "furnished": list(filters.furnished) if filters.furnished else None,
        "terrace": filters.terrace,
        "cellar": filters.cellar,
        "garage": filters.garage,
        "ownership": list(filters.ownership) if filters.ownership else None,
        "min_estate_area": filters.min_estate_area,
        "max_estate_area": filters.max_estate_area,
        "min_usable_area": filters.min_usable_area,
        "max_usable_area": filters.max_usable_area,
        "min_parking_lots": filters.min_parking_lots,
        "building_condition_level_min": filters.building_condition_level_min,
        "building_condition_level_max": filters.building_condition_level_max,
        "apartment_condition_level_min": filters.apartment_condition_level_min,
        "apartment_condition_level_max": filters.apartment_condition_level_max,
    }


def _coerce_arg(name: str, value: Any, caster: Any) -> Any:
    if value is None:
        return None
    if caster is bool:
        return bool(value)
    if caster is list:
        if isinstance(value, list):
            return [str(v) for v in value if v is not None and str(v) != ""]
        return None
    try:
        return caster(value)
    except (TypeError, ValueError):
        LOG.warning("find_comparables_relaxed: bad value for %r: %r", name, value)
        return None


def _handle_find_comparables_relaxed(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    from dataclasses import replace
    filters = state.base_filters
    for name, caster in _FCR_OVERRIDE_FIELDS:
        if name not in args:
            continue
        coerced = _coerce_arg(name, args[name], caster)
        if coerced is None and caster is list:
            continue
        filters = replace(filters, **{name: coerced})

    min_results = int(
        args.get("min_results", state.filter_defaults.min_results)
        if state.filter_defaults
        else args.get("min_results", 5)
    )
    result = find_comparables_relaxed(
        state.conn, state.target, filters, min_results=min_results,
    )
    listings = result.get("data", {}).get("listings") or []

    prev_ids = {int(l["listing_id"]) for l in state.last_cohort}
    new_ids = {int(l["listing_id"]) for l in listings}
    round_n = len(state.selection_rounds) + 1
    state.selection_rounds.append({
        "n": round_n,
        "filters": _filters_snapshot(filters, min_results=min_results),
        "cohort_size": len(listings),
        "cohort_ids": sorted(new_ids),
        "added_ids": sorted(new_ids - prev_ids),
        "removed_ids": sorted(prev_ids - new_ids),
        "n_relaxations": len(result.get("data", {}).get("relaxation_trace") or []),
        "reasoning": state.last_reasoning,
    })

    state.last_cohort = listings
    _persist_cohort_entries(state, listings, round_n=round_n)
    return result


def _persist_cohort_entries(
    state: _LoopState,
    listings: list[dict[str, Any]],
    *,
    round_n: int,
) -> None:
    """Upsert one row per cohort listing into estimation_cohort_entries.

    Server-authoritative source of truth: every find_comparables_relaxed
    round records the listings it returned. `_finalise` then flips
    `present_at_finalisation` for whatever is still in state.last_cohort
    at terminator time, so the LLM never has to retype IDs.
    """
    if state.estimation_run_id is None or not listings:
        return
    sql = (
        "INSERT INTO estimation_cohort_entries ("
        "  estimation_run_id, sreality_id, listing_id, first_seen_round_n,"
        "  last_seen_round_n, snapshot_id, distance_m, price_czk,"
        "  area_m2, price_per_m2, disposition"
        ") VALUES ("
        # listing_id is bound DIRECTLY, not resolved through the legacy key.
        # estimation_cohort_entries.listing_id is NOT NULL (it is half the PK
        # since the Phase D swap), so the old
        # `(SELECT id FROM listings WHERE sreality_id = %(sid)s)` subquery would
        # return NULL post-Gate-2 and hard-fail the INSERT — inside the bare
        # `except` below, i.e. cohort provenance would vanish with a green run.
        "  %(run_id)s, %(sid)s, %(lid)s,"
        "  %(round)s, %(round)s,"
        "  %(snap)s, %(dist)s, %(price)s, %(area)s, %(ppm2)s, %(disp)s"
        # Arbiter is listing_id (R2 Phase C, estimation_cohort_entries_run_listing_id_key).
        ") ON CONFLICT (estimation_run_id, listing_id) DO UPDATE SET"
        "  last_seen_round_n = EXCLUDED.last_seen_round_n,"
        "  snapshot_id       = COALESCE(EXCLUDED.snapshot_id, estimation_cohort_entries.snapshot_id),"
        "  distance_m        = COALESCE(EXCLUDED.distance_m, estimation_cohort_entries.distance_m),"
        "  price_czk         = COALESCE(EXCLUDED.price_czk, estimation_cohort_entries.price_czk),"
        "  area_m2           = COALESCE(EXCLUDED.area_m2, estimation_cohort_entries.area_m2),"
        "  price_per_m2      = COALESCE(EXCLUDED.price_per_m2, estimation_cohort_entries.price_per_m2),"
        "  disposition       = COALESCE(EXCLUDED.disposition, estimation_cohort_entries.disposition)"
    )
    try:
        with state.conn.transaction(), state.conn.cursor() as cur:
            for l in listings:
                cur.execute(sql, {
                    "run_id": state.estimation_run_id,
                    # sreality_id is nullable on this table and NULL post-flip —
                    # never int() it. listing_id is the required one.
                    "sid": l.get("sreality_id"),
                    "lid": int(l["listing_id"]),
                    "round": round_n,
                    "snap": l.get("latest_snapshot_id"),
                    "dist": l.get("distance_m"),
                    "price": l.get("price_czk"),
                    "area": l.get("area_m2"),
                    "ppm2": l.get("price_per_m2"),
                    "disp": l.get("disposition"),
                })
    except Exception as exc:
        LOG.warning(
            "persist_cohort_entries failed for run=%s round=%s: %s",
            state.estimation_run_id, round_n, exc,
        )


def _persist_finalisation(
    state: _LoopState,
    *,
    included_listing_ids: set[int],
    excluded_by_listing_id: dict[int, str],
    inclusion_reasons_by_listing_id: dict[int, str],
) -> None:
    """Mark which cohort entries survived to the final estimate.

    Every id here is a `listings.id` SURROGATE, matching the column this table
    is keyed on — `_finalise` translates out of the agent's sreality_id space
    before calling. The two spaces overlap numerically, so passing the wrong one
    silently updates the wrong rows rather than failing.

    Flips `present_at_finalisation` on every row still in state.last_cohort
    (whether included or excluded). Sets `excluded_by_agent` +
    `exclusion_reason` for rows the agent set aside via comparable_decisions,
    and stores any explicit inclusion reason. Hallucinated IDs were already
    filtered by `_finalise`, so nothing the LLM invented reaches this table.
    """
    if state.estimation_run_id is None:
        return
    all_ids = set(included_listing_ids) | set(excluded_by_listing_id.keys())
    if not all_ids:
        return
    try:
        with state.conn.transaction(), state.conn.cursor() as cur:
            cur.execute(
                "UPDATE estimation_cohort_entries SET "
                # Keyed on the surrogate: `sreality_id = ANY(...)` is NULL for a post-flip
                # row, so present_at_finalisation would never flip for it.
                "  present_at_finalisation = (listing_id = ANY(%(present)s)),"
                "  excluded_by_agent       = (listing_id = ANY(%(excluded)s)),"
                "  exclusion_reason        = NULL,"
                "  inclusion_reason        = NULL "
                "WHERE estimation_run_id = %(run_id)s",
                {
                    "run_id": state.estimation_run_id,
                    "present": list(all_ids),
                    "excluded": list(excluded_by_listing_id.keys()),
                },
            )
            for lid, reason in excluded_by_listing_id.items():
                cur.execute(
                    "UPDATE estimation_cohort_entries SET "
                    "  exclusion_reason = %(reason)s "
                    "WHERE estimation_run_id = %(run_id)s AND listing_id = %(lid)s",
                    {
                        "run_id": state.estimation_run_id,
                        "lid": lid,
                        "reason": reason,
                    },
                )
            for lid, reason in inclusion_reasons_by_listing_id.items():
                cur.execute(
                    "UPDATE estimation_cohort_entries SET "
                    "  inclusion_reason = %(reason)s "
                    "WHERE estimation_run_id = %(run_id)s AND listing_id = %(lid)s",
                    {
                        "run_id": state.estimation_run_id,
                        "lid": lid,
                        "reason": reason,
                    },
                )
    except Exception as exc:
        LOG.warning(
            "persist_finalisation failed for run=%s: %s",
            state.estimation_run_id, exc,
        )


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
        max_age_days=(
            int(args["max_age_days"]) if "max_age_days" in args else None
        ),
        category_main=state.base_filters.category_main,
        category_type=state.base_filters.category_type,
    )


def _handle_verify_listing_freshness(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    return verify_listing_freshness(
        state.conn,
        state.sreality_client,
        **_listing_id_kwargs(args),
        max_age_hours=int(args.get("max_age_hours", 24)),
    )


def _handle_get_manual_rental_estimates(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    return get_manual_rental_estimates(state.conn, **_listing_id_kwargs(args))


def _handle_compute_market_velocity(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    from dataclasses import replace
    filters = state.base_filters
    if "radius_m" in args:
        filters = replace(filters, radius_m=int(args["radius_m"]))
    return compute_market_velocity(
        state.conn, state.target, filters,
        lifecycle=args.get("lifecycle", "all"),
        trend_split_days=int(args.get("trend_split_days", 7)),
    )


def _handle_compute_listing_velocity(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    return compute_listing_velocity(
        state.conn,
        **_listing_id_kwargs(args),
        radius_m=int(args.get("radius_m", state.base_filters.radius_m)),
        disposition_match=args.get(
            "disposition_match", state.base_filters.disposition_match,
        ),
        lifecycle=args.get("lifecycle", "all"),
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

    # Merge into the active cohort, deduped by listing_id. Existing
    # entries win — they came from find_comparables_relaxed and carry
    # the canonical numeric fields (distance_m to the anchor, etc).
    existing_ids = {int(l["listing_id"]) for l in state.last_cohort}
    added = 0
    for listing in new_listings:
        lid = int(listing["listing_id"])
        if lid not in existing_ids:
            state.last_cohort.append(listing)
            existing_ids.add(lid)
            added += 1
    result["data"]["cohort_added"] = added
    result["data"]["cohort_size_after_merge"] = len(state.last_cohort)
    return result


def _handle_summarize_listing(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    return summarize_listing(
        state.conn, state.llm_client,
        **_listing_id_kwargs(args),
    )


def _handle_read_floor_plan(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    if state.building_run_id is None:
        raise ValueError(
            "read_floor_plan is only valid in a building flow; this "
            "estimation has no building_run_id set"
        )
    attachment_id = int(args["attachment_id"])
    from api.attachments import fetch_attachment
    row = fetch_attachment(state.conn, attachment_id)
    if row is None:
        raise ValueError(f"attachment_id={attachment_id} not found")
    if row["building_run_id"] != state.building_run_id:
        raise ValueError(
            f"attachment_id={attachment_id} does not belong to the "
            f"current building_run"
        )
    return read_floor_plan(
        state.conn, state.llm_client,
        attachment_id=attachment_id,
        force_refresh=bool(args.get("force_refresh", False)),
        estimation_run_id=state.estimation_run_id,
    )


def _handle_compare_listing_images(
    args: dict[str, Any], state: _LoopState,
) -> dict[str, Any]:
    # The surrogate wins per pair: if either side names a listing_id, both must,
    # and cohort membership is checked in surrogate space (always present on the
    # cohort). Otherwise fall back to the legacy sreality_id space — a NULL-
    # sreality cohort row is then simply not nameable and lands in `missing`.
    by_lid = args.get("listing_id_a") is not None or args.get("listing_id_b") is not None
    if by_lid:
        if args.get("listing_id_a") is None or args.get("listing_id_b") is None:
            raise ValueError(
                "compare_listing_images: supply listing_id for BOTH a and b"
            )
        a, b = int(args["listing_id_a"]), int(args["listing_id_b"])
        id_key = "listing_id"
    else:
        if args.get("sreality_id_a") is None or args.get("sreality_id_b") is None:
            raise ValueError(
                "compare_listing_images: supply a sreality_id or listing_id "
                "for both a and b"
            )
        a, b = int(args["sreality_id_a"]), int(args["sreality_id_b"])
        id_key = "sreality_id"

    cohort_ids = {
        int(l[id_key]) for l in state.last_cohort
        if l.get(id_key) is not None
    }
    missing = [i for i in (a, b) if i not in cohort_ids]
    if missing:
        raise ValueError(
            f"compare_listing_images: {id_key}(s) {missing} are not in the "
            f"current cohort. Build the cohort with find_comparables_relaxed "
            f"first, then compare two ids from the result."
        )
    n_images = int(args.get("n_images", 6))
    if by_lid:
        return compare_listing_images(
            state.conn, state.llm_client,
            listing_id_a=a, listing_id_b=b, n_images=n_images,
        )
    return compare_listing_images(
        state.conn, state.llm_client,
        sreality_id_a=a, sreality_id_b=b, n_images=n_images,
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
    if name == "read_floor_plan":
        return {
            "attachment_id": data.get("attachment_id"),
            "filename": data.get("filename"),
            "image_kind": data.get("image_kind"),
            "headline": data.get("headline"),
            "n_rooms": len(data.get("rooms") or []),
            "total_area_m2": data.get("total_area_m2"),
            "confidence": data.get("confidence"),
            "cache_hit": data.get("cache_hit"),
        }
    return {"keys": list(data.keys())[:6]}


def _terminator_summary(args: dict[str, Any]) -> dict[str, Any]:
    decisions = _normalise_decisions(args.get("comparable_decisions"))
    # comparables_used is deprecated input; report the count if the
    # agent still passed it so the trace shows what was supplied, but
    # the authoritative inclusion count comes from `_finalise` after
    # default-include against the server-side cohort.
    return {
        "estimated_monthly_rent_czk": args.get("estimated_monthly_rent_czk"),
        "rent_p25_czk": args.get("rent_p25_czk"),
        "rent_p75_czk": args.get("rent_p75_czk"),
        "confidence": args.get("confidence"),
        "n_decisions_included": sum(
            1 for d in decisions if d["decision"] == "included"
        ),
        "n_decisions_excluded": sum(
            1 for d in decisions if d["decision"] == "excluded"
        ),
        "n_comparables_used_declared": len(args.get("comparables_used") or []),
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

    # Server-derived cohort, not LLM-declared. The cohort is whatever
    # `state.last_cohort` holds at terminator time — those rows were
    # written by `_handle_find_comparables_relaxed` round by round.
    # The LLM's only authority is curation (decision='excluded' with
    # a reason); any sreality_id it names that isn't actually in the
    # cohort surfaces as a hallucination warning but never poisons
    # the included set.
    # TWO id spaces, deliberately kept apart. The LLM addresses comparables by
    # sreality_id (its tool schemas' contract), so decision handling stays in
    # that space. PERSISTENCE keys on the surrogate: estimation_cohort_entries
    # is keyed on listing_id, and a post-Gate-2 row has no sreality_id at all —
    # keying the cohort on it would both raise int(None) here and silently drop
    # that row from present_at_finalisation.
    cohort_by_lid = {int(l["listing_id"]): l for l in state.last_cohort}
    cohort_lids = set(cohort_by_lid.keys())
    # Translation for the LLM-facing half. A listing with no sreality_id simply
    # cannot be named by the agent yet (widening the tool schemas is its own
    # change) — it still participates in default-include below.
    lid_by_sid = {
        int(l["sreality_id"]): int(l["listing_id"])
        for l in state.last_cohort if l.get("sreality_id") is not None
    }
    cohort_ids = set(lid_by_sid.keys())

    decisions = _normalise_decisions(call.get("comparable_decisions"))
    decisions_in_cohort = [
        d for d in decisions if d["sreality_id"] in cohort_ids
    ]
    invented = sorted({
        d["sreality_id"] for d in decisions
        if d["sreality_id"] not in cohort_ids
    })
    if invented:
        warnings.append(
            f"agent referenced {len(invented)} sreality_id(s) not in the "
            f"latest cohort (ignored): {invented[:5]}"
            f"{'…' if len(invented) > 5 else ''}"
        )

    # Legacy comparables_used IDs ride along only for hallucination
    # detection — they no longer drive the included set. The legacy
    # path is preserved here so skills mid-migration don't silently
    # change behaviour: an ID the agent declared but no decision
    # references is still treated as included via default-include.
    legacy_declared = {
        int(i) for i in call.get("comparables_used") or []
    }
    invented_legacy = sorted(legacy_declared - cohort_ids - set(invented))
    if invented_legacy:
        warnings.append(
            f"agent referenced {len(invented_legacy)} sreality_id(s) in "
            f"comparables_used not in the latest cohort (ignored): "
            f"{invented_legacy[:5]}{'…' if len(invented_legacy) > 5 else ''}"
        )

    excluded_by_id = {
        d["sreality_id"]: d["reason"]
        for d in decisions_in_cohort if d["decision"] == "excluded"
    }
    included_reasons = {
        d["sreality_id"]: d["reason"]
        for d in decisions_in_cohort if d["decision"] == "included"
    }

    # Cross into surrogate space once, here, so everything below persists on the
    # handle estimation_cohort_entries actually keys on.
    excluded_by_lid = {
        lid_by_sid[sid]: reason for sid, reason in excluded_by_id.items()
    }
    included_reasons_by_lid = {
        lid_by_sid[sid]: reason for sid, reason in included_reasons.items()
    }

    # Default-include: every cohort listing is in `comparables_used`
    # unless the agent explicitly excluded it. This mirrors the
    # statistics — analyze_distribution already consumed the full
    # cohort, so the recorded "used" set should match. Computed over the
    # SURROGATE set, so a listing the agent could not name still counts as
    # included rather than vanishing from the record.
    included_lids = sorted(cohort_lids - set(excluded_by_lid.keys()))

    comparables_used = [
        {
            **used_entry(cohort_by_lid[lid]),
            "reason": included_reasons_by_lid.get(lid),
        }
        for lid in included_lids
    ]
    comparables_excluded = [
        {"sreality_id": sid, "reason": reason}
        for sid, reason in sorted(excluded_by_id.items())
    ]

    _persist_finalisation(
        state,
        included_listing_ids=set(included_lids),
        excluded_by_listing_id=excluded_by_lid,
        inclusion_reasons_by_listing_id=included_reasons_by_lid,
    )

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
            "comparables_excluded": comparables_excluded,
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


def _normalise_decisions(raw: Any) -> list[dict[str, Any]]:
    """Coerce comparable_decisions from the LLM into a clean list.

    Tolerates missing / malformed entries so a single bad row from
    the model never fails the run. Each returned dict has exactly
    three keys: sreality_id (int), decision ('included'/'excluded'),
    reason (str). Anything outside that shape is dropped.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            sid = int(item.get("sreality_id"))
        except (TypeError, ValueError):
            continue
        decision = item.get("decision")
        reason = item.get("reason")
        if decision not in ("included", "excluded"):
            continue
        if not isinstance(reason, str) or not reason.strip():
            continue
        out.append({
            "sreality_id": sid,
            "decision": decision,
            "reason": reason.strip(),
        })
    return out


# --- helpers --------------------------------------------------------------

def _initial_user_message(
    target: TargetSpec,
    filters: ComparableFilters,
    purchase_price_czk: int | None,
    *,
    special_instructions: str | None = None,
    contextual_text: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    subject_condition: dict[str, Any] | None = None,
) -> str:
    cond = subject_condition or {}
    payload = {
        "target": {
            "lat": target.lat,
            "lng": target.lng,
            "area_m2": target.area_m2,
            "disposition": target.disposition,
            "floor": target.floor,
            "condition": cond.get("condition"),
            "apartment_condition_level": cond.get("apartment_condition_level"),
            "building_condition_level": cond.get("building_condition_level"),
        },
        "filters": {
            "radius_m": filters.radius_m,
            "max_age_days": filters.max_age_days,
            "category_main": filters.category_main,
            "category_type": filters.category_type,
        },
        "purchase_price_czk": purchase_price_czk,
    }
    body = (
        "Estimate the monthly rent (CZK) for the following target. "
        "Follow your operating principles. The first tool call should "
        "be find_comparables_relaxed.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    if special_instructions and special_instructions.strip():
        body += (
            "\n\n<operator_instructions>\n"
            + special_instructions.strip()
            + "\n</operator_instructions>"
        )
    if contextual_text and contextual_text.strip():
        body += (
            "\n\n<contextual_text>\n"
            + contextual_text.strip()
            + "\n</contextual_text>"
        )
    if attachments:
        listing = "\n".join(
            f"- id={a['id']} filename={a.get('filename')!r} "
            f"mime={a.get('mime_type')}"
            for a in attachments
        )
        body += (
            "\n\n<custom_attachments>\n"
            "Operator-supplied images on this building_run. Call "
            "read_floor_plan(attachment_id=...) on any that look "
            "relevant to the layout BEFORE other tools.\n"
            + listing
            + "\n</custom_attachments>"
        )
    return body


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
