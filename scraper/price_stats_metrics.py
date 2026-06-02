"""Pure derived-metric math for price-stats datasets (CAGR, gross yield).

No DB, no I/O — list-of-rows in, dict out, so it unit-tests offline. Shared by
the recompute step (`scraper.price_stats_db`) and the analysis toolkit
(`toolkit.price_stats`).
"""

from __future__ import annotations

from typing import Any

MonthRow = dict[str, Any]  # {"year", "month", "price", "active_count", ...}


def _ym_index(row: MonthRow) -> int:
    return int(row["year"]) * 12 + (int(row["month"]) - 1)


def _ym_label(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _price_points(series: list[MonthRow]) -> list[tuple[int, float, MonthRow]]:
    pts = [
        (_ym_index(r), float(r["price"]), r)
        for r in series
        if r.get("price") not in (None, 0)
    ]
    pts.sort(key=lambda t: t[0])
    return pts


def cagr_pct(series: list[MonthRow], window_years: int) -> dict[str, Any]:
    """CAGR (%) over the trailing `window_years`, anchored at the latest point.

    Returns ``{"cagr_pct", "latest_price", "latest_ym", "months", "min_active"}``.
    `cagr_pct` is None when the usable history spans < 1 year (a single-endpoint
    ratio over a few months annualizes into nonsense). `min_active` is the min
    `active_count` over the window — the UI's sparsity guard.
    """
    pts = _price_points(series)
    if not pts:
        return {
            "cagr_pct": None, "latest_price": None, "latest_ym": None,
            "months": 0, "min_active": None,
        }
    end_idx, end_price, _ = pts[-1]
    window_start = end_idx - window_years * 12
    in_window = [p for p in pts if p[0] >= window_start]
    start_idx, start_price, _ = in_window[0]

    actives = [
        int(r["active_count"])
        for _, _, r in in_window
        if r.get("active_count") is not None
    ]
    years = (end_idx - start_idx) / 12.0
    cagr: float | None
    if years >= 1.0 and start_price > 0:
        cagr = ((end_price / start_price) ** (1.0 / years) - 1.0) * 100.0
    else:
        cagr = None
    return {
        "cagr_pct": round(cagr, 2) if cagr is not None else None,
        "latest_price": int(round(end_price)),
        "latest_ym": _ym_label(end_idx),
        "months": len(in_window),
        "min_active": min(actives) if actives else None,
    }


def gross_yield_pct(
    sale_per_m2: float | None, rent_per_m2_month: float | None
) -> float | None:
    """Annual gross yield (%) — per-m² cancels, so units don't matter.

    rent is Kč/m²/month, sale is Kč/m²: 12 × rent / sale × 100.
    """
    if not sale_per_m2 or not rent_per_m2_month or sale_per_m2 <= 0:
        return None
    return round(12.0 * rent_per_m2_month / sale_per_m2 * 100.0, 2)


def compute_city_metrics(
    sale_series: list[MonthRow],
    rent_series: list[MonthRow],
    *,
    window_years: int,
) -> dict[str, Any]:
    """Full derived-metric row for one (dataset, locality)."""
    sale = cagr_pct(sale_series, window_years)
    rent = cagr_pct(rent_series, window_years)
    return {
        "window_years": window_years,
        "sale_latest_price": sale["latest_price"],
        "sale_latest_ym": sale["latest_ym"],
        "sale_cagr_pct": sale["cagr_pct"],
        "sale_months": sale["months"],
        "sale_min_active": sale["min_active"],
        "rent_latest_price": rent["latest_price"],
        "rent_latest_ym": rent["latest_ym"],
        "rent_cagr_pct": rent["cagr_pct"],
        "rent_months": rent["months"],
        "rent_min_active": rent["min_active"],
        "gross_yield_pct": gross_yield_pct(
            sale["latest_price"], rent["latest_price"]
        ),
    }
