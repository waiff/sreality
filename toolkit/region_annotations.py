"""summarize_region_dispositions: natural-language annotations for the
per-disposition Kč/m² box plots in Browse > Stats.

For each disposition the chart already draws (1+kk, 2+kk, ...) we ask
Claude for a one-to-two-sentence description of that disposition's
price-per-m² distribution — where it clusters, how wide it is, what the
whiskers reveal. The annotation is a FACT about the distribution, never a
price recommendation (CLAUDE.md toolkit rule #1).

Input is the same `ppm2_box` payload that drives DispositionBoxPlots, so
the annotation can never disagree with the chart. The cohort-wide
percentiles ride along as context for cross-disposition comparisons.

The caller also supplies `ppm2_basis`, the unit those numbers are in
(migration 425's four-token vocabulary). A `'mixed'` cohort — rule 22 puts one
one click away, since the default Browse cohort is sale + rent — is REFUSED
outright: its box plot stacks monthly rents on top of purchase prices, so there
is no factual sentence to write about it and no unit to name. No LLM call, no
cache write, an explicit note instead.

The basis IS part of the cache key. The SPA sends the basis
`browse_stats_properties` resolved from the cohort's own rows, so one
`region_key` can see it change within a day (a cohort that was single-basis at
09:00 gains one rental row and reads `mixed` at 16:00), and a second caller
would otherwise get the first caller's text under its own unit. Hashing the
basis alongside the key costs at most one extra miss per basis per region per
day and closes that class outright; a caller that sends no basis still hashes
to the pre-basis value, so existing cache rows keep hitting.

Cache lives in `region_disposition_annotations`, keyed on
(region_hash, day): a region's annotations are generated once per calendar
day so repeat browser sessions don't re-bill the API. region_hash is the
sha256 of `region_key` (plus the basis, when one is sent), the caller's
deterministic serialization of the active Browse filter set. The next day's first view regenerates, picking
up the day's data drift.

Write-allowed exception per CLAUDE.md toolkit rule #5: same rationale as
`summarize_listing` — the LLM is the source of truth, we cache locally to
keep repeat lookups fast and Anthropic-friendly.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from toolkit.measures import BASIS_MIXED, unit_label

try:
    from psycopg.types.json import Jsonb as _Jsonb
except ImportError:
    def _Jsonb(value: Any) -> Any:  # type: ignore[misc]
        return value

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient


_SYSTEM_PROMPT_KEY = "llm_region_annotation_system_prompt"
_MODEL_KEY = "llm_region_annotation_model"
_CALLED_FOR = "summarize_region_dispositions"

# Matches DispositionBoxPlots' MIN_BOX_N: the chart only draws a box when
# a disposition has at least this many priced+sized listings, so we only
# annotate the same set.
DEFAULT_MIN_BOX_N = 5

_BOX_KEYS = ("n", "min", "p25", "median", "p75", "max")


class RegionAnnotationError(RuntimeError):
    """Raised when annotations cannot be produced (LLM refused / bad output)."""


RECORD_DISPOSITION_ANNOTATIONS_TOOL: dict[str, Any] = {
    "name": "record_disposition_annotations",
    "description": (
        "Record the per-disposition box-plot annotations. Call exactly "
        "once with one entry per disposition you were given. Each text is "
        "a 1-2 sentence factual description of that disposition's price-"
        "per-m² distribution — never a price recommendation."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "annotations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "disposition": {
                            "type": "string",
                            "description": "The disposition label exactly as given (e.g. '2+kk').",
                        },
                        "text": {
                            "type": "string",
                            "description": "1-2 sentences (max ~280 chars) describing the distribution's shape.",
                        },
                    },
                    "required": ["disposition", "text"],
                },
                "minItems": 0,
            },
        },
        "required": ["annotations"],
    },
}


def summarize_region_dispositions(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    region_key: str,
    dispositions: list[dict[str, Any]],
    ppm2_overall: dict[str, Any] | None = None,
    region_label: str | None = None,
    ppm2_basis: str | None = None,
    min_box_n: int = DEFAULT_MIN_BOX_N,
    force_refresh: bool = False,
) -> dict[str, Any]:
    from toolkit import _now_iso

    renderable = _renderable_dispositions(dispositions, min_box_n)
    # The basis is part of the cache identity: one region_key could in principle
    # be summarized on two different bases, and a cached sentence about capital
    # Kc/m2 is false about a monthly one.
    region_hash = _region_hash(region_key, ppm2_basis)

    # Nothing to annotate: no LLM call, no cache write.
    if not renderable:
        return _envelope(
            region_key=region_key,
            annotations={},
            model="",
            cost_usd=None,
            cache_hit=False,
            min_box_n=min_box_n,
            ppm2_basis=ppm2_basis,
            force_refresh=force_refresh,
            now_iso=_now_iso(),
        )

    # A mixed cohort has no unit, so it has no describable distribution: its
    # boxes are monthly rents and purchase prices in one axis. Refuse rather
    # than hand the model numbers whose meaning changes row to row.
    if ppm2_basis == BASIS_MIXED:
        return _envelope(
            region_key=region_key,
            annotations={},
            model="",
            cost_usd=None,
            cache_hit=False,
            min_box_n=min_box_n,
            ppm2_basis=ppm2_basis,
            force_refresh=force_refresh,
            now_iso=_now_iso(),
            notes=[
                "cohort mixes sale and rental listings, so its Kč/m² figures "
                "have no single unit; refusing to characterise it"
            ],
        )

    cache_hit = False
    if not force_refresh:
        cached = _cache_lookup(conn, region_hash)
        if cached is not None:
            cache_hit = True
            annotations = cached["annotations"]
            model = cached["model"]
            cost_usd = cached["cost_usd"]
        else:
            annotations, model, cost_usd = _produce_annotations(
                conn, llm_client,
                region_key=region_key,
                region_hash=region_hash,
                region_label=region_label,
                renderable=renderable,
                ppm2_overall=ppm2_overall,
                ppm2_basis=ppm2_basis,
            )
    else:
        annotations, model, cost_usd = _produce_annotations(
            conn, llm_client,
            region_key=region_key,
            region_hash=region_hash,
            region_label=region_label,
            renderable=renderable,
            ppm2_overall=ppm2_overall,
            ppm2_basis=ppm2_basis,
        )

    return _envelope(
        region_key=region_key,
        annotations=annotations,
        model=model,
        cost_usd=cost_usd,
        cache_hit=cache_hit,
        min_box_n=min_box_n,
        ppm2_basis=ppm2_basis,
        force_refresh=force_refresh,
        now_iso=_now_iso(),
    )


def _produce_annotations(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    region_key: str,
    region_hash: str,
    region_label: str | None,
    renderable: list[dict[str, Any]],
    ppm2_overall: dict[str, Any] | None,
    ppm2_basis: str | None,
) -> tuple[dict[str, str], str, float | None]:
    payload = _build_payload(region_label, renderable, ppm2_overall, ppm2_basis)

    system = llm_client.resolve_system_prompt(_SYSTEM_PROMPT_KEY)
    model = llm_client.resolve_model(_MODEL_KEY)

    response = llm_client.call(
        called_for=_CALLED_FOR,
        messages=[{"role": "user", "content": payload}],
        system=system,
        tools=[RECORD_DISPOSITION_ANNOTATIONS_TOOL],
        model=model,
    )

    allowed = {r["disposition"] for r in renderable}
    annotations = _extract_tool_call(response.tool_calls, allowed)

    _cache_store(
        conn,
        region_hash=region_hash,
        region_key=region_key,
        annotations=annotations,
        model=response.model,
        llm_call_id=response.llm_call_id,
        cost_usd=response.cost_usd,
    )
    return annotations, response.model, response.cost_usd


def _renderable_dispositions(
    dispositions: list[dict[str, Any]],
    min_box_n: int,
) -> list[dict[str, Any]]:
    """Keep only dispositions with a usable box (n >= min_box_n), mirroring
    the chart's render gate so the annotation set matches the visible boxes."""
    out: list[dict[str, Any]] = []
    for d in dispositions or []:
        disposition = d.get("disposition")
        box = d.get("ppm2_box")
        if not disposition or not isinstance(box, dict):
            continue
        if not all(box.get(k) is not None for k in _BOX_KEYS):
            continue
        if int(box["n"]) < min_box_n:
            continue
        out.append({"disposition": str(disposition), "box": box})
    return out


