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


from toolkit.comparables import (  # noqa: E402
    ComparableFilters,
    TargetSpec,
    find_comparables,
)
from toolkit.distribution import analyze_distribution  # noqa: E402
from toolkit.freshness import verify_listing_freshness  # noqa: E402

__all__ = [
    "ComparableFilters",
    "TargetSpec",
    "ToolMetadata",
    "ToolResult",
    "analyze_distribution",
    "find_comparables",
    "verify_listing_freshness",
]
