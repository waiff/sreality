"""Skill refiner — Phase AI slice C.

Single-pass LLM call that reads one operator feedback row + the
estimation run that triggered it, and proposes a surgical edit to
the skill that produced the run. Same-skill, suggest-then-confirm
flow: the refiner writes to `skill_refinements`; applying happens
via `PUT /admin/skills/{name}` so `skills_history` captures the
prior prompt automatically.

Prompt-only edit surface (operator's choice). The tool the refiner
calls is `record_skill_refinement`, which accepts a full
`proposed_prompt` and an `explanation`. Tool whitelists stay
locked.

Lives next to api/feedback.py (storage helpers) and is invoked from
the POST /estimations/{id}/feedback handler when
`kick_off_refinement=true`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from api.providers import (
    Message,
    TextBlock,
    ToolSchema,
)
from api.skills import SkillNotFound, load_skill

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient

LOG = logging.getLogger(__name__)


REFINER_TOOL_SCHEMA = ToolSchema(
    name="record_skill_refinement",
    description=(
        "Submit the proposed refinement. Call exactly once. "
        "proposed_prompt must be the FULL new system prompt "
        "(not a diff). explanation is 2-4 sentences."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "proposed_prompt": {"type": "string"},
            "explanation": {"type": "string"},
        },
        "required": ["proposed_prompt", "explanation"],
    },
)


def run_refinement(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    feedback: dict[str, Any],
    run: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Drive one refinement pass.

    Returns (refinement_row_or_None, terminal_feedback_status). The
    second value is what the caller should set on the parent
    `estimation_feedback` row: 'proposed' on success, 'failed'
    otherwise. The refinement_row dict is what the API returns to
    the frontend; None when the refiner errored.
    """
    skill_name = _pick_skill_name_from_run(run)
    if not skill_name:
        LOG.warning(
            "feedback %s has no agent-mode skill to refine; marking failed",
            feedback.get("id"),
        )
        return None, "failed"

    try:
        skill = load_skill(conn, skill_name)
    except SkillNotFound:
        LOG.warning("skill %r missing for refinement", skill_name)
        return None, "failed"

    system_text = _load_setting(conn, "llm_skill_refiner_system_prompt", str)
    model = _load_setting(conn, "llm_skill_refiner_model", str)
    if not system_text or not model:
        LOG.warning("refiner app_settings missing; cannot run")
        return None, "failed"

    user_text = _build_refiner_user_message(
        feedback=feedback, run=run, skill_name=skill_name,
        original_prompt=skill.system_prompt,
    )

    try:
        resp = llm_client.call(
            called_for="refine_skill",
            system=system_text,
            messages=[Message(role="user", content=[TextBlock(text=user_text)])],
            tools=[REFINER_TOOL_SCHEMA],
            model=model,
            max_tokens=8192,
            estimation_run_id=run["id"],
        )
    except Exception as exc:  # noqa: BLE001 — log + persist failure
        LOG.warning("refiner LLM call failed: %s", exc)
        return None, "failed"

    tool_calls = resp.tool_calls or []
    if not tool_calls:
        LOG.warning(
            "refiner returned no tool call; text was: %r",
            (resp.text or "")[:200],
        )
        return None, "failed"
    args = tool_calls[0].get("input") or {}
    proposed_prompt = args.get("proposed_prompt")
    explanation = args.get("explanation")
    if not isinstance(proposed_prompt, str) or not isinstance(explanation, str):
        LOG.warning(
            "refiner produced malformed tool call: %s",
            list(args.keys()),
        )
        return None, "failed"

    refinement = _insert_refinement(
        conn,
        skill_name=skill_name,
        original_prompt=skill.system_prompt,
        proposed_prompt=proposed_prompt.strip(),
        refiner_explanation=explanation.strip(),
        source_feedback_id=feedback["id"],
    )
    return refinement, "proposed"


def _pick_skill_name_from_run(run: dict[str, Any]) -> str | None:
    """Read the skill the run used from its trace's skill_choice step.

    Falls back to None on deterministic runs (no agent → no skill).
    Older agent runs predating the skill_choice step (anything before
    the slice A.1 commit) also return None — those can't be refined.
    """
    trace = run.get("trace") or {}
    for step in trace.get("steps") or []:
        if step.get("kind") != "computation":
            continue
        if step.get("label") != "skill_choice":
            continue
        out = step.get("output_summary") or {}
        name = out.get("skill_name")
        if isinstance(name, str) and name:
            return name
        return None
    return None


def _load_setting(
    conn: "psycopg.Connection", key: str, expected: type,
) -> Any:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s",
            (key,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    value = row[0]
    if isinstance(value, expected):
        return value
    return None


_REFINEMENT_COLUMNS: tuple[str, ...] = (
    "id", "skill_name", "original_prompt", "proposed_prompt",
    "refiner_explanation", "source_feedback_id", "status",
    "created_at", "applied_at",
)


def _insert_refinement(
    conn: "psycopg.Connection",
    *,
    skill_name: str,
    original_prompt: str,
    proposed_prompt: str,
    refiner_explanation: str,
    source_feedback_id: int,
) -> dict[str, Any]:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_refinements "
            "(skill_name, original_prompt, proposed_prompt, "
            " refiner_explanation, source_feedback_id) "
            "VALUES (%s, %s, %s, %s, %s) "
            f"RETURNING {', '.join(_REFINEMENT_COLUMNS)}",
            (
                skill_name, original_prompt, proposed_prompt,
                refiner_explanation, source_feedback_id,
            ),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING produced no row")
    return _refinement_row_to_dict(row)


def get_refinement(
    conn: "psycopg.Connection", refinement_id: int,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_REFINEMENT_COLUMNS)} "
            f"FROM skill_refinements WHERE id = %s",
            (refinement_id,),
        )
        row = cur.fetchone()
    return _refinement_row_to_dict(row) if row is not None else None


