"""POST /listings/lookup — batch (source, native id) → MF facts + latest estimate.

The Chrome extension overlays the Browse-card 'Výnos MF' yield
(`mf_gross_yield_pct`) and the MF reference rent (`mf_reference_rent_czk`) on
portal detail + index pages, across every scraped portal. The public views
expose only `(source, sreality_id)`, so a non-sreality card — which only knows
its own native id from its href — can't map to our row from the browser. This
server-side lookup resolves uniformly on `(source, source_id_native)` (the
migration-091 unique key; for sreality `source_id_native` is the numeric id as
text) and joins any latest successful estimation. Read-only; bearer-gated like
every other non-/health route.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg

    from api import schemas as s

# Listing columns in SELECT order, after the (source, source_id, found) prefix.
_LISTING_COLS: tuple[str, ...] = (
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
    l.category_main, l.category_type, l.area_m2, l.price_czk, l.disposition,
    l.district, l.locality, l.is_active, l.last_seen_at,
    l.mf_reference_rent_czk, l.mf_gross_yield_pct,
    e.id, e.estimate_kind, e.gross_yield_pct
FROM req
LEFT JOIN listings l
    ON l.source = req.source AND l.source_id_native = req.source_id
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

    with conn.cursor() as cur:
        cur.execute(_LOOKUP_SQL.format(values=values_sql), params)
        rows = cur.fetchall()

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        source, source_id, found = row[0], row[1], bool(row[2])
        entry: dict[str, Any] = {
            "source": source, "source_id": source_id, "found": found,
        }
        entry.update(
            {col: _clean(v) for col, v in zip(_LISTING_COLS, row[3:14])}
        )
        est_id = row[14]
        entry["latest_estimation"] = (
            {
                "estimation_id": est_id,
                "estimate_kind": row[15],
                "gross_yield_pct": _clean(row[16]),
            }
            if est_id is not None
            else None
        )
        by_key[(source, source_id)] = entry

    fallback = lambda it: {  # noqa: E731 — tiny shape for the (rare) missing row
        "source": it.source, "source_id": it.source_id, "found": False,
        "latest_estimation": None,
    }
    return {"data": [by_key.get((it.source, it.source_id), fallback(it)) for it in items]}
