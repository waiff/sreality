"""find_comparables: spatial + attribute search over `listings` table.

Pure function over a psycopg connection. Builds parameterised SQL
dynamically based on which filters are set; never string-interpolates
user values into the query body.

How to add a new filter
-----------------------
1. Add a field to ComparableFilters with a None default and a clear
   type. None must mean "no filter applied".
2. Add a branch in build_query() that appends the WHERE clause and
   binds the value via params[name] = filters.<name>.
3. Add the field to _filters_used() so the metadata block echoes it.
4. Add a hermetic test in test_comparables.py asserting both presence
   when set and absence when None.

That's the entire change. SELECT projection, ORDER BY, LIMIT, and the
spatial / freshness clauses don't need to be touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from toolkit.filter_registry import (
    FURNISHED_CANONICAL,
    OWNERSHIP_CANONICAL,
    UNKNOWN_FILTER_VALUE,
)

if TYPE_CHECKING:
    import psycopg


@dataclass(frozen=True)
class TargetSpec:
    lat: float
    lng: float
    area_m2: float | None = None
    disposition: str | None = None
    floor: int | None = None
    exclude_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ComparableFilters:
    radius_m: int = 1000
    area_band_pct: float = 0.20
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    # No implicit freshness gate. Callers that want "active and seen
    # within N days" must say so explicitly. The agent and the
    # deterministic estimator both pass these on demand.
    max_age_days: int | None = None
    active_only: bool = False
    population: Literal["active", "delisted", "all"] | None = None
    floor_band: int | None = None
    portals: list[str] | None = None
    condition_match: list[str] | None = None
    building_type_match: list[str] | None = None
    energy_rating_match: list[str] | None = None
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    # Price per m² (computed: price_czk / NULLIF(area_m2, 0)). NULL area_m2
    # falls out when either bound is set.
    min_price_per_m2: float | None = None
    max_price_per_m2: float | None = None
    # MF gross rental yield % (migration 133). Sale apartments only; NULL on
    # everything else, so they fall out when either bound is set.
    min_mf_gross_yield_pct: float | None = None
    max_mf_gross_yield_pct: float | None = None
    # Default None means "no category filter" — search every category.
    # There is deliberately no implicit apartment-rental default: callers
    # that want one category pass it explicitly (the request schemas in
    # api/schemas.py require it; the estimation path threads it from
    # CreateEstimationIn). A silent "byt"/"pronajem" default used to make
    # house and commercial cohorts impossible to drive cleanly.
    category_main: str | None = None
    category_type: str | None = None
    category_sub_cb: int | None = None
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False
    # Multi-select enums. Each may carry the `__unknown__` sentinel meaning
    # "NULL or a non-canonical value" — see _enum_or_unknown_clause.
    furnished: list[str] | None = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    ownership: list[str] | None = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    min_parking_lots: int | None = None
    # Derived condition scores (migrations 072/073). NULL rows are filtered
    # out by the `>= N` comparison — that's intentional: "show me 4+" means
    # "I want scored listings at level 4 or above", not "scored OR unscored".
    building_condition_level_min: int | None = None
    apartment_condition_level_min: int | None = None
    # TOM ("turned in") = time on market in days. Mirrors migration 052's
    # listings_public.tom_days: now() - first_seen_at for active rows,
    # last_seen_at - first_seen_at for delisted. Inclusive bounds.
    tom_days_min: int | None = None
    tom_days_max: int | None = None
    # Days-ago ranges on the source timestamps. min_days = most recent
    # allowed (e.g. min=3 means "seen >= 3 days ago", so excludes
    # listings seen in the last 2 days). max_days = oldest allowed.
    last_seen_min_days: int | None = None
    last_seen_max_days: int | None = None
    first_seen_min_days: int | None = None
    first_seen_max_days: int | None = None
    # City-quality filters (Phase QUAL). Browse + Watchdog only — the
    # registry's agenda gating keeps these out of the agent's tool
    # schema. Each entry in `city_index_rules` is a dict
    # `{"index_name": str, "op": ">="|"<=", "value": float}`. Rules are
    # AND'd. A listing matches when there exists a curated city C such
    # that the listing is within C.default_radius_m of C.centroid AND
    # every index rule holds for C.
    city_index_rules: list[dict[str, Any]] | None = None
    min_city_population: int | None = None
    max_city_population: int | None = None
    # Proximity: `{"index_rules": [...], "population_min": int|null,
    # "radius_km": int}`. A listing matches when there exists a curated
    # city C within `radius_km*1000` of the listing AND the inner rules
    # all hold for C. `radius_km` defaults to 5 in the UI but the SQL
    # requires it explicit.
    near_city_proximity: dict[str, Any] | None = None
    # Fast polygon-edge proximity (migration 142). Precomputed columns on
    # properties_public, filtered `>= value`. BROWSE / WATCHDOG only — the
    # listings-grain comparables agenda never sets these (agenda-gated), so
    # the `l.home_obec_pop` / `l.near_*` references never materialise here.
    near_pop_5km_min: int | None = None
    near_pop_15km_min: int | None = None
    near_jobs_5km_min: float | None = None
    near_jobs_15km_min: float | None = None
    near_youth_5km_min: float | None = None
    near_youth_15km_min: float | None = None
    near_overall_5km_min: float | None = None
    near_overall_15km_min: float | None = None


_DISPOSITION_LOOSE: dict[str, tuple[str, ...]] = {
    "1+kk": ("1+kk", "1+1"),
    "1+1":  ("1+kk", "1+1"),
    "2+kk": ("2+kk", "2+1"),
    "2+1":  ("2+kk", "2+1"),
    "3+kk": ("3+kk", "3+1"),
    "3+1":  ("3+kk", "3+1"),
    "4+kk": ("4+kk", "4+1"),
    "4+1":  ("4+kk", "4+1"),
    "5+kk": ("5+kk", "5+1"),
    "5+1":  ("5+kk", "5+1"),
}

_HARD_LIMIT = 500


_ALLOWED_OPS: frozenset[str] = frozenset({">=", "<=", "==", "!=", ">", "<"})


def _index_rule_predicate(
    alias: str,
    rule: dict[str, Any],
    pname_idx: str,
    pname_val: str,
) -> str:
    """Render one index-rule predicate against a city_index_values_public alias.

    `rule['op']` is sanitised against `_ALLOWED_OPS`; defaults to `>=`.
    Both rule values are bound; only the operator token is inlined.
    """
    op = rule.get("op", ">=")
    if op not in _ALLOWED_OPS:
        op = ">="
    return (
        f"EXISTS ("
        f"SELECT 1 FROM city_index_values_public {alias} "
        f"WHERE {alias}.city_id = c.city_id "
        f"AND {alias}.index_name = %({pname_idx})s "
        f"AND {alias}.value {op} %({pname_val})s)"
    )


def _city_quality_clauses(
    filters: ComparableFilters,
) -> tuple[list[str], dict[str, Any]]:
    """Render the Phase QUAL clauses (city quality, population, proximity).

    Three concerns, one helper, isolated from the rest of
    `_shared_filter_where` so the Watchdog matcher can reuse the same
    code path. Returns the clause list + parameter additions.
    """
    where: list[str] = []
    params: dict[str, Any] = {}

    rules = filters.city_index_rules or []
    pop_min = filters.min_city_population
    pop_max = filters.max_city_population

    # Population now reads the precomputed home_obec_pop column (migration 142)
    # — the listing's OWN municipality population, country-wide, no curated-city
    # join. Browse / Watchdog grain only (properties_public exposes it); the
    # listings-grain comparables agenda never sets these.
    if pop_min is not None:
        where.append("l.home_obec_pop >= %(min_city_population)s")
        params["min_city_population"] = pop_min
    if pop_max is not None:
        where.append("l.home_obec_pop <= %(max_city_population)s")
        params["max_city_population"] = pop_max

    # Fast polygon-edge proximity columns (migration 142). Plain `>= value`
    # predicates against the precomputed maxes within a fixed 5 / 15 km.
    _PROX_COLS = (
        ("near_pop_5km_min", "near_pop_5km"),
        ("near_pop_15km_min", "near_pop_15km"),
        ("near_jobs_5km_min", "near_jobs_5km"),
        ("near_jobs_15km_min", "near_jobs_15km"),
        ("near_youth_5km_min", "near_youth_5km"),
        ("near_youth_15km_min", "near_youth_15km"),
        ("near_overall_5km_min", "near_overall_5km"),
        ("near_overall_15km_min", "near_overall_15km"),
    )
    for attr, col in _PROX_COLS:
        val = getattr(filters, attr, None)
        if val is not None:
            where.append(f"l.{col} >= %({attr})s")
            params[attr] = val

    if rules:
        # Polygon containment (migration 081) when the curated city is
        # wired to an obec admin_boundary; centroid+radius is the
        # fallback for cities that didn't match a RÚIAN obec by name.
        sub_where: list[str] = [
            "((c.admin_boundary_id IS NOT NULL "
            "AND ST_Covers(b.geom, l.geom)) "
            "OR (c.admin_boundary_id IS NULL "
            "AND ST_DWithin("
            "l.geom, "
            "ST_SetSRID(ST_MakePoint(c.lng, c.lat), 4326)::geography, "
            "c.default_radius_m)))"
        ]
        for i, rule in enumerate(rules):
            idx_p, val_p = f"ciq_rule_{i}_name", f"ciq_rule_{i}_val"
            sub_where.append(_index_rule_predicate(f"viq_{i}", rule, idx_p, val_p))
            params[idx_p] = rule["index_name"]
            params[val_p] = rule["value"]
        where.append(
            "EXISTS (SELECT 1 FROM curated_cities_public c "
            "LEFT JOIN admin_boundaries_public b "
            "ON b.id = c.admin_boundary_id "
            "WHERE "
            + " AND ".join(sub_where)
            + ")"
        )

    prox = filters.near_city_proximity
    if prox:
        prox_rules = prox.get("index_rules") or []
        radius_km = prox.get("radius_km")
        if not isinstance(radius_km, (int, float)) or radius_km <= 0:
            raise ValueError("near_city_proximity.radius_km must be > 0")
        sub_where = [
            "ST_DWithin("
            "l.geom, "
            "ST_SetSRID(ST_MakePoint(c.lng, c.lat), 4326)::geography, "
            "%(near_city_radius_m)s)"
        ]
        params["near_city_radius_m"] = int(radius_km) * 1000
        for i, rule in enumerate(prox_rules):
            idx_p, val_p = f"ciq_prox_{i}_name", f"ciq_prox_{i}_val"
            sub_where.append(_index_rule_predicate(f"vp_{i}", rule, idx_p, val_p))
            params[idx_p] = rule["index_name"]
            params[val_p] = rule["value"]
        prox_pop_min = prox.get("population_min")
        if prox_pop_min is not None:
            sub_where.append("c.population >= %(near_city_population_min)s")
            params["near_city_population_min"] = prox_pop_min
        where.append(
            "EXISTS (SELECT 1 FROM curated_cities_public c WHERE "
            + " AND ".join(sub_where)
            + ")"
        )

    return where, params


def _enum_or_unknown_clause(
    values: list[str],
    col: str,
    pname: str,
    canonical: tuple[str, ...],
    params: dict[str, Any],
) -> str | None:
    """WHERE fragment for a multi-select enum that may carry the `__unknown__`
    sentinel. Real values match by `= ANY(...)`; `__unknown__` matches NULL or
    any value outside the canonical set. Returns None when nothing to filter."""
    reals = [v for v in values if v != UNKNOWN_FILTER_VALUE]
    parts: list[str] = []
    if reals:
        parts.append(f"{col} = ANY(%({pname})s)")
        params[pname] = reals
    if UNKNOWN_FILTER_VALUE in values:
        parts.append(f"({col} IS NULL OR NOT ({col} = ANY(%({pname}_canon)s)))")
        params[f"{pname}_canon"] = list(canonical)
    if not parts:
        return None
    return "(" + " OR ".join(parts) + ")"


def _shared_filter_where(
    target: TargetSpec, filters: ComparableFilters
) -> tuple[list[str], dict[str, Any]]:
    """Build WHERE clauses + bound params shared across all spatial tools.

    Includes: spatial radius, category, disposition, area band, floor band,
    condition/building/energy filters, amenity booleans, price bounds,
    locality IDs, exclude_ids, and the failure-row exclusion.

    Does NOT include the active_only / max_age_days clauses — those are
    operational rather than attribute filters, and different consumers
    (find_comparables vs compute_market_velocity) want different
    semantics. Each caller appends its own.
    """
    params: dict[str, Any] = {
        "lat": target.lat,
        "lng": target.lng,
        "radius_m": filters.radius_m,
    }
    where: list[str] = [
        "l.geom IS NOT NULL",
        (
            "ST_DWithin("
            "l.geom, "
            "ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography, "
            "%(radius_m)s)"
        ),
    ]

    if filters.category_main is not None:
        where.append("l.category_main = %(category_main)s")
        params["category_main"] = filters.category_main
    if filters.category_type is not None:
        where.append("l.category_type = %(category_type)s")
        params["category_type"] = filters.category_type

    if target.disposition is not None:
        if filters.disposition_match == "exact":
            where.append("l.disposition = %(disposition)s")
            params["disposition"] = target.disposition
        elif filters.disposition_match == "loose":
            group = _DISPOSITION_LOOSE.get(
                target.disposition, (target.disposition,)
            )
            where.append("l.disposition = ANY(%(disposition_loose)s)")
            params["disposition_loose"] = list(group)
        # "any": no clause

    if target.area_m2 is not None:
        where.append("l.area_m2 BETWEEN %(area_min)s AND %(area_max)s")
        params["area_min"] = target.area_m2 * (1 - filters.area_band_pct)
        params["area_max"] = target.area_m2 * (1 + filters.area_band_pct)

    if filters.floor_band is not None and target.floor is not None:
        where.append("l.floor BETWEEN %(floor_min)s AND %(floor_max)s")
        params["floor_min"] = target.floor - filters.floor_band
        params["floor_max"] = target.floor + filters.floor_band

    if filters.portals:
        where.append("l.source = ANY(%(portals)s)")
        params["portals"] = list(filters.portals)
    if filters.condition_match:
        where.append("l.condition = ANY(%(condition_match)s)")
        params["condition_match"] = list(filters.condition_match)
    if filters.building_type_match:
        where.append("l.building_type = ANY(%(building_type_match)s)")
        params["building_type_match"] = list(filters.building_type_match)
    if filters.energy_rating_match:
        where.append("l.energy_rating = ANY(%(energy_rating_match)s)")
        params["energy_rating_match"] = list(filters.energy_rating_match)

    if filters.has_balcony is not None:
        where.append("l.has_balcony = %(has_balcony)s")
        params["has_balcony"] = filters.has_balcony
    if filters.has_lift is not None:
        where.append("l.has_lift = %(has_lift)s")
        params["has_lift"] = filters.has_lift
    if filters.has_parking is not None:
        where.append("l.has_parking = %(has_parking)s")
        params["has_parking"] = filters.has_parking

    if filters.min_price_czk is not None:
        where.append("l.price_czk >= %(min_price_czk)s")
        params["min_price_czk"] = filters.min_price_czk
    if filters.max_price_czk is not None:
        where.append("l.price_czk <= %(max_price_czk)s")
        params["max_price_czk"] = filters.max_price_czk
    if filters.min_price_per_m2 is not None:
        where.append(
            "l.price_czk::numeric / NULLIF(l.area_m2, 0) >= %(min_price_per_m2)s"
        )
        params["min_price_per_m2"] = filters.min_price_per_m2
    if filters.max_price_per_m2 is not None:
        where.append(
            "l.price_czk::numeric / NULLIF(l.area_m2, 0) <= %(max_price_per_m2)s"
        )
        params["max_price_per_m2"] = filters.max_price_per_m2
    if filters.min_mf_gross_yield_pct is not None:
        where.append("l.mf_gross_yield_pct >= %(min_mf_gross_yield_pct)s")
        params["min_mf_gross_yield_pct"] = filters.min_mf_gross_yield_pct
    if filters.max_mf_gross_yield_pct is not None:
        where.append("l.mf_gross_yield_pct <= %(max_mf_gross_yield_pct)s")
        params["max_mf_gross_yield_pct"] = filters.max_mf_gross_yield_pct

    if filters.locality_district_id is not None:
        where.append("l.locality_district_id = %(locality_district_id)s")
        params["locality_district_id"] = filters.locality_district_id
    if filters.locality_region_id is not None:
        where.append("l.locality_region_id = %(locality_region_id)s")
        params["locality_region_id"] = filters.locality_region_id

    if filters.category_sub_cb is not None:
        where.append("l.category_sub_cb = %(category_sub_cb)s")
        params["category_sub_cb"] = filters.category_sub_cb

    if filters.furnished:
        clause = _enum_or_unknown_clause(
            list(filters.furnished), "l.furnished", "furnished",
            FURNISHED_CANONICAL, params,
        )
        if clause:
            where.append(clause)
    if filters.ownership:
        clause = _enum_or_unknown_clause(
            list(filters.ownership), "l.ownership", "ownership",
            OWNERSHIP_CANONICAL, params,
        )
        if clause:
            where.append(clause)

    if filters.terrace is not None:
        where.append("l.terrace = %(terrace)s")
        params["terrace"] = filters.terrace
    if filters.cellar is not None:
        where.append("l.cellar = %(cellar)s")
        params["cellar"] = filters.cellar
    if filters.garage is not None:
        where.append("l.garage = %(garage)s")
        params["garage"] = filters.garage

    if filters.min_estate_area is not None:
        where.append("l.estate_area >= %(min_estate_area)s")
        params["min_estate_area"] = filters.min_estate_area
    if filters.max_estate_area is not None:
        where.append("l.estate_area <= %(max_estate_area)s")
        params["max_estate_area"] = filters.max_estate_area
    if filters.min_parking_lots is not None:
        where.append("l.parking_lots >= %(min_parking_lots)s")
        params["min_parking_lots"] = filters.min_parking_lots
    if filters.building_condition_level_min is not None:
        where.append("l.building_condition_level >= %(building_condition_level_min)s")
        params["building_condition_level_min"] = filters.building_condition_level_min
    if filters.apartment_condition_level_min is not None:
        where.append("l.apartment_condition_level >= %(apartment_condition_level_min)s")
        params["apartment_condition_level_min"] = filters.apartment_condition_level_min

    # TOM bounds. The expression mirrors migration 052's
    # listings_public.tom_days computation so SQL and Python agree on
    # the definition of "days on market".
    _tom_expr = (
        "(case when l.is_active "
        "then greatest(0, floor(extract(epoch from (now() - l.first_seen_at)) / 86400)::int) "
        "else greatest(0, floor(extract(epoch from (l.last_seen_at - l.first_seen_at)) / 86400)::int) "
        "end)"
    )
    if filters.tom_days_min is not None:
        where.append(f"{_tom_expr} >= %(tom_days_min)s")
        params["tom_days_min"] = filters.tom_days_min
    if filters.tom_days_max is not None:
        where.append(f"{_tom_expr} <= %(tom_days_max)s")
        params["tom_days_max"] = filters.tom_days_max

    if filters.last_seen_max_days is not None:
        where.append(
            "l.last_seen_at >= now() - make_interval(days => %(last_seen_max_days)s)"
        )
        params["last_seen_max_days"] = filters.last_seen_max_days
    if filters.last_seen_min_days is not None:
        where.append(
            "l.last_seen_at <= now() - make_interval(days => %(last_seen_min_days)s)"
        )
        params["last_seen_min_days"] = filters.last_seen_min_days
    if filters.first_seen_max_days is not None:
        where.append(
            "l.first_seen_at >= now() - make_interval(days => %(first_seen_max_days)s)"
        )
        params["first_seen_max_days"] = filters.first_seen_max_days
    if filters.first_seen_min_days is not None:
        where.append(
            "l.first_seen_at <= now() - make_interval(days => %(first_seen_min_days)s)"
        )
        params["first_seen_min_days"] = filters.first_seen_min_days

    city_clauses, city_params = _city_quality_clauses(filters)
    where.extend(city_clauses)
    params.update(city_params)

    if not filters.include_unreliable:
        where.append(
            "NOT EXISTS ("
            "SELECT 1 FROM listing_fetch_failures lff "
            "WHERE lff.sreality_id = l.sreality_id AND lff.given_up = true"
            ")"
        )

    if target.exclude_ids:
        where.append("l.sreality_id <> ALL(%(exclude_ids)s)")
        params["exclude_ids"] = list(target.exclude_ids)

    return where, params


def build_query(
    target: TargetSpec, filters: ComparableFilters
) -> tuple[str, dict[str, Any]]:
    """Render the SQL and parameter dict for the given target+filters.

    Exposed so tests can assert on shape without a DB connection.
    """
    where, params = _shared_filter_where(target, filters)

    if filters.population == "delisted":
        where.append("l.is_active = false")
    elif filters.population == "all":
        pass
    elif filters.population == "active":
        where.append("l.is_active = true")
        if filters.max_age_days is not None:
            where.append(
                "l.last_seen_at > now() - make_interval(days => %(max_age_days)s)"
            )
            params["max_age_days"] = filters.max_age_days
    elif filters.active_only:
        where.append("l.is_active = true")
        if filters.max_age_days is not None:
            where.append(
                "l.last_seen_at > now() - make_interval(days => %(max_age_days)s)"
            )
            params["max_age_days"] = filters.max_age_days

    sql = (
        "SELECT\n"
        "  l.sreality_id, l.price_czk, l.area_m2,\n"
        "  (l.price_czk::numeric / NULLIF(l.area_m2, 0)) AS price_per_m2,\n"
        "  l.disposition, l.district,\n"
        "  l.locality_district_id, l.locality_region_id,\n"
        "  l.floor, l.total_floors,\n"
        "  l.building_type, l.condition, l.energy_rating,\n"
        "  l.has_balcony, l.has_lift, l.has_parking,\n"
        "  l.estate_area, l.usable_area, l.garden_area,\n"
        "  l.category_sub_cb,\n"
        "  l.furnished, l.terrace, l.cellar, l.garage,\n"
        "  l.parking_lots, l.ownership,\n"
        "  ST_Distance(\n"
        "    l.geom,\n"
        "    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography\n"
        "  ) AS distance_m,\n"
        "  l.first_seen_at, l.last_seen_at,\n"
        "  EXTRACT(DAY FROM (now() - l.last_seen_at))::int AS data_age_days,\n"
        "  latest_snap.id AS latest_snapshot_id,\n"
        "  latest_snap.scraped_at AS latest_snapshot_at,\n"
        "  latest_check.checked_at AS last_freshness_check_at\n"
        "FROM listings l\n"
        "LEFT JOIN LATERAL (\n"
        "  SELECT id, scraped_at FROM listing_snapshots\n"
        "  WHERE sreality_id = l.sreality_id\n"
        "  ORDER BY scraped_at DESC LIMIT 1\n"
        ") latest_snap ON true\n"
        "LEFT JOIN LATERAL (\n"
        "  SELECT checked_at FROM listing_freshness_checks\n"
        "  WHERE sreality_id = l.sreality_id\n"
        "  ORDER BY checked_at DESC LIMIT 1\n"
        ") latest_check ON true\n"
        "WHERE " + "\n  AND ".join(where) + "\n"
        "ORDER BY distance_m\n"
        f"LIMIT {_HARD_LIMIT}"
    )
    return sql, params


def _filters_used(target: TargetSpec, filters: ComparableFilters) -> dict[str, Any]:
    return {
        "target": {
            "lat": target.lat,
            "lng": target.lng,
            "area_m2": target.area_m2,
            "disposition": target.disposition,
            "floor": target.floor,
            "exclude_ids": list(target.exclude_ids),
        },
        "radius_m": filters.radius_m,
        "area_band_pct": filters.area_band_pct,
        "disposition_match": filters.disposition_match,
        "max_age_days": filters.max_age_days,
        "active_only": filters.active_only,
        "population": filters.population,
        "floor_band": filters.floor_band,
        "portals": list(filters.portals) if filters.portals else None,
        "condition_match": (
            list(filters.condition_match)
            if filters.condition_match else None
        ),
        "building_type_match": (
            list(filters.building_type_match)
            if filters.building_type_match else None
        ),
        "energy_rating_match": (
            list(filters.energy_rating_match)
            if filters.energy_rating_match else None
        ),
        "has_balcony": filters.has_balcony,
        "has_lift": filters.has_lift,
        "has_parking": filters.has_parking,
        "min_price_czk": filters.min_price_czk,
        "max_price_czk": filters.max_price_czk,
        "min_price_per_m2": filters.min_price_per_m2,
        "max_price_per_m2": filters.max_price_per_m2,
        "min_mf_gross_yield_pct": filters.min_mf_gross_yield_pct,
        "max_mf_gross_yield_pct": filters.max_mf_gross_yield_pct,
        "category_main": filters.category_main,
        "category_type": filters.category_type,
        "category_sub_cb": filters.category_sub_cb,
        "locality_district_id": filters.locality_district_id,
        "locality_region_id": filters.locality_region_id,
        "include_unreliable": filters.include_unreliable,
        "furnished": list(filters.furnished) if filters.furnished else None,
        "ownership": list(filters.ownership) if filters.ownership else None,
        "terrace": filters.terrace,
        "cellar": filters.cellar,
        "garage": filters.garage,
        "min_estate_area": filters.min_estate_area,
        "max_estate_area": filters.max_estate_area,
        "min_parking_lots": filters.min_parking_lots,
        "building_condition_level_min": filters.building_condition_level_min,
        "apartment_condition_level_min": filters.apartment_condition_level_min,
        "tom_days_min": filters.tom_days_min,
        "tom_days_max": filters.tom_days_max,
        "last_seen_min_days": filters.last_seen_min_days,
        "last_seen_max_days": filters.last_seen_max_days,
        "first_seen_min_days": filters.first_seen_min_days,
        "first_seen_max_days": filters.first_seen_max_days,
    }


def find_comparables(
    conn: "psycopg.Connection",
    target: TargetSpec,
    filters: ComparableFilters,
) -> dict[str, Any]:
    from toolkit import _max_last_seen, _now_iso

    sql, params = build_query(target, filters)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []

    listings = [_row_to_dict(cols, row) for row in rows]
    return {
        "data": {"listings": listings},
        "metadata": {
            "tool": "find_comparables",
            "filters_used": _filters_used(target, filters),
            "result_count": len(listings),
            "queried_at": _now_iso(),
            "data_freshness": _max_last_seen(listings),
            **_cohort_freshness_stats(listings),
        },
    }


_DATETIME_COLS = (
    "first_seen_at", "last_seen_at",
    "latest_snapshot_at", "last_freshness_check_at",
)


def _row_to_dict(cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
    out = dict(zip(cols, row))
    for k in _DATETIME_COLS:
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    for k in ("area_m2", "price_per_m2", "distance_m"):
        v = out.get(k)
        if v is not None:
            out[k] = float(v)
    return out


def _cohort_freshness_stats(listings: list[dict[str, Any]]) -> dict[str, Any]:
    ages = [
        l["data_age_days"] for l in listings
        if isinstance(l.get("data_age_days"), int)
    ]
    unverified = sum(
        1 for l in listings if l.get("last_freshness_check_at") is None
    )
    if not ages:
        return {
            "oldest_data_age_days": None,
            "newest_data_age_days": None,
            "median_data_age_days": None,
            "unverified_count": unverified,
        }
    sorted_ages = sorted(ages)
    n = len(sorted_ages)
    if n % 2 == 1:
        median = float(sorted_ages[n // 2])
    else:
        median = (sorted_ages[n // 2 - 1] + sorted_ages[n // 2]) / 2.0
    return {
        "oldest_data_age_days": max(ages),
        "newest_data_age_days": min(ages),
        "median_data_age_days": median,
        "unverified_count": unverified,
    }


_DEFAULT_RELAXATION_LADDER: tuple[str, ...] = (
    "radius_x1.5",
    "area_band_+0.10",
    "disposition_loose",
    "radius_x2",
    "area_band_+0.20",
    "disposition_any",
    "drop_condition",
    "drop_building_type",
    "drop_energy_rating",
    "drop_floor_band",
)


def _apply_relaxation(
    filters: ComparableFilters, base: ComparableFilters, action: str,
) -> ComparableFilters:
    """Return a new ComparableFilters with the named relaxation applied.

    Cumulative actions (radius_xN, area_band_+X) are computed off `base`
    (the original strict filters) so applying step k always yields the
    same widened value regardless of the order of intermediate steps.
    """
    if action == "radius_x1.5":
        return replace(filters, radius_m=int(round(base.radius_m * 1.5)))
    if action == "radius_x2":
        return replace(filters, radius_m=int(round(base.radius_m * 2.0)))
    if action == "area_band_+0.10":
        return replace(filters, area_band_pct=base.area_band_pct + 0.10)
    if action == "area_band_+0.20":
        return replace(filters, area_band_pct=base.area_band_pct + 0.20)
    if action == "disposition_loose":
        if filters.disposition_match == "any":
            return filters
        return replace(filters, disposition_match="loose")
    if action == "disposition_any":
        return replace(filters, disposition_match="any")
    if action == "drop_condition":
        return replace(filters, condition_match=None)
    if action == "drop_building_type":
        return replace(filters, building_type_match=None)
    if action == "drop_energy_rating":
        return replace(filters, energy_rating_match=None)
    if action == "drop_floor_band":
        return replace(filters, floor_band=None)
    raise ValueError(f"unknown relaxation action: {action}")


def find_comparables_relaxed(
    conn: "psycopg.Connection",
    target: TargetSpec,
    filters: ComparableFilters,
    min_results: int = 5,
    relaxation_ladder: list[str] | None = None,
) -> dict[str, Any]:
    """Wrap find_comparables with a deterministic relaxation ladder.

    Runs the strict query first. If result_count < min_results, walks
    `relaxation_ladder` (default `_DEFAULT_RELAXATION_LADDER`), applying
    each action in order until the cohort hits min_results or the ladder
    is exhausted. Every intermediate step is recorded in
    `data.relaxation_trace` for full provenance. Locality, category,
    price bounds, and active_only are NEVER relaxed — they encode user
    intent.
    """
    from toolkit import _max_last_seen, _now_iso

    ladder = (
        list(relaxation_ladder)
        if relaxation_ladder is not None
        else list(_DEFAULT_RELAXATION_LADDER)
    )

    base = filters
    current = filters
    trace: list[dict[str, Any]] = []
    last_result: dict[str, Any] = find_comparables(conn, target, current)
    trace.append({
        "step": 0,
        "action": None,
        "filters_snapshot": _filters_used(target, current),
        "result_count": last_result["metadata"]["result_count"],
    })

    relaxations_applied = 0
    if last_result["metadata"]["result_count"] < min_results:
        for action in ladder:
            current = _apply_relaxation(current, base, action)
            last_result = find_comparables(conn, target, current)
            relaxations_applied += 1
            trace.append({
                "step": relaxations_applied,
                "action": action,
                "filters_snapshot": _filters_used(target, current),
                "result_count": last_result["metadata"]["result_count"],
            })
            if last_result["metadata"]["result_count"] >= min_results:
                break

    listings = last_result["data"]["listings"]
    return {
        "data": {
            "listings": listings,
            "relaxation_trace": trace,
            "min_results_satisfied": len(listings) >= min_results,
        },
        "metadata": {
            "tool": "find_comparables_relaxed",
            "filters_used": _filters_used(target, current),
            "result_count": len(listings),
            "queried_at": _now_iso(),
            "data_freshness": _max_last_seen(listings),
            "relaxations_applied": relaxations_applied,
            "min_results": min_results,
            **_cohort_freshness_stats(listings),
        },
    }
