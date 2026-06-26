"""Operator-editable per-family comparison-tag priorities (Wave 5 Stage 2).

The coded defaults live in `toolkit.dedup_engine` (`default_priority_for_family`); this
module is the DB glue: it reads/writes the operator's reordering from a single
`app_settings.dedup_tag_priorities` JSON value ({family: [tag, ...]}) and exposes a
validated view for the Settings UI. The engine threads the loaded overrides into
`rooms_in_priority`; an absent / partial entry falls back to the coded default
(`normalize_priority`), so a fresh deploy behaves exactly as the hardcoded order until the
operator drags something.
"""

from __future__ import annotations

from typing import Any

from toolkit.dedup_engine import (
    TAG_PRIORITY_FAMILIES,
    default_priority_for_family,
    normalize_priority,
)

SETTING_KEY = "dedup_tag_priorities"


def load_tag_priority_overrides(conn: Any) -> dict[str, list[str]]:
    """The operator's per-family reordering from app_settings, validated to known families
    + tags. Absent / malformed → {} (the engine then uses the coded defaults). Never raises."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (SETTING_KEY,))
        row = cur.fetchone()
    raw = row[0] if row and row[0] is not None else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for fam in TAG_PRIORITY_FAMILIES:
        order = raw.get(fam)
        if isinstance(order, list):
            norm = normalize_priority([t for t in order if isinstance(t, str)],
                                      default_priority_for_family(fam))
            out[fam] = list(norm)
    return out


def priorities_view(conn: Any) -> list[dict[str, Any]]:
    """Per-family view for the Settings UI: the current (normalized) order, the coded
    default order (= the full valid tag set), and whether it's been edited."""
    overrides = load_tag_priority_overrides(conn)
    view: list[dict[str, Any]] = []
    for fam in TAG_PRIORITY_FAMILIES:
        default = list(default_priority_for_family(fam))
        current = overrides.get(fam, default)
        view.append({
            "family": fam,
            "order": current,
            "default_order": default,
            "is_default": current == default,
        })
    return view


def set_family_priority(conn: Any, family: str, order: list[str]) -> list[str]:
    """Validate + persist one family's reordering into the app_settings JSON blob, leaving
    the other families untouched. Returns the normalized stored order. Raises ValueError on
    an unknown family."""
    if family not in TAG_PRIORITY_FAMILIES:
        raise ValueError(f"unknown tag-priority family {family!r}")
    import json

    norm = list(normalize_priority(
        [t for t in order if isinstance(t, str)], default_priority_for_family(family)))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s FOR UPDATE", (SETTING_KEY,))
        row = cur.fetchone()
        blob = dict(row[0]) if row and isinstance(row[0], dict) else {}
        blob[family] = norm
        cur.execute(
            "INSERT INTO app_settings (key, value, description, updated_at, updated_by) "
            "VALUES (%s, %s::jsonb, %s, now(), %s) "
            "ON CONFLICT (key) DO UPDATE "
            "  SET value = excluded.value, updated_at = now(), updated_by = excluded.updated_by",
            (SETTING_KEY, json.dumps(blob),
             "Dedup tag comparison priorities (per family, operator-reordered)", "settings_ui"),
        )
    return norm
