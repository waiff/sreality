"""compare_listings_visually: forensic same-property verdict for one room pair.

The dedup engine's visual layer (rule D) pairs LIKE rooms across two listings
(kitchen↔kitchen, bath↔bath, …) and asks the model whether the two photos
depict the same physical property. This runs that comparison for ONE room type
— a small set of images of that room from each listing — and returns a
High|Medium|Low verdict + rationale, using the operator's forensic prompt.

The engine calls this per room type in priority order and stops at the first
High (operator decision: only High auto-merges). Result cached per
(canonical pair, room_type, model) in listing_visual_matches — a re-run is free.

Write-allowed toolkit exception (CLAUDE.md toolkit rule #5). Image bytes from R2
via boto3 (scraper.image_storage), base64 into the vision payload.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from scraper import image_storage
from toolkit.vision_images import DOCUMENT_MAX_EDGE, image_block

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient

_PROMPT_KEY = "llm_visual_match_prompt"
_MODEL_KEY = "llm_visual_match_model"
_CALLED_FOR = "compare_listings_visually"

_SITE_PLAN_PROMPT_KEY = "llm_site_plan_match_prompt"
_SITE_PLAN_MODEL_KEY = "llm_site_plan_match_model"
_SITE_PLAN_CALLED_FOR = "compare_listing_site_plans"
_SITE_PLAN_VERDICTS = ("same_unit", "different_unit", "inconclusive")

_FLOOR_PLAN_PROMPT_KEY = "llm_floor_plan_match_prompt"
_FLOOR_PLAN_MODEL_KEY = "llm_floor_plan_match_model"
_FLOOR_PLAN_CALLED_FOR = "compare_listing_floor_plans"
_FLOOR_PLAN_VERDICTS = ("same_layout", "different_layout", "inconclusive", "no_2d_plan")

# N×N plan gate: cap plans per side sent to one vision call. Real listings carry 1-3 plans;
# this only guards a pathological count from blowing the token limit (20 downscaled plans ≈
# 32k tokens, well under the 200k cap), and is generous enough to never drop a real plan.
_MAX_PLANS_PER_SIDE = 20

# The forensic compare is the only call whose verdict auto-merges, so its resolution
# is gated: it stays at the (quality-neutral) document tier until the Haiku+768 A/B
# (scripts/validate_vision_models) confirms 768px reproduces every historical High.
# Then this becomes COMPARISON_MAX_EDGE in a one-line follow-up.
_COMPARE_MAX_EDGE = DOCUMENT_MAX_EDGE


class VisualMatchError(RuntimeError):
    """Raised when a verdict cannot be produced (no images, R2 missing, LLM refused)."""


RECORD_VISUAL_MATCH_TOOL: dict[str, Any] = {
    "name": "record_visual_match",
    "description": (
        "Record the forensic same-property verdict for the two sets of images. "
        "Call exactly once."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["High", "Medium", "Low"],
                "description": "Confidence that both image sets depict the SAME property.",
            },
            "rationale": {
                "type": "string",
                "description": "1-3 sentences: the definitive proof or red flags behind the verdict.",
            },
        },
        "required": ["verdict", "rationale"],
    },
}


def compare_listings_visually(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    room_type: str,
    image_ids_a: list[int],
    image_ids_b: list[int],
    force_refresh: bool = False,
    model: str | None = None,
) -> dict[str, Any]:
    """Forensic verdict for one room type across two listings (cache on miss).

    image_ids_a / image_ids_b are the classifier-selected images of `room_type`
    for each listing (the caller picks them; this keeps the tool free of the
    classification dependency and trivially testable). data.verdict ∈
    High|Medium|Low. `model` overrides the default (the dedup cosine tier routes
    high-confidence rooms to Haiku, uncertain ones to Sonnet); the cache key
    includes the model, so the two verdicts cache independently.
    """
    from toolkit import _now_iso

    if sreality_id_a == sreality_id_b:
        raise VisualMatchError("cannot compare a listing to itself")
    a, b = sorted((sreality_id_a, sreality_id_b))
    model = model or llm_client.resolve_model(_MODEL_KEY)

    if not force_refresh:
        cached = _cache_lookup(conn, a, b, room_type, model)
        if cached is not None:
            return _envelope(cached, a, b, room_type, model, cache_hit=True, queried_at=_now_iso())

    verdict, rationale, cost_usd, llm_call_id = _produce(
        conn, llm_client, a, b, room_type,
        image_ids_a if a == sreality_id_a else image_ids_b,
        image_ids_b if a == sreality_id_a else image_ids_a,
        model,
    )
    _cache_store(conn, a, b, room_type, verdict, rationale, model, llm_call_id, cost_usd)
    return _envelope(
        {"verdict": verdict, "rationale": rationale, "cost_usd": cost_usd},
        a, b, room_type, model, cache_hit=False, queried_at=_now_iso(),
    )


def _produce(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    a: int,
    b: int,
    room_type: str,
    keys_a_ids: list[int],
    keys_b_ids: list[int],
    model: str,
) -> tuple[str, str, float, int]:
    content = _build_compare_content(conn, room_type, keys_a_ids, keys_b_ids)
    system = llm_client.resolve_system_prompt(_PROMPT_KEY)
    response = llm_client.call(
        called_for=_CALLED_FOR,
        messages=[{"role": "user", "content": content}],
        system=system,
        tools=[RECORD_VISUAL_MATCH_TOOL],
        model=model,
    )
    verdict, rationale = _extract(response.tool_calls)
    return verdict, rationale, float(response.cost_usd or 0.0), response.llm_call_id


def _build_compare_content(
    conn: "psycopg.Connection",
    room_type: str,
    keys_a_ids: list[int],
    keys_b_ids: list[int],
) -> list[dict[str, Any]]:
    """Assemble the forensic same-property payload for one room pair.

    Shared by the synchronous tool and the batch lane's build_compare_request so
    the two paths send byte-identical content."""
    if not image_storage.is_configured():
        raise VisualMatchError("R2 is not configured; cannot fetch image bytes for vision")

    keys_a = _storage_paths(conn, keys_a_ids)
    keys_b = _storage_paths(conn, keys_b_ids)
    if not keys_a or not keys_b:
        raise VisualMatchError(f"missing {room_type} images for one side")

    r2 = image_storage.R2Client.from_env()
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"Listing A — {room_type} ({len(keys_a)} image(s)):"}
    ]
    content.extend(_blocks(r2, keys_a, _COMPARE_MAX_EDGE))
    content.append({"type": "text", "text": f"Listing B — {room_type} ({len(keys_b)} image(s)):"})
    content.extend(_blocks(r2, keys_b, _COMPARE_MAX_EDGE))
    content.append({
        "type": "text",
        "text": (
            "Both sets show the same room type. Decide whether they depict the "
            "same physical property, then call record_visual_match once."
        ),
    })
    return content


def build_compare_request(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    room_type: str,
    image_ids_a: list[int],
    image_ids_b: list[int],
) -> dict[str, Any]:
    """Build one room-pair forensic request for the async batch lane.

    Canonicalises the pair (sorted ids) so the request matches the cache key the
    ingester writes. Returns {system, messages, tools, model}."""
    if sreality_id_a == sreality_id_b:
        raise VisualMatchError("cannot compare a listing to itself")
    a, b = sorted((sreality_id_a, sreality_id_b))
    model = llm_client.resolve_model(_MODEL_KEY)
    keys_a_ids = image_ids_a if a == sreality_id_a else image_ids_b
    keys_b_ids = image_ids_b if a == sreality_id_a else image_ids_a
    content = _build_compare_content(conn, room_type, keys_a_ids, keys_b_ids)
    system = llm_client.resolve_system_prompt(_PROMPT_KEY)
    return {
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "tools": [RECORD_VISUAL_MATCH_TOOL],
        "model": model,
    }


def cached_visual_verdict(
    conn: "psycopg.Connection",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    room_type: str,
    model: str,
) -> str | None:
    """Cache-only verdict read for the batch lane (skip already-warm room pairs)."""
    a, b = sorted((sreality_id_a, sreality_id_b))
    row = _cache_lookup(conn, a, b, room_type, model)
    return row["verdict"] if row else None


def persist_visual_match(
    conn: "psycopg.Connection",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    room_type: str,
    tool_calls: list[dict[str, Any]],
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    """Write a batched record_visual_match result to the cache (same row the sync
    tool writes), keyed on the canonical pair + room + model."""
    a, b = sorted((sreality_id_a, sreality_id_b))
    verdict, rationale = _extract(tool_calls)
    _cache_store(conn, a, b, room_type, verdict, rationale, model, llm_call_id, cost_usd)


def _storage_paths(conn: "psycopg.Connection", image_ids: list[int]) -> list[str]:
    if not image_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT storage_path FROM images "
            "WHERE id = ANY(%s) AND storage_path IS NOT NULL "
            "ORDER BY sequence ASC NULLS LAST, id ASC",
            (image_ids,),
        )
        return [r[0] for r in cur.fetchall()]


def _blocks(r2: Any, keys: list[str], max_edge: int) -> list[dict[str, Any]]:
    # Downscale before encoding: full-res originals blow the 200k-token prompt
    # limit when several are packed into one call (toolkit.vision_images).
    return [image_block(r2, key, max_edge) for key in keys]


def _labelled_plan_blocks(
    r2: Any, keys: list[str], label_prefix: str, max_edge: int
) -> list[dict[str, Any]]:
    """One labelled image block per plan ("<prefix> plan k:" then the image) so the N×N
    plan gate can reference a specific plan ("A plan 2 matches B plan 1") and the model
    treats each as a distinct candidate rather than one blurred set."""
    out: list[dict[str, Any]] = []
    for i, key in enumerate(keys, 1):
        out.append({"type": "text", "text": f"{label_prefix} plan {i}:"})
        out.append(image_block(r2, key, max_edge))
    return out


def _extract(tool_calls: list[dict[str, Any]]) -> tuple[str, str]:
    matching = [tc for tc in tool_calls if tc.get("name") == "record_visual_match"]
    if not matching:
        raise VisualMatchError("LLM did not invoke record_visual_match; refusing to guess")
    payload = matching[0].get("input") or {}
    verdict = payload.get("verdict")
    if verdict not in ("High", "Medium", "Low"):
        raise VisualMatchError(f"record_visual_match returned bad verdict: {verdict!r}")
    rationale = payload.get("rationale")
    return verdict, rationale if isinstance(rationale, str) else ""


def _cache_lookup(
    conn: "psycopg.Connection", a: int, b: int, room_type: str, model: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, rationale, cost_usd FROM listing_visual_matches "
            "WHERE sreality_id_a = %s AND sreality_id_b = %s "
            "AND room_type = %s AND model = %s",
            (a, b, room_type, model),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"verdict": row[0], "rationale": row[1], "cost_usd": float(row[2]) if row[2] is not None else None}


def _cache_store(
    conn: "psycopg.Connection",
    a: int, b: int, room_type: str, verdict: str, rationale: str,
    model: str, llm_call_id: int, cost_usd: float,
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        # listing_id_{a,b} mirror the sreality_id side-for-side during the surrogate-key
        # dual-write (never re-canonicalize a/b — runbook §0.5). Arbiter is the
        # order-independent expression index (R2 Phase C,
        # listing_visual_matches_listing_id_pair_key).
        cur.execute(
            "INSERT INTO listing_visual_matches "
            "(sreality_id_a, listing_id_a, sreality_id_b, listing_id_b, room_type, verdict, rationale, "
            " model, llm_call_id, cost_usd) "
            "VALUES (%s, (SELECT id FROM listings WHERE sreality_id = %s), "
            "%s, (SELECT id FROM listings WHERE sreality_id = %s), %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (LEAST(listing_id_a, listing_id_b), GREATEST(listing_id_a, listing_id_b), "
            "room_type, model) DO UPDATE SET "
            "  listing_id_a = EXCLUDED.listing_id_a, listing_id_b = EXCLUDED.listing_id_b, "
            "  verdict = EXCLUDED.verdict, rationale = EXCLUDED.rationale, "
            "  llm_call_id = EXCLUDED.llm_call_id, cost_usd = EXCLUDED.cost_usd, "
            "  created_at = now()",
            (a, a, b, b, room_type, verdict, rationale, model, llm_call_id, cost_usd),
        )


def _envelope(
    payload: dict[str, Any], a: int, b: int, room_type: str, model: str,
    *, cache_hit: bool, queried_at: str,
) -> dict[str, Any]:
    return {
        "data": {
            "sreality_id_a": a,
            "sreality_id_b": b,
            "room_type": room_type,
            "verdict": payload["verdict"],
            "rationale": payload.get("rationale"),
            "model": model,
            "cost_usd": payload.get("cost_usd"),
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "compare_listings_visually",
            "filters_used": {"sreality_id_a": a, "sreality_id_b": b, "room_type": room_type},
            "result_count": 1,
            "queried_at": queried_at,
            "data_freshness": None,
        },
    }


# --- site-plan comparison (development guard) ------------------------------- #

RECORD_SITE_PLAN_MATCH_TOOL: dict[str, Any] = {
    "name": "record_site_plan_match",
    "description": (
        "Record whether two listings' site/situation plans point to the same "
        "unit or to different units of one development. Call exactly once."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": list(_SITE_PLAN_VERDICTS),
                "description": (
                    "same_unit = both plans highlight the same unit; "
                    "different_unit = they highlight different units of one "
                    "development; inconclusive = cannot tell."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "1-3 sentences citing the number/letter/position evidence.",
            },
        },
        "required": ["verdict", "rationale"],
    },
}


def compare_listing_site_plans(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    image_ids_a: list[int],
    image_ids_b: list[int],
    force_refresh: bool = False,
    model: str | None = None,
) -> dict[str, Any]:
    """Compare two listings' SITE-PLAN images (the development guard, cache on miss).

    Returns data.verdict ∈ same_unit | different_unit | inconclusive. The engine
    uses different_unit to QUEUE (never auto-merge), and a same-development
    site-plan pair never auto-merges on its own. Write-allowed toolkit exception
    (rule #5); cache in listing_site_plan_matches. `model` lets a caller override the
    flat app_settings default (e.g. a per-family route) — omit to use it as-is.
    """
    from toolkit import _now_iso

    if sreality_id_a == sreality_id_b:
        raise VisualMatchError("cannot compare a listing to itself")
    a, b = sorted((sreality_id_a, sreality_id_b))
    model = model or llm_client.resolve_model(_SITE_PLAN_MODEL_KEY)

    if not force_refresh:
        cached = _site_plan_cache_lookup(conn, a, b, model)
        if cached is not None:
            return _site_plan_envelope(cached, a, b, model, cache_hit=True, queried_at=_now_iso())

    verdict, rationale, cost_usd, llm_call_id = _produce_site_plan(
        conn, llm_client, a, b,
        image_ids_a if a == sreality_id_a else image_ids_b,
        image_ids_b if a == sreality_id_a else image_ids_a,
        model,
    )
    _site_plan_cache_store(conn, a, b, verdict, rationale, model, llm_call_id, cost_usd)
    return _site_plan_envelope(
        {"verdict": verdict, "rationale": rationale, "cost_usd": cost_usd},
        a, b, model, cache_hit=False, queried_at=_now_iso(),
    )


def _produce_site_plan(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    a: int,
    b: int,
    keys_a_ids: list[int],
    keys_b_ids: list[int],
    model: str,
) -> tuple[str, str, float, int]:
    content = _build_site_plan_content(conn, keys_a_ids, keys_b_ids)
    system = llm_client.resolve_system_prompt(_SITE_PLAN_PROMPT_KEY)
    response = llm_client.call(
        called_for=_SITE_PLAN_CALLED_FOR,
        messages=[{"role": "user", "content": content}],
        system=system,
        tools=[RECORD_SITE_PLAN_MATCH_TOOL],
        model=model,
    )
    verdict, rationale = _extract_site_plan(response.tool_calls)
    return verdict, rationale, float(response.cost_usd or 0.0), response.llm_call_id


def _build_site_plan_content(
    conn: "psycopg.Connection",
    keys_a_ids: list[int],
    keys_b_ids: list[int],
) -> list[dict[str, Any]]:
    """Assemble the development-guard payload for two listings' site plans.

    Shared by the synchronous tool and the batch lane's build_site_plan_request."""
    if not image_storage.is_configured():
        raise VisualMatchError("R2 is not configured; cannot fetch image bytes for vision")

    keys_a = _storage_paths(conn, keys_a_ids)[:_MAX_PLANS_PER_SIDE]
    keys_b = _storage_paths(conn, keys_b_ids)[:_MAX_PLANS_PER_SIDE]
    if not keys_a or not keys_b:
        raise VisualMatchError("missing site-plan images for one side")

    r2 = image_storage.R2Client.from_env()
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"Listing A — {len(keys_a)} site/situation plan(s):"}
    ]
    content.extend(_labelled_plan_blocks(r2, keys_a, "Listing A", DOCUMENT_MAX_EDGE))
    content.append({"type": "text", "text": f"Listing B — {len(keys_b)} site/situation plan(s):"})
    content.extend(_labelled_plan_blocks(r2, keys_b, "Listing B", DOCUMENT_MAX_EDGE))
    content.append({
        "type": "text",
        "text": "Identify the unit each listing highlights across its plans, then compare A vs B. "
                "same_unit if ANY pair shares a unit; different_unit only if NO pair does. "
                "Call record_site_plan_match once.",
    })
    return content