def apply_refinement(
    conn: "psycopg.Connection", refinement_id: int,
) -> dict[str, Any]:
    """Apply a proposed refinement to its skill row.

    The actual `skills.system_prompt` UPDATE goes through the same
    code path as a direct admin edit (so skills_history captures
    the prior value via the migration 029 trigger). We then flip
    the refinement's status to 'applied' and stamp applied_at, and
    the parent feedback row to 'applied'.
    """
    from api.skills import update_skill

    refinement = get_refinement(conn, refinement_id)
    if refinement is None:
        raise ValueError(f"refinement {refinement_id} not found")
    if refinement["status"] != "proposed":
        raise ValueError(
            f"refinement {refinement_id} is {refinement['status']}, "
            "only 'proposed' refinements can be applied"
        )

    update_skill(
        conn,
        name=refinement["skill_name"],
        fields={"system_prompt": refinement["proposed_prompt"]},
        updated_by="refiner",
    )

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE skill_refinements "
            "SET status = 'applied', applied_at = now() "
            f"WHERE id = %s RETURNING {', '.join(_REFINEMENT_COLUMNS)}",
            (refinement_id,),
        )
        applied_row = cur.fetchone()
        cur.execute(
            "UPDATE estimation_feedback SET status = 'applied' "
            "WHERE refinement_id = %s",
            (refinement_id,),
        )

    if applied_row is None:
        raise RuntimeError("UPDATE ... RETURNING produced no row")
    return _refinement_row_to_dict(applied_row)


def dismiss_refinement(
    conn: "psycopg.Connection", refinement_id: int,
) -> dict[str, Any]:
    """Dismiss a proposed refinement without applying it.

    Flips both the refinement and its parent feedback row to
    'dismissed'. Idempotent: dismissing an already-dismissed
    refinement is a no-op and returns the row.
    """
    refinement = get_refinement(conn, refinement_id)
    if refinement is None:
        raise ValueError(f"refinement {refinement_id} not found")
    if refinement["status"] == "applied":
        raise ValueError(
            "cannot dismiss an already-applied refinement"
        )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE skill_refinements SET status = 'dismissed' "
            f"WHERE id = %s RETURNING {', '.join(_REFINEMENT_COLUMNS)}",
            (refinement_id,),
        )
        dismissed_row = cur.fetchone()
        cur.execute(
            "UPDATE estimation_feedback SET status = 'dismissed' "
            "WHERE refinement_id = %s AND status != 'applied'",
            (refinement_id,),
        )
    if dismissed_row is None:
        raise RuntimeError("UPDATE ... RETURNING produced no row")
    return _refinement_row_to_dict(dismissed_row)


def _refinement_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    out: dict[str, Any] = dict(zip(_REFINEMENT_COLUMNS, row))
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
    return out


def _build_refiner_user_message(
    *,
    feedback: dict[str, Any],
    run: dict[str, Any],
    skill_name: str,
    original_prompt: str,
) -> str:
    """Materialise the refiner's input.

    Includes the original prompt, the operator's feedback text, and
    a compact serialisation of the run's trace (the bounded step
    summaries — the full payloads in `estimation_trace_payloads` are
    NOT included; if the refiner needs more, the bounded summaries
    already cover what each tool produced).
    """
    trace = run.get("trace") or {}
    payload = {
        "skill_name": skill_name,
        "feedback_text": feedback.get("feedback_text"),
        "run": {
            "id": run.get("id"),
            "estimated_monthly_rent_czk": run.get("estimated_monthly_rent_czk"),
            "rent_p25_czk": run.get("rent_p25_czk"),
            "rent_p75_czk": run.get("rent_p75_czk"),
            "confidence": run.get("confidence"),
            "warnings": run.get("warnings"),
            "input_spec": run.get("input_spec"),
            "comparables_used": run.get("comparables_used"),
            "comparables_excluded": run.get("comparables_excluded"),
        },
        "trace_summary": trace.get("summary"),
        "trace_steps": _compact_steps(trace.get("steps") or []),
        "original_prompt": original_prompt,
    }
    return (
        "Operator feedback on one estimation run is below. Read the "
        "original system prompt, the feedback, and the trace, then "
        "call record_skill_refinement exactly once with the full new "
        "prompt (not a diff) and a 2-4 sentence explanation.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _compact_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim trace.steps for the refiner prompt.

    Keep step kind, label/tool, output_summary. Drop the heavy
    `input` block when it's a tool_call — it's redundant with the
    surrounding reasoning step and bloats the prompt.
    """
    out: list[dict[str, Any]] = []
    for s in steps:
        entry: dict[str, Any] = {
            "n": s.get("n"),
            "kind": s.get("kind"),
            "duration_ms": s.get("duration_ms"),
            "output_summary": s.get("output_summary"),
        }
        if s.get("kind") == "tool_call":
            entry["tool"] = s.get("tool")
        elif s.get("kind") == "computation":
            entry["label"] = s.get("label")
        out.append(entry)
    return out
