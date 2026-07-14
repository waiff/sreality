"""Operator-editable per-family vision-model routing for the dedup engine's forensic lanes.

Mirrors `toolkit.dedup_priorities`'s DB glue exactly: a single `app_settings` JSON value
({family: model_id}) that overrides the lane's flat `llm_*_match_model` default for one
family at a time. Built because the site-plan (development-guard) lane routes ALL families
through one model — Session 5b's bake-off (dedup_vision_bakeoff_results, migration 303)
found the live default (gpt-5-mini) scores only 50% correct / 50% dangerous on pozemek
site-plans vs 92.9% for claude-sonnet-4-5, but byt/dum/komercni are already acceptable on
the cheap model, so a flat model bump would regress their cost with no accuracy need.

An absent / partial entry falls back to the lane's flat default (`resolve_model(key)`), so
a fresh deploy — and every family not explicitly overridden — behaves exactly as before.
Provider-agnostic by construction: a value is any model id `LLMClient.call` accepts
(`provider_for_model` derives the backend), never a hardcoded provider branch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from toolkit.dedup_engine import TAG_PRIORITY_FAMILIES

if TYPE_CHECKING:
    from api.llm_client import LLMClient

SITE_PLAN_OVERRIDE_KEY = "llm_site_plan_match_model_by_family"


def load_model_overrides(conn: Any, setting_key: str) -> dict[str, str]:
    """The operator's per-family model routing from app_settings, validated to known
    families with a non-empty string model id. Absent / malformed -> {}. Never raises."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (setting_key,))
        row = cur.fetchone()
    raw = row[0] if row and row[0] is not None else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for fam in TAG_PRIORITY_FAMILIES:
        model = raw.get(fam)
        if isinstance(model, str) and model.strip():
            out[fam] = model.strip()
    return out


def resolve_model_for_family(
    conn: Any,
    llm_client: "LLMClient",
    *,
    setting_key: str,
    default_key: str,
    family: str | None,
    overrides: dict[str, str] | None = None,
) -> str:
    """The model to use for `family` on the lane keyed by `default_key`: the per-family
    override if one is set, else the lane's flat default. Pass a pre-loaded `overrides`
    dict when resolving many pairs in one run to avoid re-querying app_settings per pair."""
    table = overrides if overrides is not None else load_model_overrides(conn, setting_key)
    if family and family in table:
        return table[family]
    return llm_client.resolve_model(default_key)


def site_plan_model_overrides_view(conn: Any, llm_client: "LLMClient") -> list[dict[str, Any]]:
    """Per-family view: the resolved model (override or flat default) and whether it's
    been overridden. Read-only convenience for an operator inspecting current routing."""
    default = llm_client.resolve_model("llm_site_plan_match_model")
    overrides = load_model_overrides(conn, SITE_PLAN_OVERRIDE_KEY)
    return [
        {
            "family": fam,
            "model": overrides.get(fam, default),
            "default_model": default,
            "is_override": fam in overrides,
        }
        for fam in TAG_PRIORITY_FAMILIES
    ]


def set_family_site_plan_model(conn: Any, family: str, model: str | None) -> dict[str, str]:
    """Validate + persist one family's site-plan model override, leaving the others
    untouched. `model=None` clears the override (family falls back to the flat default).
    Returns the stored overrides dict. Raises ValueError on an unknown family."""
    if family not in TAG_PRIORITY_FAMILIES:
        raise ValueError(f"unknown family {family!r}")
    import json

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s FOR UPDATE",
            (SITE_PLAN_OVERRIDE_KEY,),
        )
        row = cur.fetchone()
        blob = dict(row[0]) if row and isinstance(row[0], dict) else {}
        if model is None:
            blob.pop(family, None)
        else:
            v = model.strip()
            if not v:
                raise ValueError("model name cannot be empty")
            blob[family] = v
        cur.execute(
            "INSERT INTO app_settings (key, value, description, updated_at, updated_by) "
            "VALUES (%s, %s::jsonb, %s, now(), %s) "
            "ON CONFLICT (key) DO UPDATE "
            "  SET value = excluded.value, updated_at = now(), updated_by = excluded.updated_by",
            (SITE_PLAN_OVERRIDE_KEY, json.dumps(blob),
             "Site-plan development-guard model, per-family override", "settings_ui"),
        )
    return blob