def build_site_plan_request(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    image_ids_a: list[int],
    image_ids_b: list[int],
    model: str | None = None,
) -> dict[str, Any]:
    """Build one development-guard (site-plan) request for the async batch lane.
    `model` lets a caller override the flat app_settings default (e.g. a per-family route)."""
    if sreality_id_a == sreality_id_b:
        raise VisualMatchError("cannot compare a listing to itself")
    a, b = sorted((sreality_id_a, sreality_id_b))
    model = model or llm_client.resolve_model(_SITE_PLAN_MODEL_KEY)
    keys_a_ids = image_ids_a if a == sreality_id_a else image_ids_b
    keys_b_ids = image_ids_b if a == sreality_id_a else image_ids_a
    content = _build_site_plan_content(conn, keys_a_ids, keys_b_ids)
    system = llm_client.resolve_system_prompt(_SITE_PLAN_PROMPT_KEY)
    return {
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "tools": [RECORD_SITE_PLAN_MATCH_TOOL],
        "model": model,
    }


def cached_site_plan_verdict(
    conn: "psycopg.Connection",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    model: str,
) -> str | None:
    """Cache-only site-plan verdict read for the batch lane."""
    a, b = sorted((sreality_id_a, sreality_id_b))
    row = _site_plan_cache_lookup(conn, a, b, model)
    return row["verdict"] if row else None


