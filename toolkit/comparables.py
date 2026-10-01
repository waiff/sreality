"""find_comparables: spatial + attribute search over `listings` table.

Pure function over a psycopg connection. Builds parameterised SQL
dynamically based on which filters are set; never string-interpolates
user values into the query body.

How to add a filter
-------------------
Declare a FilterDef in toolkit/filter_registry.py. A column-backed bound, list or flag compiles
from its derived `sql_kind` (toolkit/filter_compiler.py, and Browse's TS auto-dispatch); anything
else needs one `_HOOKS` entry there. Add the field to the consumer model (ComparableFilters /
WatchdogFilterSpec) and to `_filters_used`. tests/toolkit/test_filter_compiler.py checks both
directions (every model field is a registry id; every agenda id has a model field, today's
known gaps M1/M2 pinned by name), so a missing field fails CI instead of being dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from toolkit.filter_compiler import LISTINGS_GRAIN, compile_filter_where
from toolkit.measures import (
    cohort_basis,
    measure_backed,
    per_m2_basis_sql,
    per_m2_sql,
    spec_ppm2_basis,
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
    # The subject's adverts (listings.id): EVERY advert of their properties is left out of
    # the cohort (decision 13), so naming any one of them excludes all of its siblings.
    exclude_listing_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ComparableFilters:
    radius_m: int = 1000
    area_band_pct: float = 0.20
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    # No implicit freshness gate. Callers that want "active and seen
    # within N days" must say so explicitly. The agent and the
    # deterministic estimator both pass these on demand.
    max_age_days: int | None = None
    # The single lifecycle selector. `active` = is_active=true (plus the
    # max_age_days recency gate when set); `delisted` = is_active=false —
    # the ADVERTISEMENT ended, which is not a sale and carries no
    # transacted price (migration 453's header, on 70,130 long-unseen
    # rows: not "probably sold": unknown). Registered sales are their own
    # store, `sold_transactions`. `all` / None = no is_active gate. Rendered by
    # `_lifecycle_where`, the one place Toolkit rule #4's "active"
    # definition lives.
    lifecycle: Literal["active", "delisted", "all"] | None = None
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
    # Price per m²: THE measure (`measure_price_per_m2`, migration 425) --
    # basis-resolved from (category_main, category_type) and withheld below its
    # basis floor. Rows with no area, no price, an undecidable basis, or a
    # sub-floor price fall out when either bound is set.
    min_price_per_m2: float | None = None
    max_price_per_m2: float | None = None
    # Default None means "no category filter" — search every category.
    # There is deliberately no implicit apartment-rental default: callers
    # that want one category pass it explicitly (the request schemas in
    # api/schemas.py require it; the estimation path threads it from
    # CreateEstimationIn). A silent "byt"/"pronajem" default used to make
    # house and commercial cohorts impossible to drive cleanly.
    category_main: str | None = None
    category_type: str | None = None
    category_sub_cb: int | None = None
    include_unreliable: bool = False
    # Multi-select enums. Each may carry the `__unknown__` sentinel meaning
    # "NULL or a non-canonical value" (toolkit/filter_compiler._enum_or_unknown_clause).
    furnished: list[str] | None = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    ownership: list[str] | None = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_parking_lots: int | None = None
    # Derived condition scores (migrations 072/073). NULL rows are filtered
    # out by the `>= N` / `<= N` comparison — that's intentional: "show me
    # 4+" means "I want scored listings at level 4 or above", not "scored
    # OR unscored".
    building_condition_level_min: int | None = None
    building_condition_level_max: int | None = None
    apartment_condition_level_min: int | None = None
    apartment_condition_level_max: int | None = None
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


def _shared_filter_where(
    target: TargetSpec, filters: ComparableFilters, *, radius: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Build WHERE clauses + bound params shared across all spatial tools.

    The listings adapter: target-relative clauses and the cohort invariants here, every
    registry filter through `compile_filter_where(…, LISTINGS_GRAIN)`. `radius=False`
    (the transit corridor) leaves out the target circle and its param, nothing else.

    Does NOT include the lifecycle / max_age_days clauses — those are
    operational rather than attribute filters. Each caller appends them
    via the shared `_lifecycle_where` helper.

    The spatial pair reads `ll` — every caller's FROM must carry
    `JOIN listing_location ll ON ll.listing_id = l.id` (W4-a). The `::geography`
    cast is not optional: `listing_location.geom` is geometry(Point,4326), and
    `ST_DWithin(geometry, geometry, n)` measures n in DEGREES, so an uncast call
    would silently return a ~111 km cohort for a 1 km radius. The cast is served
    by `listing_location_geog_gist` (migration 507).
    """
    params: dict[str, Any] = {"lat": target.lat, "lng": target.lng}
    where: list[str] = [
        # THIS IS THE W5 CONSUMER RULE, in its strictly narrower form — do not remove it
        # thinking the spatial clause below already implies it. A comparable must have a
        # POINT, so the foreign arm of `claims_common.SERVED_LOCATION_PREDICATE` is the
        # one thing this lane drops; everything the rule hides, this hides too. Pinned by
        # tests/test_location_w5_serve_resolved.py.
        "ll.geom IS NOT NULL",
    ]
    if radius:
        # Emitted whenever radius is on, even for a NULL radius_m (the agent can send one):
        # that is an empty cohort, never a nationwide one.
        where.append(
            "ST_DWithin("
            "ll.geom::geography, "
            "ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography, "
            "%(radius_m)s)"
        )
        params["radius_m"] = filters.radius_m

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

    compiled, compiled_params = compile_filter_where(vars(filters), LISTINGS_GRAIN)
    where.extend(compiled)
    params.update(compiled_params)

    if not filters.include_unreliable:
        # Stays sreality-keyed: listing_fetch_failures is a queue table, not an R2
        # carrier (rule 5), so it has no listing_id to join on. Post-Gate-2 this
        # fails OPEN — `NULL = NULL` finds no failure row, NOT EXISTS is true, the
        # listing is KEPT — so the filter degrades to a no-op for non-sreality
        # rows rather than dropping them. Acceptable; re-key only if that table
        # ever gains a listing_id.
        where.append(
            "NOT EXISTS ("
            "SELECT 1 FROM listing_fetch_failures lff "
            "WHERE lff.sreality_id = l.sreality_id AND lff.given_up = true"
            ")"
        )

    # Decision 13: a property counts ONCE, as its canonical advert (`repr_listing_ref_id`, which
    # the rollup writes from property_canonical_listings, migration 561), and every advert of
    # the subject's property is out. An advert not yet attached to a property is no comparable.
    where.append(
        "EXISTS (SELECT 1 FROM properties canon_p "
        "WHERE canon_p.id = l.property_id AND canon_p.repr_listing_ref_id = l.id)"
    )
    if target.exclude_listing_ids:
        where.append(
            "NOT EXISTS (SELECT 1 FROM listings subj "
            "WHERE subj.id = ANY(%(exclude_listing_ids)s) AND subj.property_id = l.property_id)"
        )
        params["exclude_listing_ids"] = list(target.exclude_listing_ids)

    return where, params


