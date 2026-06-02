"""Read-only analyses over price-stats datasets — facts + provenance.

Three views over a dataset's per-municipality monthly series:
  * `dataset_summary`      — dataset-wide rollup: growth of the AVERAGE rent /
                             sale price (goal #1) + average gross yield (goal #2)
  * `dataset_city_metrics` — the precomputed per-city table (CAGR, yield)
  * `dataset_city_series`  — one city's monthly series, both categories (chart)

Growth is CAGR over a chosen window (`scraper.price_stats_metrics`). The
dataset-wide aggregate averages each month's price ACROSS cities (weighted by
`active_count` so big markets count more) before taking the CAGR — that's
"growth of the average rental per the dataset", not a mean of per-city rates.
No opinions, standard envelope (toolkit rule #1/#2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from psycopg.rows import dict_row

from scraper.price_stats_metrics import cagr_pct, gross_yield_pct
from toolkit import _now_iso

if TYPE_CHECKING:
    import psycopg

SALE = 1
LEASE = 2


def _aggregate_series(
    obs: list[dict[str, Any]], category_type_cb: int
) -> list[dict[str, Any]]:
    """Active-count-weighted average price per month across all cities."""
    by_ym: dict[tuple[int, int], dict[str, float]] = {}
    for r in obs:
        if r["category_type_cb"] != category_type_cb or r["price"] in (None, 0):
            continue
        key = (r["year"], r["month"])
        acc = by_ym.setdefault(key, {"wsum": 0.0, "w": 0.0, "active": 0.0})
        weight = float(r["active_count"] or 1)
        acc["wsum"] += float(r["price"]) * weight
        acc["w"] += weight
        acc["active"] += float(r["active_count"] or 0)
    out = []
    for (year, month) in sorted(by_ym):
        acc = by_ym[(year, month)]
        out.append(
            {
                "year": year, "month": month,
                "price": acc["wsum"] / acc["w"] if acc["w"] else None,
                "active_count": int(acc["active"]),
            }
        )
    return out


def dataset_summary(
    conn: "psycopg.Connection", dataset_id: int, *, window_years: int = 5
) -> dict[str, Any]:
    """Dataset-wide growth-of-average + average gross yield."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT category_type_cb, year, month, price, active_count "
            "FROM price_stat_observations WHERE dataset_id = %s",
            (dataset_id,),
        )
        obs = cur.fetchall()

    sale_series = _aggregate_series(obs, SALE)
    rent_series = _aggregate_series(obs, LEASE)
    sale = cagr_pct(sale_series, window_years)
    rent = cagr_pct(rent_series, window_years)
    yield_pct = gross_yield_pct(sale["latest_price"], rent["latest_price"])
    n_cities = _distinct_city_count(conn, dataset_id)

    data = {
        "dataset_id": dataset_id,
        "window_years": window_years,
        "sale_cagr_pct": sale["cagr_pct"],
        "sale_latest_price": sale["latest_price"],
        "sale_latest_ym": sale["latest_ym"],
        "rent_cagr_pct": rent["cagr_pct"],
        "rent_latest_price": rent["latest_price"],
        "rent_latest_ym": rent["latest_ym"],
        "gross_yield_pct": yield_pct,
        "cities": n_cities,
    }
    return _envelope(
        "dataset_summary", data,
        {"dataset_id": dataset_id, "window_years": window_years},
        1, _freshness(conn, dataset_id),
    )


def _distinct_city_count(conn: "psycopg.Connection", dataset_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(DISTINCT (entity_type, entity_id)) "
            "FROM price_stat_observations WHERE dataset_id = %s",
            (dataset_id,),
        )
        return int(cur.fetchone()[0])


def dataset_city_metrics(
    conn: "psycopg.Connection", dataset_id: int
) -> dict[str, Any]:
    """Precomputed per-city table (CAGR, yield, sparsity flags)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM price_stat_city_metrics_public "
            "WHERE dataset_id = %s ORDER BY locality_name",
            (dataset_id,),
        )
        rows = cur.fetchall()
    return _envelope(
        "dataset_city_metrics", rows, {"dataset_id": dataset_id},
        len(rows), _freshness(conn, dataset_id),
    )


def dataset_city_series(
    conn: "psycopg.Connection", dataset_id: int, entity_type: str, entity_id: int
) -> dict[str, Any]:
    """One city's monthly series for both categories (chart drill-down)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT category_type_cb, year, month, price, active_count, "
            "new_count, deleted_count FROM price_stat_observations "
            "WHERE dataset_id = %s AND entity_type = %s AND entity_id = %s "
            "ORDER BY year, month",
            (dataset_id, entity_type, entity_id),
        )
        rows = cur.fetchall()
    data = {
        "sale": [r for r in rows if r["category_type_cb"] == SALE],
        "rent": [r for r in rows if r["category_type_cb"] == LEASE],
    }
    return _envelope(
        "dataset_city_series", data,
        {"dataset_id": dataset_id, "entity_type": entity_type, "entity_id": entity_id},
        len(rows), _freshness(conn, dataset_id),
    )


def _freshness(conn: "psycopg.Connection", dataset_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(fetched_at) FROM price_stat_observations WHERE dataset_id = %s",
            (dataset_id,),
        )
        val = cur.fetchone()[0]
    return val.isoformat() if val else None


def _envelope(
    tool: str, data: Any, filters_used: dict[str, Any], result_count: int,
    data_freshness: str | None,
) -> dict[str, Any]:
    return {
        "data": data,
        "metadata": {
            "tool": tool,
            "filters_used": filters_used,
            "result_count": result_count,
            "queried_at": _now_iso(),
            "data_freshness": data_freshness,
        },
    }
