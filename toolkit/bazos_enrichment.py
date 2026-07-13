"""Fill a listing's missing typed attributes from its free-text description.

Description-only portals (bazos today) carry no structured floor / amenities /
condition / building_type / energy — only price, area, disposition, coords, and
the seller's text. A slim 8-field variant of the per-source-parser
`record_listing` tool (ONLY the gap fields `_FIELD_MAP` consumes — the full
RECORD_LISTING_TOOL forced a verbatim description echo plus 10 unconsumed
fields, ~2.5x the output tokens and routine truncation) extracts those typed
fields from the description with a cheap model (Haiku, tool call FORCED via
tool_choice so a prose answer can't burn the call), caches the extraction in
`listing_description_enrichments` (keyed `(sreality_id, snapshot_id, model)`
so a new snapshot OR a model upgrade auto-invalidates; a no-extraction miss is
cached too, else it re-bills every run forever), and fills ONLY
the listings columns that are currently NULL — the deterministic HTML-parsed
fields (price / area / disposition) are authoritative and never overwritten.

Write-allowed exception (CLAUDE.md toolkit rule #5): caches an LLM fact and fills
gap columns; the LLM is the source, the table is the mirror. Only high/medium
confidence values are written — `low` is treated as a guess and dropped.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Callable
from unicodedata import combining, normalize

from scraper.floor import is_plausible_floor

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

    from api.llm_client import LLMClient

CALLED_FOR = "enrich_listing_description"
DEFAULT_MODEL = "claude-haiku-4-5"
ENRICHMENT_MODEL_KEY = "enrichment_model"


def resolve_enrichment_model(conn: "psycopg.Connection") -> str:
    """The operator-set enrichment model (app_settings.enrichment_model), else the
    Haiku default. A published setting: absent -> Haiku (today's behaviour); set it
    to e.g. gpt-5-mini and the provider is derived from the id at call/submit time."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s", (ENRICHMENT_MODEL_KEY,)
        )
        row = cur.fetchone()
    if row and isinstance(row[0], str):
        return row[0]
    return DEFAULT_MODEL
_ACCEPT_CONFIDENCE = frozenset({"high", "medium"})

_SYSTEM = (
    "You extract structured Czech real-estate attributes from a property "
    "listing's free-text description. Call record_listing exactly once. Set a "
    "field's value ONLY when the description explicitly states or unambiguously "
    "implies it; otherwise use null. Use confidence 'high' for explicit "
    "statements, 'medium' for strong implications, 'low' for guesses. Do not "
    "invent values. Czech terms: výtah=lift, balkón/lodžie/terasa=balcony, "
    "garáž/parkování/stání=parking, cihla/panel=building material, "
    "po rekonstrukci/novostavba/před rekonstrukcí=condition. floor is the "
    "APARTMENT's own storey (e.g. 've 3. patře', '3. NP'), NEVER the building's "
    "total number of floors ('třípodlažní dům' / 'z celkových 5 pater')."
)


def _strip_diacritics(text: str) -> str:
    return "".join(c for c in normalize("NFD", text) if not combining(c))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _norm_building_type(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _strip_diacritics(value.strip().lower()) or None


def _norm_condition(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    key = _strip_diacritics(value).lower().strip()
    key = re.sub(r"\s+stav$", "", key)            # "velmi dobry stav" -> "velmi dobry"
    return re.sub(r"\s+", "_", key) or None        # canonical: "velmi_dobry"


def _norm_energy(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*([A-Ga-g])\b", value)
    return m.group(1).upper() if m else None


def _envelope(value_type: list[str], description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "value": {"type": value_type, "description": description},
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
        },
        "required": ["value", "confidence"],
    }


# Slim extraction tool: ONLY the 8 gap fields _FIELD_MAP consumes, same
# {value, confidence} envelopes and field semantics as the full
# RECORD_LISTING_TOOL (scraper/source_parsers/common.py) minus the verbatim
# description echo and the deterministic-from-HTML fields the enricher drops.
ENRICH_LISTING_TOOL: dict[str, Any] = {
    "name": "record_listing",
    "description": (
        "Record the structured attributes extracted from the listing "
        "description. Call exactly once. Every field uses the "
        "{value, confidence} envelope; use null for value when the text "
        "does not state it."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "floor": _envelope(["integer", "null"],
                "Floor number, ground = 0, suterén = -1."),
            "total_floors": _envelope(["integer", "null"],
                "Total floors in the building."),
            "has_balcony": _envelope(["boolean", "null"],
                "True if balcony, loggia, or terrace mentioned."),
            "has_lift": _envelope(["boolean", "null"], "Elevator / výtah."),
            "has_parking": _envelope(["boolean", "null"],
                "Garage, parking lot, or parkovací stání."),
            "building_type": _envelope(["string", "null"],
                "cihla | panel | smisena | skelet | drevo | kamen | "
                "montovana | nizkoenergeticka."),
            "condition": _envelope(["string", "null"],
                "novostavba | po rekonstrukci | velmi dobrý stav | "
                "dobrý stav | před rekonstrukcí | ve výstavbě | k demolici."),
            "energy_rating": _envelope(["string", "null"],
                "Single capital letter A through G."),
        },
        "required": [
            "floor", "total_floors", "has_balcony", "has_lift",
            "has_parking", "building_type", "condition", "energy_rating",
        ],
    },
}


# record_listing field -> (listings column, transform). ONLY the gap fields a
# description-only portal lacks; price/area/disposition/locality/category are
# deterministic from the HTML and intentionally excluded.
_FIELD_MAP: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "floor": ("floor", _as_int),
    "total_floors": ("total_floors", _as_int),
    "has_balcony": ("has_balcony", _as_bool),
    "has_lift": ("has_lift", _as_bool),
    "has_parking": ("has_parking", _as_bool),
    "building_type": ("building_type", _norm_building_type),
    "condition": ("condition", _norm_condition),
    "energy_rating": ("energy_rating", _norm_energy),
}


