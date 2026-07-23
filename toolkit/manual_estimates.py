"""Read operator-recorded manual rental estimates attached to a listing."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg


_COLS = (
    "id", "sreality_id", "rent_czk", "author", "source_kind", "notes",
    "created_at", "updated_at",
)


def get_manual_rental_estimates(
    conn: "psycopg.Connection",
    sreality_id: int | None = None,
    *,
    listing_id: int | None = None,
) -> dict[str, Any]:
    from toolkit import _listing_id_clause, _now_iso

    id_clause, id_val = _listing_id_clause(
        sreality_id, listing_id, lid_col="listing_id",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"select {', '.join(_COLS)} "
            "from manual_rental_estimates "
            f"where {id_clause} "
            "order by created_at desc",
            (id_val,),
        )
        rows = cur.fetchall()

    estimates = [_row_to_dict(row) for row in rows]
    return {
        "data": {"estimates": estimates},
        "metadata": {
            "tool": "get_manual_rental_estimates",
            "filters_used": (
                {"listing_id": listing_id} if listing_id is not None
                else {"sreality_id": sreality_id}
            ),
            "result_count": len(estimates),
            "queried_at": _now_iso(),
            "data_freshness": _latest_updated_at(estimates),
        },
    }


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    out = dict(zip(_COLS, row))
    for k in ("created_at", "updated_at"):
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


def _latest_updated_at(estimates: list[dict[str, Any]]) -> str | None:
    stamps = [e["updated_at"] for e in estimates if e.get("updated_at")]
    if not stamps:
        return None
    return max(stamps)