def _build_payload(
    region_label: str | None,
    renderable: list[dict[str, Any]],
    ppm2_overall: dict[str, Any] | None,
    ppm2_basis: str | None = None,
) -> str:
    # The unit is STATED, not implied. `unit_label` is the one place the three
    # strings live; an absent or unrecognised basis renders no unit at all and
    # the prompt is told so explicitly, which is what it already did implicitly
    # ("do not assume which") — only now the caller can remove the ambiguity.
    unit = unit_label(ppm2_basis)
    unit_sfx = f" ({unit})" if unit else ""
    lines: list[str] = []
    lines.append(f"Region: {region_label or '(filtered cohort)'}")
    lines.append(
        f"Price-per-m² basis: {ppm2_basis} — unit is {unit}"
        if unit
        else "Price-per-m² basis: not supplied — the unit of every number below "
             "is UNKNOWN. Do not name a unit and do not assume sale or rental."
    )
    if ppm2_overall:
        p25 = ppm2_overall.get("p25")
        p50 = ppm2_overall.get("p50")
        p75 = ppm2_overall.get("p75")
        if p50 is not None:
            lines.append(
                f"Cohort-wide price per m²{unit_sfx}: "
                f"p25={p25} median={p50} p75={p75}"
            )
    lines.append("")
    lines.append(f"Per-disposition price-per-m² box statistics{unit_sfx}:")
    for r in renderable:
        b = r["box"]
        lines.append(
            f"- {r['disposition']} (n={b['n']}): "
            f"min={b['min']} p25={b['p25']} median={b['median']} "
            f"p75={b['p75']} max={b['max']}"
        )
    return "\n".join(lines)


