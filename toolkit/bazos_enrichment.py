"""Fill a listing's missing typed attributes from its free-text description.

Description-only portals (bazos today) carry no structured floor / amenities /
condition / building_type / energy — only price, area, disposition, coords, and
the seller's text. This reuses the per-source-parser `RECORD_LISTING_TOOL` to
extract those typed fields from the description with a cheap model (Haiku),
caches the extraction in `listing_description_enrichments` (keyed
`(sreality_id, snapshot_id, model)` so a new snapshot OR a model upgrade
auto-invalidates), and fills ONLY
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
from scraper.source_parsers.common import RECORD_LISTING_TOOL

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

    from api.llm_client import LLMClient

CALLED_FOR = "enrich_listing_description"
DEFAULT_MODEL = "claude-haiku-4-5"
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
    """
    model = model or DEFAULT_MODEL
    with conn.cursor() as cur:
        cur.execute(_SELECT_TARGET, (sreality_id, sreality_id))
        row = cur.fetchone()
    if row is None:
        return {"status": "not_found", "sreality_id": sreality_id}
    snapshot_id, description = row[0], row[1]
    current = dict(zip(_TARGET_COLS, row[2:]))
    if not description or not description.strip():
        return {"status": "no_description", "sreality_id": sreality_id}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM listing_description_enrichments "
            "WHERE sreality_id = %s AND snapshot_id = %s AND model = %s",
            (sreality_id, snapshot_id, model),
        )
        if cur.fetchone() is not None:
            return {"status": "cached", "sreality_id": sreality_id}

    resp = llm_client.call(
        called_for=CALLED_FOR,
        system=_SYSTEM,
        messages=[{"role": "user", "content": description[:8000]}],
        tools=[RECORD_LISTING_TOOL],
        model=model,
        max_tokens=1024,
    )
    extraction = next(
        (tc["input"] for tc in resp.tool_calls if tc.get("name") == "record_listing"),
        None,
    )
    if not isinstance(extraction, dict):
        return {"status": "no_extraction", "sreality_id": sreality_id,
                "llm_call_id": resp.llm_call_id}

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
             model, resp.llm_call_id, resp.cost_usd),
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
        "cost_usd": resp.cost_usd,
        "llm_call_id": resp.llm_call_id,
    }
