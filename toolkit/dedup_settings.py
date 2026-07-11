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
    DedupSetting(
        "dedup_defer_incomplete_downloads", "bool", False,
        "Defer pairs while images are still downloading", "Engine",
        "Extends the tagging-readiness gate: a pair also DEFERS (never merges/dismisses/pays "
        "vision) while either listing still has an image pending download "
        "(storage_path NULL, download_attempts < 5). Today the gate only waits for already-"
        "downloaded images to be CLIP-tagged, so a pair can pay for forensic vision before its "
        "full photo set has landed — and the free pHash signal that would have merged it for "
        "free arrives minutes later. Bounded + self-healing: an image that exhausts its 5 "
        "download attempts stops blocking; the pair re-decides for free once the last image "
        "downloads + tags. Off = decide on whatever images have arrived (today's behaviour).",
    ),
    DedupSetting(
        "dedup_nonbyt_attr_merge_enabled", "bool", False,
        "Attribute auto-merge (houses / land / commercial)", "Engine",
        "For non-apartment families, auto-merge a co-located candidate whose areas match "
        "within 2% AND whose asking prices are identical — WITHOUT paying for the forensic "
        "room compare. The floor-plan gate is still applied (a different 2D layout dismisses) "
        "and any pair where BOTH sides carry a site plan still pays the development guard, so "
        "the two conservative vetoes are unchanged. Validated 99.6% vs the vision verdict on "
        "574 decided house/land/commercial pairs (the 2 misses were floor-plan dismissals the "
        "retained gate still catches); merges are reversible. Off = every non-byt candidate "
        "pays forensic vision as before.",
    ),
    DedupSetting(
        "dedup_nonbyt_phash_single_enabled", "bool", False,
        "pHash single-pair auto-merge (houses / land / commercial)", "Engine",
        "For non-apartment families, ONE near-identical image pair (Hamming <= 6) suffices "
        "for the pHash fast-path merge (classic threshold is 2). House/land photo sets are "
        "property-unique — unlike apartment development renders, which stay on the 2-pair "
        "rule. The floor-plan gate and the both-site-plan development guard are unchanged; "
        "merges are reversible and carry the distinct reason 'phash_single'. Cost plan "
        "§2.2 arm (a); replay precision in the shipping PR. Off = classic 2-pair rule "
        "for every family.",
    ),
    DedupSetting(
        "dedup_nonbyt_cosine_merge_min", "float", 0,
        "Pair max-cosine auto-merge threshold (houses / land / commercial)", "Engine",
        "For non-apartment families, auto-merge when the best CLIP cosine between ANY image "
        "of one listing and ANY image of the other reaches this value — a free pgvector "
        "lookup, no LLM. 0 disables; the validated operating point is 0.98 (99.57% agreement "
        "with the forensic verdict on decided pairs). A pair where either side has no stored "
        "embeddings never fires. Floor-plan gate + both-site-plan development guard "
        "unchanged; merges are reversible and carry the distinct reason 'cosine_high'. "
        "Cost plan §2.2 arm (b).",
    ),
    DedupSetting(
        "dedup_facade_dismiss_enabled", "bool", False,
        "Facade Low auto-dismiss (houses / land / commercial)", "Engine",
        "For non-apartment families, a confident forensic LOW verdict on the exterior_facade "
        "room qualifies for auto-dismiss, exactly like the kitchen/bathroom wet rooms do for "
        "apartments — the facade is the identity-bearing surface for houses and land. All "
        "other dismissal conservatism is unchanged: every common room must be verdicted, any "
        "High still merges, any Medium on a qualifying room still queues for review. byt "
        "facades never qualify (a development's shared shell says nothing about the unit). "
        "Cost plan Phase 4 item 2 (operator-requested); replay evidence in the shipping PR. "
        "Off = kitchen/bathroom-only dismissal as before.",
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
    DedupSetting(
        "dedup_byt_geo_enabled", "bool", False,
        "Byt geo rung (street-less apartments)", "Geo",
        "Master switch for the SCHEDULED byt geo-cell rung: a street-less apartment "
        "(invisible to the street pass) blocks on its geo cell + disposition instead. "
        "CANDIDATE-GENERATION ONLY — the cell+disposition signal never auto-merges; "
        "pHash / forensic High stay the sole merge gates, and centroid-pinned mega-"
        "cells ride the bounded oversized-group path. Gates the dedicated "
        "--byt-geo-only cron; the real-time dirty drain's byt sub-pass runs regardless "
        "(the same posture as the geo sub-pass vs dedup_geo_enabled). Off by default "
        "until the operator flips it after the migration-290 backfill.",
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
