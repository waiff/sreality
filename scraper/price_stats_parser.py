"""Pure mapping for the sreality `estate_prices` / `localities/suggest` JSON.

No HTTP, no DB — just dict-in / dict-out, so it unit-tests offline. Three jobs:
build the `estate_prices` query params from a dataset filter spec, fold the
monthly response arrays into per-month observation rows, and pick the best
municipality match out of a `localities/suggest` response.
"""

from __future__ import annotations

from typing import Any

from scraper.price_stats_enums import ENTITY_MUNICIPALITY


class PriceStatsParseError(Exception):
    """The estate_prices response wasn't the shape we expect."""


def build_estate_prices_params(
    dataset: dict[str, Any],
    *,
    entity_id: int,
    entity_type: str,
    category_type_cb: int,
    default_from: str,
    default_to: str,
) -> dict[str, Any]:
    """Query params for one estate_prices call.

    Only non-null filters are included — the API treats an absent param as "no
    filter", so omitting them widens the cohort (the safe default for sparse
    localities). `default_from`/`default_to` are `YYYY-MM` strings.
    """
    params: dict[str, Any] = {
        "category_main_cb": int(dataset["category_main_cb"]),
        "category_type_cb": int(category_type_cb),
        "entity_id": int(entity_id),
        "entity_type": entity_type,
        "distance": int(dataset.get("distance") or 0),
        "default_from": default_from,
        "default_to": default_to,
    }
    # building_condition / building_type / ownership are string codes.
    for field in ("building_condition", "building_type", "ownership"):
        val = dataset.get(field)
        if val not in (None, ""):
            params[field] = str(val)
    for field in ("usable_area_from", "usable_area_to"):
        val = dataset.get(field)
        if val not in (None, ""):
            params[field] = int(val)
    return params


def parse_estate_prices(payload: dict[str, Any]) -> dict[str, Any]:
    """Fold an estate_prices response into per-month rows + scalar aggregates.

    Returns ``{"months": [{year, month, price, active_count, new_count,
    deleted_count}], "aggregates": {advert_count, new_advert_count,
    avg_price_per_area, avg_published_days_overall, avg_views_per_day},
    "previous_range": ...}``. A month present in only one of the two source
    arrays still yields a row (the other metrics are None).
    """
    if not isinstance(payload, dict) or "result" not in payload:
        raise PriceStatsParseError("missing 'result' key")
    result = payload["result"] or {}

    by_key: dict[tuple[int, int], dict[str, Any]] = {}

    def slot(year: Any, month: Any) -> dict[str, Any]:
        key = (int(year), int(month))
        row = by_key.get(key)
        if row is None:
            row = {
                "year": key[0], "month": key[1], "price": None,
                "active_count": None, "new_count": None, "deleted_count": None,
            }
            by_key[key] = row
        return row

    for pt in result.get("dev_price_by_month") or []:
        if pt.get("year") is None or pt.get("month") is None:
            continue
        slot(pt["year"], pt["month"])["price"] = pt.get("price")
    for pt in result.get("dev_count_advert_by_month") or []:
        if pt.get("year") is None or pt.get("month") is None:
            continue
        row = slot(pt["year"], pt["month"])
        row["active_count"] = pt.get("active")
        row["new_count"] = pt.get("new")
        row["deleted_count"] = pt.get("deleted")

    months = [by_key[k] for k in sorted(by_key)]
    aggregates = {
        "advert_count": result.get("advert_count"),
        "new_advert_count": result.get("new_advert_count"),
        "avg_price_per_area": result.get("avg_price_per_area"),
        "avg_published_days_overall": result.get("avg_published_days_overall"),
        "avg_views_per_day": result.get("avg_views_per_day"),
    }
    return {
        "months": months,
        "aggregates": aggregates,
        "previous_range": result.get("previous_range"),
    }


def parse_suggest_municipality(
    payload: dict[str, Any], *, phrase: str
) -> dict[str, Any] | None:
    """Best municipality (`source == 'muni'`) match from a suggest response.

    Prefers an exact (case-insensitive) name match on `municipality`, else the
    first municipality result. Returns the fields we cache in
    `price_stat_localities`, or None if no municipality is in the results.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    if not results:
        return None

    munis: list[dict[str, Any]] = []
    for r in results:
        ud = r.get("userData") or {}
        if ud.get("source") == ENTITY_MUNICIPALITY:
            munis.append(ud)
    if not munis:
        return None

    want = phrase.strip().casefold()
    chosen = next(
        (m for m in munis if (m.get("municipality") or "").strip().casefold() == want),
        munis[0],
    )
    lat, lon = chosen.get("latitude"), chosen.get("longitude")
    return {
        "entity_id": chosen.get("id"),
        "entity_type": ENTITY_MUNICIPALITY,
        "name": chosen.get("municipality") or chosen.get("suggestFirstRow"),
        "municipality_id": chosen.get("municipality_id"),
        "municipality_seo_name": chosen.get("municipality_seo_name"),
        "district": chosen.get("district"),
        "district_id": chosen.get("district_id"),
        "district_seo_name": chosen.get("district_seo_name"),
        "region": chosen.get("region"),
        "region_id": chosen.get("region_id"),
        "region_seo_name": chosen.get("region_seo_name"),
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
    }
