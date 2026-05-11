"""compare_listing_images: pairwise visual similarity via Claude vision.

Phase 6 visual layer. Scores two listings across six fixed tenant-
relevant dimensions (exterior, kitchen, windows_and_light, floor_finish,
lighting, styling). The Phase 7 reasoning agent uses per-dimension
scores to decide whether a comparable's price signal is trustworthy
along the axes that matter for a given listing.

Cache lives in `listing_image_comparisons`, keyed on the canonical
ordered pair (sreality_id_a < sreality_id_b). Repeat calls return
instantly with no LLM cost.

Image bytes come from R2 via boto3 GetObject; we base64-encode them
into the Anthropic vision payload. This is more robust than passing
the public R2 URL (doesn't require Anthropic to reach our bucket;
survives bucket-permission changes; the read path is reusable).

Write-allowed exception per CLAUDE.md toolkit rule #5: same rationale
as `find_anchor_amenities` and `summarize_listing` — the LLM is the
source of truth, we cache locally to keep repeat lookups fast and
Anthropic-friendly. Vision is materially more expensive than text,
so caching matters more here than anywhere else in the toolkit.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from scraper import image_storage

try:
    from psycopg.types.json import Jsonb as _Jsonb
except ImportError:
    def _Jsonb(value: Any) -> Any:  # type: ignore[misc]
        return value

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient


_SYSTEM_PROMPT_KEY = "llm_image_compare_system_prompt"
_MODEL_KEY = "llm_image_compare_model"
_CALLED_FOR = "compare_listing_images"

DIMENSIONS = (
    "exterior",
    "kitchen",
    "windows_and_light",
    "floor_finish",
    "lighting",
    "styling",
)


class ImageCompareError(RuntimeError):
    """Raised when a comparison cannot be produced (no images, R2 not configured, LLM refused)."""


def _dimension_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "score": {
                "type": ["number", "null"],
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0.0=different, 1.0=indistinguishable. null when observed=false.",
            },
            "observed": {
                "type": "boolean",
                "description": "True only if the dimension's subject is visible in BOTH listings' images.",
            },
            "reasoning": {
                "type": "string",
                "description": "1-2 sentences citing visible features.",
            },
        },
        "required": ["score", "observed", "reasoning"],
    }


RECORD_IMAGE_COMPARISON_TOOL: dict[str, Any] = {
    "name": "record_image_comparison",
    "description": (
        "Record the structured pairwise comparison of two listings' "
        "images across six fixed dimensions. Call exactly once."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "dimensions": {
                "type": "object",
                "additionalProperties": False,
                "properties": {dim: _dimension_schema() for dim in DIMENSIONS},
                "required": list(DIMENSIONS),
            },
            "overall_similarity": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Mean of observed dimension scores; 0.0 if zero observed.",
            },
            "summary": {
                "type": "string",
                "description": "1-2 sentences naming the strongest match and divergence.",
            },
        },
        "required": ["dimensions", "overall_similarity", "summary"],
    },
}


def compare_listing_images(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    n_images: int = 6,
    force_refresh: bool = False,
) -> dict[str, Any]:
    from toolkit import _now_iso

    if sreality_id_a == sreality_id_b:
        raise ImageCompareError("cannot compare a listing to itself")

    a, b = sorted((sreality_id_a, sreality_id_b))
    cache_hit = False

    if not force_refresh:
        cached = _cache_lookup(conn, a, b)
        if cached is not None:
            cache_hit = True
            comparison = cached["comparison"]
            model = cached["model"]
            cost_usd = cached["cost_usd"]
            n_a = cached["n_images_a"]
            n_b = cached["n_images_b"]
            data_freshness = _max_listing_last_seen(conn, a, b)
        else:
            comparison, model, cost_usd, n_a, n_b, data_freshness = (
                _produce_comparison(conn, llm_client, a, b, n_images)
            )
    else:
        comparison, model, cost_usd, n_a, n_b, data_freshness = (
            _produce_comparison(conn, llm_client, a, b, n_images)
        )

    return {
        "data": {
            "sreality_id_a": a,
            "sreality_id_b": b,
            "comparison": comparison,
            "n_images_a": n_a,
            "n_images_b": n_b,
            "model": model,
            "cost_usd": float(cost_usd) if cost_usd is not None else None,
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "compare_listing_images",
            "filters_used": {
                "sreality_id_a": sreality_id_a,
                "sreality_id_b": sreality_id_b,
                "n_images": n_images,
                "force_refresh": force_refresh,
            },
            "result_count": 1,
            "queried_at": _now_iso(),
            "data_freshness": data_freshness,
        },
    }


def _produce_comparison(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    a: int,
    b: int,
    n_images: int,
) -> tuple[dict[str, Any], str, float | None, int, int, str | None]:
    if not image_storage.is_configured():
        raise ImageCompareError(
            "R2 is not configured (R2_* env vars missing); "
            "cannot fetch listing images for vision"
        )

    images_a = _fetch_image_keys(conn, a, n_images)
    if not images_a:
        raise ImageCompareError(
            f"no R2-stored images for sreality_id={a}; cannot compare"
        )
    images_b = _fetch_image_keys(conn, b, n_images)
    if not images_b:
        raise ImageCompareError(
            f"no R2-stored images for sreality_id={b}; cannot compare"
        )

    r2 = image_storage.R2Client.from_env()
    blocks_a = _build_image_blocks(r2, images_a)
    blocks_b = _build_image_blocks(r2, images_b)

    content: list[dict[str, Any]] = []
    content.append({"type": "text", "text": f"Listing A images ({len(blocks_a)}):"})
    content.extend(blocks_a)
    content.append({"type": "text", "text": f"Listing B images ({len(blocks_b)}):"})
    content.extend(blocks_b)
    content.append({
        "type": "text",
        "text": (
            "Compare these two listings across the six dimensions. "
            "Mark any dimension whose subject is not visible in both "
            "listings as observed=false with score=null."
        ),
    })

    system = llm_client.resolve_system_prompt(_SYSTEM_PROMPT_KEY)
    model = llm_client.resolve_model(_MODEL_KEY)

    response = llm_client.call(
        called_for=_CALLED_FOR,
        messages=[{"role": "user", "content": content}],
        system=system,
        tools=[RECORD_IMAGE_COMPARISON_TOOL],
        model=model,
    )
    comparison = _extract_tool_call(response.tool_calls)

    _cache_store(
        conn,
        sreality_id_a=a,
        sreality_id_b=b,
        comparison=comparison,
        n_images_a=len(blocks_a),
        n_images_b=len(blocks_b),
        model=response.model,
        llm_call_id=response.llm_call_id,
        cost_usd=response.cost_usd,
    )
    return (
        comparison,
        response.model,
        response.cost_usd,
        len(blocks_a),
        len(blocks_b),
        _max_listing_last_seen(conn, a, b),
    )


def _fetch_image_keys(
    conn: "psycopg.Connection",
    sreality_id: int,
    n_images: int,
) -> list[str]:
    sql = (
        "SELECT storage_path FROM images "
        "WHERE sreality_id = %s AND storage_path IS NOT NULL "
        "ORDER BY sequence ASC NULLS LAST LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id, n_images))
        rows = cur.fetchall()
    return [r[0] for r in rows]


def _build_image_blocks(
    r2: Any, keys: list[str],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for key in keys:
        data = r2.download_bytes(key)
        encoded = base64.standard_b64encode(data).decode("ascii")
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": encoded,
            },
        })
    return blocks


def _extract_tool_call(
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    matching = [
        tc for tc in tool_calls
        if tc.get("name") == "record_image_comparison"
    ]
    if not matching:
        raise ImageCompareError(
            "LLM did not invoke record_image_comparison; refusing to guess"
        )
    if len(matching) > 1:
        raise ImageCompareError(
            "LLM invoked record_image_comparison more than once"
        )
    payload = matching[0].get("input") or {}
    if not isinstance(payload, dict):
        raise ImageCompareError("record_image_comparison input was not an object")
    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, dict):
        raise ImageCompareError("record_image_comparison missing dimensions object")
    for dim in DIMENSIONS:
        if dim not in dimensions:
            raise ImageCompareError(
                f"record_image_comparison missing dimension: {dim}"
            )
    return dict(payload)


def _cache_lookup(
    conn: "psycopg.Connection",
    a: int,
    b: int,
) -> dict[str, Any] | None:
    sql = (
        "SELECT comparison, n_images_a, n_images_b, model, cost_usd "
        "FROM listing_image_comparisons "
        "WHERE sreality_id_a = %s AND sreality_id_b = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (a, b))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "comparison": row[0],
        "n_images_a": row[1],
        "n_images_b": row[2],
        "model": row[3],
        "cost_usd": float(row[4]) if row[4] is not None else None,
    }


def _cache_store(
    conn: "psycopg.Connection",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    comparison: dict[str, Any],
    n_images_a: int,
    n_images_b: int,
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    sql = (
        "INSERT INTO listing_image_comparisons "
        "(sreality_id_a, sreality_id_b, comparison, n_images_a, n_images_b, "
        " model, llm_call_id, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (sreality_id_a, sreality_id_b) DO UPDATE SET "
        " comparison = EXCLUDED.comparison, "
        " n_images_a = EXCLUDED.n_images_a, "
        " n_images_b = EXCLUDED.n_images_b, "
        " model = EXCLUDED.model, "
        " llm_call_id = EXCLUDED.llm_call_id, "
        " cost_usd = EXCLUDED.cost_usd, "
        " created_at = now()"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            sql,
            (
                sreality_id_a, sreality_id_b, _Jsonb(comparison),
                n_images_a, n_images_b, model, llm_call_id, cost_usd,
            ),
        )


def _max_listing_last_seen(
    conn: "psycopg.Connection",
    a: int,
    b: int,
) -> str | None:
    sql = (
        "SELECT MAX(last_seen_at) FROM listings "
        "WHERE sreality_id IN (%s, %s)"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (a, b))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return row[0].isoformat()
