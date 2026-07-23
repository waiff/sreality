"""compare_listing_images: pairwise visual similarity via Claude vision.

Phase 6 visual layer. Scores two listings across six fixed tenant-
relevant dimensions (exterior, kitchen, windows_and_light, floor_finish,
lighting, styling). The Phase 7 reasoning agent uses per-dimension
scores to decide whether a comparable's price signal is trustworthy
along the axes that matter for a given listing.

Cache lives in `listing_image_comparisons`, keyed on the canonical
ordered pair (LEAST/GREATEST over listing_id — the surrogate PK, not
sreality_id, per the R2 dedup identity chain PR3). Repeat calls return
instantly with no LLM cost.

Image bytes come from R2 via the shared `toolkit.vision_images.image_block`
helper (downscale + base64), at the comparison tier. This is more robust
than passing the public R2 URL (doesn't require Anthropic to reach our
bucket; survives bucket-permission changes; the read path is reusable).

Write-allowed exception per CLAUDE.md toolkit rule #5: same rationale
as `find_anchor_amenities` and `summarize_listing` — the LLM is the
source of truth, we cache locally to keep repeat lookups fast and
Anthropic-friendly. Vision is materially more expensive than text,
so caching matters more here than anywhere else in the toolkit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scraper import image_storage
from toolkit.vision_images import COMPARISON_MAX_EDGE, image_block

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
    sreality_id_a: int | None = None,
    sreality_id_b: int | None = None,
    n_images: int = 6,
    force_refresh: bool = False,
    listing_id_a: int | None = None,
    listing_id_b: int | None = None,
) -> dict[str, Any]:
    from toolkit import _now_iso

    # Each slot is addressable by the surrogate listing_id (preferred) or the
    # portal-native sreality_id; a mix per slot across a/b is rejected so the
    # self-compare guard and canonical-order resolve stay in one id-space.
    by_lid = listing_id_a is not None or listing_id_b is not None
    if by_lid:
        if listing_id_a is None or listing_id_b is None:
            raise ImageCompareError(
                "compare_listing_images: supply listing_id for BOTH a and b"
            )
        if listing_id_a == listing_id_b:
            raise ImageCompareError("cannot compare a listing to itself")
    else:
        if sreality_id_a is None or sreality_id_b is None:
            raise ImageCompareError(
                "compare_listing_images: supply a sreality_id or listing_id "
                "for both a and b"
            )
        if sreality_id_a == sreality_id_b:
            raise ImageCompareError("cannot compare a listing to itself")

    # Canonical order is by listing_id (the surrogate PK), not sreality_id —
    # sreality_id rides along side-coupled with whichever listing_id it
    # belongs to, so the persisted a/b columns and n_images_a/_b never end up
    # swapped relative to each other.
    (sid_a, lid_a), (sid_b, lid_b) = sorted(
        _resolve_listing_ids(
            conn,
            sreality_id_a=sreality_id_a, sreality_id_b=sreality_id_b,
            listing_id_a=listing_id_a, listing_id_b=listing_id_b,
        ),
        key=lambda p: p[1],
    )
    cache_hit = False

    if not force_refresh:
        cached = _cache_lookup(conn, lid_a, lid_b)
        if cached is not None:
            cache_hit = True
            comparison = cached["comparison"]
            model = cached["model"]
            cost_usd = cached["cost_usd"]
            n_a = cached["n_images_a"]
            n_b = cached["n_images_b"]
            data_freshness = _max_listing_last_seen(conn, lid_a, lid_b)
        else:
            comparison, model, cost_usd, n_a, n_b, data_freshness = (
                _produce_comparison(conn, llm_client, sid_a, sid_b, lid_a, lid_b, n_images)
            )
    else:
        comparison, model, cost_usd, n_a, n_b, data_freshness = (
            _produce_comparison(conn, llm_client, sid_a, sid_b, lid_a, lid_b, n_images)
        )

    return {
        "data": {
            "sreality_id_a": sid_a,
            "sreality_id_b": sid_b,
            "comparison": comparison,
            "n_images_a": n_a,
            "n_images_b": n_b,
            "model": model,
            "cost_usd": float(cost_usd) if cost_usd is not None else None,
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "compare_listing_images",
            "filters_used": (
                {
                    "listing_id_a": listing_id_a,
                    "listing_id_b": listing_id_b,
                    "n_images": n_images,
                    "force_refresh": force_refresh,
                }
                if by_lid else
                {
                    "sreality_id_a": sreality_id_a,
                    "sreality_id_b": sreality_id_b,
                    "n_images": n_images,
                    "force_refresh": force_refresh,
                }
            ),
            "result_count": 1,
            "queried_at": _now_iso(),
            "data_freshness": data_freshness,
        },
    }


def _resolve_listing_ids(
    conn: "psycopg.Connection",
    *,
    sreality_id_a: int | None = None,
    sreality_id_b: int | None = None,
    listing_id_a: int | None = None,
    listing_id_b: int | None = None,
) -> list[tuple[int, int]]:
    """[(sreality_id, listing_id), ...] for the two slots, order matching the call args.

    Resolves by the surrogate listing_id when supplied, else the sreality_id.
    """
    if listing_id_a is not None or listing_id_b is not None:
        keys = [listing_id_a, listing_id_b]
        sql = "SELECT sreality_id, id FROM listings WHERE id = ANY(%s)"
        with conn.cursor() as cur:
            cur.execute(sql, (keys,))
            by_id = {r[1]: (r[0], r[1]) for r in cur.fetchall()}
        for lid in keys:
            if lid not in by_id:
                raise ImageCompareError(f"listing_id={lid} not found in listings")
        return [by_id[listing_id_a], by_id[listing_id_b]]

    keys = [sreality_id_a, sreality_id_b]
    sql = "SELECT sreality_id, id FROM listings WHERE sreality_id = ANY(%s)"
    with conn.cursor() as cur:
        cur.execute(sql, (keys,))
        rows = {r[0]: r[1] for r in cur.fetchall()}
    for sid in keys:
        if sid not in rows:
            raise ImageCompareError(f"sreality_id={sid} not found in listings")
    return [(sreality_id_a, rows[sreality_id_a]), (sreality_id_b, rows[sreality_id_b])]


def _produce_comparison(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    sid_a: int,
    sid_b: int,
    lid_a: int,
    lid_b: int,
    n_images: int,
) -> tuple[dict[str, Any], str, float | None, int, int, str | None]:
    if not image_storage.is_configured():
        raise ImageCompareError(
            "R2 is not configured (R2_* env vars missing); "
            "cannot fetch listing images for vision"
        )

    images_a = _fetch_image_keys(conn, lid_a, n_images)
    if not images_a:
        raise ImageCompareError(
            f"no R2-stored images for listing_id={lid_a}; cannot compare"
        )
    images_b = _fetch_image_keys(conn, lid_b, n_images)
    if not images_b:
        raise ImageCompareError(
            f"no R2-stored images for listing_id={lid_b}; cannot compare"
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
        sreality_id_a=sid_a,
        listing_id_a=lid_a,
        sreality_id_b=sid_b,
        listing_id_b=lid_b,
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
        _max_listing_last_seen(conn, lid_a, lid_b),
    )


def _fetch_image_keys(
    conn: "psycopg.Connection",
    listing_id: int,
    n_images: int,
) -> list[str]:
    # Keyed on the surrogate (images.listing_id, fully populated): sreality_id
    # is NULL for a post-Gate-2 listing, which would silently starve this query.
    sql = (
        "SELECT storage_path FROM images "
        "WHERE listing_id = %s AND storage_path IS NOT NULL "
        "ORDER BY sequence ASC NULLS LAST LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (listing_id, n_images))
        rows = cur.fetchall()
    return [r[0] for r in rows]


def _build_image_blocks(
    r2: Any, keys: list[str],
) -> list[dict[str, Any]]:
    # Photo comparison: the shared downscaler at the comparison tier (sub-megapixel
    # is ample for visual similarity, and cuts vision tokens to ~1/3 vs full-res).
    return [image_block(r2, key, COMPARISON_MAX_EDGE) for key in keys]


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
    lid_a: int,
    lid_b: int,
) -> dict[str, Any] | None:
    sql = (
        "SELECT comparison, n_images_a, n_images_b, model, cost_usd "
        "FROM listing_image_comparisons "
        "WHERE LEAST(listing_id_a, listing_id_b) = %s "
        "  AND GREATEST(listing_id_a, listing_id_b) = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (lid_a, lid_b))
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
    listing_id_a: int,
    sreality_id_b: int,
    listing_id_b: int,
    comparison: dict[str, Any],
    n_images_a: int,
    n_images_b: int,
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    # Arbiter is the order-independent expression index
    # (listing_image_comparisons_listing_id_pair_key): a swapped-order call still
    # matches the existing row, and DO UPDATE SET overwrites every column —
    # including sreality_id_a/_b — from THIS call's fresh values, so a/b never
    # end up mismatched with their own n_images_a/n_images_b.
    sql = (
        "INSERT INTO listing_image_comparisons "
        "(sreality_id_a, listing_id_a, sreality_id_b, listing_id_b, "
        " comparison, n_images_a, n_images_b, "
        " model, llm_call_id, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (LEAST(listing_id_a, listing_id_b), GREATEST(listing_id_a, listing_id_b)) "
        "DO UPDATE SET "
        " sreality_id_a = EXCLUDED.sreality_id_a, "
        " sreality_id_b = EXCLUDED.sreality_id_b, "
        " listing_id_a = EXCLUDED.listing_id_a, "
        " listing_id_b = EXCLUDED.listing_id_b, "
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
                sreality_id_a, listing_id_a,
                sreality_id_b, listing_id_b,
                _Jsonb(comparison),
                n_images_a, n_images_b, model, llm_call_id, cost_usd,
            ),
        )


def _max_listing_last_seen(
    conn: "psycopg.Connection",
    lid_a: int,
    lid_b: int,
) -> str | None:
    sql = (
        "SELECT MAX(last_seen_at) FROM listings "
        "WHERE id IN (%s, %s)"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (lid_a, lid_b))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return row[0].isoformat()
