"""New-listing notifications ("Watchdog") backend (Phase U2.7).

Three responsibilities:

1. CRUD over `notification_subscriptions` — the operator's saved
   filter specs. Each row holds a name + a `WatchdogFilterSpec` JSONB
   blob mirroring (a subset of) `toolkit.ComparableFilters`.

2. The background matcher. A FastAPI lifespan-spawned asyncio task
   wakes every `notifications_matcher_interval_seconds`, walks listings
   whose `first_seen_at > watermark` against every active subscription,
   and writes one `notification_dispatches` row per (subscription,
   listing) match. The UNIQUE constraint on `(subscription_id,
   sreality_id)` means re-runs over the same window are idempotent.

3. Operator-triggered "Run estimation" kickoff. Each dispatch row
   gets a button that POSTs here; we INSERT a `pending`
   `estimation_runs` row, link it on the dispatch, and let FastAPI's
   `BackgroundTasks` finish the work asynchronously so the UI returns
   immediately and polls for the yield to land.

`WatchdogFilterSpec` is intentionally a separate, narrower model than
`ComparableFilters`: the watchdog matcher does NOT require a target
lat/lng (district / disposition / price filters alone are useful), but
DOES accept a spatial center + radius for "alert me about anything
near X". `_build_match_clauses` converts the spec into parameterised
SQL — reusing the same column semantics as
`toolkit/comparables._shared_filter_where` so the matcher can never
disagree with Browse on what a filter means.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, field_validator, model_validator

from api.cursor import decode_cursor, encode_cursor
from scraper import db as scraper_db

if TYPE_CHECKING:
    import psycopg

LOG = logging.getLogger(__name__)


# --- filter spec ----------------------------------------------------------


class DistrictChip(BaseModel):
    """One entry in `WatchdogFilterSpec.districts`.

    Mirrors the frontend's `DistrictChip` (`frontend/src/lib/filters.ts`). A
    resolved pick carries `level` ('obec' | 'okres' | 'kraj' | 'locality') and
    the admin `id` (admin_boundaries.id for an admin level, or the containing
    obec_id for a 'locality' chip) and is matched by STABLE ID -- so an obec
    pick can't collide with its same-named okres. Free-text place matching
    (the 'locality' street-pick branch and the no-level legacy fallback) goes
    through `place_search_text` (street + locality, migration 182) so portals
    that store the street outside `locality` (bazos) match too. A chip with no
    level/id (a legacy saved filter) falls back to ILIKE-by-name across
    `district` / `place_search_text` / `okres` / `region`. `context` is the
    parent municipality (display + legacy narrow); `excluded` flips the chip
    from an INCLUDE to an EXCLUDE filter (NOT-ed in the matcher WHERE).
    """

    name: str
    context: str | None = None
    excluded: bool = False
    level: str | None = None
    id: int | None = None


class WatchdogFilterSpec(BaseModel):
    """JSON shape persisted in `notification_subscriptions.filter_spec`.

    Mirrors the subset of `toolkit.ComparableFilters` that the Browse
    sidebar exposes; the matcher converts this into a parameterised
    WHERE clause via `_build_match_clauses`. Every field defaults to
    `None` so a watchdog can be as wide ("any apartment for rent") or
    narrow ("furnished 3+kk in Praha 2 under 30 000 Kč near these
    coordinates") as the operator wants.

    Spatial filter is optional. When `lat` / `lng` / `radius_m` are all
    set the matcher restricts to a circle around the point; missing any
    of the three drops the spatial clause entirely.
    """

    # Category coords — defaults match the Browse defaults so a
    # blank-save watchdog already targets "apartments for rent" rather
    # than every category in the database.
    category_main: str | None = "byt"
    category_type: str | None = "pronajem"
    category_sub_cb: int | None = None

    # Portal-agnostic property sub-type (multi-select; matches any).
    subtype: list[str] | None = None

    # Disposition (multi-select; matches any).
    dispositions: list[str] | None = None

    # Spatial (all three required to apply).
    lat: float | None = None
    lng: float | None = None
    radius_m: int | None = None

    # Locality ids (cheap server-side filter — Browse exposes districts
    # by name, but we store the id so renamed admin units don't break
    # historical watchdogs).
    locality_district_id: int | None = None
    locality_region_id: int | None = None

    # Optional district name match (for ergonomic "Praha 2"-style
    # watchdogs without resolving the id first). Each chip is a
    # `DistrictChip` — `{name, context}` — so the matcher's SQL
    # mirrors the per-chip predicate Browse uses (migration 074):
    # name match AND'd with an optional parent-municipality context
    # narrow. Migration 075 lifted any pre-existing rows from
    # `text[]` to the chip shape; the field_validator below also
    # accepts plain `list[str]` request bodies for clients that
    # haven't redeployed yet.
    districts: list["DistrictChip"] | None = None

    @field_validator("districts", mode="before")
    @classmethod
    def _lift_legacy_districts(cls, v: Any) -> Any:
        if v is None:
            return None
        if not isinstance(v, list):
            return v
        out: list[Any] = []
        for item in v:
            if isinstance(item, str):
                out.append({"name": item, "context": None})
            else:
                out.append(item)
        return out

    # Price + area bounds.
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    # Price per m² (price_czk / NULLIF(area_m2, 0)). NULL area_m2 falls
    # out when either bound is set.
    min_price_per_m2: float | None = None
    max_price_per_m2: float | None = None
    # MF gross rental yield % (migration 133). Sale apartments only.
    min_mf_gross_yield_pct: float | None = None
    max_mf_gross_yield_pct: float | None = None
    min_area_m2: float | None = None
    max_area_m2: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None

    # Tri-state amenities (None = don't care).
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None

    # Enumerated columns. furnished/ownership are multi-select; each may carry
    # the `__unknown__` sentinel (NULL or a non-canonical value).
    furnished: list[str] | None = None
    ownership: list[str] | None = None
    portals: list[str] | None = None
    condition_match: list[str] | None = None

    # Parking lots minimum.
    min_parking_lots: int | None = None

    # Derived condition scores (migrations 072 / 073). NULL rows excluded
    # by the `>= N` / `<= N` comparison — same semantics as the Browse
    # filter.
    building_condition_level_min: int | None = None
    building_condition_level_max: int | None = None
    apartment_condition_level_min: int | None = None
    apartment_condition_level_max: int | None = None

    # Price-history aggregates (migrations 091 / 093 / 095 / 173).
    # Property-grain columns maintained by the recompute job; the matcher
    # reads them off properties_public, and registry ids match 1:1.
    # `price_change_window_days` (30 / 90 / 365, None = all time) picks
    # which precomputed count column `price_change_count_min` reads.
    # `total_price_change_pct` is signed: negative = total drop of at
    # least that much, positive = total rise. Stored specs predating
    # migration 173 may carry the retired per-direction keys
    # (price_drop_count_min etc.) — Pydantic's extra='ignore' default
    # drops them on load.
    price_change_count_min: int | None = None
    price_change_window_days: Literal[30, 90, 365] | None = None
    total_price_change_pct: float | None = None

    # Phase QUAL — curated-city quality predicates. Browse + Watchdog
    # only; not exposed to the estimation agent.
    city_index_rules: list[dict[str, Any]] | None = None
    min_city_population: int | None = None
    max_city_population: int | None = None
    near_city_proximity: dict[str, Any] | None = None
    # Fast polygon-edge proximity (migration 142). Precomputed columns on
    # properties_public; `>= value`. Same definition as Browse (lockstep via
    # toolkit.comparables._city_quality_clauses).
    near_pop_5km_min: int | None = None
    near_pop_15km_min: int | None = None
    near_jobs_5km_min: float | None = None
    near_jobs_15km_min: float | None = None
    near_youth_5km_min: float | None = None
    near_youth_15km_min: float | None = None
    near_overall_5km_min: float | None = None
    near_overall_15km_min: float | None = None

    @field_validator("furnished", "ownership", mode="before")
    @classmethod
    def _wrap_bare_str(cls, v: Any) -> Any:
        """Accept a bare string (legacy single-select callers) as [string]."""
        return [v] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _spatial_all_or_none(self) -> "WatchdogFilterSpec":
        spatial = [self.lat, self.lng, self.radius_m]
        set_count = sum(1 for v in spatial if v is not None)
        if set_count not in (0, 3):
            raise ValueError(
                "lat / lng / radius_m must be all set or all None"
            )
        return self


def _build_match_clauses(
    spec: WatchdogFilterSpec,
) -> tuple[list[str], dict[str, Any]]:
    """Render the filter spec as parameterised WHERE clauses.

    The matcher prepends a watermark / window clause; this helper owns
    the spec-derived part only. Keep column semantics aligned with
    `toolkit/comparables._shared_filter_where` so Browse / Watchdog
    can never disagree on what a filter means.
    """
    where: list[str] = []
    params: dict[str, Any] = {}

    if spec.category_main is not None:
        where.append("l.category_main = %(category_main)s")
        params["category_main"] = spec.category_main
    if spec.category_type is not None:
        where.append("l.category_type = %(category_type)s")
        params["category_type"] = spec.category_type
    if spec.category_sub_cb is not None:
        where.append("l.category_sub_cb = %(category_sub_cb)s")
        params["category_sub_cb"] = spec.category_sub_cb

    if spec.subtype:
        where.append("l.subtype = ANY(%(subtype)s)")
        params["subtype"] = list(spec.subtype)

    if spec.dispositions:
        where.append("l.disposition = ANY(%(dispositions)s)")
        params["dispositions"] = list(spec.dispositions)

    if (
        spec.lat is not None
        and spec.lng is not None
        and spec.radius_m is not None
    ):
        # properties_public projects lat/lng (ST_Y/ST_X of the geom); it does
        # not expose the raw geom column, so build the target point from
        # lat/lng rather than referencing l.geom.
        where.append("l.lat IS NOT NULL")
        where.append("l.lng IS NOT NULL")
        where.append(
            "ST_DWithin("
            "ST_SetSRID(ST_MakePoint(l.lng, l.lat), 4326)::geography, "
            "ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography, "
            "%(radius_m)s)"
        )
        params["lat"] = spec.lat
        params["lng"] = spec.lng
        params["radius_m"] = spec.radius_m

    if spec.locality_district_id is not None:
        where.append("l.locality_district_id = %(locality_district_id)s")
        params["locality_district_id"] = spec.locality_district_id
    if spec.locality_region_id is not None:
        where.append("l.locality_region_id = %(locality_region_id)s")
        params["locality_region_id"] = spec.locality_region_id
    if spec.districts:
        # Per-chip predicate kept in lockstep with browse_stats (migration 182)
        # and Browse (queries.ts districtsFilterClause): a resolved pick matches
        # by STABLE ADMIN ID at its level (obec_id / okres_id / region_id) so an
        # obec pick can't collide with its same-named okres; a 'locality' pick
        # narrows to its containing obec + a place-text match; a legacy chip
        # with no level/id falls back to the name ILIKE across
        # district/place_search_text/okres/region. Free-text matching uses
        # place_search_text (street + locality, migration 182), never bare
        # locality — bazos stores the street outside locality.
        # INCLUDE chips are OR'd (match any); EXCLUDE chips are NOT-ed (subtract).
        _ID_COL = {"obec": "obec_id", "okres": "okres_id", "kraj": "region_id"}
        inc_clauses: list[str] = []
        exc_clauses: list[str] = []
        for i, chip in enumerate(spec.districts):
            if chip.level in _ID_COL and chip.id is not None:
                id_key = f"district_id_{i}"
                params[id_key] = chip.id
                clause = f"l.{_ID_COL[chip.level]} = %({id_key})s"
            elif chip.level == "locality":
                # Wildcards live in the parameter VALUE, not as inline SQL '%'
                # literals (psycopg treats a bare '%' as a malformed placeholder).
                n_key = f"district_name_{i}"
                params[n_key] = f"%{chip.name}%"
                place_match = f"l.place_search_text ILIKE %({n_key})s"
                if chip.id is not None:
                    id_key = f"district_id_{i}"
                    params[id_key] = chip.id
                    clause = f"(l.obec_id = %({id_key})s AND {place_match})"
                else:
                    clause = place_match
            else:
                # Legacy / unresolved chip: name ILIKE across all name columns,
                # AND'd with an optional parent-municipality context narrow.
                n_key = f"district_name_{i}"
                params[n_key] = f"%{chip.name}%"
                name_half = (
                    f"(l.district ILIKE %({n_key})s "
                    f"OR l.place_search_text ILIKE %({n_key})s "
                    f"OR l.okres ILIKE %({n_key})s "
                    f"OR l.region ILIKE %({n_key})s)"
                )
                if chip.context:
                    c_key = f"district_ctx_{i}"
                    params[c_key] = f"%{chip.context}%"
                    ctx_half = (
                        f"(l.district ILIKE %({c_key})s "
                        f"OR l.place_search_text ILIKE %({c_key})s "
                        f"OR l.okres ILIKE %({c_key})s "
                        f"OR l.region ILIKE %({c_key})s)"
                    )
                    clause = f"({name_half} AND {ctx_half})"
                else:
                    clause = name_half
            (exc_clauses if chip.excluded else inc_clauses).append(clause)
        if inc_clauses:
            where.append("(" + " OR ".join(inc_clauses) + ")")
        if exc_clauses:
            where.append("NOT (" + " OR ".join(exc_clauses) + ")")

    if spec.min_price_czk is not None:
        where.append("l.price_czk >= %(min_price_czk)s")
        params["min_price_czk"] = spec.min_price_czk
    if spec.max_price_czk is not None:
        where.append("l.price_czk <= %(max_price_czk)s")
        params["max_price_czk"] = spec.max_price_czk
    if spec.min_price_per_m2 is not None:
        where.append(
            "l.price_czk::numeric / NULLIF(l.area_m2, 0) >= %(min_price_per_m2)s"
        )
        params["min_price_per_m2"] = spec.min_price_per_m2
    if spec.max_price_per_m2 is not None:
        where.append(
            "l.price_czk::numeric / NULLIF(l.area_m2, 0) <= %(max_price_per_m2)s"
        )
        params["max_price_per_m2"] = spec.max_price_per_m2
    if spec.min_mf_gross_yield_pct is not None:
        where.append("l.mf_gross_yield_pct >= %(min_mf_gross_yield_pct)s")
        params["min_mf_gross_yield_pct"] = spec.min_mf_gross_yield_pct
    if spec.max_mf_gross_yield_pct is not None:
        where.append("l.mf_gross_yield_pct <= %(max_mf_gross_yield_pct)s")
        params["max_mf_gross_yield_pct"] = spec.max_mf_gross_yield_pct
    if spec.min_area_m2 is not None:
        where.append("l.area_m2 >= %(min_area_m2)s")
        params["min_area_m2"] = spec.min_area_m2
    if spec.max_area_m2 is not None:
        where.append("l.area_m2 <= %(max_area_m2)s")
        params["max_area_m2"] = spec.max_area_m2
    if spec.min_usable_area is not None:
        where.append("l.usable_area >= %(min_usable_area)s")
        params["min_usable_area"] = spec.min_usable_area
    if spec.max_usable_area is not None:
        where.append("l.usable_area <= %(max_usable_area)s")
        params["max_usable_area"] = spec.max_usable_area
    if spec.min_estate_area is not None:
        where.append("l.estate_area >= %(min_estate_area)s")
        params["min_estate_area"] = spec.min_estate_area
    if spec.max_estate_area is not None:
        where.append("l.estate_area <= %(max_estate_area)s")
        params["max_estate_area"] = spec.max_estate_area

    if spec.has_balcony is not None:
        where.append("l.has_balcony = %(has_balcony)s")
        params["has_balcony"] = spec.has_balcony
    if spec.has_lift is not None:
        where.append("l.has_lift = %(has_lift)s")
        params["has_lift"] = spec.has_lift
    if spec.has_parking is not None:
        where.append("l.has_parking = %(has_parking)s")
        params["has_parking"] = spec.has_parking
    if spec.terrace is not None:
        where.append("l.terrace = %(terrace)s")
        params["terrace"] = spec.terrace
    if spec.cellar is not None:
        where.append("l.cellar = %(cellar)s")
        params["cellar"] = spec.cellar
    if spec.garage is not None:
        where.append("l.garage = %(garage)s")
        params["garage"] = spec.garage

    # furnished / ownership: multi-select with the `__unknown__` sentinel.
    # Reuse the exact Browse helper so the two surfaces can't disagree.
    from toolkit.comparables import _enum_or_unknown_clause
    from toolkit.filter_registry import (
        FURNISHED_CANONICAL,
        OWNERSHIP_CANONICAL,
        PRICE_CHANGE_COUNT_COLUMNS,
    )
    if spec.furnished:
        clause = _enum_or_unknown_clause(
            list(spec.furnished), "l.furnished", "furnished",
            FURNISHED_CANONICAL, params,
        )
        if clause:
            where.append(clause)
    if spec.ownership:
        clause = _enum_or_unknown_clause(
            list(spec.ownership), "l.ownership", "ownership",
            OWNERSHIP_CANONICAL, params,
        )
        if clause:
            where.append(clause)
    if spec.portals:
        where.append("l.source = ANY(%(portals)s)")
        params["portals"] = list(spec.portals)
    if spec.condition_match:
        where.append("l.condition = ANY(%(condition_match)s)")
        params["condition_match"] = list(spec.condition_match)

    if spec.min_parking_lots is not None:
        where.append("l.parking_lots >= %(min_parking_lots)s")
        params["min_parking_lots"] = spec.min_parking_lots

    if spec.building_condition_level_min is not None:
        where.append("l.building_condition_level >= %(building_condition_level_min)s")
        params["building_condition_level_min"] = spec.building_condition_level_min
    if spec.building_condition_level_max is not None:
        where.append("l.building_condition_level <= %(building_condition_level_max)s")
        params["building_condition_level_max"] = spec.building_condition_level_max
    if spec.apartment_condition_level_min is not None:
        where.append("l.apartment_condition_level >= %(apartment_condition_level_min)s")
        params["apartment_condition_level_min"] = spec.apartment_condition_level_min
    if spec.apartment_condition_level_max is not None:
        where.append("l.apartment_condition_level <= %(apartment_condition_level_max)s")
        params["apartment_condition_level_max"] = spec.apartment_condition_level_max

    # Property-grain derived aggregates (only meaningful against
    # properties_public, which the matcher reads). NULL rows excluded by the
    # comparison. The window picks the precomputed count column; the column
    # name comes from the registry's canonical dict, never from the spec.
    if spec.price_change_count_min is not None:
        count_col = PRICE_CHANGE_COUNT_COLUMNS[spec.price_change_window_days]
        where.append(f"l.{count_col} >= %(price_change_count_min)s")
        params["price_change_count_min"] = spec.price_change_count_min
    if spec.total_price_change_pct is not None and spec.total_price_change_pct != 0:
        op = "<=" if spec.total_price_change_pct < 0 else ">="
        where.append(f"l.total_price_change_pct {op} %(total_price_change_pct)s")
        params["total_price_change_pct"] = spec.total_price_change_pct

    # Phase QUAL — city quality predicates. Delegated to the same helper
    # `_shared_filter_where` calls so Browse and Watchdog stay in lockstep.
    from toolkit.comparables import ComparableFilters, _city_quality_clauses
    cq_filters = ComparableFilters(
        city_index_rules=spec.city_index_rules,
        min_city_population=spec.min_city_population,
        max_city_population=spec.max_city_population,
        near_city_proximity=spec.near_city_proximity,
        near_pop_5km_min=spec.near_pop_5km_min,
        near_pop_15km_min=spec.near_pop_15km_min,
        near_jobs_5km_min=spec.near_jobs_5km_min,
        near_jobs_15km_min=spec.near_jobs_15km_min,
        near_youth_5km_min=spec.near_youth_5km_min,
        near_youth_15km_min=spec.near_youth_15km_min,
        near_overall_5km_min=spec.near_overall_5km_min,
        near_overall_15km_min=spec.near_overall_15km_min,
    )
    city_clauses, city_params = _city_quality_clauses(cq_filters)
    where.extend(city_clauses)
    params.update(city_params)

    return where, params


# --- CRUD: subscriptions --------------------------------------------------


@dataclass
class SubscriptionRow:
    id: str
    name: str
    filter_spec: dict[str, Any]
    is_active: bool
    created_at: str
    updated_at: str
    dispatch_count: int


_SUB_COLS = "id, name, filter_spec, is_active, created_at, updated_at"


def _row_to_sub(row: tuple[Any, ...], dispatch_count: int) -> SubscriptionRow:
    return SubscriptionRow(
        id=str(row[0]),
        name=row[1],
        filter_spec=row[2] or {},
        is_active=bool(row[3]),
        created_at=row[4].isoformat() if row[4] else "",
        updated_at=row[5].isoformat() if row[5] else "",
        dispatch_count=dispatch_count,
    )


def list_subscriptions(
    conn: "psycopg.Connection",
    *,
    include_inactive: bool = True,
) -> list[dict[str, Any]]:
    where = "" if include_inactive else "WHERE is_active = true"
    sql = (
        f"SELECT {_SUB_COLS}, "
        "  (SELECT count(*) FROM notification_dispatches "
        "     WHERE subscription_id = notification_subscriptions.id) AS dispatch_count "
        "FROM notification_subscriptions "
        f"{where} "
        "ORDER BY created_at DESC"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return [
        _row_to_sub(r[:-1], int(r[-1] or 0)).__dict__
        for r in rows
    ]


def get_subscription(
    conn: "psycopg.Connection", subscription_id: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SUB_COLS}, "
            "  (SELECT count(*) FROM notification_dispatches "
            "     WHERE subscription_id = %s) AS dispatch_count "
            "FROM notification_subscriptions WHERE id = %s",
            (subscription_id, subscription_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _row_to_sub(row[:-1], int(row[-1] or 0)).__dict__


def create_subscription(
    conn: "psycopg.Connection",
    *,
    name: str,
    filter_spec: WatchdogFilterSpec,
    is_active: bool = True,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notification_subscriptions (name, filter_spec, is_active) "
            "VALUES (%s, %s::jsonb, %s) RETURNING id",
            (name, json.dumps(filter_spec.model_dump()), is_active),
        )
        row = cur.fetchone()
    assert row is not None
    return get_subscription(conn, str(row[0])) or {}


def update_subscription(
    conn: "psycopg.Connection",
    subscription_id: str,
    *,
    name: str | None = None,
    filter_spec: WatchdogFilterSpec | None = None,
    is_active: bool | None = None,
) -> dict[str, Any] | None:
    sets: list[str] = []
    params: list[Any] = []
    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if filter_spec is not None:
        sets.append("filter_spec = %s::jsonb")
        params.append(json.dumps(filter_spec.model_dump()))
    if is_active is not None:
        sets.append("is_active = %s")
        params.append(is_active)
    if not sets:
        return get_subscription(conn, subscription_id)
    params.append(subscription_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE notification_subscriptions SET {', '.join(sets)} "
            "WHERE id = %s",
            params,
        )
        if cur.rowcount == 0:
            return None
    return get_subscription(conn, subscription_id)


def delete_subscription(
    conn: "psycopg.Connection", subscription_id: str,
) -> bool:
    """Hard delete. Cascade drops the dispatches via the FK."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM notification_subscriptions WHERE id = %s",
            (subscription_id,),
        )
        return cur.rowcount > 0


