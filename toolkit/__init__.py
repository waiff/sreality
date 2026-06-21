"""Pure-function analytical tools over the sreality database.

Tools return a standard envelope:

    {"data": ..., "metadata": {tool, filters_used, result_count,
                               queried_at, data_freshness}}

See CLAUDE.md "Toolkit and API rules" for the contract.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TypedDict


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


# Lazy re-exports (PEP 562). This package historically eager-imported every
# submodule here, which coupled EVERY consumer to the UNION of all submodule
# dependencies — a slim `from toolkit.bazos_enrichment import ...` paid for
# boto3/Pillow/vision/etc. at import time and broke when one was missing (the
# enrich_bazos outage). Importing `toolkit` now costs nothing; each re-exported
# symbol's submodule is imported on first access and cached into globals().
# Submodule imports (`from toolkit.x import y`) resolve through normal import
# machinery and never reach __getattr__, so they are unaffected.
_LAZY_EXPORTS: dict[str, str] = {
    "find_anchor_amenities": "amenities",
    "extract_building_units": "building_extraction",
    "cluster_comparables": "clustering",
    "ComparableFilters": "comparables",
    "TargetSpec": "comparables",
    "find_comparables": "comparables",
    "find_comparables_relaxed": "comparables",
    "discover_condition_markers": "condition_markers",
    "score_listing_condition": "condition_scoring",
    "analyze_distribution": "distribution",
    "read_floor_plan": "floor_plan",
    "verify_listing_freshness": "freshness",
    "compare_listing_images": "image_similarity",
    "get_manual_rental_estimates": "manual_estimates",
    "describe_neighborhood": "neighborhoods",
    "find_distribution_outliers": "outliers",
    "summarize_region_dispositions": "region_annotations",
    "compare_snapshots": "snapshots",
    "summarize_listing": "summaries",
    "find_comparables_along_axis": "transit_axis",
    "compute_listing_velocity": "velocity",
    "compute_market_velocity": "velocity",
    "compute_amenity_supply": "walkability",
    "compute_walkability": "walkability",
}


def __getattr__(name: str) -> Any:  # PEP 562
    submodule = _LAZY_EXPORTS.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"toolkit.{submodule}"), name)
    globals()[name] = value  # cache so subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


if TYPE_CHECKING:  # let type-checkers / IDEs see the lazily re-exported names
    from toolkit.amenities import find_anchor_amenities
    from toolkit.building_extraction import extract_building_units
    from toolkit.clustering import cluster_comparables
    from toolkit.comparables import (
        ComparableFilters,
        TargetSpec,
        find_comparables,
        find_comparables_relaxed,
    )
    from toolkit.condition_markers import discover_condition_markers
    from toolkit.condition_scoring import score_listing_condition
    from toolkit.distribution import analyze_distribution
    from toolkit.floor_plan import read_floor_plan
    from toolkit.freshness import verify_listing_freshness
    from toolkit.image_similarity import compare_listing_images
    from toolkit.manual_estimates import get_manual_rental_estimates
    from toolkit.neighborhoods import describe_neighborhood
    from toolkit.outliers import find_distribution_outliers
    from toolkit.region_annotations import summarize_region_dispositions
    from toolkit.snapshots import compare_snapshots
    from toolkit.summaries import summarize_listing
    from toolkit.transit_axis import find_comparables_along_axis
    from toolkit.velocity import compute_listing_velocity, compute_market_velocity
    from toolkit.walkability import compute_amenity_supply, compute_walkability

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
    "discover_condition_markers",
    "extract_building_units",
    "find_anchor_amenities",
    "find_comparables",
    "find_comparables_along_axis",
    "find_comparables_relaxed",
    "find_distribution_outliers",
    "get_manual_rental_estimates",
    "read_floor_plan",
    "score_listing_condition",
    "summarize_listing",
    "summarize_region_dispositions",
    "verify_listing_freshness",
]

# Every public symbol must be either an eager attribute (defined above) or a
# lazy re-export. Checked at import time so a typo in _LAZY_EXPORTS fails loudly
# (any test importing toolkit catches it) instead of surfacing as a late
# AttributeError on first access of the missed symbol.
assert set(__all__) <= set(globals()) | set(_LAZY_EXPORTS), (
    "toolkit.__all__ has symbols missing from both eager globals and "
    f"_LAZY_EXPORTS: {sorted(set(__all__) - set(globals()) - set(_LAZY_EXPORTS))}"
)