def _extract_tool_call(
    tool_calls: list[dict[str, Any]],
    allowed: set[str],
) -> dict[str, str]:
    matching = [
        tc for tc in tool_calls
        if tc.get("name") == "record_disposition_annotations"
    ]
    if not matching:
        raise RegionAnnotationError(
            "LLM did not invoke record_disposition_annotations; refusing to guess"
        )
    if len(matching) > 1:
        raise RegionAnnotationError(
            "LLM invoked record_disposition_annotations more than once"
        )
    payload = matching[0].get("input") or {}
    if not isinstance(payload, dict):
        raise RegionAnnotationError("record_disposition_annotations input was not an object")
    entries = payload.get("annotations")
    if not isinstance(entries, list):
        raise RegionAnnotationError("record_disposition_annotations.annotations was not a list")

    out: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        disposition = entry.get("disposition")
        text = entry.get("text")
        if not isinstance(disposition, str) or not isinstance(text, str):
            continue
        if disposition not in allowed:
            continue
        stripped = text.strip()
        if stripped:
            out[disposition] = stripped
    return out


def _region_hash(region_key: str, ppm2_basis: str | None = None) -> str:
    # No basis -> the pre-basis hash, byte for byte, so an existing cache row
    # and any caller that does not send one keep resolving to the same entry.
    material = region_key if not ppm2_basis else f"{region_key}\x1f{ppm2_basis}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cache_lookup(
    conn: "psycopg.Connection",
    region_hash: str,
) -> dict[str, Any] | None:
    sql = (
        "SELECT annotations, model, cost_usd "
        "FROM region_disposition_annotations "
        "WHERE region_hash = %s AND day = current_date"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (region_hash,))
        row = cur.fetchone()
    if row is None:
        return None
    annotations = row[0]
    if not isinstance(annotations, dict):
        return None
    return {
        "annotations": {str(k): str(v) for k, v in annotations.items()},
        "model": row[1],
        "cost_usd": float(row[2]) if row[2] is not None else None,
    }


def _cache_store(
    conn: "psycopg.Connection",
    *,
    region_hash: str,
    region_key: str,
    annotations: dict[str, str],
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    sql = (
        "INSERT INTO region_disposition_annotations "
        "(region_hash, region_key, annotations, model, llm_call_id, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (region_hash, day) DO UPDATE SET "
        " region_key = EXCLUDED.region_key, "
        " annotations = EXCLUDED.annotations, "
        " model = EXCLUDED.model, "
        " llm_call_id = EXCLUDED.llm_call_id, "
        " cost_usd = EXCLUDED.cost_usd, "
        " created_at = now()"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            sql,
            (
                region_hash, region_key, _Jsonb(annotations),
                model, llm_call_id, cost_usd,
            ),
        )


def _envelope(
    *,
    region_key: str,
    annotations: dict[str, str],
    model: str,
    cost_usd: float | None,
    cache_hit: bool,
    min_box_n: int,
    force_refresh: bool,
    now_iso: str,
    ppm2_basis: str | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "data": {
            "region_key": region_key,
            "annotations": annotations,
            "model": model,
            "cost_usd": float(cost_usd) if cost_usd is not None else None,
            "cache_hit": cache_hit,
            "ppm2_basis": ppm2_basis,
            "ppm2_unit": unit_label(ppm2_basis),
        },
        "metadata": {
            "tool": "summarize_region_dispositions",
            "filters_used": {
                "region_key": region_key,
                "min_box_n": min_box_n,
                "ppm2_basis": ppm2_basis,
                "force_refresh": force_refresh,
            },
            "result_count": len(annotations),
            "queried_at": now_iso,
            "data_freshness": None,
        },
    }
    if notes:
        envelope["metadata"]["notes"] = notes
    return envelope