# --- dispatches (the notification feed) -----------------------------------


_LISTING_PROJECTION = (
    "l.sreality_id, l.category_main, l.category_type, l.price_czk, "
    "l.price_unit, l.area_m2, l.disposition, l.locality, l.district, "
    "l.is_active, l.first_seen_at, l.last_seen_at, l.mf_gross_yield_pct, "
    "l.source, l.source_url"
)


def list_dispatches(
    conn: "psycopg.Connection",
    *,
    subscription_id: str | None = None,
    seen: Literal["all", "seen", "unseen"] = "all",
    limit: int = 50,
    offset: int = 0,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return the notification feed, KEYSET-paginated on (dispatched_at, id).

    One row per `notification_dispatches` × `listings` join; rows that
    fired against multiple subscriptions are grouped client-side by the
    matching subscription names — but on the wire each row is the
    canonical (dispatch, listing) pair so the table renders one line
    per dispatch and the UI dedups (sreality_id → list of subscriptions)
    when it wants the "fired by N watchdogs" presentation.

    The feed is append-only and grows under the background matcher; keyset
    on (dispatched_at, id) keeps a live scroll dup/skip-free (offset would
    shift as new dispatches prepend). `id` is a uuid — fine as a
    deterministic tiebreaker. `total` is computed once, on the first page.
    """
    where: list[str] = []
    params: dict[str, Any] = {}
    if subscription_id is not None:
        where.append("d.subscription_id = %(subscription_id)s")
        params["subscription_id"] = subscription_id
    if seen == "seen":
        where.append("d.seen_at IS NOT NULL")
    elif seen == "unseen":
        where.append("d.seen_at IS NULL")
    filter_sql = "WHERE " + " AND ".join(where) if where else ""

    page_where = list(where)
    if cursor is not None:
        c_ts, c_id = decode_cursor(cursor)
        page_where.append(
            "(d.dispatched_at, d.id) < (%(c_ts)s::timestamptz, %(c_id)s::uuid)"
        )
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    page_where_sql = "WHERE " + " AND ".join(page_where) if page_where else ""

    sql = (
        "SELECT d.id, d.subscription_id, s.name AS subscription_name, "
        "       d.sreality_id, d.property_id, d.change_kind, "
        "       d.dispatched_at, d.seen_at, "
        "       d.estimation_run_id, "
        "       er.status AS estimation_status, "
        "       er.estimate_kind AS estimation_kind, "
        "       er.estimated_monthly_rent_czk, "
        "       er.estimated_sale_price_czk, "
        "       er.gross_yield_pct, er.confidence, "
        f"      {_LISTING_PROJECTION} "
        "FROM notification_dispatches d "
        "JOIN notification_subscriptions s ON s.id = d.subscription_id "
        "LEFT JOIN listings l ON l.sreality_id = d.sreality_id "
        "LEFT JOIN estimation_runs er ON er.id = d.estimation_run_id "
        f"{page_where_sql} "
        "ORDER BY d.dispatched_at DESC, d.id DESC "
        "LIMIT %(limit)s OFFSET %(offset)s"
    )
    list_params = {**params, "limit": limit, "offset": 0 if cursor else offset}

    with conn.cursor() as cur:
        cur.execute(sql, list_params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        total: int | None = None
        if cursor is None and offset == 0:
            count_params = {k: params[k] for k in params if k not in ("c_ts", "c_id")}
            cur.execute(
                "SELECT count(*) FROM notification_dispatches d "
                "JOIN notification_subscriptions s ON s.id = d.subscription_id "
                f"{filter_sql}",
                count_params,
            )
            total_row = cur.fetchone()
            total = int(total_row[0]) if total_row else 0

    next_cursor: str | None = None
    if len(rows) == limit and rows:
        ts_idx = cols.index("dispatched_at")
        id_idx = cols.index("id")
        last = rows[-1]
        next_cursor = encode_cursor([last[ts_idx].isoformat(), str(last[id_idx])])

    return {
        "data": [_dispatch_row_to_dict(cols, r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_cursor": next_cursor,
    }


def _dispatch_row_to_dict(cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
    out: dict[str, Any] = dict(zip(cols, row))
    for k in ("dispatched_at", "seen_at", "first_seen_at", "last_seen_at"):
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    if "subscription_id" in out and out["subscription_id"] is not None:
        out["subscription_id"] = str(out["subscription_id"])
    if "id" in out and out["id"] is not None:
        out["id"] = str(out["id"])
    if out.get("area_m2") is not None:
        out["area_m2"] = float(out["area_m2"])
    if out.get("gross_yield_pct") is not None:
        out["gross_yield_pct"] = float(out["gross_yield_pct"])
    if out.get("mf_gross_yield_pct") is not None:
        out["mf_gross_yield_pct"] = float(out["mf_gross_yield_pct"])
    return out


def mark_dispatch_seen(
    conn: "psycopg.Connection", dispatch_id: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE notification_dispatches SET seen_at = now() "
            "WHERE id = %s AND seen_at IS NULL",
            (dispatch_id,),
        )
    return _fetch_dispatch(conn, dispatch_id)


def _fetch_dispatch(
    conn: "psycopg.Connection", dispatch_id: str,
) -> dict[str, Any] | None:
    sql = (
        "SELECT d.id, d.subscription_id, s.name AS subscription_name, "
        "       d.sreality_id, d.property_id, d.change_kind, "
        "       d.dispatched_at, d.seen_at, "
        "       d.estimation_run_id, "
        "       er.status AS estimation_status, "
        "       er.estimate_kind AS estimation_kind, "
        "       er.estimated_monthly_rent_czk, "
        "       er.estimated_sale_price_czk, "
        "       er.gross_yield_pct, er.confidence, "
        f"      {_LISTING_PROJECTION} "
        "FROM notification_dispatches d "
        "JOIN notification_subscriptions s ON s.id = d.subscription_id "
        "LEFT JOIN listings l ON l.sreality_id = d.sreality_id "
        "LEFT JOIN estimation_runs er ON er.id = d.estimation_run_id "
        "WHERE d.id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (dispatch_id,))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if cur.description else []
    if row is None:
        return None
    return _dispatch_row_to_dict(cols, row)


# --- estimation kickoff ---------------------------------------------------


_PRONAJEM = "pronajem"


def _resolve_listing_for_estimate(
    conn: "psycopg.Connection", sreality_id: int,
) -> dict[str, Any] | None:
    """Read everything the deterministic estimate needs straight from
    `listings`. The notification matcher only ever fires on listings we
    already have a row for, so we don't have to re-scrape.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sreality_id, "
            "  ST_Y(geom::geometry) AS lat, "
            "  ST_X(geom::geometry) AS lng, "
            "  area_m2, disposition, floor, "
            "  category_main, category_type, "
            "  price_czk, price_unit "
            "FROM listings WHERE sreality_id = %s",
            (sreality_id,),
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if cur.description else []
    if row is None:
        return None
    out = dict(zip(cols, row))
    if out.get("lat") is None or out.get("lng") is None:
        return None
    return out


def kickoff_estimation_for_dispatch(
    conn: "psycopg.Connection", dispatch_id: str,
) -> tuple[dict[str, Any], int | None]:
    """Stamp a pending estimation_runs row on the dispatch and return it.

    Returns `(dispatch_row, new_estimation_run_id)`. When the dispatch
    already has a run linked we surface that row untouched and return
    `new_estimation_run_id = None`; the caller decides whether to
    schedule a re-run. When the listing has no geom we surface a
    `failed` estimation row immediately rather than queueing it,
    because the deterministic estimator requires lat/lng.
    """
    dispatch = _fetch_dispatch(conn, dispatch_id)
    if dispatch is None:
        return ({}, None)

    if dispatch.get("estimation_run_id") is not None:
        return (dispatch, None)

    sreality_id = int(dispatch["sreality_id"])
    listing = _resolve_listing_for_estimate(conn, sreality_id)

    if listing is None:
        run_id = _insert_failed_run(
            conn, sreality_id, error_message="listing missing or has no geom",
        )
        _link_dispatch_run(conn, dispatch_id, run_id)
        return (_fetch_dispatch(conn, dispatch_id) or {}, None)

    # The watchdog "Estimate rent" action always runs a RENTAL estimate — even
    # for a sale listing — so the operator sees "what would this flat rent for"
    # (the input to a yield calc). That means the comparable cohort must be
    # rentals (category_type='pronajem'), regardless of the subject's own
    # category_type. category_main (byt/dum/…) carries through unchanged.
    spec = {
        "lat": float(listing["lat"]),
        "lng": float(listing["lng"]),
        "area_m2": float(listing["area_m2"]) if listing.get("area_m2") else None,
        "disposition": listing.get("disposition"),
        "floor": listing.get("floor"),
        "exclude_ids": [sreality_id],
        # category_main/type are NOT columns on estimation_runs; carry them in
        # input_spec so run_pending_estimation can build ComparableFilters.
        "category_main": listing.get("category_main"),
        "category_type": "pronajem",
    }
    estimate_kind = "rent"

    run_id = _insert_pending_run(
        conn,
        sreality_id=sreality_id,
        spec=spec,
        estimate_kind=estimate_kind,
    )
    _link_dispatch_run(conn, dispatch_id, run_id)
    return (_fetch_dispatch(conn, dispatch_id) or {}, run_id)


def _link_dispatch_run(
    conn: "psycopg.Connection", dispatch_id: str, run_id: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE notification_dispatches SET estimation_run_id = %s "
            "WHERE id = %s",
            (run_id, dispatch_id),
        )


def _insert_pending_run(
    conn: "psycopg.Connection",
    *,
    sreality_id: int,
    spec: dict[str, Any],
    estimate_kind: str,
) -> int:
    """INSERT a 'pending' estimation_runs row that the background task
    will UPDATE to a terminal status once estimate_yield returns.

    category_main/category_type ride inside `spec` (input_spec jsonb) —
    estimation_runs has no such columns.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO estimation_runs ("
            "  source, mode, status, estimate_kind, "
            "  input_sreality_id, input_spec, "
            "  trace"
            ") VALUES ("
            "  'ui', 'deterministic', 'pending', %s, "
            "  %s, %s::jsonb, "
            "  %s::jsonb"
            ") RETURNING id",
            (
                estimate_kind,
                sreality_id,
                json.dumps(spec),
                json.dumps({
                    "version": 2,
                    "summary": "queued from watchdog notification",
                    "steps": [],
                }),
            ),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _insert_failed_run(
    conn: "psycopg.Connection", sreality_id: int, *, error_message: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO estimation_runs ("
            "  source, mode, status, estimate_kind, "
            "  input_sreality_id, input_spec, error_message, trace"
            ") VALUES ("
            "  'ui', 'deterministic', 'failed', 'rent', "
            "  %s, '{}'::jsonb, %s, %s::jsonb"
            ") RETURNING id",
            (
                sreality_id,
                error_message,
                json.dumps({
                    "version": 2,
                    "summary": f"failed: {error_message}",
                    "steps": [],
                }),
            ),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def run_pending_estimation(run_id: int) -> None:
    """Background-task entry point. Opens a fresh DB connection (the
    request connection is closed by the time this runs), loads the
    pending row, runs the deterministic estimate, and UPDATEs the row
    to its terminal status.

    Catches every exception locally — a failure must NOT crash the
    FastAPI worker. The row's `status='failed'` + `error_message`
    columns are the audit trail.
    """
    from api.estimation_runs import _update_run_terminal  # local import to avoid cycle
    from api.estimate_yield import estimate_yield
    from toolkit import ComparableFilters, TargetSpec

    LOG.info("run_pending_estimation start run_id=%s", run_id)
    conn: Any = None
    try:
        conn = scraper_db.connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT input_sreality_id, input_spec, estimate_kind "
                "FROM estimation_runs WHERE id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            LOG.warning("run_pending_estimation: run %s missing", run_id)
            return

        sreality_id = row[0]
        spec = row[1] or {}
        estimate_kind = row[2] or "rent"
        # category_main/type travel in input_spec (no such columns on the table).
        category_main = spec.get("category_main")
        category_type = spec.get("category_type")

        if (
            spec.get("lat") is None
            or spec.get("lng") is None
        ):
            _update_run_terminal(
                conn, run_id,
                status="failed",
                error_message="missing lat/lng on input_spec",
            )
            return

        target = TargetSpec(
            lat=float(spec["lat"]),
            lng=float(spec["lng"]),
            area_m2=spec.get("area_m2"),
            disposition=spec.get("disposition"),
            floor=spec.get("floor"),
            exclude_ids=list(spec.get("exclude_ids") or [sreality_id]),
        )
        # Use the same defaults as the deterministic UI path; reading
        # from app_settings keeps the operator-tunable knobs honoured.
        from api.estimation_runs import load_filter_defaults
        defaults = load_filter_defaults(conn)
        filters = ComparableFilters(
            radius_m=defaults.radius_m,
            area_band_pct=defaults.area_band_pct,
            disposition_match=defaults.disposition_match,
            max_age_days=defaults.max_age_days_for(estimate_kind),
            lifecycle=defaults.lifecycle,
            category_main=category_main or "byt",
            category_type=category_type
                or ("pronajem" if estimate_kind == "rent" else "prodej"),
        )

        # status -> running before the call so the UI sees progress
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE estimation_runs SET status = 'running' WHERE id = %s",
                (run_id,),
            )

        try:
            result = estimate_yield(
                conn, target, filters, None,
                estimate_kind=estimate_kind,
            )
        except Exception as exc:  # noqa: BLE001 — see docstring
            LOG.warning(
                "run_pending_estimation: estimate_yield failed run_id=%s: %s",
                run_id, exc,
            )
            _update_run_terminal(
                conn, run_id,
                status="failed",
                error_message=f"{type(exc).__name__}: {exc}"[:1000],
            )
            return

        d = result["data"]
        _update_run_terminal(
            conn, run_id,
            status="success",
            estimated_monthly_rent_czk=d.get("estimated_monthly_rent_czk"),
            rent_p25_czk=d.get("rent_p25_czk"),
            rent_p75_czk=d.get("rent_p75_czk"),
            estimated_sale_price_czk=d.get("estimated_sale_price_czk"),
            sale_p25_czk=d.get("sale_p25_czk"),
            sale_p75_czk=d.get("sale_p75_czk"),
            gross_yield_pct=d.get("gross_yield_pct"),
            confidence=d.get("confidence"),
            comparables_used=d.get("comparables_used"),
            warnings=d.get("warnings") or None,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        LOG.exception("run_pending_estimation crashed run_id=%s: %s", run_id, exc)
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


# --- matcher loop ---------------------------------------------------------


@dataclass
class MatcherSettings:
    interval_seconds: int
    window_listings: int


def _load_matcher_settings(conn: "psycopg.Connection") -> MatcherSettings:
    interval = _read_int_setting(
        conn, "notifications_matcher_interval_seconds", default=300,
    )
    window = _read_int_setting(
        conn, "notifications_match_window_listings", default=1000,
    )
    return MatcherSettings(
        interval_seconds=max(0, interval),
        window_listings=max(1, window),
    )


def _read_int_setting(
    conn: "psycopg.Connection", key: str, *, default: int,
) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return default
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return default


def match_once(conn: "psycopg.Connection") -> dict[str, int]:
    """One pass of the matcher. Returns counters useful for logging.

    Cheap to call directly — used by both the lifespan loop and the
    operator-facing "run matcher now" button. Idempotent against the
    (subscription_id, property_id, change_kind) UNIQUE constraint;
    emits change_kind='new'.

    Property grain (Slice 2b): the matcher walks `properties_public`, so a
    property listed on several portals fires once, not once per source. The
    stored sreality_id is the property's representative listing (for the feed
    + the run-estimation path).

    Per-subscription cursor model (migration 065). Each subscription
    has its own `last_matched_first_seen_at`; the matcher considers
    properties with `first_seen_at > cursor` for that subscription only,
    then advances the cursor to the max first_seen_at of the evaluated
    window. New watchdogs default the cursor to `now() - 24h` so the
    feed shows immediate backfill matches rather than sitting empty
    until the next scrape lands.
    """
    settings = _load_matcher_settings(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, filter_spec, last_matched_first_seen_at "
            "FROM notification_subscriptions "
            "WHERE is_active = true"
        )
        sub_rows = cur.fetchall()

    total_inserted = 0
    total_listings_in_window = 0
    cursors_advanced = 0

    for sub_id, raw_spec, cursor_ts in sub_rows:
        try:
            spec = WatchdogFilterSpec(**(raw_spec or {}))
        except Exception as exc:  # noqa: BLE001 — bad spec, skip but keep loop alive
            LOG.warning(
                "matcher: subscription %s has invalid filter_spec: %s",
                sub_id, exc,
            )
            continue

        # One failing subscription must not zero the whole feed. The
        # connection is autocommit, so a raised execute aborts only that
        # statement — no rollback needed — but we still isolate the rest of
        # the per-subscription body so an unexpected error (bad SQL from a
        # future spec field, a transient DB hiccup) skips just this
        # subscription and the others still match.
        try:
            where, params = _build_match_clauses(spec)
            where.append("l.first_seen_at > %(cursor)s")
            params["cursor"] = cursor_ts
            joined_where = " AND ".join(where)

            # Phase 1: find the window upper bound (max first_seen_at of
            # the next batch of matching listings, capped at the operator
            # knob). Reading the max separately means we can advance the
            # cursor past listings even when the dedup constraint blocked
            # the dispatch insert (re-running the matcher won't re-evaluate
            # the same listings forever).
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT max(first_seen_at), count(*) FROM ("
                    "  SELECT l.first_seen_at FROM properties_public l "
                    f"  WHERE {joined_where} "
                    "  ORDER BY l.first_seen_at ASC "
                    "  LIMIT %(window_size)s"
                    ") sub",
                    {**params, "window_size": settings.window_listings},
                )
                row = cur.fetchone()
            upper = row[0] if row and row[0] is not None else None
            listings_in_window = int(row[1]) if row and row[1] is not None else 0
            total_listings_in_window += listings_in_window

            if upper is None:
                continue

            # Phase 2: insert dispatches for matches in the window.
            insert_where = where + ["l.first_seen_at <= %(upper)s"]
            insert_params = {
                **params,
                "upper": upper,
                "subscription_id": str(sub_id),
            }
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO notification_dispatches "
                    "  (subscription_id, property_id, sreality_id, "
                    "   change_kind, status, channel) "
                    "SELECT %(subscription_id)s, l.property_id, l.sreality_id, "
                    "       'new', 'sent', 'in_app' "
                    "FROM properties_public l "
                    f"WHERE {' AND '.join(insert_where)} "
                    "ON CONFLICT (subscription_id, property_id, change_kind) DO NOTHING",
                    insert_params,
                )
                total_inserted += cur.rowcount or 0

            # Phase 3: advance the cursor for this subscription. Done in a
            # separate UPDATE so a crash between INSERT and UPDATE only
            # costs us a re-scan of the same window on the next pass —
            # ON CONFLICT means no duplicate dispatches.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE notification_subscriptions "
                    "SET last_matched_first_seen_at = %s "
                    "WHERE id = %s AND last_matched_first_seen_at < %s",
                    (upper, sub_id, upper),
                )
                if cur.rowcount:
                    cursors_advanced += 1
        except Exception as exc:  # noqa: BLE001 — isolate one sub, keep loop alive
            LOG.exception(
                "matcher: subscription %s failed, skipping: %s", sub_id, exc,
            )
            continue

    return {
        "subscriptions_evaluated": len(sub_rows),
        "matches_inserted": total_inserted,
        "listings_in_window": total_listings_in_window,
        "cursors_advanced": cursors_advanced,
    }


def _recent_price_drop_property_ids(
    conn: "psycopg.Connection", *, window_days: int,
) -> list[int]:
    """Property ids whose combined snapshot history shows a price decrease
    whose later snapshot landed inside the window.

    The window function runs over the full per-property price series (so the
    `prev` of an in-window snapshot is correct even if it predates the
    window), but the candidate set is pre-narrowed to properties touched in
    the window so the scan stays bounded.
    """
    with conn.cursor() as cur:
        cur.execute(
            "WITH steps AS ("
            "  SELECT c.property_id, s.scraped_at, s.price_czk, "
            "         lag(s.price_czk) OVER w AS prev "
            "  FROM listing_snapshots s "
            "  JOIN listings c ON c.sreality_id = s.sreality_id "
            "  WHERE c.property_id IS NOT NULL AND s.price_czk IS NOT NULL "
            "    AND c.property_id IN ("
            "      SELECT c2.property_id FROM listing_snapshots s2 "
            "      JOIN listings c2 ON c2.sreality_id = s2.sreality_id "
            "      WHERE s2.scraped_at > now() - %(win)s::interval "
            "        AND c2.property_id IS NOT NULL"
            "    ) "
            "  WINDOW w AS (PARTITION BY c.property_id ORDER BY s.scraped_at, s.id)"
            ") "
            "SELECT DISTINCT property_id FROM steps "
            "WHERE prev IS NOT NULL AND price_czk < prev "
            "  AND scraped_at > now() - %(win)s::interval",
            {"win": f"{window_days} days"},
        )
        return [int(r[0]) for r in cur.fetchall()]


def match_changes_once(conn: "psycopg.Connection") -> dict[str, int]:
    """One pass of the property-change matcher (Slice 2b).

    Emits `change_kind='price_drop'` dispatches for properties that had a
    price decrease observed within the lookback window
    (`notifications_price_drop_window_days`, default 2) AND match an active
    subscription's spec. Deduped on (subscription_id, property_id,
    change_kind), so a property fires `price_drop` at most once per
    subscription. Reuses `_build_match_clauses` against properties_public so
    Browse / Watchdog stay in lockstep on what every filter means.

    Distinct from `match_once` (`change_kind='new'`, fires on newly-seen
    properties via the first_seen_at cursor): this fires on EXISTING
    properties that change, so it has no cursor — the window bounds the scan
    and the UNIQUE constraint makes re-scans idempotent. Meant to run on a
    ~daily cadence; the matcher loop gates it.
    """
    window_days = _read_int_setting(
        conn, "notifications_price_drop_window_days", default=2,
    )
    drop_ids = _recent_price_drop_property_ids(conn, window_days=window_days)
    if not drop_ids:
        return {
            "subscriptions_evaluated": 0,
            "price_drops_in_window": 0,
            "changes_inserted": 0,
        }

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, filter_spec FROM notification_subscriptions "
            "WHERE is_active = true"
        )
        sub_rows = cur.fetchall()

    total_inserted = 0
    for sub_id, raw_spec in sub_rows:
        try:
            spec = WatchdogFilterSpec(**(raw_spec or {}))
        except Exception as exc:  # noqa: BLE001 — bad spec, skip but keep loop alive
            LOG.warning(
                "change matcher: subscription %s has invalid filter_spec: %s",
                sub_id, exc,
            )
            continue

        try:
            where, params = _build_match_clauses(spec)
            joined_where = " AND ".join(where) if where else "true"
            params["subscription_id"] = str(sub_id)
            params["drop_ids"] = drop_ids
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO notification_dispatches "
                    "  (subscription_id, property_id, sreality_id, "
                    "   change_kind, status, channel) "
                    "SELECT %(subscription_id)s, l.property_id, l.sreality_id, "
                    "       'price_drop', 'sent', 'in_app' "
                    "FROM properties_public l "
                    f"WHERE l.property_id = ANY(%(drop_ids)s) AND {joined_where} "
                    "ON CONFLICT (subscription_id, property_id, change_kind) DO NOTHING",
                    params,
                )
                total_inserted += cur.rowcount or 0
        except Exception as exc:  # noqa: BLE001 — isolate one sub, keep loop alive
            LOG.exception(
                "change matcher: subscription %s failed, skipping: %s",
                sub_id, exc,
            )
            continue

    return {
        "subscriptions_evaluated": len(sub_rows),
        "price_drops_in_window": len(drop_ids),
        "changes_inserted": total_inserted,
    }


async def matcher_loop(stop_event: asyncio.Event) -> None:
    """The forever-running async matcher. Reads its own DB connection
    each pass; idle waits respect `notifications_matcher_interval_seconds`
    so an operator who edits the row only needs to wait for the current
    sleep to elapse.

    Each pass runs the new-listing matcher (`match_once`). The property-change
    matcher (`match_changes_once`) runs at most once per
    `notifications_change_match_interval_seconds` (default 86400 = daily); the
    gate is in-memory, so a process restart simply runs it once on the next
    pass — harmless, the UNIQUE constraint dedups.
    """
    LOG.info("notification matcher loop starting")
    # monotonic timestamp of the last property-change pass; 0 => run on the
    # first pass after (re)start.
    last_change_run = 0.0
    while not stop_event.is_set():
        # Read interval each pass so live edits to app_settings take
        # effect without a restart.
        interval = 300
        try:
            interval = await asyncio.to_thread(_read_interval_seconds)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("matcher: failed to read interval: %s", exc)

        if interval <= 0:
            LOG.info("notification matcher disabled (interval=0); idling")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            else:
                break

        try:
            stats = await asyncio.to_thread(_match_once_in_thread)
            if stats.get("matches_inserted", 0) > 0:
                LOG.info("notification matcher: %s", stats)
            else:
                LOG.debug("notification matcher: %s", stats)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("notification matcher pass failed: %s", exc)

        # Property-change matcher, gated to ~daily. interval<=0 disables it.
        change_interval = 86400
        try:
            change_interval = await asyncio.to_thread(_read_change_interval_seconds)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("change matcher: failed to read interval: %s", exc)
        if change_interval > 0 and (time.monotonic() - last_change_run) >= change_interval:
            try:
                cstats = await asyncio.to_thread(_match_changes_in_thread)
                last_change_run = time.monotonic()
                if cstats.get("changes_inserted", 0) > 0:
                    LOG.info("notification change matcher: %s", cstats)
                else:
                    LOG.debug("notification change matcher: %s", cstats)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("notification change matcher pass failed: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(interval))
        except asyncio.TimeoutError:
            continue
        else:
            break
    LOG.info("notification matcher loop stopped")


def _read_interval_seconds() -> int:
    conn = scraper_db.connect()
    try:
        return _read_int_setting(
            conn, "notifications_matcher_interval_seconds", default=300,
        )
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _read_change_interval_seconds() -> int:
    conn = scraper_db.connect()
    try:
        return _read_int_setting(
            conn, "notifications_change_match_interval_seconds", default=86400,
        )
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _match_once_in_thread() -> dict[str, int]:
    conn = scraper_db.connect()
    try:
        return match_once(conn)
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _match_changes_in_thread() -> dict[str, int]:
    conn = scraper_db.connect()
    try:
        return match_changes_once(conn)
    finally:
        with contextlib.suppress(Exception):
            conn.close()
