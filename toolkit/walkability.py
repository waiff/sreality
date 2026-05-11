"""Walkability + amenity-supply scoring over the OSM amenity cache.

Two narrow tools that project the POI cohort returned by
`find_anchor_amenities` onto two different signals:

- `compute_walkability` — proximity only: how close is the *nearest*
  POI of each category. Composite is a weighted mean of per-category
  proximity scores.
- `compute_amenity_supply` — count only: how many POIs of each
  category fall within the radius, expressed as a ratio against a
  target count and bucketed (`scarce|adequate|abundant`).

Two facts, two tools, agent picks. Walkability stays a single number
the agent can sort by; supply stays a separate dig-deeper call.

Both reuse `find_anchor_amenities` for the underlying POI lookup, so
the OSM cache (`amenities` + `amenity_fetches`) and the Overpass
fallback are shared. Neither function writes directly; the underlying
amenity call already lives under the toolkit-rule-#5 OSM-mirror
write exception.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from toolkit.amenities import CATEGORY_TAGS, find_anchor_amenities

if TYPE_CHECKING:
    import psycopg

    from scraper.overpass_client import OverpassClient


_DEFAULT_CATEGORIES: tuple[str, ...] = (
    "tram_stop",
    "metro_station",
    "bus_stop",
    "supermarket",
    "convenience",
    "pharmacy",
    "school_primary",
    "park",
)

_DEFAULT_WEIGHTS: dict[str, float] = {
    "tram_stop":      2.0,
    "metro_station":  2.0,
    "bus_stop":       1.0,
    "supermarket":    1.5,
    "convenience":    0.5,
    "pharmacy":       1.0,
    "school_primary": 0.5,
    "park":           0.75,
}

_DEFAULT_TARGET_COUNTS: dict[str, int] = {
    "tram_stop":      3,
    "metro_station":  1,
    "bus_stop":       3,
    "supermarket":    1,
    "convenience":    2,
    "pharmacy":       1,
    "school_primary": 1,
    "park":           1,
}

# scarce: ratio < 0.5; adequate: 0.5 <= ratio < 1.5; abundant: ratio >= 1.5
_SCARCE_RATIO = 0.5
_ABUNDANT_RATIO = 1.5

Adequacy = Literal["scarce", "adequate", "abundant"]


def compute_walkability(
    conn: "psycopg.Connection",
    lat: float,
    lng: float,
    radius_m: int = 1000,
    categories: list[str] | None = None,
    weights: dict[str, float] | None = None,
    overpass_client: "OverpassClient | None" = None,
    cache_ttl_days: int = 30,
) -> dict[str, Any]:
    from toolkit import _now_iso

    requested = list(categories) if categories is not None else list(_DEFAULT_CATEGORIES)
    _validate_categories(requested)
    weight_map = dict(weights) if weights is not None else dict(_DEFAULT_WEIGHTS)

    amen = find_anchor_amenities(
        conn,
        lat=lat,
        lng=lng,
        radius_m=radius_m,
        categories=requested,
        cache_ttl_days=cache_ttl_days,
        overpass_client=overpass_client,
    )
    by_category: dict[str, dict[str, Any]] = amen["data"]["categories"]

    rows: list[dict[str, Any]] = []
    weighted_sum = 0.0
    weight_total = 0.0
    saw_any_score = False
    missing: list[str] = []

    for cat in requested:
        cat_data = by_category.get(cat) or {}
        nearest = cat_data.get("nearest_distance_m")
        items = cat_data.get("items") or []
        weight = float(weight_map.get(cat, 1.0))

        if nearest is None:
            category_score: int | None = None
            score_contrib = 0.0
            nearest_payload: dict[str, Any] | None = None
            missing.append(cat)
        else:
            saw_any_score = True
            proximity = max(0.0, 1.0 - float(nearest) / float(radius_m))
            category_score = int(round(100.0 * proximity))
            score_contrib = float(category_score)
            top = items[0] if items else {}
            nearest_payload = {
                "source_id": top.get("source_id"),
                "name":      top.get("name"),
                "lat":       top.get("lat"),
                "lng":       top.get("lng"),
            }

        weighted_sum += score_contrib * weight
        weight_total += weight

        rows.append({
            "category":            cat,
            "nearest_distance_m":  nearest,
            "category_score":      category_score,
            "weight":              weight,
            "nearest":             nearest_payload,
        })

    walkability_score: int | None
    if not saw_any_score or weight_total == 0.0:
        walkability_score = None if not saw_any_score else 0
    else:
        walkability_score = int(round(weighted_sum / weight_total))

    metadata = {
        "tool": "compute_walkability",
        "filters_used": {
            "lat": lat,
            "lng": lng,
            "radius_m": radius_m,
            "categories": requested,
            "weights": {k: weight_map.get(k, 1.0) for k in requested},
            "cache_ttl_days": cache_ttl_days,
        },
        "result_count": sum(
            1 for r in rows if r["nearest_distance_m"] is not None
        ),
        "queried_at": _now_iso(),
        "data_freshness": amen["metadata"].get("data_freshness"),
    }

    return {
        "data": {
            "center":             {"lat": lat, "lng": lng},
            "radius_m":           radius_m,
            "walkability_score":  walkability_score,
            "categories":         rows,
            "missing_categories": missing,
        },
        "metadata": metadata,
    }


def compute_amenity_supply(
    conn: "psycopg.Connection",
    lat: float,
    lng: float,
    radius_m: int = 1000,
    categories: list[str] | None = None,
    target_counts: dict[str, int] | None = None,
    overpass_client: "OverpassClient | None" = None,
    cache_ttl_days: int = 30,
) -> dict[str, Any]:
    from toolkit import _now_iso

    requested = list(categories) if categories is not None else list(_DEFAULT_CATEGORIES)
    _validate_categories(requested)
    targets = dict(target_counts) if target_counts is not None else dict(_DEFAULT_TARGET_COUNTS)

    amen = find_anchor_amenities(
        conn,
        lat=lat,
        lng=lng,
        radius_m=radius_m,
        categories=requested,
        cache_ttl_days=cache_ttl_days,
        overpass_client=overpass_client,
    )
    by_category: dict[str, dict[str, Any]] = amen["data"]["categories"]

    rows: list[dict[str, Any]] = []
    summary: dict[Adequacy, list[str]] = {
        "scarce":   [],
        "adequate": [],
        "abundant": [],
    }

    for cat in requested:
        cat_data = by_category.get(cat) or {}
        count = int(cat_data.get("count") or 0)
        target = int(targets.get(cat, 1))
        if target <= 0:
            target = 1
        ratio = count / target
        adequacy = _classify_adequacy(ratio)
        summary[adequacy].append(cat)
        rows.append({
            "category":     cat,
            "count":        count,
            "target_count": target,
            "supply_ratio": round(ratio, 3),
            "adequacy":     adequacy,
        })

    metadata = {
        "tool": "compute_amenity_supply",
        "filters_used": {
            "lat": lat,
            "lng": lng,
            "radius_m": radius_m,
            "categories":    requested,
            "target_counts": {k: targets.get(k, 1) for k in requested},
            "cache_ttl_days": cache_ttl_days,
        },
        "result_count": len(rows),
        "queried_at":   _now_iso(),
        "data_freshness": amen["metadata"].get("data_freshness"),
    }

    return {
        "data": {
            "center":     {"lat": lat, "lng": lng},
            "radius_m":   radius_m,
            "categories": rows,
            "summary":    summary,
        },
        "metadata": metadata,
    }


def _validate_categories(requested: list[str]) -> None:
    unknown = [c for c in requested if c not in CATEGORY_TAGS]
    if unknown:
        valid = ", ".join(sorted(CATEGORY_TAGS))
        raise ValueError(f"unknown categories: {unknown}. valid: {valid}")


def _classify_adequacy(ratio: float) -> Adequacy:
    if ratio < _SCARCE_RATIO:
        return "scarce"
    if ratio < _ABUNDANT_RATIO:
        return "adequate"
    return "abundant"
