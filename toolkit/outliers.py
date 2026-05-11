"""find_distribution_outliers: IQR-based detection with cross-referenced reasons.

Pure read. Reuses the percentile helper from analyze_distribution rather
than reimplementing it. For each outlier, optionally calls compare_snapshots
to surface price-trajectory and time-on-market evidence, and runs one batched
query against listing_fetch_failures so a known-flaky listing is flagged.

Reasons:
  - statistical_outlier   : always present for outliers (>iqr_multiplier × IQR)
  - stairstep_dropping    : compare_snapshots reported that pattern
  - fetch_failures        : a row exists in listing_fetch_failures
  - long_time_on_market   : compare_snapshots reported time_on_market_days > 60
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import psycopg


_OutlierField = Literal["price_per_m2", "price_czk"]
_LONG_TOM_DAYS = 60
_MIN_SAMPLE = 5


def find_distribution_outliers(
    conn: "psycopg.Connection",
    listings: list[dict[str, Any]],
    field: _OutlierField = "price_per_m2",
    iqr_multiplier: float = 1.5,
    investigate_history: bool = True,
) -> dict[str, Any]:
    from toolkit.distribution import _percentile

    pairs = [
        (l["sreality_id"], float(l[field]))
        for l in listings
        if l.get(field) is not None and l.get("sreality_id") is not None
    ]
    n = len(pairs)
    md_filters = {
        "field": field,
        "iqr_multiplier": iqr_multiplier,
        "investigate_history": investigate_history,
    }

    if n < _MIN_SAMPLE:
        return _envelope(
            field=field, iqr_multiplier=iqr_multiplier,
            median=None, iqr=None,
            outliers=[],
            non_outlier_ids=[sid for sid, _ in pairs],
            listings=listings,
            md_filters=md_filters,
            notes=[
                f"sample size {n} below threshold "
                f"(need >= {_MIN_SAMPLE} for IQR-based detection)"
            ],
        )

    sorted_values = sorted(v for _, v in pairs)
    median = statistics.median(sorted_values)
    p25 = _percentile(sorted_values, 25)
    p75 = _percentile(sorted_values, 75)
    iqr = p75 - p25
    threshold = iqr_multiplier * iqr

    outlier_pairs: list[tuple[int, float]] = []
    non_outlier_ids: list[int] = []
    for sid, v in pairs:
        if iqr > 0 and abs(v - median) > threshold:
            outlier_pairs.append((sid, v))
        else:
            non_outlier_ids.append(sid)

    failure_map = _fetch_failure_attempts(
        conn, [sid for sid, _ in outlier_pairs]
    )

    outliers_out: list[dict[str, Any]] = []
    for sid, v in outlier_pairs:
        deviation = (v - median) / iqr if iqr > 0 else 0.0
        direction = "high" if v > median else "low"
        reasons: list[str] = ["statistical_outlier"]
        attempts = failure_map.get(sid, 0)
        if attempts > 0:
            reasons.append("fetch_failures")

        history_summary: dict[str, Any] | None = None
        if investigate_history:
            from toolkit.snapshots import compare_snapshots
            h_data = compare_snapshots(conn, sid)["data"]
            pattern = h_data.get("price_change_pattern")
            tom = h_data.get("time_on_market_days") or 0
            if pattern == "stairstep_dropping":
                reasons.append("stairstep_dropping")
            if tom > _LONG_TOM_DAYS:
                reasons.append("long_time_on_market")
            history_summary = {
                "snapshot_count": h_data.get("snapshot_count", 0),
                "price_change_pattern": pattern,
                "time_on_market_days": tom,
                "active_failure_attempts": attempts,
            }

        outliers_out.append({
            "sreality_id": sid,
            "value": v,
            "deviation_iqr_units": round(deviation, 4),
            "direction": direction,
            "reasons": reasons,
            "history_summary": history_summary,
        })

    return _envelope(
        field=field, iqr_multiplier=iqr_multiplier,
        median=median, iqr=iqr,
        outliers=outliers_out,
        non_outlier_ids=non_outlier_ids,
        listings=listings,
        md_filters=md_filters,
    )


def _fetch_failure_attempts(
    conn: "psycopg.Connection", outlier_ids: list[int]
) -> dict[int, int]:
    if not outlier_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sreality_id, attempts FROM listing_fetch_failures "
            "WHERE sreality_id = ANY(%s)",
            (outlier_ids,),
        )
        return {row[0]: int(row[1]) for row in cur.fetchall()}


def _envelope(
    *,
    field: str,
    iqr_multiplier: float,
    median: float | None,
    iqr: float | None,
    outliers: list[dict[str, Any]],
    non_outlier_ids: list[int],
    listings: list[dict[str, Any]],
    md_filters: dict[str, Any],
    notes: list[str] | None = None,
) -> dict[str, Any]:
    from toolkit import _max_last_seen, _now_iso

    metadata: dict[str, Any] = {
        "tool": "find_distribution_outliers",
        "filters_used": md_filters,
        "result_count": len(outliers),
        "queried_at": _now_iso(),
        "data_freshness": _max_last_seen(listings),
    }
    if notes:
        metadata["notes"] = notes
    return {
        "data": {
            "field": field,
            "iqr_multiplier": iqr_multiplier,
            "median": median,
            "iqr": iqr,
            "outliers": outliers,
            "non_outlier_ids": non_outlier_ids,
        },
        "metadata": metadata,
    }