def persist_site_plan_match(
    conn: "psycopg.Connection",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    tool_calls: list[dict[str, Any]],
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    """Write a batched record_site_plan_match result to the cache (same row the
    sync tool writes), keyed on the canonical pair + model."""
    a, b = sorted((sreality_id_a, sreality_id_b))
    verdict, rationale = _extract_site_plan(tool_calls)
    _site_plan_cache_store(conn, a, b, verdict, rationale, model, llm_call_id, cost_usd)


def _extract_site_plan(tool_calls: list[dict[str, Any]]) -> tuple[str, str]:
    matching = [tc for tc in tool_calls if tc.get("name") == "record_site_plan_match"]
    if not matching:
        raise VisualMatchError("LLM did not invoke record_site_plan_match; refusing to guess")
    payload = matching[0].get("input") or {}
    verdict = payload.get("verdict")
    if verdict not in _SITE_PLAN_VERDICTS:
        raise VisualMatchError(f"record_site_plan_match returned bad verdict: {verdict!r}")
    rationale = payload.get("rationale")
    return verdict, rationale if isinstance(rationale, str) else ""


def _site_plan_cache_lookup(
    conn: "psycopg.Connection", a: int, b: int, model: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, rationale, cost_usd FROM listing_site_plan_matches "
            "WHERE sreality_id_a = %s AND sreality_id_b = %s AND model = %s",
            (a, b, model),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"verdict": row[0], "rationale": row[1], "cost_usd": float(row[2]) if row[2] is not None else None}


def _site_plan_cache_store(
    conn: "psycopg.Connection",
    a: int, b: int, verdict: str, rationale: str,
    model: str, llm_call_id: int, cost_usd: float,
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        # listing_id_{a,b} mirror the sreality_id side-for-side (never re-canonicalize
        # a/b — runbook §0.5). Arbiter is the order-independent expression index
        # (R2 Phase C, listing_site_plan_matches_listing_id_pair_key).
        cur.execute(
            "INSERT INTO listing_site_plan_matches "
            "(sreality_id_a, listing_id_a, sreality_id_b, listing_id_b, verdict, rationale, "
            " model, llm_call_id, cost_usd) "
            "VALUES (%s, (SELECT id FROM listings WHERE sreality_id = %s), "
            "%s, (SELECT id FROM listings WHERE sreality_id = %s), %s, %s, %s, %s, %s) "
            "ON CONFLICT (LEAST(listing_id_a, listing_id_b), GREATEST(listing_id_a, listing_id_b), "
            "model) DO UPDATE SET "
            "  listing_id_a = EXCLUDED.listing_id_a, listing_id_b = EXCLUDED.listing_id_b, "
            "  verdict = EXCLUDED.verdict, rationale = EXCLUDED.rationale, "
            "  llm_call_id = EXCLUDED.llm_call_id, cost_usd = EXCLUDED.cost_usd, "
            "  created_at = now()",
            (a, a, b, b, verdict, rationale, model, llm_call_id, cost_usd),
        )


def _site_plan_envelope(
    payload: dict[str, Any], a: int, b: int, model: str,
    *, cache_hit: bool, queried_at: str,
) -> dict[str, Any]:
    return {
        "data": {
            "sreality_id_a": a,
            "sreality_id_b": b,
            "verdict": payload["verdict"],
            "rationale": payload.get("rationale"),
            "model": model,
            "cost_usd": payload.get("cost_usd"),
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "compare_listing_site_plans",
            "filters_used": {"sreality_id_a": a, "sreality_id_b": b},
            "result_count": 1,
            "queried_at": queried_at,
            "data_freshness": None,
        },
    }


# --- floor-plan comparison (per-unit layout guard, migration 234) ----------- #
# Mirrors the site-plan guard but its verdict CAN auto-dismiss a merge: the engine
# runs it whenever it WOULD merge a pair that has floor plans on both sides; a
# `different_layout` verdict is the only new auto-dismiss. `extracted` carries the
# per-plan OCR (unit number / floor / area / balcony) used plan-to-plan only.

_PLAN_OCR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "unit_number": {"type": "string", "description": "byt č. / apartment number, if printed"},
        "floor": {"type": "string", "description": "podlaží / NP / patro, if printed"},
        "total_area_m2": {"type": "number", "description": "total m² printed on the plan"},
        "has_balcony": {"type": "boolean"},
        "has_terrace": {"type": "boolean"},
    },
}

