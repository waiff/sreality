"""Skill <-> SKILL.md round-trip serialisation.

Mirrors the Anthropic agent-SDK skill folder convention: a directory
named after the skill containing `SKILL.md` with a YAML frontmatter
block (`---` ... `---`) followed by the system prompt body. Today's
repo only exercises the SKILL.md file; the folder-with-references
shape is supported on import (zip with `<name>/SKILL.md` inside) but
not produced on export.

stdlib only — the schema is fixed and tiny so we hand-roll the YAML
emit / parse rather than pulling in PyYAML. Keep this module small
and well-typed; it backs `POST /admin/skills/import`.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from api.skills import Skill, SkillLimits, SkillValidationError


_FRONTMATTER_FENCE = "---"


def serialize_skill(skill: Skill) -> str:
    """Render a Skill as a SKILL.md document (frontmatter + body)."""
    lines: list[str] = [_FRONTMATTER_FENCE]
    lines.append(f"name: {skill.name}")
    lines.append(f"description: {_scalar(skill.description)}")
    lines.append("allowed_tools:")
    for t in skill.allowed_tools:
        lines.append(f"  - {t}")
    lines.append("preferred_model:")
    for provider, model in skill.preferred_model.items():
        lines.append(f"  {provider}: {model}")
    lines.append("limits:")
    lines.append(f"  max_iterations: {skill.limits.max_iterations}")
    lines.append(f"  max_cost_usd: {_format_number(skill.limits.max_cost_usd)}")
    lines.append(
        f"  wall_clock_timeout_s: {_format_number(skill.limits.wall_clock_timeout_s)}"
    )
    lines.append(_FRONTMATTER_FENCE)
    lines.append("")
    lines.append(skill.system_prompt.rstrip())
    lines.append("")
    return "\n".join(lines)


def parse_skill_file(content: bytes, *, filename: str) -> dict[str, Any]:
    """Parse a SKILL.md (or zip containing one) into an update-shape dict.

    Returns `{name, description, system_prompt, allowed_tools,
    preferred_model, limits}` — the same shape `update_skill` and
    `insert_skill` accept. Raises `SkillValidationError` on any
    structural problem (missing frontmatter, multiple SKILL.md in zip,
    unknown nested key, etc.).
    """
    if filename.lower().endswith(".zip"):
        text = _extract_md_from_zip(content)
    else:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillValidationError(f"SKILL.md is not UTF-8: {exc}") from exc

    frontmatter, body = _split_frontmatter(text)
    parsed = _parse_frontmatter(frontmatter)

    name = parsed.get("name")
    description = parsed.get("description")
    allowed_tools = parsed.get("allowed_tools")
    preferred_model = parsed.get("preferred_model")
    limits = parsed.get("limits")

    if not isinstance(name, str) or not name.strip():
        raise SkillValidationError("frontmatter is missing a non-empty 'name'")
    if not isinstance(description, str):
        raise SkillValidationError("frontmatter is missing 'description'")
    if not isinstance(allowed_tools, list) or not allowed_tools:
        raise SkillValidationError(
            "frontmatter 'allowed_tools' must be a non-empty list"
        )
    if not isinstance(preferred_model, dict) or not preferred_model:
        raise SkillValidationError(
            "frontmatter 'preferred_model' must be a non-empty map"
        )
    if not isinstance(limits, dict):
        raise SkillValidationError("frontmatter 'limits' must be an object")

    return {
        "name": name.strip(),
        "description": description.strip(),
        "system_prompt": body.strip(),
        "allowed_tools": [str(t).strip() for t in allowed_tools],
        "preferred_model": {str(k): str(v) for k, v in preferred_model.items()},
        "limits": _coerce_limits(limits),
    }


# --- helpers --------------------------------------------------------------

def _scalar(value: str) -> str:
    """Emit a scalar value safely. Quote if it contains a YAML control char."""
    needs_quotes = any(c in value for c in (":", "#", "\n"))
    if not needs_quotes:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_number(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    if value == int(value):
        return f"{value:.2f}"
    return f"{value:g}"


def _extract_md_from_zip(content: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise SkillValidationError(f"upload is not a valid zip: {exc}") from exc
    md_names = [
        n for n in zf.namelist()
        if n.lower().endswith("skill.md") and not n.endswith("/")
    ]
    if not md_names:
        raise SkillValidationError(
            "zip does not contain a SKILL.md entry"
        )
    if len(md_names) > 1:
        raise SkillValidationError(
            f"zip contains {len(md_names)} SKILL.md entries; expected exactly one "
            f"({md_names})"
        )
    raw = zf.read(md_names[0])
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillValidationError(f"SKILL.md in zip is not UTF-8: {exc}") from exc


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_text, body_text). Frontmatter is between two `---`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        raise SkillValidationError(
            "SKILL.md must start with a '---' frontmatter fence"
        )
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_FENCE:
            closing = i
            break
    if closing is None:
        raise SkillValidationError(
            "SKILL.md frontmatter is missing its closing '---' fence"
        )
    frontmatter = "\n".join(lines[1:closing])
    # The canonical SKILL.md includes a "## System prompt body" header
    # before the actual prompt — strip it (and any other lines up to
    # and including the first blank-separated heading) so the stored
    # prompt is just the operating instructions, mirroring the
    # migration 029 seed.
    body_lines = lines[closing + 1:]
    body = _extract_prompt_body("\n".join(body_lines))
    return frontmatter, body


_PROMPT_MARKER = "## System prompt body"


def _extract_prompt_body(after_frontmatter: str) -> str:
    """Pull the prompt body out of the canonical SKILL.md scaffolding.

    If the file uses the repo's canonical shape (intro paragraphs +
    `## System prompt body` heading + actual body), return everything
    after that heading. Otherwise return the full text after the
    frontmatter, trimmed.
    """
    if _PROMPT_MARKER in after_frontmatter:
        _, _, tail = after_frontmatter.partition(_PROMPT_MARKER)
        return tail.lstrip("\n").strip()
    return after_frontmatter.strip()


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Minimal YAML-ish parser tuned to the SKILL.md schema.

    Supports: top-level scalars (`key: value`), top-level lists
    (`key:` followed by `  - item` lines), and nested maps
    (`key:` followed by `  subkey: value` lines, one level deep).
    Rejects anything outside this shape so a malformed file errors
    loudly instead of silently misparsing.
    """
    result: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line.startswith(" "):
            raise SkillValidationError(
                f"unexpected indentation at top level: {line!r}"
            )
        key, sep, value = line.partition(":")
        if not sep:
            raise SkillValidationError(f"expected 'key: value' on line: {line!r}")
        key = key.strip()
        value = value.strip()
        if value:
            result[key] = _scalar_value(value)
            i += 1
            continue
        # Block scalar — peek at the next non-empty line to determine list vs map.
        block, next_i = _consume_block(lines, i + 1)
        result[key] = block
        i = next_i
    return result


