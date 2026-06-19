"""POST /listings/lookup — batch (source, native id) → MF facts + latest estimate.

The Chrome extension overlays the Browse-card 'Výnos MF' yield
(`mf_gross_yield_pct`) and the MF reference rent (`mf_reference_rent_czk`) on
portal detail + index pages, across every scraped portal, and deep-links each
listing to its SPA page (`/listing/{sreality_id}`). The public views expose
only `(source, sreality_id)`, so a non-sreality card — which only knows its own
native id from its href — can't map to our row from the browser. This
server-side lookup resolves uniformly on `(source, source_id_native)` (the
migration-091 unique key; for sreality `source_id_native` is the numeric id as
text) and joins any latest successful estimation. Read-only; bearer-gated like
every other non-/health route.

It also returns the listing's `property_id` + its **deal-pipeline membership**
(rule #22) so the panel's "Přidat do pipeline" toggle knows its state in the
one call it already makes — the toggle then writes through the existing
bearer-gated `POST/DELETE /pipeline/cards` (the same path the SPA uses). The
pipeline is property-grain, so membership is read off `l.property_id`.

Rows come back keyed by column name (dict_row) — no positional index math, so
projecting one more column is a one-line change that can't silently misalign.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from psycopg.rows import dict_row

if TYPE_CHECKING:
    import psycopg

    from api import schemas as s

# Listing columns projected onto each entry (dict keys === SELECT aliases).
# `sreality_id` is the app-wide listing identity (negative synthetic for
# non-sreality portals) — the SPA route is /listing/{sreality_id}.
_LISTING_COLS: tuple[str, ...] = (
    "sreality_id", "property_id",
    "category_main", "category_type", "area_m2", "price_czk", "disposition",
    "district", "locality", "is_active", "last_seen_at",
    "mf_reference_rent_czk", "mf_gross_yield_pct",
)

_LOOKUP_SQL = """
WITH req(source, source_id) AS (VALUES {values})
SELECT
    req.source,
    req.source_id,
    (l.source_id_native IS NOT NULL) AS found,
    l.sreality_id, l.property_id,
    l.category_main, l.category_type, l.area_m2, l.price_czk, l.disposition,
    l.district, l.locality, l.is_active, l.last_seen_at,
    l.mf_reference_rent_czk, l.mf_gross_yield_pct,
    e.id AS estimation_id, e.estimate_kind AS estimation_kind,
    e.gross_yield_pct AS estimation_yield,
    (pp.property_id IS NOT NULL) AS in_pipeline,
    ps.key   AS pipeline_stage_key,
    ps.label AS pipeline_stage_label
FROM req
LEFT JOIN listings l
    ON l.source = req.source AND l.source_id_native = req.source_id
LEFT JOIN property_pipeline pp ON pp.property_id = l.property_id
LEFT JOIN pipeline_stages   ps ON ps.id = pp.stage_id
LEFT JOIN LATERAL (
    SELECT er.id, er.estimate_kind, er.gross_yield_pct
    FROM estimation_runs er
    WHERE er.status = 'success'
      AND (
        (l.source = 'sreality' AND er.input_sreality_id = l.sreality_id)
        OR (l.source <> 'sreality' AND er.input_url = l.source_url)
      )
    ORDER BY er.created_at DESC
    LIMIT 1
) e ON true
"""


def _clean(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def lookup_portal_listings(
    conn: "psycopg.Connection", items: "list[s.PortalLookupItem]",
) -> dict[str, Any]:
    """Resolve each (source, source_id) to its MF facts + latest estimate.

    One row per requested item, in request order; `found=false` (and null
    fields) when we have no listing for that pair.
    """
    values_sql = ", ".join(["(%s::text, %s::text)"] * len(items))
    params: list[str] = []
    for it in items:
        params.extend([it.source, it.source_id])

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_LOOKUP_SQL.format(values=values_sql), params)
        rows = cur.fetchall()

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        entry: dict[str, Any] = {
            "source": row["source"],
            "source_id": row["source_id"],
            "found": bool(row["found"]),
        }
        entry.update({col: _clean(row[col]) for col in _LISTING_COLS})
        est_id = row["estimation_id"]
        entry["latest_estimation"] = (
            {
                "estimation_id": est_id,
                "estimate_kind": row["estimation_kind"],
                "gross_yield_pct": _clean(row["estimation_yield"]),
            }
            if est_id is not None
            else None
        )
        # Deal-pipeline membership is property-grain (rule #22): only present
        # once the listing is attached to a property (a freshly-scraped row is
        # property_id NULL for ~5 min — the panel hides the toggle until then).
        entry["pipeline"] = (
            {
                "in_pipeline": bool(row["in_pipeline"]),
                "stage_key": row["pipeline_stage_key"],
                "stage_label": row["pipeline_stage_label"],
            }
            if row["property_id"] is not None
            else None
        )
        by_key[(row["source"], row["source_id"])] = entry

    fallback = lambda it: {  # noqa: E731 — tiny shape for the (rare) missing row
        "source": it.source, "source_id": it.source_id, "found": False,
        "latest_estimation": None, "pipeline": None,
    }
    return {"data": [by_key.get((it.source, it.source_id), fallback(it)) for it in items]}
