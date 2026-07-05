"""Registry of operator-tunable dedup-engine settings — ONE source of truth.

Every dedup knob (toggle / threshold / model) is declared here once: key, type,
default, range, label, group, help. The engine reads its defaults from this
registry (so there is no second copy of a default), and the API + Settings UI
render + validate + edit them. Values live in app_settings (created on first
edit); an absent key reads its registry default, so a fresh deploy behaves
exactly as the coded defaults until the operator changes something.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DedupSetting:
    key: str
    kind: str            # 'bool' | 'float' | 'model'
    default: Any
    label: str
    group: str
    help: str = ""
    min: float | None = None
    max: float | None = None


REGISTRY: tuple[DedupSetting, ...] = (
    # --- Engine ---
    DedupSetting(
        "dedup_auto_merge_enabled", "bool", True,
        "Auto-merge enabled", "Engine",
        "Master switch. When off, the engine still finds candidates but queues "
        "every one for manual review instead of merging — and spends no vision.",
    ),
    DedupSetting(
        "dedup_forensics_autodismiss_enabled", "bool", True,
        "Auto-dismiss confident 'different'", "Engine",
        "When the forensic compare confidently says 'different property' (a "
        "distinctive room — kitchen/bathroom — is Low and no room matched), "
        "close the pair out instead of queuing it. Calibrated safe (0/273 "
        "operator-merged pairs carried a Low).",
    ),
    DedupSetting(
        "dedup_floor_plan_budget", "float", 10000,
        "Floor-plan checks per free run", "Engine",
        "Cap on inline Sonnet floor-plan validations on a scheduled (free) run — "
        "the one paid call there, fired only on pairs the engine WOULD merge. Beyond "
        "the cap, both-plan pairs defer to the next run. 0 = consume only warmed "
        "verdicts ($0).",
        0, 100000,
    ),
    DedupSetting(
        "dedup_floor_plan_inconclusive_to_review", "bool", True,
        "Floor-plan 'inconclusive' → review", "Engine",
        "When the floor-plan gate returns 'inconclusive', send the pair to the manual "
        "review queue instead of letting the merge proceed. Off = treat inconclusive "
        "as 'same layout' and merge.",
    ),
    DedupSetting(
        "dedup_candidate_redecide_hours", "float", 24,
        "Candidate re-decide backoff (hours)", "Engine",
        "How long the candidate drain leaves an already-evaluated proposed pair alone "
        "before re-deciding it. New CLIP-tagged photo evidence re-opens a pair "
        "immediately regardless of the backoff. Stops the drain from re-chewing the "
        "same inconclusive pairs every 2 hours.",
        1, 720,
    ),
    DedupSetting(
        "dedup_batch_warmer_enabled", "bool", False,
        "Batch vision warmer", "Engine",
        "Pre-warm the vision caches via Anthropic's Message Batches API (50% cheaper, "
        "async) so the engine merges over warm cache for free. Off = pay cold vision "
        "inline. The dedup_batches workflow no-ops while this is off.",
    ),
    # --- CLIP (free tagging + the cosine recall tier) ---
    DedupSetting(
        "dedup_prefer_clip_tags", "bool", False,
        "Use free CLIP room tags", "CLIP",
        "Source like-room pairing from the free self-hosted CLIP tagger instead "
        "of the paid LLM classify. Drops the per-listing tagging cost to zero and "
        "is the FIRST tagger for houses/land/commercial (unblocking their dedup). "
        "Falls back to the LLM where CLIP hasn't tagged a listing yet.",
    ),
    DedupSetting(
        "dedup_clip_cosine_enabled", "bool", False,
        "CLIP cosine recall tier", "CLIP",
        "Route each room's forensic compare by the same-room CLIP cosine — high "
        "→ cheap Haiku, uncertain → Sonnet, too-low → skip that room (never a "
        "dismiss; the pair still queues). Needs embeddings backfilled.",
    ),
    DedupSetting(
        "dedup_cosine_haiku_min", "float", 0.90,
        "Cosine: Haiku band floor", "CLIP",
        "A same-room cosine at or above this routes the compare to Haiku "
        "(near-certain, cheap). Trial: same-property same-tag median ≈ 0.90.",
        0.0, 1.0,
    ),
    DedupSetting(
        "dedup_cosine_sonnet_min", "float", 0.70,
        "Cosine: Sonnet band floor", "CLIP",
        "A cosine in [this, Haiku floor) routes to Sonnet; below this the room is "
        "skipped (not dismissed). Trial: same-property same-tag p25 ≈ 0.81, so "
        "0.70 rarely skips a true match.",
        0.0, 1.0,
    ),
    DedupSetting(
        "dedup_render_exclude_min", "float", 0.95,
        "Render-score exclusion floor (byt)", "CLIP",
        "For apartments, a photo whose CLIP render-score is at/above this is treated "
        "as a shared development RENDER and dropped from the pHash count + the forensic "
        "compare (a development reuses renders across distinct units). Higher = only the "
        "most certain renders are excluded (fewer real photos wrongly dropped).",
        0.0, 1.0,
    ),
    # --- Geo (single-dwelling: house / land / commercial) ---
    DedupSetting(
        "dedup_geo_enabled", "bool", False,
        "Geo dedup enabled (houses / land / commercial)", "Geo",
        "Master switch for the GEO candidate path. Apartments dedup on street + "
        "disposition; houses/land/commercial have no usable disposition, so they are "
        "matched by geo-proximity + area instead. When on, the scheduled full scan also "
        "runs the geo pass through the SAME free-first flow (pHash → cosine → forensic "
        "compare with facade/site-plan priority → floor/site-plan gate) — only the "
        "candidate FILTER differs. Off by default until calibrated.",
    ),
    DedupSetting(
        "dedup_geo_area_max_pct", "float", 0.20,
        "Geo candidate area tolerance", "Geo",
        "Two co-located single-dwelling listings are a geo CANDIDATE only when their "
        "areas are within this fraction (0.20 = ±20%). Wider than the apartment street "
        "gate because the candidate is still confirmed by the free-first visual flow — "
        "this controls RECALL into that flow, not the merge itself.",
        0.0, 1.0,
    ),
    # --- Vision models ---
    DedupSetting(
        "llm_visual_match_model", "model", "claude-sonnet-4-5",
        "Forensic model (default / Sonnet band)", "Vision models",
        "The accurate model that makes the final merge call. Used for the "
        "uncertain cosine band and whenever the cosine tier is off.",
    ),
    DedupSetting(
        "dedup_visual_match_model_haiku", "model", "claude-haiku-4-5",
        "Forensic model (Haiku band)", "Vision models",
        "The cheap model the cosine tier routes high-confidence rooms to.",
    ),
    DedupSetting(
        "llm_room_classify_model", "model", "claude-haiku-4-5",
        "Room classifier model (LLM fallback)", "Vision models",
        "Labels photos by room type when CLIP hasn't tagged a listing. With CLIP "
        "tags preferred, this is rarely paid.",
    ),
    DedupSetting(
        "llm_site_plan_match_model", "model", "claude-sonnet-4-5",
        "Site-plan development guard model", "Vision models",
        "Checks whether two site/situation plans highlight the same unit "
        "(blocks near-identical units of one development from auto-merging).",
    ),
    DedupSetting(
        "llm_floor_plan_match_model", "model", "claude-sonnet-4-5",
        "Floor-plan validation gate model", "Vision models",
        "Validates a would-be merge when both listings carry a floor plan: a "
        "different layout dismisses, a one-sided floor plan queues for the operator.",
    ),
)

REGISTRY_BY_KEY: dict[str, DedupSetting] = {s.key: s for s in REGISTRY}


def default_for(key: str) -> Any:
    """The coded default for a key (raises KeyError if unregistered)."""
    return REGISTRY_BY_KEY[key].default


def read_setting(conn: Any, key: str) -> Any:
    """The LIVE value of a registry setting: the app_settings row if present, else the
    registry default — coerced to the setting's kind. The one reader every backend caller
    should use, so a knob is read the same way everywhere (no per-call default drift)."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
    raw = row[0] if row and row[0] is not None else default_for(key)
    return coerce(REGISTRY_BY_KEY[key], raw)


def coerce(setting: DedupSetting, value: Any) -> Any:
    """Validate + coerce a raw value to the setting's kind, clamped to range.
    Raises ValueError on a value that can't be coerced."""
    if setting.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    if setting.kind == "float":
        v = float(value)
        if setting.min is not None:
            v = max(setting.min, v)
        if setting.max is not None:
            v = min(setting.max, v)
        return v
    if setting.kind == "model":
        v = str(value).strip()
        if not v:
            raise ValueError("model name cannot be empty")
        return v
    raise ValueError(f"unknown setting kind {setting.kind!r}")
