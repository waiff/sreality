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
    sreality_id: int,
) -> dict[str, Any]:
    from toolkit import _now_iso

    with conn.cursor() as cur:
        cur.execute(
            f"select {', '.join(_COLS)} "
            "from manual_rental_estimates "
            "where sreality_id = %s "
            "order by created_at desc",
            (sreality_id,),
        )
        rows = cur.fetchall()

    estimates = [_row_to_dict(row) for row in rows]
    return {
        "data": {"estimates": estimates},
        "metadata": {
            "tool": "get_manual_rental_estimates",
            "filters_used": {"sreality_id": sreality_id},
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
