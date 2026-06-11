"""score_listing_condition: per-listing building + apartment condition scoring.

Phase B of the condition-scoring feature. For one listing's latest
snapshot, calls Claude with the curated marker dictionary + 5-level
rubric (both injected from app_settings at call time) and returns
building_level / apartment_level (1..5) with per-axis confidence
and the marker IDs the LLM relied on.

Cache lives in `listing_condition_scores`, keyed on
(sreality_id, snapshot_id). The cache write happens inside the same
transaction as a guarded UPDATE on `listings.building_condition_level`
/ `apartment_condition_level`, so the latest score is always
discoverable directly from the listing row for downstream filtering
(comparables, /browse, frontend) without a JOIN.

Write-allowed exception per CLAUDE.md toolkit rule #5: same rationale
as `discover_condition_markers` — the LLM is the source of truth, we
cache locally so the operator can iterate on the rubric / dictionary
without re-billing per pass.

The two big app_settings rows (`llm_condition_rubric`,
`llm_condition_marker_dictionary`) are populated by
`scripts/seed_condition_settings.py` from the committed JSON files,
NOT by migration 072 directly. If either is still empty {} at call
time, the scorer raises ScoringError with a clear message.
"""

from __future__ import annotations

import base64
import json
import logging
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

LOG = logging.getLogger(__name__)


_SYSTEM_PROMPT_KEY = "llm_condition_system_prompt"
_MODEL_KEY = "llm_condition_model"
_RUBRIC_KEY = "llm_condition_rubric"
_DICTIONARY_KEY = "llm_condition_marker_dictionary"
_CALLED_FOR = "score_listing_condition"

# Schema bounds. The rubric defines the actual ceiling (5 today);
# this constant pins the tool-schema validation envelope.
_LEVEL_MIN = 1
_LEVEL_MAX = 5

_MARKER_DICTIONARY_PLACEHOLDER = "<MARKER_DICTIONARY>"
_RUBRIC_PLACEHOLDER = "<RUBRIC>"


class ScoringError(RuntimeError):
    """Raised when a score cannot be produced (no listing, no snapshot,
    settings not seeded, LLM refused / malformed output)."""


_REQUIRED_FIELDS: tuple[str, ...] = (
    "building_level", "apartment_level",
    "building_markers_found", "apartment_markers_found",
    "building_confidence", "apartment_confidence",
    "notes",
)


RECORD_LISTING_CONDITION_TOOL: dict[str, Any] = {
    "name": "record_listing_condition",
    "description": (
        "Record per-axis condition levels for one listing. Call exactly "
        "once. Levels are integers in [1, 5] where 5 = excellent. The "
        "scorer system prompt explains the rubric and confidence policy."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "building_level": {
                "type": "integer",
                "minimum": _LEVEL_MIN,
                "maximum": _LEVEL_MAX,
                "description": "Building condition level (1..5).",
            },
            "apartment_level": {
                "type": "integer",
                "minimum": _LEVEL_MIN,
                "maximum": _LEVEL_MAX,
                "description": "Apartment condition level (1..5).",
            },
            "building_markers_found": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Marker IDs (e.g. 'B015') the LLM matched in this listing for the building scope.",
            },
            "apartment_markers_found": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Marker IDs (e.g. 'A005') the LLM matched in this listing for the apartment scope.",
            },
            "building_confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0..1 confidence per the rubric's confidence_policy bands.",
            },
            "apartment_confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0..1 confidence per the rubric's confidence_policy bands.",
            },
            "notes": {
                "type": "string",
                "description": "Free-form 0-300 chars. Empty string when nothing non-obvious to report.",
            },
        },
        "required": list(_REQUIRED_FIELDS),
    },
}