def columns_from_extraction(
    extraction: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """The listings columns to UPDATE from a record_listing extraction.

    Fills a column only when: the field is a known gap field, its value is
    non-null at high/medium confidence, it transforms to a usable value, AND
    the column is currently NULL (never overwrite an authoritative value).
    """
    out: dict[str, Any] = {}
    for field, (col, transform) in _FIELD_MAP.items():
        if current.get(col) is not None:
            continue
        env = extraction.get(field)
        if not isinstance(env, dict) or env.get("confidence") not in _ACCEPT_CONFIDENCE:
            continue
        value = env.get("value")
        if value is None:
            continue
        transformed = transform(value)
        if transformed is not None:
            out[col] = transformed
    # Plausibility: an apartment's storey can't exceed the building's total and
    # lives in a sane band — drop a floor that violates either (usually a misread
    # building-total number). The total may come from this same extraction or the
    # already-stored column. One shared guard with the deterministic miner.
    if "floor" in out:
        total = out.get("total_floors")
        if total is None:
            total = current.get("total_floors")
        if not is_plausible_floor(out["floor"], total):
            del out["floor"]
    return out


_SELECT_TARGET = (
    "WITH latest AS ("
    "  SELECT sreality_id, MAX(id) AS snapshot_id "
    "  FROM listing_snapshots WHERE sreality_id = %s GROUP BY sreality_id) "
    "SELECT ls.snapshot_id, l.description, l.floor, l.total_floors, "
    "       l.has_balcony, l.has_lift, l.has_parking, l.building_type, "
    "       l.condition, l.energy_rating "
    "FROM listings l JOIN latest ls ON ls.sreality_id = l.sreality_id "
    "WHERE l.sreality_id = %s"
)

_TARGET_COLS = (
    "floor", "total_floors", "has_balcony", "has_lift", "has_parking",
    "building_type", "condition", "energy_rating",
)


def resolve_current(
    conn: "psycopg.Connection", sreality_id: int, snapshot_id: int,
) -> dict[str, Any] | None:
    """Fetch this listing's CURRENT (ingest-time-fresh) gap columns, but only
    when `snapshot_id` still matches its latest snapshot.

    Public alias for `scripts.ingest_enrich_batch`, which resolves state
    fresh — not the possibly-hours-stale `current` captured at submit time —
    right before writing. An intervening scrape may have replaced the
    description a batch result was extracted from (a new latest snapshot) or
    already filled a gap column deterministically; either way the extraction
    is stale and the caller should skip persisting it. Mirrors
    `toolkit.condition_scoring.resolve_snapshot`'s same-purpose guard.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_TARGET, (sreality_id, sreality_id))
        row = cur.fetchone()
    if row is None or int(row[0]) != snapshot_id:
        return None
    return dict(zip(_TARGET_COLS, row[2:]))


def _select_and_check(
    conn: "psycopg.Connection", sreality_id: int, *, model: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Select the target row and check the cache — the single place that
    runs `_SELECT_TARGET` + the cache lookup, shared by `build_enrich_request`
    (batch path) and `enrich_listing_description` (sync path) so neither
    duplicates the SQL.

    Returns `(skip_status, None)` when there's nothing to enrich (row
    missing / no description / already cached for this model), or
    `(None, request)` when the caller should make the LLM call.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_TARGET, (sreality_id, sreality_id))
        row = cur.fetchone()
    if row is None:
        return "not_found", None
    snapshot_id, description = row[0], row[1]
    current = dict(zip(_TARGET_COLS, row[2:]))
    if not description or not description.strip():
        return "no_description", None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM listing_description_enrichments "
            "WHERE sreality_id = %s AND snapshot_id = %s AND model = %s",
            (sreality_id, snapshot_id, model),
        )
        if cur.fetchone() is not None:
            return "cached", None

    return None, {
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": description[:8000]}],
        "tools": [ENRICH_LISTING_TOOL],
        "tool_choice": "record_listing",
        "model": model,
        "max_tokens": 512,
        "snapshot_id": snapshot_id,
        "current": current,
    }


def build_enrich_request(
    conn: "psycopg.Connection", sreality_id: int, *, model: str,
) -> dict[str, Any] | None:
    """Select + cache-check for one listing — the pure-ish request builder
    the sync path and `scripts.submit_enrich_batch` both call, so the two
    paths never diverge (mirrors `toolkit.condition_scoring.build_scoring_request`).

    Returns `None` when there's nothing to enrich (not found / no
    description / already cached for this model) — the caller just skips.
    No LLM call happens here; the returned dict carries `system` /
    `messages` / `tools` / `tool_choice` / `model` / `max_tokens` for the
    call plus `snapshot_id` / `current` for `persist_enrich_result`.
    """
    _, req = _select_and_check(conn, sreality_id, model=model)
    return req


def persist_enrich_result(
    conn: "psycopg.Connection",
    *,
    sreality_id: int,
    snapshot_id: int,
    current: dict[str, Any],
    extraction: Any,
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> dict[str, Any]:
    """Write path shared by the sync scorer and the batch ingester.

    `extraction` is whatever the caller pulled off the `record_listing`
    tool call's `input` (or `None`/non-dict when the model didn't produce
    one). A non-dict extraction negative-caches the miss so the selector
    (keyed on row existence) doesn't re-bill this listing forever; a dict
    extraction fills the listings gap columns + caches it. Returns the
    same status dict shape `enrich_listing_description` has always returned.
    """
    if not isinstance(extraction, dict):
        # Negative-cache the miss: the selector keys on row existence, so
        # without a row this listing would re-bill on every run forever.
        # tool_choice makes a prose answer near-impossible, but a truncated
        # response must still not become a permanent re-bill loop.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO listing_description_enrichments "
                "(sreality_id, snapshot_id, extracted, filled, model, llm_call_id, cost_usd) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (sreality_id, snapshot_id, json.dumps({"no_extraction": True}),
                 json.dumps({}), model, llm_call_id, cost_usd),
            )
        conn.commit()
        return {"status": "no_extraction", "sreality_id": sreality_id,
                "llm_call_id": llm_call_id}

    columns = columns_from_extraction(extraction, current)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO listing_description_enrichments "
            "(sreality_id, snapshot_id, extracted, filled, model, llm_call_id, cost_usd) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            # Targetless (not `(sid, snapshot, model)`): the only conflictable
            # constraint is the cache key (id is GENERATED ALWAYS), so this is
            # equivalent AND survives migration 249's constraint swap regardless of
            # apply-vs-deploy order — no fragile lockstep.
            "ON CONFLICT DO NOTHING",
            (sreality_id, snapshot_id, json.dumps(extraction), json.dumps(columns),
             model, llm_call_id, cost_usd),
        )
        if columns:
            sets = ", ".join(f"{col} = %s" for col in columns)
            cur.execute(
                f"UPDATE listings SET {sets} WHERE sreality_id = %s",
                (*columns.values(), sreality_id),
            )
    conn.commit()
    return {
        "status": "ok",
        "sreality_id": sreality_id,
        "filled": sorted(columns),
        "cost_usd": cost_usd,
        "llm_call_id": llm_call_id,
    }


def enrich_listing_description(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    sreality_id: int,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Extract typed attributes from one listing's description and fill gaps.

    Returns a status dict. No-op (no LLM cost) when the listing has no
    description or its latest snapshot is already enriched by this `model`
    (the cache is keyed `(sreality_id, snapshot_id, model)`, migration 249, so a
    model upgrade re-attempts every listing rather than reusing an older miss).

    Thin orchestration over `build_enrich_request` (select + cache gate) and
    `persist_enrich_result` (the write path) — the same two functions
    `scripts.submit_enrich_batch` / `scripts.ingest_enrich_batch` call, so
    the sync and batch paths share every line of selection/write logic.
    """
    model = model or DEFAULT_MODEL
    skip_status, req = _select_and_check(conn, sreality_id, model=model)
    if req is None:
        return {"status": skip_status, "sreality_id": sreality_id}

    resp = llm_client.call(
        called_for=CALLED_FOR,
        system=req["system"],
        messages=req["messages"],
        tools=req["tools"],
        tool_choice=req["tool_choice"],
        model=req["model"],
        max_tokens=req["max_tokens"],
    )
    extraction = next(
        (tc["input"] for tc in resp.tool_calls if tc.get("name") == "record_listing"),
        None,
    )
    return persist_enrich_result(
        conn,
        sreality_id=sreality_id,
        snapshot_id=req["snapshot_id"],
        current=req["current"],
        extraction=extraction,
        model=model,
        llm_call_id=resp.llm_call_id,
        cost_usd=resp.cost_usd,
    )