RECORD_FLOOR_PLAN_MATCH_TOOL: dict[str, Any] = {
    "name": "record_floor_plan_match",
    "description": (
        "Record whether two listings' floor plans show the SAME apartment layout "
        "(same unit) or DIFFERENT layouts/units. Call exactly once."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": list(_FLOOR_PLAN_VERDICTS),
                "description": (
                    "same_layout = >=1 2D plan of A matches >=1 2D plan of B (arrangement + "
                    "room positions + no contradicting label); different_layout = BOTH sides "
                    "have usable 2D plans and NONE match (arrangement / room-count / positions "
                    "differ OR a unit-number / floor / area label contradicts); no_2d_plan = "
                    ">=1 side has NO usable 2D plan (only 3D renders / illegible) so a reliable "
                    "2D-to-2D compare is impossible; inconclusive = BOTH sides have usable 2D "
                    "plans but you still cannot decide."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "1-3 sentences citing the deciding layout/label evidence.",
            },
            "plan_a": _PLAN_OCR_SCHEMA,
            "plan_b": _PLAN_OCR_SCHEMA,
        },
        "required": ["verdict", "rationale"],
    },
}


def compare_listing_floor_plans(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    image_ids_a: list[int],
    image_ids_b: list[int],
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Compare two listings' FLOOR-PLAN images (the per-unit layout guard, cache on miss).

    Returns data.verdict ∈ same_layout | different_layout | inconclusive. The engine
    uses different_layout to DISMISS a merge it would otherwise make; same_layout /
    inconclusive let it proceed. Write-allowed toolkit exception (rule #5); cache in
    listing_floor_plan_matches.
    """
    from toolkit import _now_iso

    if sreality_id_a == sreality_id_b:
        raise VisualMatchError("cannot compare a listing to itself")
    a, b = sorted((sreality_id_a, sreality_id_b))
    model = llm_client.resolve_model(_FLOOR_PLAN_MODEL_KEY)

    if not force_refresh:
        cached = _floor_plan_cache_lookup(conn, a, b, model)
        if cached is not None:
            return _floor_plan_envelope(cached, a, b, model, cache_hit=True, queried_at=_now_iso())

    verdict, rationale, extracted, cost_usd, llm_call_id = _produce_floor_plan(
        conn, llm_client, a, b,
        image_ids_a if a == sreality_id_a else image_ids_b,
        image_ids_b if a == sreality_id_a else image_ids_a,
        model,
    )
    _floor_plan_cache_store(conn, a, b, verdict, rationale, extracted, model, llm_call_id, cost_usd)
    return _floor_plan_envelope(
        {"verdict": verdict, "rationale": rationale, "extracted": extracted, "cost_usd": cost_usd},
        a, b, model, cache_hit=False, queried_at=_now_iso(),
    )


def _produce_floor_plan(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    a: int,
    b: int,
    keys_a_ids: list[int],
    keys_b_ids: list[int],
    model: str,
) -> tuple[str, str, dict[str, Any] | None, float, int]:
    content = _build_floor_plan_content(conn, keys_a_ids, keys_b_ids)
    system = llm_client.resolve_system_prompt(_FLOOR_PLAN_PROMPT_KEY)
    response = llm_client.call(
        called_for=_FLOOR_PLAN_CALLED_FOR,
        messages=[{"role": "user", "content": content}],
        system=system,
        tools=[RECORD_FLOOR_PLAN_MATCH_TOOL],
        model=model,
    )
    verdict, rationale, extracted = _extract_floor_plan(response.tool_calls)
    return verdict, rationale, extracted, float(response.cost_usd or 0.0), response.llm_call_id


def _build_floor_plan_content(
    conn: "psycopg.Connection",
    keys_a_ids: list[int],
    keys_b_ids: list[int],
) -> list[dict[str, Any]]:
    """Assemble the floor-plan payload for two listings (shared by the sync tool and
    the batch lane's build_floor_plan_request)."""
    if not image_storage.is_configured():
        raise VisualMatchError("R2 is not configured; cannot fetch image bytes for vision")

    keys_a = _storage_paths(conn, keys_a_ids)[:_MAX_PLANS_PER_SIDE]
    keys_b = _storage_paths(conn, keys_b_ids)[:_MAX_PLANS_PER_SIDE]
    if not keys_a or not keys_b:
        raise VisualMatchError("missing floor-plan images for one side")

    r2 = image_storage.R2Client.from_env()
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"Listing A — {len(keys_a)} floor plan(s):"}
    ]
    content.extend(_labelled_plan_blocks(r2, keys_a, "Listing A", DOCUMENT_MAX_EDGE))
    content.append({"type": "text", "text": f"Listing B — {len(keys_b)} floor plan(s):"})
    content.extend(_labelled_plan_blocks(r2, keys_b, "Listing B", DOCUMENT_MAX_EDGE))
    content.append({
        "type": "text",
        "text": "Compare EVERY plan of A against EVERY plan of B (N×N). same_layout if ANY pair "
                "matches; different_layout only if NO pair matches. Call record_floor_plan_match once.",
    })
    return content


def build_floor_plan_request(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    image_ids_a: list[int],
    image_ids_b: list[int],
) -> dict[str, Any]:
    """Build one floor-plan-guard request for the async batch lane."""
    if sreality_id_a == sreality_id_b:
        raise VisualMatchError("cannot compare a listing to itself")
    a, b = sorted((sreality_id_a, sreality_id_b))
    model = llm_client.resolve_model(_FLOOR_PLAN_MODEL_KEY)
    keys_a_ids = image_ids_a if a == sreality_id_a else image_ids_b
    keys_b_ids = image_ids_b if a == sreality_id_a else image_ids_a
    content = _build_floor_plan_content(conn, keys_a_ids, keys_b_ids)
    system = llm_client.resolve_system_prompt(_FLOOR_PLAN_PROMPT_KEY)
    return {
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "tools": [RECORD_FLOOR_PLAN_MATCH_TOOL],
        "model": model,
    }


def cached_floor_plan_verdict(
    conn: "psycopg.Connection",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    model: str,
) -> str | None:
    """Cache-only floor-plan verdict read for the batch lane / engine consume path."""
    a, b = sorted((sreality_id_a, sreality_id_b))
    row = _floor_plan_cache_lookup(conn, a, b, model)
    return row["verdict"] if row else None


def persist_floor_plan_match(
    conn: "psycopg.Connection",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    tool_calls: list[dict[str, Any]],
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    """Write a batched record_floor_plan_match result to the cache (same row the sync
    tool writes), keyed on the canonical pair + model."""
    a, b = sorted((sreality_id_a, sreality_id_b))
    verdict, rationale, extracted = _extract_floor_plan(tool_calls)
    _floor_plan_cache_store(conn, a, b, verdict, rationale, extracted, model, llm_call_id, cost_usd)


def _extract_floor_plan(
    tool_calls: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any] | None]:
    matching = [tc for tc in tool_calls if tc.get("name") == "record_floor_plan_match"]
    if not matching:
        raise VisualMatchError("LLM did not invoke record_floor_plan_match; refusing to guess")
    payload = matching[0].get("input") or {}
    verdict = payload.get("verdict")
    if verdict not in _FLOOR_PLAN_VERDICTS:
        raise VisualMatchError(f"record_floor_plan_match returned bad verdict: {verdict!r}")
    rationale = payload.get("rationale")
    extracted: dict[str, Any] = {}
    for side in ("plan_a", "plan_b"):
        if isinstance(payload.get(side), dict) and payload[side]:
            extracted[side] = payload[side]
    return verdict, rationale if isinstance(rationale, str) else "", (extracted or None)


def _floor_plan_cache_lookup(
    conn: "psycopg.Connection", a: int, b: int, model: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, rationale, extracted, cost_usd FROM listing_floor_plan_matches "
            "WHERE sreality_id_a = %s AND sreality_id_b = %s AND model = %s",
            (a, b, model),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "verdict": row[0], "rationale": row[1], "extracted": row[2],
        "cost_usd": float(row[3]) if row[3] is not None else None,
    }


def _floor_plan_cache_store(
    conn: "psycopg.Connection",
    a: int, b: int, verdict: str, rationale: str, extracted: dict[str, Any] | None,
    model: str, llm_call_id: int, cost_usd: float,
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        # listing_id_{a,b} mirror the sreality_id side-for-side (never re-canonicalize
        # a/b — runbook §0.5). Arbiter is the order-independent expression index
        # (R2 Phase C, listing_floor_plan_matches_listing_id_pair_key).
        cur.execute(
            "INSERT INTO listing_floor_plan_matches "
            "(sreality_id_a, listing_id_a, sreality_id_b, listing_id_b, verdict, rationale, extracted, "
            " model, llm_call_id, cost_usd) "
            "VALUES (%s, (SELECT id FROM listings WHERE sreality_id = %s), "
            "%s, (SELECT id FROM listings WHERE sreality_id = %s), %s, %s, %s::jsonb, %s, %s, %s) "
            "ON CONFLICT (LEAST(listing_id_a, listing_id_b), GREATEST(listing_id_a, listing_id_b), "
            "model) DO UPDATE SET "
            "  listing_id_a = EXCLUDED.listing_id_a, listing_id_b = EXCLUDED.listing_id_b, "
            "  verdict = EXCLUDED.verdict, rationale = EXCLUDED.rationale, "
            "  extracted = EXCLUDED.extracted, llm_call_id = EXCLUDED.llm_call_id, "
            "  cost_usd = EXCLUDED.cost_usd, created_at = now()",
            (a, a, b, b, verdict, rationale,
             json.dumps(extracted) if extracted is not None else None,
             model, llm_call_id, cost_usd),
        )


def _floor_plan_envelope(
    payload: dict[str, Any], a: int, b: int, model: str,
    *, cache_hit: bool, queried_at: str,
) -> dict[str, Any]:
    return {
        "data": {
            "sreality_id_a": a,
            "sreality_id_b": b,
            "verdict": payload["verdict"],
            "rationale": payload.get("rationale"),
            "extracted": payload.get("extracted"),
            "model": model,
            "cost_usd": payload.get("cost_usd"),
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "compare_listing_floor_plans",
            "filters_used": {"sreality_id_a": a, "sreality_id_b": b},
            "result_count": 1,
            "queried_at": queried_at,
            "data_freshness": None,
        },
    }