def score_listing_condition(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id: int,
    snapshot_id: int | None = None,
    n_images: int = 0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Score one listing on building + apartment condition axes.

    The cache lookup is per (sreality_id, snapshot_id). If the listing's
    latest snapshot already has a score row, return it immediately. On
    cache miss, call the LLM, write the cache row, and UPDATE the two
    new `listings` columns inside one transaction (with a latest-wins
    guard so a stale-snapshot scorer can't overwrite a fresher score).

    n_images defaults to 0 — text-only — to match how Phase A discovery
    actually ran in production. Caller passes n_images > 0 when R2 is
    configured and vision is wanted.
    """
    from toolkit import _now_iso

    snapshot = _resolve_snapshot(conn, sreality_id, snapshot_id)
    if snapshot is None:
        raise ScoringError(
            f"no snapshot found for sreality_id={sreality_id}"
            + (f", snapshot_id={snapshot_id}" if snapshot_id is not None else "")
        )

    resolved_snapshot_id = snapshot["id"]
    cache_hit = False
    if not force_refresh:
        cached = _cache_lookup(conn, sreality_id, resolved_snapshot_id)
        if cached is not None:
            cache_hit = True
            score = cached
        else:
            score = _produce_score(
                conn, llm_client, sreality_id, snapshot, n_images,
            )
    else:
        score = _produce_score(
            conn, llm_client, sreality_id, snapshot, n_images,
        )

    return {
        "data": {
            "sreality_id": sreality_id,
            "snapshot_id": resolved_snapshot_id,
            "building_level": score["building_level"],
            "apartment_level": score["apartment_level"],
            "building_markers_found": score["building_markers_found"],
            "apartment_markers_found": score["apartment_markers_found"],
            "building_confidence": score["building_confidence"],
            "apartment_confidence": score["apartment_confidence"],
            "notes": score.get("notes", ""),
            "n_images": score.get("n_images", 0),
            "model": score["model"],
            "cost_usd": float(score["cost_usd"]) if score.get("cost_usd") is not None else None,
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "score_listing_condition",
            "filters_used": {
                "sreality_id": sreality_id,
                "snapshot_id": snapshot_id,
                "n_images": n_images,
                "force_refresh": force_refresh,
            },
            "result_count": 1,
            "queried_at": _now_iso(),
            "data_freshness": snapshot["scraped_at"].isoformat(),
        },
    }


def build_scoring_context(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
) -> dict[str, Any]:
    """Resolve the static per-run request context once: the fully built
    system prompt (template + rubric + marker dictionary, ~61KB) and the
    model. The batch submitter passes this to `build_scoring_request` so
    a 2000-listing build doesn't re-read app_settings 2000 times."""
    system_template = llm_client.resolve_system_prompt(_SYSTEM_PROMPT_KEY)
    model = llm_client.resolve_model(_MODEL_KEY)
    rubric = _resolve_jsonb_setting(conn, _RUBRIC_KEY)
    dictionary = _resolve_jsonb_setting(conn, _DICTIONARY_KEY)
    system = _build_system_prompt(system_template, rubric=rubric, dictionary=dictionary)
    return {"system": system, "model": model}


def build_scoring_request(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id: int,
    snapshot: dict[str, Any],
    n_images: int,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the LLM request for one listing's condition score.

    Returns Anthropic-shaped `system` (str) / `messages` / `tools`
    dicts plus the resolved `model` and the effective `n_images`. The
    synchronous path feeds this straight to `LLMClient.call` (which
    accepts dict messages); the batch submitter feeds it to
    `AnthropicProvider.build_batch_request_params`. Keeping one builder
    guarantees both paths share an identical cached system+tools prefix.

    `context` is an optional prebuilt `build_scoring_context` result;
    when omitted the static settings are resolved per call (unchanged
    behaviour for the synchronous callers).
    """
    listing = _fetch_listing(conn, sreality_id)
    text_payload = _build_text_payload(listing, snapshot)
    image_blocks = _build_image_blocks_if_available(conn, sreality_id, n_images)

    if context is None:
        context = build_scoring_context(conn, llm_client)

    return {
        "system": context["system"],
        "messages": [
            {"role": "user", "content": _build_content(text_payload, image_blocks)}
        ],
        "tools": [RECORD_LISTING_CONDITION_TOOL],
        "model": context["model"],
        "n_images": len(image_blocks),
    }


def persist_scoring_result(
    conn: "psycopg.Connection",
    *,
    sreality_id: int,
    snapshot: dict[str, Any],
    parsed: dict[str, Any],
    n_images: int,
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    """Write the cache row + guarded listings UPDATE for a parsed score.

    Shared terminal step for both the synchronous scorer and the batch
    ingester. `parsed` must already be validated via
    `extract_condition_tool_call`.
    """
    _cache_store_and_update_listings(
        conn,
        sreality_id=sreality_id,
        snapshot_id=snapshot["id"],
        snapshot_scraped_at=snapshot["scraped_at"],
        parsed=parsed,
        n_images=n_images,
        model=model,
        llm_call_id=llm_call_id,
        cost_usd=cost_usd,
    )


def propagate_condition_levels(conn: "psycopg.Connection") -> int:
    """Copy genuine condition levels across same-property siblings.

    One set-based, idempotent statement. Source per property = the
    genuinely-scored listing (levels set, `condition_levels_propagated_from`
    IS NULL — i.e. it paid the LLM), preferring the most recent
    `listing_condition_scores.created_at`. Targets = siblings whose levels
    are still NULL or were themselves propagated — a genuine own-score is
    never clobbered — and whose current levels differ from the source's.
    Returns the number of listings updated.
    """
    sql = (
        "WITH source AS ( "
        "  SELECT DISTINCT ON (l.property_id) "
        "         l.property_id, l.sreality_id, "
        "         l.building_condition_level, l.apartment_condition_level "
        "  FROM listings l "
        "  WHERE l.property_id IS NOT NULL "
        "    AND l.condition_levels_propagated_from IS NULL "
        "    AND (l.building_condition_level IS NOT NULL "
        "         OR l.apartment_condition_level IS NOT NULL) "
        "  ORDER BY l.property_id, "
        "           (SELECT MAX(cs.created_at) "
        "            FROM listing_condition_scores cs "
        "            WHERE cs.sreality_id = l.sreality_id) DESC NULLS LAST, "
        "           l.sreality_id "
        ") "
        "UPDATE listings t "
        "SET building_condition_level = s.building_condition_level, "
        "    apartment_condition_level = s.apartment_condition_level, "
        "    condition_levels_propagated_from = s.sreality_id "
        "FROM source s "
        "WHERE t.property_id = s.property_id "
        "  AND t.sreality_id <> s.sreality_id "
        "  AND ( "
        "    (t.building_condition_level IS NULL "
        "     AND t.apartment_condition_level IS NULL) "
        "    OR t.condition_levels_propagated_from IS NOT NULL "
        "  ) "
        "  AND ( "
        "    t.building_condition_level IS DISTINCT FROM s.building_condition_level "
        "    OR t.apartment_condition_level IS DISTINCT FROM s.apartment_condition_level "
        "  )"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.rowcount


def _produce_score(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    sreality_id: int,
    snapshot: dict[str, Any],
    n_images: int,
) -> dict[str, Any]:
    from api.providers.base import ProviderError

    req = build_scoring_request(
        conn, llm_client, sreality_id=sreality_id, snapshot=snapshot, n_images=n_images,
    )
    try:
        response = llm_client.call(
            called_for=_CALLED_FOR,
            messages=req["messages"],
            system=req["system"],
            tools=req["tools"],
            model=req["model"],
        )
    except ProviderError as exc:
        if req["n_images"] and "prompt is too long" in str(exc):
            LOG.warning(
                "score_listing_condition: prompt too long for "
                "sreality_id=%d with %d images; retrying without images",
                sreality_id, req["n_images"],
            )
            req = build_scoring_request(
                conn, llm_client, sreality_id=sreality_id, snapshot=snapshot, n_images=0,
            )
            response = llm_client.call(
                called_for=_CALLED_FOR,
                messages=req["messages"],
                system=req["system"],
                tools=req["tools"],
                model=req["model"],
            )
        else:
            raise

    parsed = extract_condition_tool_call(response.tool_calls)

    persist_scoring_result(
        conn,
        sreality_id=sreality_id,
        snapshot=snapshot,
        parsed=parsed,
        n_images=req["n_images"],
        model=response.model,
        llm_call_id=response.llm_call_id,
        cost_usd=response.cost_usd,
    )

    return {
        **parsed,
        "n_images": req["n_images"],
        "model": response.model,
        "cost_usd": response.cost_usd,
    }


def _resolve_snapshot(
    conn: "psycopg.Connection",
    sreality_id: int,
    snapshot_id: int | None,
) -> dict[str, Any] | None:
    if snapshot_id is not None:
        sql = (
            "SELECT id, scraped_at, raw_json FROM listing_snapshots "
            "WHERE id = %s AND sreality_id = %s"
        )
        params: tuple[Any, ...] = (snapshot_id, sreality_id)
    else:
        sql = (
            "SELECT id, scraped_at, raw_json FROM listing_snapshots "
            "WHERE sreality_id = %s ORDER BY scraped_at DESC LIMIT 1"
        )
        params = (sreality_id,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "scraped_at": row[1], "raw_json": row[2]}


# Public alias for the batch scripts (submit / ingest) which resolve a
# listing's snapshot outside the synchronous scorer.
resolve_snapshot = _resolve_snapshot


def _fetch_listing(
    conn: "psycopg.Connection",
    sreality_id: int,
) -> dict[str, Any]:
    sql = (
        "SELECT category_main, category_type, price_czk, price_unit, "
        "area_m2, disposition, locality, district, floor, total_floors, "
        "has_balcony, has_parking, has_lift, "
        "building_type, condition, energy_rating "
        "FROM listings WHERE sreality_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id,))
        row = cur.fetchone()
    if row is None:
        raise ScoringError(
            f"listing sreality_id={sreality_id} has snapshot but no listings row"
        )
    return {
        "category_main": row[0],
        "category_type": row[1],
        "price_czk": row[2],
        "price_unit": row[3],
        "area_m2": float(row[4]) if row[4] is not None else None,
        "disposition": row[5],
        "locality": row[6],
        "district": row[7],
        "floor": row[8],
        "total_floors": row[9],
        "has_balcony": row[10],
        "has_parking": row[11],
        "has_lift": row[12],
        "building_type": row[13],
        "condition": row[14],
        "energy_rating": row[15],
    }


def _build_text_payload(
    listing: dict[str, Any], snapshot: dict[str, Any],
) -> str:
    raw = snapshot.get("raw_json") or {}
    description = raw.get("text") or ""
    items = raw.get("items") or []
    items_text = "\n".join(
        f"- {it.get('name')}: {it.get('value')}"
        for it in items
        if isinstance(it, dict) and it.get("name")
    )
    structured = "\n".join(
        f"- {k}: {v}"
        for k, v in listing.items()
        if v is not None
    )
    return (
        "Structured fields:\n"
        f"{structured}\n\n"
        "Free-text description (Czech):\n"
        f"{description}\n\n"
        "Other items from the listing page:\n"
        f"{items_text}\n"
    )


def _build_image_blocks_if_available(
    conn: "psycopg.Connection",
    sreality_id: int,
    n_images: int,
) -> list[dict[str, Any]]:
    if n_images <= 0 or not image_storage.is_configured():
        return []
    keys = _fetch_image_keys(conn, sreality_id, n_images)
    if not keys:
        return []
    r2 = image_storage.R2Client.from_env()
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


def _build_content(
    text_payload: str, image_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text_payload}]
    if image_blocks:
        content.append({
            "type": "text",
            "text": f"Listing images ({len(image_blocks)}):",
        })
        content.extend(image_blocks)
    content.append({
        "type": "text",
        "text": (
            "Score building and apartment condition per the rubric in the "
            "system prompt. Emit the marker IDs you matched. Use the "
            "fallback chain when markers are absent."
        ),
    })
    return content


def _build_system_prompt(
    template: str,
    *,
    rubric: dict[str, Any],
    dictionary: dict[str, Any],
) -> str:
    """Substitute the rubric + marker dictionary placeholders.

    Both are injected as compact JSON. The system prompt template
    references them by the literal placeholder strings
    `<MARKER_DICTIONARY>` and `<RUBRIC>` so the operator can re-position
    them via the Settings UI without code changes.
    """
    rubric_json = json.dumps(rubric, ensure_ascii=False, separators=(",", ":"))
    compact_dictionary = _compact_dictionary(dictionary)
    dictionary_json = json.dumps(compact_dictionary, ensure_ascii=False, separators=(",", ":"))
    out = template
    if _RUBRIC_PLACEHOLDER in out:
        out = out.replace(_RUBRIC_PLACEHOLDER, rubric_json)
    else:
        out = out + "\n\nRubric:\n" + rubric_json
    if _MARKER_DICTIONARY_PLACEHOLDER in out:
        out = out.replace(_MARKER_DICTIONARY_PLACEHOLDER, dictionary_json)
    else:
        out = out + "\n\nMarker dictionary:\n" + dictionary_json
    return out


def _compact_dictionary(dictionary: dict[str, Any]) -> dict[str, Any]:
    """Strip noisy fields the LLM doesn't need at scoring time.

    The full curated dictionary carries per-cluster `count`,
    `sentiment_counts`, `level_hint_counts`, `source_counts`, and
    `examples` — those are useful for human review but waste tokens
    on every scoring call. The scorer only needs: marker_id, canonical
    phrase, sentiment_majority, level_hint_majority, and variants
    (so it can recognise alternate phrasings).
    """
    keep_fields = ("marker_id", "canonical", "sentiment_majority",
                   "level_hint_majority", "variants")
    out: dict[str, Any] = {"schema_version": dictionary.get("schema_version", 1)}
    for scope in ("building", "apartment"):
        out[scope] = [
            {k: c.get(k) for k in keep_fields if k in c}
            for c in dictionary.get(scope, [])
        ]
    return out


def _resolve_jsonb_setting(
    conn: "psycopg.Connection",
    key: str,
) -> dict[str, Any]:
    sql = "SELECT value FROM app_settings WHERE key = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (key,))
        row = cur.fetchone()
    if row is None:
        raise ScoringError(
            f"app_settings.{key} missing — apply migration 072 "
            f"and run scripts/seed_condition_settings.py"
        )
    value = row[0]
    if not isinstance(value, dict) or not value:
        raise ScoringError(
            f"app_settings.{key} is empty ({{}}). Run "
            f"`python -m scripts.seed_condition_settings` to populate it "
            f"from data/condition_rubric_v1.json + "
            f"data/condition_markers_curated.json."
        )
    return value


def extract_condition_tool_call(
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    matching = [
        tc for tc in tool_calls
        if tc.get("name") == "record_listing_condition"
    ]
    if not matching:
        raise ScoringError(
            "LLM did not invoke record_listing_condition; refusing to guess"
        )
    if len(matching) > 1:
        raise ScoringError(
            "LLM invoked record_listing_condition more than once"
        )
    payload = matching[0].get("input") or {}
    if not isinstance(payload, dict):
        raise ScoringError("record_listing_condition input was not an object")
    for key in _REQUIRED_FIELDS:
        if key not in payload:
            raise ScoringError(
                f"record_listing_condition missing field: {key}"
            )

    for field in ("building_level", "apartment_level"):
        if not isinstance(payload[field], int):
            raise ScoringError(f"{field} must be an integer, got {type(payload[field]).__name__}")
        if not (_LEVEL_MIN <= payload[field] <= _LEVEL_MAX):
            raise ScoringError(
                f"{field}={payload[field]} out of range [{_LEVEL_MIN}, {_LEVEL_MAX}]"
            )

    for field in ("building_confidence", "apartment_confidence"):
        v = payload[field]
        if not isinstance(v, (int, float)):
            raise ScoringError(f"{field} must be numeric, got {type(v).__name__}")
        if not (0.0 <= float(v) <= 1.0):
            raise ScoringError(f"{field}={v} out of range [0.0, 1.0]")

    for field in ("building_markers_found", "apartment_markers_found"):
        if not isinstance(payload[field], list):
            raise ScoringError(f"{field} must be a list")
        for m in payload[field]:
            if not isinstance(m, str):
                raise ScoringError(f"{field} entries must be strings (marker_ids)")

    return dict(payload)


def _cache_lookup(
    conn: "psycopg.Connection",
    sreality_id: int,
    snapshot_id: int,
) -> dict[str, Any] | None:
    sql = (
        "SELECT building_level, apartment_level, "
        "       building_markers_found, apartment_markers_found, "
        "       building_confidence, apartment_confidence, "
        "       notes, n_images, model, cost_usd "
        "FROM listing_condition_scores "
        "WHERE sreality_id = %s AND snapshot_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id, snapshot_id))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "building_level": row[0],
        "apartment_level": row[1],
        "building_markers_found": row[2] or [],
        "apartment_markers_found": row[3] or [],
        "building_confidence": float(row[4]) if row[4] is not None else None,
        "apartment_confidence": float(row[5]) if row[5] is not None else None,
        "notes": row[6] or "",
        "n_images": row[7],
        "model": row[8],
        "cost_usd": float(row[9]) if row[9] is not None else None,
    }


def _cache_store_and_update_listings(
    conn: "psycopg.Connection",
    *,
    sreality_id: int,
    snapshot_id: int,
    snapshot_scraped_at: Any,
    parsed: dict[str, Any],
    n_images: int,
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    """Atomic cache write + guarded `listings` UPDATE.

    The guard subquery enforces the latest-wins invariant (CLAUDE.md
    rule #8): if a fresher snapshot has been recorded between when we
    started scoring and when we go to UPDATE, leave listings.* alone
    — that fresher snapshot's score will eventually overwrite ours
    when its own scoring run lands.

    An own score also clears `condition_levels_propagated_from`: a
    genuine score supersedes any sibling-propagated copy.
    """
    insert_sql = (
        "INSERT INTO listing_condition_scores "
        "(sreality_id, snapshot_id, "
        " building_level, apartment_level, "
        " building_markers_found, apartment_markers_found, "
        " building_confidence, apartment_confidence, "
        " notes, n_images, model, llm_call_id, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (sreality_id, snapshot_id) DO UPDATE SET "
        " building_level = EXCLUDED.building_level, "
        " apartment_level = EXCLUDED.apartment_level, "
        " building_markers_found = EXCLUDED.building_markers_found, "
        " apartment_markers_found = EXCLUDED.apartment_markers_found, "
        " building_confidence = EXCLUDED.building_confidence, "
        " apartment_confidence = EXCLUDED.apartment_confidence, "
        " notes = EXCLUDED.notes, "
        " n_images = EXCLUDED.n_images, "
        " model = EXCLUDED.model, "
        " llm_call_id = EXCLUDED.llm_call_id, "
        " cost_usd = EXCLUDED.cost_usd, "
        " created_at = now()"
    )
    update_sql = (
        "UPDATE listings "
        "SET building_condition_level = %s, "
        "    apartment_condition_level = %s, "
        "    condition_levels_propagated_from = NULL "
        "WHERE sreality_id = %s "
        "  AND (SELECT scraped_at FROM listing_snapshots "
        "       WHERE id = %s) "
        "      >= COALESCE("
        "        (SELECT MAX(scraped_at) FROM listing_snapshots "
        "         WHERE sreality_id = listings.sreality_id), 'epoch'::timestamptz)"
    )

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            insert_sql,
            (
                sreality_id, snapshot_id,
                parsed["building_level"], parsed["apartment_level"],
                _Jsonb(parsed["building_markers_found"]),
                _Jsonb(parsed["apartment_markers_found"]),
                parsed["building_confidence"], parsed["apartment_confidence"],
                parsed.get("notes", ""), n_images, model, llm_call_id, cost_usd,
            ),
        )
        cur.execute(
            update_sql,
            (
                parsed["building_level"], parsed["apartment_level"],
                sreality_id, snapshot_id,
            ),
        )