def _lifecycle_where(
    lifecycle: str | None, max_age_days: int | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Render the is_active lifecycle gate (+ optional recency) for a cohort.

    The single source of truth for Toolkit rule #4's "active" definition.
    Every cohort query — comparables, market velocity, the transit-axis
    search — renders its is_active clause here, so the surfaces can never
    drift. `active` couples the optional max_age_days freshness gate;
    `delisted` is is_active=false; `all`/None emit nothing.
    """
    where: list[str] = []
    params: dict[str, Any] = {}
    if lifecycle == "active":
        where.append("l.is_active = true")
        if max_age_days is not None:
            where.append(
                "l.last_seen_at > now() - make_interval(days => %(max_age_days)s)"
            )
            params["max_age_days"] = max_age_days
    elif lifecycle == "delisted":
        where.append("l.is_active = false")
    return where, params


def build_query(
    target: TargetSpec, filters: ComparableFilters
) -> tuple[str, dict[str, Any]]:
    """Render the SQL and parameter dict for the given target+filters.

    Exposed so tests can assert on shape without a DB connection.
    """
    where, params = _shared_filter_where(target, filters)

    life_where, life_params = _lifecycle_where(
        filters.lifecycle, filters.max_age_days,
    )
    where.extend(life_where)
    params.update(life_params)

    sql = (
        "SELECT\n"
        # The surrogate leads the projection (R2). find_comparables builds its
        # column names from cur.description, so this flows into every comparable
        # dict with no mapping to update.
        "  l.id AS listing_id, l.sreality_id, l.price_czk, l.area_m2,\n"
        # The measure AND its label, together. Every downstream consumer that
        # has to name a unit -- analyze_distribution, find_distribution_outliers,
        # cluster_comparables, estimate_yield's scaling step, the agent's cohort
        # ledger -- reads the basis off these dicts, so projecting the number
        # without the label would leave all of them guessing. category_main /
        # category_type ride along as the label's inputs (and the LLM's cohort
        # context); price_unit is projected as raw provenance ONLY -- it is a
        # duplicate spelling of category_type and must never be read as a unit.
        f"  {per_m2_sql('l')} AS price_per_m2,\n"
        f"  {per_m2_basis_sql('l')} AS price_per_m2_basis,\n"
        "  l.category_main, l.category_type, l.price_unit, l.area_basis,\n"
        "  l.disposition, ll.okres_name AS district,\n"
        "  l.floor, l.total_floors,\n"
        "  l.building_type, l.condition, l.energy_rating,\n"
        "  l.has_balcony, l.has_lift, l.has_parking,\n"
        "  l.estate_area, l.usable_area, l.garden_area,\n"
        "  l.category_sub_cb,\n"
        "  l.furnished, l.terrace, l.cellar, l.garage,\n"
        "  l.parking_lots, l.ownership,\n"
        "  ST_Distance(\n"
        "    ll.geom::geography,\n"
        "    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography\n"
        "  ) AS distance_m,\n"
        "  l.first_seen_at, l.last_seen_at,\n"
        "  EXTRACT(DAY FROM (now() - l.last_seen_at))::int AS data_age_days,\n"
        "  latest_snap.id AS latest_snapshot_id,\n"
        "  latest_snap.scraped_at AS latest_snapshot_at,\n"
        "  latest_check.checked_at AS last_freshness_check_at\n"
        "FROM listings l\n"
        # W4-a: the listing's place. INNER, because `_shared_filter_where` always
        # requires a non-NULL point anyway — an unresolved listing can never be a
        # comparable, and an inner join says so to the planner.
        "JOIN listing_location ll ON ll.listing_id = l.id\n"
        "LEFT JOIN LATERAL (\n"
        "  SELECT id, scraped_at FROM listing_snapshots\n"
        # Re-keyed onto the surrogate: post-Gate-2 `sreality_id = l.sreality_id`
        # is NULL = NULL, which never matches, so latest_snapshot_id would be
        # NULL for every non-sreality comparable — silently breaking rule 8
        # ("estimates capture the snapshot_id of each comparable"). Backed by
        # listing_snapshots_listing_id_scraped_at_idx (mig 333), an exact mirror
        # of the legacy composite, so the plan is unchanged.
        "  WHERE listing_id = l.id\n"
        "  ORDER BY scraped_at DESC LIMIT 1\n"
        ") latest_snap ON true\n"
        "LEFT JOIN LATERAL (\n"
        "  SELECT checked_at FROM listing_freshness_checks\n"
        # Stays legacy-keyed: listing_freshness_checks has NO listing_id column
        # (rule 9 — observability/throttling, not history, so it was never made
        # an R2 carrier). Post-Gate-2 this yields NULL for non-sreality rows, so
        # `verified_during_estimate` reads false and confidence is dampened for
        # them. Degraded, not wrong — and it cannot be fixed here: it needs that
        # table to gain a carrier column first.
        "  WHERE sreality_id = l.sreality_id\n"
        "  ORDER BY checked_at DESC LIMIT 1\n"
        ") latest_check ON true\n"
        "WHERE " + "\n  AND ".join(where) + "\n"
        "ORDER BY distance_m\n"
        f"LIMIT {_HARD_LIMIT}"
    )
    return sql, params


def _cohort_ppm2_basis(
    filters: ComparableFilters,
    listings: list[dict[str, Any]] | None,
) -> str | None:
    """The basis every per-m² number in this envelope is in.

    Read off the ROWS whenever they exist, never off the pins: a filter spec
    says what was asked for, and an unpinned `category_main` on a capital deal
    admits plots (Kč/m² of PLOT) beside flats (Kč/m² of FLOOR). Labelling that
    cohort from the spec is exactly the "one blanket unit for a mixed cohort"
    this program exists to end — `cohort_basis` answers `mixed` there, and
    `mixed` is a state every consumer already knows how to render as a gap.
    Only the row-less caller (a spec echoed before any query ran) falls back to
    `spec_ppm2_basis`, which returns None rather than guess.
    """
    if listings is None:
        return spec_ppm2_basis(filters.category_main, filters.category_type)
    return cohort_basis(measure_backed(listings))


def _filters_used(
    target: TargetSpec,
    filters: ComparableFilters,
    listings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "target": {
            "lat": target.lat,
            "lng": target.lng,
            "area_m2": target.area_m2,
            "disposition": target.disposition,
            "floor": target.floor,
            "exclude_listing_ids": list(target.exclude_listing_ids),
        },
        "radius_m": filters.radius_m,
        "area_band_pct": filters.area_band_pct,
        "disposition_match": filters.disposition_match,
        "max_age_days": filters.max_age_days,
        "lifecycle": filters.lifecycle,
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
        # The unit the two bounds above are IN, and the unit every per-m² number
        # in this envelope is in -- resolved from the ROWS that came back (see
        # _cohort_ppm2_basis). `mixed`, `unknown` and None all mean the same
        # thing to a consumer: render the gap, never a unit.
        "price_per_m2_basis": _cohort_ppm2_basis(filters, listings),
        "category_main": filters.category_main,
        "category_type": filters.category_type,
        "category_sub_cb": filters.category_sub_cb,
        "include_unreliable": filters.include_unreliable,
        "furnished": list(filters.furnished) if filters.furnished else None,
        "ownership": list(filters.ownership) if filters.ownership else None,
        "terrace": filters.terrace,
        "cellar": filters.cellar,
        "garage": filters.garage,
        "min_estate_area": filters.min_estate_area,
        "max_estate_area": filters.max_estate_area,
        "min_usable_area": filters.min_usable_area,
        "max_usable_area": filters.max_usable_area,
        "min_parking_lots": filters.min_parking_lots,
        "building_condition_level_min": filters.building_condition_level_min,
        "building_condition_level_max": filters.building_condition_level_max,
        "apartment_condition_level_min": filters.apartment_condition_level_min,
        "apartment_condition_level_max": filters.apartment_condition_level_max,
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
            "filters_used": _filters_used(target, filters, listings),
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
    price bounds, and lifecycle are NEVER relaxed — they encode user
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
        # Each snapshot is labelled by the rows THAT step returned: the trace is
        # fed verbatim into the agent's prompt, so a step that widened into two
        # bases must say so at the step that did it.
        "filters_snapshot": _filters_used(
            target, current, last_result["data"]["listings"],
        ),
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
                "filters_snapshot": _filters_used(
                    target, current, last_result["data"]["listings"],
                ),
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
            "filters_used": _filters_used(target, current, listings),
            "result_count": len(listings),
            "queried_at": _now_iso(),
            "data_freshness": _max_last_seen(listings),
            "relaxations_applied": relaxations_applied,
            "min_results": min_results,
            **_cohort_freshness_stats(listings),
        },
    }
