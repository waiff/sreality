"""Skill loading + updating for the reasoning agent.

A `Skill` is one row in the `skills` table (migration 029). The
canonical content lives in `skills/<name>/SKILL.md` in git; the
migration's seed `INSERT` imports it once. Day-to-day edits happen
through `update_skill` (Settings page → PUT /admin/skills/{name}),
and the `skills_history` trigger preserves every prior version.

Validation in `update_skill` rejects:
- tool names outside the global registry (`AGENT_TOOL_NAMES`),
- preferred_model missing one of the registered provider names,
- limits outside sane bounds.

The validation is deliberately narrow — we want operators to be
able to experiment, but not to break the agent loop by typo'ing a
tool name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg


class SkillNotFound(LookupError):
    """Raised by load_skill when the named row does not exist."""


class SkillValidationError(ValueError):
    """Raised by update_skill on invalid fields."""


# Set in api/main.py once the agent tool registry is built. Skill
# validation rejects allowed_tools entries outside this set.
AGENT_TOOL_NAMES: set[str] = set()

# Set in api/main.py once providers are constructed. Skill
# validation rejects preferred_model maps missing any of these.
PROVIDER_NAMES: set[str] = set()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str]
    preferred_model: dict[str, str]
    limits: "SkillLimits"
    updated_at: str | None = None
    archived_at: str | None = None


@dataclass(frozen=True)
class SkillLimits:
    max_iterations: int
    max_cost_usd: float
    wall_clock_timeout_s: float


def load_skill(conn: "psycopg.Connection", name: str) -> Skill:
    """Load one skill by name. Archived skills load fine — past
    estimations referencing them must stay readable. The
    new-estimation flow gates archived skills out at the schema /
    list-skills boundary instead."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, description, system_prompt, allowed_tools, "
            "preferred_model, limits, updated_at, archived_at "
            "FROM skills WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
    if row is None:
        raise SkillNotFound(f"skill {name!r} not found")
    return _row_to_skill(row)


def list_skills(
    conn: "psycopg.Connection", *, include_archived: bool = False,
) -> list[Skill]:
    """List skills. Archived skills are hidden by default — the
    operator's Settings page and any future picker should pass
    `include_archived=True` only when they explicitly want the full
    history."""
    base = (
        "SELECT name, description, system_prompt, allowed_tools, "
        "preferred_model, limits, updated_at, archived_at "
        "FROM skills"
    )
    where = "" if include_archived else " WHERE archived_at IS NULL"
    with conn.cursor() as cur:
        cur.execute(base + where + " ORDER BY name")
        rows = cur.fetchall()
    return [_row_to_skill(r) for r in rows]


def update_skill(
    conn: "psycopg.Connection",
    name: str,
    fields: dict[str, Any],
    *,
    updated_by: str | None = None,
) -> Skill:
    """Partial-update a skill row. Raises SkillNotFound if missing."""
    if not fields:
        return load_skill(conn, name)

    sets: list[str] = []
    params: dict[str, Any] = {"name": name}

    if "description" in fields:
        sets.append("description = %(description)s")
        params["description"] = _validate_str(fields["description"], "description")
    if "system_prompt" in fields:
        sets.append("system_prompt = %(system_prompt)s")
        params["system_prompt"] = _validate_str(
            fields["system_prompt"], "system_prompt"
        )
    if "allowed_tools" in fields:
        tools = _validate_allowed_tools(fields["allowed_tools"])
        sets.append("allowed_tools = %(allowed_tools)s::jsonb")
        params["allowed_tools"] = _jsonb_dumps(tools)
    if "preferred_model" in fields:
        model_map = _validate_preferred_model(fields["preferred_model"])
        sets.append("preferred_model = %(preferred_model)s::jsonb")
        params["preferred_model"] = _jsonb_dumps(model_map)
    if "limits" in fields:
        limits = _validate_limits(fields["limits"])
        sets.append("limits = %(limits)s::jsonb")
        params["limits"] = _jsonb_dumps(limits)
    if updated_by is not None:
        sets.append("updated_by = %(updated_by)s")
        params["updated_by"] = updated_by

    if not sets:
        return load_skill(conn, name)

    sql = (
        f"UPDATE skills SET {', '.join(sets)} "
        f"WHERE name = %(name)s RETURNING name"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None:
        raise SkillNotFound(f"skill {name!r} not found")
    return load_skill(conn, name)


# --- helpers --------------------------------------------------------------

def _row_to_skill(row: tuple[Any, ...]) -> Skill:
    (
        name, description, system_prompt, allowed_tools,
        preferred_model, limits, updated_at, archived_at,
    ) = row
    return Skill(
        name=name,
        description=description,
        system_prompt=system_prompt,
        allowed_tools=list(allowed_tools or []),
        preferred_model=dict(preferred_model or {}),
        limits=SkillLimits(
            max_iterations=int(limits.get("max_iterations", 12)),
            max_cost_usd=float(limits.get("max_cost_usd", 1.0)),
            wall_clock_timeout_s=float(limits.get("wall_clock_timeout_s", 120.0)),
        ),
        updated_at=str(updated_at) if updated_at is not None else None,
        archived_at=str(archived_at) if archived_at is not None else None,
    )


def _validate_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillValidationError(f"{label} must be a non-empty string")
    return value


def _validate_allowed_tools(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SkillValidationError(
            "allowed_tools must be a non-empty list of tool names"
        )
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise SkillValidationError(
                "allowed_tools entries must be strings"
            )
        if AGENT_TOOL_NAMES and entry not in AGENT_TOOL_NAMES:
            raise SkillValidationError(
                f"unknown tool {entry!r}; "
                f"registered tools: {sorted(AGENT_TOOL_NAMES)}"
            )
        out.append(entry)
    return out


def _validate_preferred_model(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise SkillValidationError(
            "preferred_model must be a {provider: model} map"
        )
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise SkillValidationError(
                "preferred_model entries must be string -> string"
            )
        if PROVIDER_NAMES and k not in PROVIDER_NAMES:
            raise SkillValidationError(
                f"unknown provider {k!r}; "
                f"registered providers: {sorted(PROVIDER_NAMES)}"
            )
        out[k] = v
    if PROVIDER_NAMES and not set(out.keys()) >= PROVIDER_NAMES:
        missing = PROVIDER_NAMES - set(out.keys())
        raise SkillValidationError(
            f"preferred_model is missing entries for {sorted(missing)}"
        )
    return out


def _validate_limits(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillValidationError("limits must be an object")
    max_iter = value.get("max_iterations")
    if not isinstance(max_iter, int) or not (1 <= max_iter <= 50):
        raise SkillValidationError(
            "limits.max_iterations must be an int in [1, 50]"
        )
    max_cost = value.get("max_cost_usd")
    if not isinstance(max_cost, (int, float)) or not (0 < max_cost <= 20):
        raise SkillValidationError(
            "limits.max_cost_usd must be a number in (0, 20]"
        )
    wall = value.get("wall_clock_timeout_s")
    if not isinstance(wall, (int, float)) or not (1 <= wall <= 600):
        raise SkillValidationError(
            "limits.wall_clock_timeout_s must be a number in [1, 600]"
        )
    return {
        "max_iterations": max_iter,
        "max_cost_usd": float(max_cost),
        "wall_clock_timeout_s": float(wall),
    }


def _jsonb_dumps(value: Any) -> str:
    import json
    return json.dumps(value)
