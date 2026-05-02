"""analyze_distribution: descriptive stats over a list of listings.

Pure function. No DB connection. Works on a list of dicts shaped like
the rows returned by find_comparables.
"""

from __future__ import annotations

import statistics
from typing import Any, Literal

_FIELD = Literal["price_czk", "price_per_m2", "area_m2"]
_BIN_COUNT = 20
_NOISE_FRAC = 0.05


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
        data = _small_sample(field, values, pairs)
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
        "outlier_ids": [], "modality_estimate": "unclear",
    }


def _small_sample(
    field: str,
    values: list[float],
    pairs: list[tuple[Any, float]],
) -> dict[str, Any]:
    return {
        "n": len(values), "field": field,
        "min": min(values), "max": max(values),
        "mean": statistics.mean(values), "median": statistics.median(values),
        "p10": None, "p25": None, "p75": None, "p90": None,
        "stddev": None, "iqr": None,
        "outlier_ids": [], "modality_estimate": "unclear",
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
    stddev = statistics.stdev(values) if n >= 2 else None
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
        "modality_estimate": _modality(values),
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


def _modality(
    values: list[float],
) -> Literal["unimodal", "bimodal", "multimodal", "unclear"]:
    """Histogram-based modality estimate.

    Bins values into 20 buckets, smooths with a 3-bucket moving average,
    counts local maxima that clear two cuts: an absolute floor (5% of
    total count, suppresses tail noise on small samples) and a relative
    floor (50% of the tallest smoothed bin, suppresses minor wiggles
    from being read as second/third modes). A plateau across adjacent
    equal-height bins is counted once via strict-greater-than on the
    left side.
    """
    if len(values) < 5:
        return "unclear"
    lo, hi = min(values), max(values)
    if hi == lo:
        return "unimodal"
    width = (hi - lo) / _BIN_COUNT
    bins = [0] * _BIN_COUNT
    for v in values:
        idx = min(int((v - lo) / width), _BIN_COUNT - 1)
        bins[idx] += 1

    # Two passes of a 3-bucket moving average. One pass leaves enough
    # noise that adjacent bins inside a single Gaussian lump can both
    # read as peaks; the second pass collapses them.
    smoothed: list[float] = [float(b) for b in bins]
    for _ in range(2):
        nxt: list[float] = []
        for i in range(_BIN_COUNT):
            window = smoothed[max(0, i - 1):min(_BIN_COUNT, i + 2)]
            nxt.append(sum(window) / len(window))
        smoothed = nxt

    abs_floor = _NOISE_FRAC * len(values)
    rel_floor = 0.5 * max(smoothed)
    threshold = max(abs_floor, rel_floor)
    peaks = 0
    for i in range(_BIN_COUNT):
        if smoothed[i] < threshold:
            continue
        left_strict = (i == 0) or smoothed[i] > smoothed[i - 1]
        right_loose = (i == _BIN_COUNT - 1) or smoothed[i] >= smoothed[i + 1]
        if left_strict and right_loose:
            peaks += 1

    if peaks == 1:
        return "unimodal"
    if peaks == 2:
        return "bimodal"
    if peaks >= 3:
        return "multimodal"
    return "unclear"
