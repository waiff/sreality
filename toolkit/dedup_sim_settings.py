"""Operator-tunable knob registry for the NEW DEDUP simulation engine.

Registry entries (this file) are the source of truth for what settings
exist, their type/category/constraints, and their plain-language
explanation — the design's non-negotiable "every settings-panel knob
carries a plain-language blurb" (docs/design/new-dedup/PROGRAM.md).
`dedup_sim.settings` (migration 372) stores operator OVERRIDES only; a
missing row means "use the registry default," the same split
`toolkit/filter_registry.py` + `filter_visibility` (migration 059)
already uses in this codebase. Adding a setting a later wave needs is a
registry-only PR — no new migration, since the override table doesn't
need a row until someone actually changes the value.

Default values and `decided` flags come from PROGRAM.md's 2026-08-05 Q&A
decisions ledger — never invented here (the operator owns every
threshold/weight/rule per the design's non-negotiables).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg


class Category(StrEnum):
    GENERAL = "general"
    L0_CANDIDATES = "l0_candidates"
    L1_EXACT_ATTRS = "l1_exact_attrs"
    L2_PHASH = "l2_phash"
    L3_EMBEDDINGS = "l3_embeddings"
    L4_VISION = "l4_vision"


class ValueType(StrEnum):
    INTEGER = "integer"
    NUMERIC = "numeric"
    BOOLEAN = "boolean"
    TEXT = "text"


@dataclass(frozen=True)
class SettingDef:
    """One tunable knob, fully described.

    `decided` distinguishes an operator-confirmed value (PROGRAM.md's
    Q&A ledger) from a placeholder default that a later wave's gate
    still has to calibrate against a real sample — the Settings UI
    surfaces this so "waterfall vs first-shared-family" doesn't read
    the same as "not yet measured."
    """
    key: str
    category: Category
    value_type: ValueType
    default: Any
    explanation: str
    decided: bool = True
    enum_choices: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None


REGISTRY: dict[str, SettingDef] = {
    d.key: d
    for d in [
        SettingDef(
            key="l0_geo_radius_m",
            category=Category.L0_CANDIDATES,
            value_type=ValueType.NUMERIC,
            default=75,
            explanation=(
                "How close two listings' coordinates need to be, in meters, "
                "to become a geo-based candidate pair. 75m covers GPS and "
                "geocoding noise for the same building without pulling in "
                "the next lot."
            ),
        ),
        SettingDef(
            key="l0_floor_tolerance",
            category=Category.L0_CANDIDATES,
            value_type=ValueType.INTEGER,
            default=2,
            explanation=(
                "For apartments (byt) only: how many floors apart two "
                "listings can be and still count as a candidate pair. Floor "
                "numbers are self-reported and often off by one or two, so "
                "an exact match would miss real duplicates."
            ),
        ),
        SettingDef(
            key="l0_area_tolerance_pct_general",
            category=Category.L0_CANDIDATES,
            value_type=ValueType.NUMERIC,
            default=5,
            minimum=0,
            explanation=(
                "How far apart two listings' usable area can be, as a "
                "percent of the larger one, and still count as a candidate "
                "pair. Applies to every property type except pozemek (land)."
            ),
        ),
        SettingDef(
            key="l0_area_tolerance_pct_pozemek",
            category=Category.L0_CANDIDATES,
            value_type=ValueType.NUMERIC,
            default=2,
            minimum=0,
            explanation=(
                "Same as the general area tolerance, but tighter for "
                "pozemek (land): plot sizes are surveyed more precisely "
                "than living area, so a wider gap is more likely a "
                "genuinely different lot."
            ),
        ),
        SettingDef(
            key="l1_exact_attrs_enabled",
            category=Category.L1_EXACT_ATTRS,
            value_type=ValueType.BOOLEAN,
            default=False,
            decided=False,
            explanation=(
                "Whether the exact-attributes level (matching on precise "
                "structured fields) is active. Ships off — it's only "
                "calibrated once the rest of the stack has produced a real "
                "sample to check it against (Wave 7)."
            ),
        ),
        SettingDef(
            key="l2_phash_hamming_threshold",
            category=Category.L2_PHASH,
            value_type=ValueType.INTEGER,
            default=11,
            minimum=0,
            maximum=64,
            explanation=(
                "How different two images' perceptual hashes (pHash) can be "
                "and still count as a visual match — lower is stricter. 11 "
                "is the global starting point; some image tags (e.g. floor "
                "plans) may need a tighter per-tag override once Wave 4 "
                "calibrates against real pairs."
            ),
        ),
        SettingDef(
            key="l2_phash_family_semantics",
            category=Category.L2_PHASH,
            value_type=ValueType.TEXT,
            default="waterfall",
            enum_choices=("waterfall", "first_shared_family"),
            explanation=(
                "How pHash evidence across different room/image tags "
                "combines into one verdict for a pair. 'Waterfall' checks "
                "tag families in priority order and stops at the first one "
                "with enough qualifying pairs; 'first shared family' "
                "instead requires the two listings to share any one tag "
                "family at all before comparing. Waterfall is the default."
            ),
        ),
        SettingDef(
            key="l3_embeddings_similarity_threshold",
            category=Category.L3_EMBEDDINGS,
            value_type=ValueType.NUMERIC,
            default=0.98,
            minimum=0.0,
            maximum=1.0,
            decided=False,
            explanation=(
                "Minimum DINOv2 cosine similarity between two images to "
                "count as a visual match. 0.98 is the starting point from "
                "the design Q&A; expect this to move once Wave 5 "
                "calibrates it against real embedding evidence."
            ),
        ),
        SettingDef(
            key="l3_embeddings_family_semantics",
            category=Category.L3_EMBEDDINGS,
            value_type=ValueType.TEXT,
            default="waterfall",
            enum_choices=("waterfall", "first_shared_family"),
            explanation=(
                "Same choice as the pHash family-semantics toggle above, "
                "applied to embedding evidence instead — set independently "
                "because the two signals can disagree on which tag family "
                "is most reliable."
            ),
        ),
        SettingDef(
            key="l3_runpod_daily_cost_cap_usd",
            category=Category.L3_EMBEDDINGS,
            value_type=ValueType.NUMERIC,
            default=1.00,
            minimum=0.0,
            explanation=(
                "Soft daily spend cap for the RunPod GPU pods that compute "
                "DINOv2 embeddings. Pods are serverless and on-demand, "
                "spinning up only for a candidate-scoped batch, so this is "
                "a sanity check on run-rate, not a hard limit RunPod itself "
                "enforces."
            ),
        ),
        SettingDef(
            key="l4_vision_model",
            category=Category.L4_VISION,
            value_type=ValueType.TEXT,
            default="gpt-5-mini",
            explanation=(
                "Which vision model reviews the hardest pairs pHash and "
                "embeddings can't resolve. GPT-5-mini today; qwen is "
                "planned as a pluggable alternative once Wave 6 needs a "
                "second provider."
            ),
        ),
        SettingDef(
            key="l4_vision_batch_mode",
            category=Category.L4_VISION,
            value_type=ValueType.TEXT,
            default="manual",
            enum_choices=("manual",),
            explanation=(
                "Vision review only ever runs in manually-triggered "
                "batches, never an automatic background sweep, to keep "
                "spend visible and bounded. The only valid value today is "
                "'manual' — this is a setting rather than a hardcoded "
                "constant so turning on scheduled batches later is an "
                "operator decision, not a silent code change."
            ),
        ),
    ]
}


def _overrides(conn: "psycopg.Connection | None") -> dict[str, Any]:
    """Read `dedup_sim.settings`. Empty dict if `conn` is None or the
    table doesn't exist yet (e.g. a branch that predates migration 372)."""
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM dedup_sim.settings")
            rows = cur.fetchall()
    except Exception:
        return {}
    return {r[0]: r[1] for r in rows}


