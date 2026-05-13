"""Tests for api.skill_io — SKILL.md round-trip."""

from __future__ import annotations

import io
import zipfile

import pytest

from api.skill_io import parse_skill_file, serialize_skill
from api.skills import Skill, SkillLimits, SkillValidationError


def _sample_skill() -> Skill:
    return Skill(
        name="rental_estimator_v1",
        description="Czech apartment rental estimator.",
        system_prompt="Operating principle 1.\n\nOperating principle 2.",
        allowed_tools=[
            "find_comparables_relaxed",
            "analyze_distribution",
            "record_estimate",
        ],
        preferred_model={
            "anthropic": "claude-sonnet-4-5",
            "gemini": "gemini-2.5-pro",
        },
        limits=SkillLimits(
            max_iterations=12,
            max_cost_usd=1.0,
            wall_clock_timeout_s=120.0,
        ),
    )


def test_serialize_then_parse_roundtrips_fields():
    skill = _sample_skill()
    body = serialize_skill(skill)
    parsed = parse_skill_file(body.encode("utf-8"), filename="SKILL.md")

    assert parsed["name"] == skill.name
    assert parsed["description"] == skill.description
    assert parsed["allowed_tools"] == skill.allowed_tools
    assert parsed["preferred_model"] == skill.preferred_model
    assert parsed["limits"] == {
        "max_iterations": 12,
        "max_cost_usd": 1.0,
        "wall_clock_timeout_s": 120.0,
    }
    assert parsed["system_prompt"] == skill.system_prompt


def test_parse_strips_canonical_scaffolding_header():
    md = (
        "---\n"
        "name: x\n"
        "description: y\n"
        "allowed_tools:\n  - record_estimate\n"
        "preferred_model:\n  anthropic: m\n"
        "limits:\n"
        "  max_iterations: 1\n"
        "  max_cost_usd: 0.1\n"
        "  wall_clock_timeout_s: 10\n"
        "---\n"
        "\n"
        "# x — canonical content\n"
        "\n"
        "intro paragraph that should not end up in the prompt.\n"
        "\n"
        "## System prompt body\n"
        "\n"
        "You are a Czech analyst. This is the real prompt body.\n"
    )
    parsed = parse_skill_file(md.encode("utf-8"), filename="SKILL.md")
    assert parsed["system_prompt"].startswith("You are a Czech analyst.")
    assert "intro paragraph" not in parsed["system_prompt"]


def test_parse_canonical_skill_md_yields_prompt_body():
    from pathlib import Path
    path = Path("skills/rental_estimator_v1/SKILL.md")
    content = path.read_bytes()
    parsed = parse_skill_file(content, filename=str(path))
    assert parsed["name"] == "rental_estimator_v1"
    assert "STOP WITH `record_estimate`" in parsed["system_prompt"]
    assert "canonical content" not in parsed["system_prompt"]


def test_parse_zip_with_skill_md_returns_same_shape():
    skill = _sample_skill()
    body = serialize_skill(skill)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("rental_estimator_v1/SKILL.md", body)
    parsed = parse_skill_file(buf.getvalue(), filename="bundle.zip")
    assert parsed["name"] == skill.name
    assert parsed["allowed_tools"] == skill.allowed_tools


def test_parse_zip_with_multiple_skill_md_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/SKILL.md", "---\nname: a\n---\nbody")
        zf.writestr("b/SKILL.md", "---\nname: b\n---\nbody")
    with pytest.raises(SkillValidationError, match="SKILL.md entries"):
        parse_skill_file(buf.getvalue(), filename="bundle.zip")


def test_parse_zip_without_skill_md_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(SkillValidationError, match="does not contain"):
        parse_skill_file(buf.getvalue(), filename="bundle.zip")


def test_parse_rejects_missing_frontmatter():
    with pytest.raises(SkillValidationError, match="frontmatter fence"):
        parse_skill_file(b"no frontmatter here", filename="SKILL.md")


def test_parse_rejects_unclosed_frontmatter():
    with pytest.raises(SkillValidationError, match="closing"):
        parse_skill_file(
            b"---\nname: x\ndescription: y\n", filename="SKILL.md",
        )


def test_parse_rejects_missing_required_field():
    md = (
        "---\n"
        "name: x\n"
        "allowed_tools:\n  - record_estimate\n"
        "preferred_model:\n  anthropic: m\n"
        "limits:\n"
        "  max_iterations: 1\n"
        "  max_cost_usd: 0.1\n"
        "  wall_clock_timeout_s: 10\n"
        "---\nbody"
    )
    with pytest.raises(SkillValidationError, match="description"):
        parse_skill_file(md.encode("utf-8"), filename="SKILL.md")