def _consume_block(lines: list[str], start: int) -> tuple[Any, int]:
    """Consume an indented block starting at `start`. Return (value, next_i)."""
    items: list[Any] | None = None
    mapping: dict[str, Any] | None = None
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if not line.startswith("  "):
            break
        stripped = line[2:]
        if stripped.startswith("- "):
            if mapping is not None:
                raise SkillValidationError(
                    f"mixed map/list in block at line {i}: {line!r}"
                )
            items = items or []
            items.append(_scalar_value(stripped[2:].strip()))
        else:
            if items is not None:
                raise SkillValidationError(
                    f"mixed list/map in block at line {i}: {line!r}"
                )
            sub_key, sep, sub_val = stripped.partition(":")
            if not sep:
                raise SkillValidationError(
                    f"expected 'key: value' in block at line {i}: {line!r}"
                )
            mapping = mapping or {}
            mapping[sub_key.strip()] = _scalar_value(sub_val.strip())
        i += 1
    if items is None and mapping is None:
        raise SkillValidationError("empty block after key")
    return (items if items is not None else mapping), i


def _scalar_value(raw: str) -> Any:
    """Coerce a scalar literal: quoted string, int, float, or bare string."""
    if not raw:
        return ""
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _coerce_limits(value: dict[str, Any]) -> dict[str, Any]:
    """Coerce limit fields into the {int, float, float} shape update_skill wants."""
    try:
        return {
            "max_iterations": int(value["max_iterations"]),
            "max_cost_usd": float(value["max_cost_usd"]),
            "wall_clock_timeout_s": float(value["wall_clock_timeout_s"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SkillValidationError(
            "limits requires int max_iterations, number max_cost_usd, "
            "number wall_clock_timeout_s"
        ) from exc


__all__ = [
    "SkillLimits",  # re-export for convenience of callers
    "parse_skill_file",
    "serialize_skill",
]
