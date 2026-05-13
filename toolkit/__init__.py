"""Pure-function analytical tools over the sreality database.

Tools return a standard envelope:

    {"data": ..., "metadata": {tool, filters_used, result_count,
                               queried_at, data_freshness}}

See CLAUDE.md "Toolkit and API rules" for the contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypedDict


class ToolMetadata(TypedDict):
    tool: str
    filters_used: dict[str, Any]
    result_count: int
    queried_at: str
    data_freshness: str | None


class ToolResult(TypedDict):
    data: Any
    metadata: ToolMetadata


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _max_last_seen(listings: list[dict[str, Any]]) -> str | None:
    stamps = [
        l["last_seen_at"]
        for l in listings
        if l.get("last_seen_at") is not None
    ]
    if not stamps:
        return None
    parsed = [
        s if isinstance(s, datetime) else datetime.fromisoformat(str(s))
        for s in stamps
    ]
    return max(parsed).isoformat()


from toolkit.amenities import find_anchor_amenities  # noqa: E402
from toolkit.building_extraction import extract_building_units  # noqa: E402
from toolkit.clustering import cluster_comparables  # noqa: E402
from toolkit.comparables import (  # noqa: E402
    ComparableFilters,
    TargetSpec,
    find_comparables,
    find_comparables_relaxed,
)
from toolkit.distribution import analyze_distribution  # noqa: E402
from toolkit.floor_plan import read_floor_plan  # noqa: E402
from toolkit.freshness import verify_listing_freshness  # noqa: E402
from toolkit.image_similarity import compare_listing_images  # noqa: E402
from toolkit.manual_estimates import get_manual_rental_estimates  # noqa: E402
from toolkit.neighborhoods import describe_neighborhood  # noqa: E402
from toolkit.outliers import find_distribution_outliers  # noqa: E402
from toolkit.snapshots import compare_snapshots  # noqa: E402
from toolkit.summaries import summarize_listing  # noqa: E402
from toolkit.transit_axis import find_comparables_along_axis  # noqa: E402
from toolkit.velocity import (  # noqa: E402
    compute_listing_velocity,
    compute_market_velocity,
)
from toolkit.walkability import (  # noqa: E402
    compute_amenity_supply,
    compute_walkability,
)

__all__ = [
    "ComparableFilters",
    "TargetSpec",
    "ToolMetadata",
    "ToolResult",
    "analyze_distribution",
    "cluster_comparables",
    "compare_listing_images",
    "compare_snapshots",
    "compute_amenity_supply",
    "compute_listing_velocity",
    "compute_market_velocity",
    "compute_walkability",
    "describe_neighborhood",
    "extract_building_units",
    "find_anchor_amenities",
    "find_comparables",
    "find_comparables_along_axis",
    "find_comparables_relaxed",
    "find_distribution_outliers",
    "get_manual_rental_estimates",
    "read_floor_plan",
    "summarize_listing",
    "verify_listing_freshness",
]