def effective_value(key: str, conn: "psycopg.Connection | None" = None) -> Any:
    """The setting's current value: an operator override if one exists,
    else the registry default. Raises KeyError for an unknown key."""
    default = REGISTRY[key].default
    return _overrides(conn).get(key, default)


def effective_settings(conn: "psycopg.Connection | None" = None) -> dict[str, Any]:
    """{key: effective_value} for every registered setting."""
    overrides = _overrides(conn)
    return {k: overrides.get(k, d.default) for k, d in REGISTRY.items()}


def list_with_metadata(conn: "psycopg.Connection | None" = None) -> list[dict[str, Any]]:
    """Full registry + effective values: one dict per setting with its
    category, type, constraints, blurb, and current value (default or
    override) — the shape a Settings page renders directly."""
    overrides = _overrides(conn)
    return [
        {
            "key": d.key,
            "category": str(d.category),
            "value_type": str(d.value_type),
            "value": overrides.get(d.key, d.default),
            "default": d.default,
            "is_override": d.key in overrides,
            "decided": d.decided,
            "explanation": d.explanation,
            "enum_choices": list(d.enum_choices) if d.enum_choices else None,
            "minimum": d.minimum,
            "maximum": d.maximum,
        }
        for d in REGISTRY.values()
    ]


class SettingValidationError(ValueError):
    pass


def _validate(d: SettingDef, value: Any) -> None:
    if d.value_type is ValueType.BOOLEAN:
        if not isinstance(value, bool):
            raise SettingValidationError(f"{d.key} expects a boolean")
        return
    if d.value_type in (ValueType.INTEGER, ValueType.NUMERIC):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingValidationError(f"{d.key} expects a number")
        if d.value_type is ValueType.INTEGER and value != int(value):
            raise SettingValidationError(f"{d.key} expects an integer")
        if d.minimum is not None and value < d.minimum:
            raise SettingValidationError(f"{d.key} must be >= {d.minimum}")
        if d.maximum is not None and value > d.maximum:
            raise SettingValidationError(f"{d.key} must be <= {d.maximum}")
        return
    if d.value_type is ValueType.TEXT:
        if not isinstance(value, str):
            raise SettingValidationError(f"{d.key} expects text")
        if d.enum_choices is not None and value not in d.enum_choices:
            raise SettingValidationError(f"{d.key} must be one of {d.enum_choices}")


def update_setting(
    conn: "psycopg.Connection",
    key: str,
    value: Any,
    updated_by: str,
) -> None:
    """Validate and upsert an operator override. Raises KeyError for an
    unknown key, SettingValidationError for a value that fails its
    registered type/range/enum check."""
    d = REGISTRY[key]
    _validate(d, value)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dedup_sim.settings (key, value, updated_by) "
            "VALUES (%s, %s::jsonb, %s) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = excluded.value, updated_at = now(), updated_by = excluded.updated_by",
            (key, json.dumps(value), updated_by),
        )


def reset_setting(conn: "psycopg.Connection", key: str) -> None:
    """Delete an operator override, reverting the setting to its
    registry default. Raises KeyError for an unknown key."""
    if key not in REGISTRY:
        raise KeyError(key)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM dedup_sim.settings WHERE key = %s", (key,))
