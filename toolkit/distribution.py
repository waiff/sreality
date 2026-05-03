"""analyze_distribution: descriptive stats over a list of listings.

Pure function. No DB connection. Works on a list of dicts shaped like
the rows returned by find_comparables. Returns plain stdlib statistics
(no opinionated heuristics) so every number is rederivable in a
spreadsheet.
"""

from __future__ import annotations

import statistics
from typing import Any, Literal

_FIELD = Literal["price_czk", "price_per_m2", "area_m2"]


def analyze_distribution(
    listings: list[dict[str, Any]],
    field: _FIELD = "price_per_m2",
) -> dict[str, Any]:
    from toolkit import _max_last_seen, _now_iso

    pairs = [
        (l.get("sreality_id"), l.get(field))
        for l in listings
        if l.get(field) is not None
    ]
    values = [float(v) for _, v in pairs]
    n = len(values)

    if n == 0:
        data = _empty(field)
    elif n < 5:
        data = _small_sample(field, values)
    else:
        data = _full_stats(field, values, pairs)

    return {
        "data": data,
        "metadata": {
            "tool": "analyze_distribution",
            "filters_used": {"field": field},
            "result_count": n,
            "queried_at": _now_iso(),
            "data_freshness": _max_last_seen(listings),
        },
    }


def _empty(field: str) -> dict[str, Any]:
    return {
        "n": 0, "field": field,
        "min": None, "max": None, "mean": None, "median": None,
        "p10": None, "p25": None, "p75": None, "p90": None,
        "stddev": None, "iqr": None,
        "outlier_ids": [],
    }


def _small_sample(field: str, values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values), "field": field,
        "min": min(values), "max": max(values),
        "mean": statistics.mean(values), "median": statistics.median(values),
        "p10": None, "p25": None, "p75": None, "p90": None,
        "stddev": None, "iqr": None,
        "outlier_ids": [],
    }


def _full_stats(
    field: str,
    values: list[float],
    pairs: list[tuple[Any, float]],
) -> dict[str, Any]:
    n = len(values)
    sorted_values = sorted(values)
    median = statistics.median(values)
    p10 = _percentile(sorted_values, 10)
    p25 = _percentile(sorted_values, 25)
    p75 = _percentile(sorted_values, 75)
    p90 = _percentile(sorted_values, 90)
    iqr = p75 - p25
    stddev = statistics.stdev(values)
    outliers = [
        sid for sid, v in pairs
        if sid is not None and abs(v - median) > 1.5 * iqr
    ]
    return {
        "n": n, "field": field,
        "min": min(values), "max": max(values),
        "mean": statistics.mean(values), "median": median,
        "p10": p10, "p25": p25, "p75": p75, "p90": p90,
        "stddev": stddev, "iqr": iqr,
        "outlier_ids": outliers,
    }


def _percentile(sorted_values: list[float], p: float) -> float:
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    k = (n - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, n - 1)
    frac = k - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac
