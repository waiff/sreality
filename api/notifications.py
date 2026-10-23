"""New-listing notifications ("Watchdog") backend (Phase U2.7).

Two responsibilities:

1. CRUD over `notification_subscriptions` — the operator's saved
   filter specs. Each row holds a name + a `WatchdogFilterSpec` JSONB
   blob whose field names are filter-registry ids (`toolkit/filter_registry.py`).

2. The three producers (`match_once`, `match_changes_once`,
   `match_monitored_collections_once`), run every 300 s by the realtime worker's
   `notifications` lane, their one caller and clock (the API runs none since W2b). They
   write one `notification_dispatches` row per event over fixed windows (a watchdog's
   'new' over properties first seen in the last 24 h); the one identity index
   (`EVENT_IDENTITY`, migration 653) makes every re-run idempotent.

`WatchdogFilterSpec` is intentionally a separate model from
`ComparableFilters`: the watchdog matcher does NOT require a target
lat/lng (district / disposition / price filters alone are useful), but
DOES accept the map box Browse filters on ("alert me about anything in
this area"), and it carries the city-quality filters rule 17 keeps off the
cohort model. `_build_match_clauses` renders it over `properties_public`
through the one filter compiler (`toolkit/filter_compiler.compile_filter_where`)
that also renders every estimation cohort, so the matcher and the cohort
tools cannot disagree on what a filter means. One named exception: a portal
matches when ANY of the property's ads is on it here (MS19, `all_sources`),
and the canonical ad's own portal on the cohorts.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, field_validator, model_validator

from api.cursor import decode_cursor, encode_cursor
from api.location_filter import DistrictChip, MapBounds, bounds_where, district_where
from location_data.claims_common import served_location_predicate
from toolkit.filter_compiler import PROPERTIES_GRAIN, compile_filter_where
from toolkit.property_carriers import NOTIFICATION_EVENTS_SHARED_LOCK_SQL

if TYPE_CHECKING:
    import psycopg

LOG = logging.getLogger(__name__)

# The producers whose rows the feed shows (rule 16; CHECK notification_dispatches_source_ck).
# system_health rows are history the app no longer shows (ruling R2, notifications W1).
SourceKind = Literal["watchdog", "collection_monitor", "share"]
SourceKindFilter = Literal[SourceKind, "all"]

# ONE event identity (program § 3.2, migration 653): the columns of notification_dispatches_identity_uq
# in index order, NULLS NOT DISTINCT over the rows that are not a marked repeat. Every producer INSERT
# and the share grant conflict on it; the merge carrier (toolkit/property_carriers.Dispatches) marks a
# retired property's alert a duplicate on it. The account trigger (migrations 364, 641) stamps
# account_id BEFORE INSERT, so the conflict check sees it. Tests pin all three to the migration.
EVENT_IDENTITY: tuple[str, ...] = (
    "account_id", "source_kind", "subscription_id", "collection_id", "change_kind", "property_id",
    "event_ref", "actor_account_id",
)
ON_EVENT_CONFLICT = f"ON CONFLICT ({', '.join(EVENT_IDENTITY)}) WHERE duplicate_of IS NULL DO NOTHING"


# --- filter spec ----------------------------------------------------------


class WatchdogFilterSpec(BaseModel):
    """JSON shape persisted in `notification_subscriptions.filter_spec`.

    Field names are registry ids (`toolkit/filter_registry.py`) for what the
    Browse sidebar exposes; the matcher converts this into a parameterised
    WHERE clause via `_build_match_clauses`. Every field defaults to
    `None` so a watchdog can be as wide ("any apartment for rent") or
    narrow ("furnished 3+kk in Praha 2 under 30 000 Kč near these
    coordinates") as the operator wants.

    The map box is optional and the ONE spatial shape: `bounds` is what
    Browse filters on (its viewport, or the square around a centre+radius
    pick), so a watchdog saved from Browse watches the rows Browse showed.
    Place chips are an independent AND; null everywhere = no restriction.
    """

    # Category. `category_type` (deal type) stays single-valued — rent and
    # sale are different price scales, so Browse keeps it an exclusive pill;
    # its default targets "for rent" so a blank-save watchdog isn't every
    # deal type. `category_main_in` is multi-select (a listing matches if
    # its category_main is in the list); null = no constraint. Mirrors the
    # Browse split (scalar category_type, multi category_main_in) and the
    # dispositions / disposition_match precedent.
    category_main_in: list[str] | None = None
    category_type: str | None = "pronajem"
    category_sub_cb: int | None = None

    # Portal-agnostic property sub-type (multi-select; matches any).
    subtype: list[str] | None = None

    # Disposition (multi-select; matches any).
    dispositions: list[str] | None = None

    # The map box (rule 16): the lat/lng band Browse's list and both stats RPCs apply.
    bounds: MapBounds | None = None

    # Place. Each chip is a `DistrictChip` — a LEVEL plus a RÚIAN CODE
    # (`api/location_filter.py`), the one predicate Browse, Stats, the map and
    # this matcher share (rule 16). A chip without both is refused (422).
    districts: list["DistrictChip"] | None = None

    # Price + area bounds.
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    # When true AND a price bound is set, keep no-price (price_czk IS NULL)
    # listings instead of dropping them. No-op when no bound is set. Mirrors
    # the Browse toggle so a saved alert means the same thing (rule #16).
    include_no_price: bool = False
    # Price per m²: properties_public.price_per_m2, i.e. measure_price_per_m2
    # (migration 425) -- basis-resolved from (category_main, category_type) and
    # withheld below its basis floor. Rows with no area, no price, an
    # undecidable basis, or a sub-floor price fall out when either bound is set.
    min_price_per_m2: float | None = None
    max_price_per_m2: float | None = None
    # MF gross rental yield %: properties_public's read-time mf_reference() (migrations 565 + 567).
    # Sale flats with a value only -- a town-level range carries no yield.
    min_mf_gross_yield_pct: float | None = None
    max_mf_gross_yield_pct: float | None = None
    min_area_m2: float | None = None
    max_area_m2: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    # Lost by merge f2d7b359 (extra='ignore' then dropped them on every save); now pinned
    # by an equality test against the WATCHDOG agenda. A blob saved before that merge
    # holds `building_material` as one scalar bucket, which `_wrap_bare_str` lifts.
    building_material: list[str] | None = None
    energy_rating_match: list[str] | None = None
    min_garden_area: float | None = None
    max_garden_area: float | None = None

    # Tri-state amenities (None = don't care).
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None

    # Enumerated columns. furnished/ownership/condition_match are multi-select;
    # each may carry the `__unknown__` sentinel (NULL or a non-canonical value).
    furnished: list[str] | None = None
    ownership: list[str] | None = None
    # The portal rule's two sides (MS19 + migration 597), compiled by toolkit/filter_compiler.
    portals: list[str] | None = None
    portals_excluded: list[str] | None = None
    portals_excluded_active_only: bool = False
    condition_match: list[str] | None = None

    # Parking lots minimum.
    min_parking_lots: int | None = None

    # RETIRED (condition scoring parked, operator ruling 2026-10-20): kept only so an old
    # saved spec deserialises; any value raises in toolkit/filter_compiler.RETIRED_FILTERS.
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
    # properties_public; `>= value`. Compiled as plain `>=` bounds by
    # toolkit/filter_compiler.
    near_pop_5km_min: int | None = None
    near_pop_15km_min: int | None = None
    near_jobs_5km_min: float | None = None
    near_jobs_15km_min: float | None = None
    near_youth_5km_min: float | None = None
    near_youth_15km_min: float | None = None
    near_overall_5km_min: float | None = None
    near_overall_15km_min: float | None = None

    @field_validator(
        "furnished", "ownership", "category_main_in", "building_material", mode="before"
    )
    @classmethod
    def _wrap_bare_str(cls, v: Any) -> Any:
        """Accept a bare string (legacy single-select callers) as [string]."""
        return [v] if isinstance(v, str) else v

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_category_main(cls, data: Any) -> Any:
        """Lift a legacy scalar `category_main` key (specs saved before the
        scalar→multiselect split) onto `category_main_in`, so existing
        watchdogs keep their category constraint instead of silently
        widening to "any category" on load."""
        if not isinstance(data, dict):
            return data
        if "category_main_in" not in data and "category_main" in data:
            legacy = data.get("category_main")
            if legacy is not None:
                data = {
                    **data,
                    "category_main_in": (
                        [legacy] if isinstance(legacy, str) else legacy
                    ),
                }
        return data


def _build_match_clauses(
    spec: WatchdogFilterSpec,
) -> tuple[list[str], dict[str, Any]]:
    """Render the filter spec as parameterised WHERE clauses.

    The matcher prepends a watermark / window clause; this helper owns
    the spec-derived part only. Spec-derived clauses come from
    `compile_filter_where(…, PROPERTIES_GRAIN)`; only the served predicate,
    the map box and the place chips are rendered here.
    """
    # THE CONSUMER RULE (W5, operator ruling 2026-09-13), unconditional and first.
    # Browse inherits it from `browse_projection` (migration 512); the matcher cannot,
    # because it reads `properties_public` — a DETAIL-by-id surface that deliberately
    # keeps serving an unresolved listing to a direct link. So the ONE definition is
    # rendered here instead, keyed on the property's LOCATION ad
    # (`properties_public.location_listing_ref_id`, migration 636), as browse_projection
    # keys it. Without it a watchdog would fire on a listing nobody can find in Browse,
    # and "matches" would mean two different things.
    where: list[str] = [served_location_predicate("l.location_listing_ref_id")]
    params: dict[str, Any] = {}

    # The map box, on the lat/lng properties_public projects off the location ad's
    # point: the band Browse's list and both stats RPCs apply (rule 16).
    b_where, b_params = bounds_where(spec.bounds, alias="l")
    where.extend(b_where)
    params.update(b_params)

    if spec.districts:
        # Delegates to the shared builder (`api.location_filter`) so Browse,
        # Stats, the map and the Watchdog can never disagree on what a place
        # chip means (rule 16): `<level>_id = any(codes)`, nothing else.
        d_where, d_params = district_where(spec.districts, alias="l")
        where.extend(d_where)
        params.update(d_params)

    compiled, compiled_params = compile_filter_where(dict(spec), PROPERTIES_GRAIN)
    where.extend(compiled)
    params.update(compiled_params)
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


# VISIBLE (program § 3.6): what an account sees of its alerts, ONE fragment over
# `notification_dispatches d`, carried by every in-app read, count and update (the feed and its
# total, the unread count, mark-all-seen, mark-seen, each watchdog's count) and by W4's push
# fan-out. Three arms, each answering the same under RLS and on the service role:
#   - not a marked repeat (`duplicate_of`, written by a merge since migration 653);
#   - not about a property the EVENT'S OWN account dismissed (migration 536's active row, probed
#     on its (property_id, account_id) index: the service role bypasses RLS, so the account is
#     named, never left to the caller's policy);
#   - not from a retired watchdog (a delete retires it, F21; its rows stay for analysis).
# Detection still writes every row — the `reactivated` detector reads prior ones — so an undo
# brings it back intact; a system_health row (property_id NULL) is never dismissed.
VISIBLE_SQL = (
    "d.duplicate_of IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM property_dismissals pd "
    "WHERE pd.property_id = d.property_id AND pd.account_id = d.account_id "
    "AND pd.lifted_at IS NULL) "
    "AND (d.subscription_id IS NULL OR EXISTS (SELECT 1 FROM notification_subscriptions ws "
    "WHERE ws.id = d.subscription_id AND ws.retired_at IS NULL))"
)


def list_subscriptions(
    conn: "psycopg.Connection",
    *,
    include_inactive: bool = True,
) -> list[dict[str, Any]]:
    # A retired watchdog (deleted, F21) leaves the list; its rows stay, hidden by VISIBLE_SQL.
    where = "WHERE retired_at IS NULL" + ("" if include_inactive else " AND is_active = true")
    sql = (
        f"SELECT {_SUB_COLS}, "
        "  (SELECT count(*) FROM notification_dispatches d "
        "     WHERE d.subscription_id = notification_subscriptions.id "
        f"    AND {VISIBLE_SQL}) AS dispatch_count "
        "FROM notification_subscriptions "
        f"{where} "
        "ORDER BY created_at DESC"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return [_row_to_sub(r[:-1], int(r[-1] or 0)).__dict__ for r in rows]


def get_subscription(
    conn: "psycopg.Connection", subscription_id: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SUB_COLS}, "
            "  (SELECT count(*) FROM notification_dispatches d "
            f"    WHERE d.subscription_id = %s AND {VISIBLE_SQL}) AS dispatch_count "
            "FROM notification_subscriptions WHERE id = %s AND retired_at IS NULL",
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
    account_id: uuid.UUID | str,
) -> dict[str, Any]:
    # account_id is NOT NULL (migration 364) and REQUIRED here (no default): the
    # route resolves exactly one via tenant_pool.require_account_id, which 400s
    # when the caller has none, so this INSERT can never bind a silent NULL.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notification_subscriptions "
            "  (name, filter_spec, is_active, account_id) "
            "VALUES (%s, %s::jsonb, %s, %s) RETURNING id",
            (
                name,
                json.dumps(filter_spec.model_dump()),
                is_active,
                account_id,
            ),
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
            "WHERE id = %s AND retired_at IS NULL",
            params,
        )
        if cur.rowcount == 0:
            return None
    return get_subscription(conn, subscription_id)


def delete_subscription(
    conn: "psycopg.Connection", subscription_id: str,
) -> bool:
    """Retire, never delete (F21; program § 3.2): the watchdog stops matching (the producers
    read `retired_at IS NULL`), leaves list and get, and its alerts leave every read through
    VISIBLE_SQL while the rows stay for analysis — the FK's cascade no longer erases them. True
    when a live watchdog was retired; a missing or already retired one is the route's 404."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE notification_subscriptions SET retired_at = now(), is_active = false "
            "WHERE id = %s AND retired_at IS NULL",
            (subscription_id,),
        )
        return cur.rowcount > 0


# --- dispatches (the notification feed) -----------------------------------


# EVERY column here reads an owner-rights view (`listings_public`; the place and
# the MF yield `properties_public`), never `listings` — and that is a FIX, not a style
# choice. This feed runs on the tenant pool, i.e. under
# `SET LOCAL ROLE authenticated`, and `listings` has carried RLS with NO POLICY
# since migration 001: under that role the table is empty, so the LEFT JOIN this
# projection used to sit on matched nothing and every one of these columns
# arrived NULL in the feed. `listings_public` is an owner-rights view
# over the same rows with no WHERE of its own (migration 494), so it returns
# exactly what a BYPASSRLS reader always saw here and what the browser role never
# did. The two views are also the only way this query can reach a resolved location:
# migration 501 revoked `listing_location` from `authenticated`, which is the
# same reason pipeline_board_public reads its label off properties_public.
#
# `display_label` is the ONE place string (migration 503). It replaced the
# `locality, district` pair the feed used to ship, which the SPA fell back
# through as `locality ?? district` while the extension fell back the other way
# — two surfaces naming the same listing two different places. It is the
# PROPERTY's (ruling R5): a dispatch is property-grain (rule 16), so the feed
# names the place its property page, Browse and the map name, its location ad's.
_LISTING_PROJECTION = (
    "lp.category_main, lp.category_type, lp.price_czk, "
    "lp.price_unit, lp.area_m2, lp.disposition, lp.subtype, "
    "pp.display_label, "
    "lp.is_active, lp.first_seen_at, lp.last_seen_at, "
    "lp.source, lp.source_url, "
    # MF is property-grain, like the dispatch itself (rule 16): the feed shows
    # the number the matcher filtered on, off the same properties_public row.
    "pp.mf_gross_yield_pct"
)

# The unified feed projection + FROM, shared by list_dispatches + _fetch_dispatch
# so the two never diverge. LEFT JOINs (not INNER): a collection_monitor row has
# subscription_id NULL, so an INNER join to subscriptions would silently drop it.
# Exposes the source discriminator + provenance the watchdog feed never needed.
# `share_kind` says what a share opens, read off its change_kind (`shared_<collection|pipeline|
# dashboard>`, migration 652); `share_ref` its id, off the share's own columns (api/shares.py),
# never a key: the collection id, the pipeline owner (the actor), the dashboard id (event_ref).
_DISPATCH_SELECT = (
    "d.id, d.source_kind, "
    "d.subscription_id, s.name AS subscription_name, "
    "d.collection_id, c.name AS collection_name, "
    "d.listing_id, d.property_id, d.change_kind, d.message, "
    "CASE WHEN d.source_kind = 'share' THEN split_part(d.change_kind, '_', 2) END AS share_kind, "
    "CASE d.change_kind WHEN 'shared_collection' THEN d.collection_id::text "
    "  WHEN 'shared_pipeline' THEN d.actor_account_id::text "
    "  WHEN 'shared_dashboard' THEN d.event_ref END AS share_ref, "
    "d.dispatched_at, d.seen_at, "
    "d.trigger_price_czk, d.prev_price_czk, "
    f"{_LISTING_PROJECTION}"
)

_DISPATCH_FROM = (
    "FROM notification_dispatches d "
    "LEFT JOIN notification_subscriptions s ON s.id = d.subscription_id "
    "LEFT JOIN collections c ON c.id = d.collection_id "
    # Surrogate join (R2): safe for the whole append-only ledger, not just new
    # rows — Phase A4's backfill left zero (sreality_id NOT NULL, listing_id NULL)
    # rows, so no historical dispatch loses its listing fields here. A plain
    # equality keeps this an index lookup; a COALESCE/OR fallback would not.
    # ONE join, to the VIEW: the bare `listings` join it replaced was invisible to
    # the tenant role (RLS, no policy) — see _LISTING_PROJECTION.
    "LEFT JOIN listings_public lp ON lp.id = d.listing_id "
    "LEFT JOIN properties_public pp ON pp.property_id = d.property_id "
)


def list_dispatches(
    conn: "psycopg.Connection",
    *,
    subscription_id: str | None = None,
    collection_id: int | None = None,
    source_kind: SourceKindFilter = "all",
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
    per dispatch and the UI dedups (property_id → list of subscriptions)
    when it wants the "fired by N watchdogs" presentation.

    The feed is append-only and grows under the background matcher; keyset
    on (dispatched_at, id) keeps a live scroll dup/skip-free (offset would
    shift as new dispatches prepend). `id` is a uuid — fine as a
    deterministic tiebreaker. `total` is computed once, on the first page.
    """
    where: list[str] = [VISIBLE_SQL]
    params: dict[str, Any] = {}
    if subscription_id is not None:
        where.append("d.subscription_id = %(subscription_id)s")
        params["subscription_id"] = subscription_id
    if collection_id is not None:
        where.append("d.collection_id = %(collection_id)s")
        params["collection_id"] = collection_id
    if source_kind != "all":
        where.append("d.source_kind = %(source_kind)s")
        params["source_kind"] = source_kind
    if seen == "seen":
        where.append("d.seen_at IS NOT NULL")
    elif seen == "unseen":
        where.append("d.seen_at IS NULL")
    filter_sql = "WHERE " + " AND ".join(where)

    page_where = list(where)
    if cursor is not None:
        c_ts, c_id = decode_cursor(cursor)
        page_where.append(
            "(d.dispatched_at, d.id) < (%(c_ts)s::timestamptz, %(c_id)s::uuid)"
        )
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    page_where_sql = "WHERE " + " AND ".join(page_where)

    sql = (
        f"SELECT {_DISPATCH_SELECT} "
        f"{_DISPATCH_FROM}"
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
        # Count on the first page only (cursor None); legacy offset path keeps
        # its total. See list_estimation_runs for the rationale.
        if cursor is None:
            count_params = {k: params[k] for k in params if k not in ("c_ts", "c_id")}
            cur.execute(
                f"SELECT count(*) FROM notification_dispatches d {filter_sql}",
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
    if out.get("mf_gross_yield_pct") is not None:
        out["mf_gross_yield_pct"] = float(out["mf_gross_yield_pct"])
    return out


# The row a tap on `dispatch_id` stands for: itself, or, for a row a merge marked a duplicate, the
# row it duplicates (the survivor's, which the carrier keeps flat: one hop). Under RLS a foreign id
# resolves to nothing.
_RESOLVE_DISPATCH_SQL = (
    "SELECT coalesce(d.duplicate_of, d.id) FROM notification_dispatches d WHERE d.id = %s"
)


def mark_dispatch_seen(
    conn: "psycopg.Connection", dispatch_id: str,
) -> dict[str, Any] | None:
    """Mark one visible dispatch seen and return it. An id a merge later marked a duplicate
    resolves to the row it duplicates first, which is the one stamped and returned, so a tap on an
    old push lands on the survivor's alert; None (the route's 404) when the resolved row is missing
    or hidden (VISIBLE_SQL)."""
    with conn.cursor() as cur:
        cur.execute(_RESOLVE_DISPATCH_SQL, (dispatch_id,))
        row = cur.fetchone()
        if row is None:
            return None
        target = str(row[0])
        cur.execute(
            "UPDATE notification_dispatches d SET seen_at = now() "
            f"WHERE d.id = %s AND d.seen_at IS NULL AND {VISIBLE_SQL}",
            (target,),
        )
    return _fetch_dispatch(conn, target)


def _fetch_dispatch(
    conn: "psycopg.Connection", dispatch_id: str,
) -> dict[str, Any] | None:
    sql = (
        f"SELECT {_DISPATCH_SELECT} "
        f"{_DISPATCH_FROM}"
        f"WHERE d.id = %s AND {VISIBLE_SQL}"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (dispatch_id,))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if cur.description else []
    if row is None:
        return None
    return _dispatch_row_to_dict(cols, row)


def get_unread_count(conn: "psycopg.Connection") -> dict[str, int]:
    """The one unread number (bell and badge): the caller's visible unseen dispatches."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM notification_dispatches d "
            f"WHERE d.seen_at IS NULL AND {VISIBLE_SQL}"
        )
        row = cur.fetchone()
    return {"unread_count": int(row[0]) if row else 0}


def mark_all_seen(
    conn: "psycopg.Connection",
    *,
    source_kind: SourceKindFilter = "all",
) -> int:
    """Mark every visible unseen dispatch (optionally scoped to a source) as seen.

    A hidden dispatch (VISIBLE_SQL: a repeat, a dismissed property's, a retired watchdog's) keeps
    its unseen state, so a dismissal undone brings it back unread.
    """
    sql = (
        "UPDATE notification_dispatches d SET seen_at = now() "
        f"WHERE d.seen_at IS NULL AND {VISIBLE_SQL}"
    )
    with conn.cursor() as cur:
        if source_kind == "all":
            cur.execute(sql)
        else:
            cur.execute(sql + " AND d.source_kind = %s", (source_kind,))
        return cur.rowcount or 0


# --- producers (the worker's notifications lane; program § 3.3) -----------
#
# Three producers, one clock: the realtime worker's `notifications` lane runs `match_once`
# (step watchdog_new), `match_changes_once` (price) and `match_monitored_collections_once`
# (monitor) every 300 s, each step on its own connection; nothing else calls them (W2b deleted
# the API's matcher_loop). Every INSERT ends in ON_EVENT_CONFLICT, the one identity
# (EVENT_IDENTITY, migration 653), so a re-run or a deploy overlap's second worker writes a
# union, never a duplicate. `event_ref` (migration 652) is the discriminator within (source,
# kind, property); each producer's equals what 652 backfilled from the pre-W3 key on the rows
# written before it (tests/api/test_notifications.py renders and pins both), so no event already
# told re-fires.

# How far back each producer looks (program § 3.3, ruling R6 (a)). A watchdog's windows also
# start no earlier than its `active_since`, a collection's no earlier than `monitor_since`.
NEW_WINDOW = "24 hours"
PRICE_WINDOW = "7 days"
MONITOR_WINDOW = "7 days"
# 'Delisted' only once the property has stayed gone this long (F14): the delist pipeline's
# effective dwell, so a portal blink sends neither 'Delisted' nor, later, 'Relisted'.
INACTIVE_DWELL = "24 hours"
# One producer statement past this is cancelled and fails its watchdog (or its step) instead of
# holding a pass. The heaviest measured, a broad watchdog's price statement over 7 days of
# canonical steps, read 1.4 s (2026-10-21).
PRODUCER_STATEMENT_TIMEOUT = "120s"
# What a stored spec fails with deterministically: the model's ValidationError (a ValueError) and
# the one compiler's ValueError / LookupError. The write routes refuse such a spec (422); a stored
# one is counted `subscriptions_invalid`, never a statement failure.
SPEC_REJECTIONS: tuple[type[Exception], ...] = (ValueError, LookupError)


def check_filter_spec(spec: "WatchdogFilterSpec") -> None:
    """Raise what the matcher would raise for this spec (one of SPEC_REJECTIONS), else nothing."""
    _build_match_clauses(spec)


def _commit_one(conn: "psycopg.Connection", sql: str, params: dict[str, Any]) -> int:
    """One producer statement in its own transaction under the bound, committed at once: the KEY
    SHARE locks its INSERT takes on the properties it dispatches last one statement, never a whole
    step, so a merge (autodedup's 5 s lock_timeout) never waits on a producer. SET LOCAL, never a
    session SET: connect() is the transaction-mode pooler, where a session GUC leaks to others.

    The notification-events lock, SHARED, as its own statement before the INSERT (the protocol is
    toolkit.property_carriers'): the identity includes property_id, which a merge changes, so the
    INSERT's snapshot must come after any merge holding the lock has committed, and a merge that
    comes second must wait for this commit and mark the twin it then sees."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = '{PRODUCER_STATEMENT_TIMEOUT}'")
        cur.execute(NOTIFICATION_EVENTS_SHARED_LOCK_SQL)
        cur.execute(sql, params)
        return cur.rowcount or 0


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


def _read_bool_setting(
    conn: "psycopg.Connection", key: str, *, default: bool,
) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return default
    value = row[0]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


# --- images-first publication gate (operator decision 2026-07-02) ----------
#
# Never surface a NEW listing before its first image is stored. A candidate
# property is dispatchable only when (a) any of its listings has a stored
# image, OR (b) it is older than the timeout (portals with slow CDNs / genuinely
# photoless listings must not be silenced forever), OR (c) it has ZERO image
# rows at all AND is past a short floor — image rows appear at detail write, so
# zero rows after the floor ≈ no photos at source. The gate HOLDS a property:
# every pass re-reads the whole 24 h window, so a held property is dispatched on
# the first pass after its release, never lost.

IMAGE_GATE_ZERO_ROWS_FLOOR_MINUTES = 5
# A hold must end inside the 24 h window or the property would leave it unreleased: a
# timeout past this is clamped, leaving an hour (twelve passes) to dispatch the release.
IMAGE_GATE_TIMEOUT_CEILING_MINUTES = 23 * 60

_IMAGE_GATE_SQL = (
    "( EXISTS ("
    "    SELECT 1 FROM listings gl"
    "    JOIN images gi ON gi.listing_id = gl.id"
    "    WHERE gl.property_id = l.property_id"
    "      AND gi.storage_path IS NOT NULL"
    "  )"
    "  OR l.first_seen_at < now() - make_interval(mins => %(image_timeout_minutes)s)"
    "  OR ("
    "    l.first_seen_at < now()"
    "      - make_interval(mins => %(image_zero_rows_floor_minutes)s)"
    "    AND NOT EXISTS ("
    "      SELECT 1 FROM listings gl"
    "      JOIN images gi ON gi.listing_id = gl.id"
    "      WHERE gl.property_id = l.property_id"
    "    )"
    "  )"
    ")"
)


@dataclass
class ImageGateSettings:
    enabled: bool
    timeout_minutes: int


def _load_image_gate_settings(conn: "psycopg.Connection") -> ImageGateSettings:
    enabled = _read_bool_setting(
        conn, "notifications_new_requires_image", default=True,
    )
    timeout = _read_int_setting(
        conn, "notifications_new_requires_image_timeout_minutes", default=30,
    )
    return ImageGateSettings(
        enabled=enabled,
        timeout_minutes=min(max(1, timeout), IMAGE_GATE_TIMEOUT_CEILING_MINUTES),
    )


# The watchdogs a producer serves: switched on and not retired (migration 652; deleting a
# watchdog retires it, `delete_subscription`).
_LIVE_WATCHDOGS_SQL = (
    "SELECT id, filter_spec, active_since FROM notification_subscriptions "
    "WHERE is_active = true AND retired_at IS NULL"
)


def _live_watchdogs(conn: "psycopg.Connection") -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(_LIVE_WATCHDOGS_SQL)
        return cur.fetchall()


@dataclass
class _Tally:
    """One producer's per-watchdog outcome: a spec the matcher refuses is `invalid` (the same every
    pass), a statement that raised is `failed` (the lane reddens its step for it)."""
    evaluated: int
    invalid: int = 0
    failed: int = 0
    inserted: int = 0

    def stats(self, inserted_key: str) -> dict[str, int]:
        return {"subscriptions_evaluated": self.evaluated,
                "subscriptions_invalid": self.invalid,
                "subscriptions_failed": self.failed,
                inserted_key: self.inserted}


def _insert_per_watchdog(
    conn: "psycopg.Connection", tally: _Tally, sub_id: Any, raw_spec: Any, sql: str,
    params: dict[str, Any], *, extra_where: list[str], label: str,
) -> None:
    """One watchdog's INSERT (`{where}` in `sql` = its spec + `extra_where`), committed on its own
    (`_commit_one`), so one failing watchdog rolls back alone and the others still match."""
    try:
        where, spec_params = _build_match_clauses(WatchdogFilterSpec(**(raw_spec or {})))
    except SPEC_REJECTIONS as exc:
        LOG.warning("%s: subscription %s has a spec the matcher refuses: %s", label, sub_id, exc)
        tally.invalid += 1
        return
    try:
        tally.inserted += _commit_one(
            conn, sql.replace("{where}", " AND ".join([*where, *extra_where])),
            {**spec_params, **params, "subscription_id": str(sub_id)},
        )
    except Exception as exc:  # noqa: BLE001 — isolate one sub, keep loop alive
        LOG.exception("%s: subscription %s failed, skipping: %s", label, sub_id, exc)
        tally.failed += 1


# Step watchdog_new. `l` is properties_public: `listing_id` is the canonical advert
# (migration 561). 'new' carries no event_ref (program § 3.2: once ever per property).
_WATCHDOG_NEW_SQL = (
    "INSERT INTO notification_dispatches "
    "  (subscription_id, source_kind, property_id, listing_id, "
    "   change_kind, trigger_price_czk, event_ref) "
    "SELECT %(subscription_id)s, 'watchdog', l.property_id, l.listing_id, "
    "       'new', l.price_czk, NULL "
    "FROM properties_public l "
    "WHERE {where} "
    f"{ON_EVENT_CONFLICT}"
)


def match_once(conn: "psycopg.Connection") -> dict[str, int]:
    """Step watchdog_new: one 'new' dispatch per (live watchdog, matching property first
    seen inside the last 24 h and after the watchdog last started).

    ONE INSERT per watchdog over a fixed window, `first_seen_at > greatest(now() − 24 h,
    active_since)` (F1): a property whose match facts arrive late (its location resolves
    an hour on) still fires while it is under a day old, and a watchdog created, switched
    back on or given a new filter (each restamps `active_since`, migration 652) alerts only
    on what appears afterwards, never a backfill (R6 (a)). The identity (watchdog, 'new',
    property; no event_ref) is once ever and the only idempotence — no cursor, no lookback.

    Images-first gate (`notifications_new_requires_image`, default on): the INSERT also
    requires `_IMAGE_GATE_SQL`, so a brand-new property with pending image downloads is
    HELD and dispatched by the first pass after its images land or its timeout expires.
    """
    gate = _load_image_gate_settings(conn)
    extra_where = [
        "l.first_seen_at > greatest(now() - %(new_window)s::interval, %(active_since)s)",
    ]
    params: dict[str, Any] = {"new_window": NEW_WINDOW}
    if gate.enabled:
        extra_where.append(_IMAGE_GATE_SQL)
        params["image_timeout_minutes"] = gate.timeout_minutes
        params["image_zero_rows_floor_minutes"] = IMAGE_GATE_ZERO_ROWS_FLOOR_MINUTES

    subs = _live_watchdogs(conn)
    tally = _Tally(evaluated=len(subs))
    for sub_id, raw_spec, active_since in subs:
        _insert_per_watchdog(
            conn, tally, sub_id, raw_spec, _WATCHDOG_NEW_SQL,
            {**params, "active_since": active_since},
            extra_where=extra_where, label="matcher",
        )
    return tally.stats("matches_inserted")


# THE canonical-ad price-step fragment (program § 3.3, step price), read by the watchdog's
# price_drop and the collection monitor's price_drop / price_rise alike: a step of the
# property's CANONICAL advert scraped since it became canonical (`repr_since`, migration 561)
# — a merge, detach or delisting that hands the slot over replays nothing, and the price an
# alert quotes is the price the property shows. `listing_price_steps` (migration 559)
# compares an advert only with its own previous priced snapshot, so a step never spans two.
_CANONICAL_PRICE_STEPS = (
    "canonical_steps AS ("
    "  SELECT ps.property_id, ps.listing_id, ps.snapshot_id, ps.scraped_at, "
    "         ps.price_czk, ps.prev_price_czk "
    "  FROM listing_price_steps ps "
    "  JOIN properties p ON p.id = ps.property_id "
    "   AND p.repr_listing_ref_id = ps.listing_id AND ps.scraped_at > p.repr_since "
    "  WHERE ps.scraped_at > now() - %(price_window)s::interval"
    ")"
)

# A watchdog alerts on drops only; event_ref = the step's snapshot id.
_WATCHDOG_PRICE_SQL = (
    f"WITH {_CANONICAL_PRICE_STEPS} "
    "INSERT INTO notification_dispatches "
    "  (subscription_id, source_kind, property_id, listing_id, change_kind, "
    "   trigger_price_czk, prev_price_czk, event_ref) "
    "SELECT %(subscription_id)s, 'watchdog', l.property_id, l.listing_id, 'price_drop', "
    "       st.price_czk, st.prev_price_czk, st.snapshot_id::text "
    "FROM canonical_steps st "
    "JOIN properties_public l ON l.property_id = st.property_id "
    "WHERE {where} "
    f"{ON_EVENT_CONFLICT}"
)


def match_changes_once(conn: "psycopg.Connection") -> dict[str, int]:
    """Step price: every canonical-ad price step of the last 7 days, from ONE fragment
    (`_CANONICAL_PRICE_STEPS`), for both producers.

    - Watchdog: one `price_drop` per (live watchdog x matching property x drop step after
      the watchdog's `active_since`), event_ref = the step's snapshot id. The spec
      matches the property's CURRENT state through `_build_match_clauses` (rule 16).
    - Collection monitor: one `price_drop` / `price_rise` per (monitored collection x
      member property x step after `monitor_since`), event_ref = the snapshot id,
      set-based across every monitored collection.

    Per-snapshot grain: a property that keeps dropping fires once per real cut, and a
    re-scan of the window is idempotent because the snapshot id is stable.
    """
    params: dict[str, Any] = {"price_window": PRICE_WINDOW}
    subs = _live_watchdogs(conn)
    tally = _Tally(evaluated=len(subs))
    for sub_id, raw_spec, active_since in subs:
        _insert_per_watchdog(
            conn, tally, sub_id, raw_spec, _WATCHDOG_PRICE_SQL,
            {**params, "active_since": active_since},
            extra_where=["st.price_czk < st.prev_price_czk",
                         "st.scraped_at > %(active_since)s"],
            label="change matcher",
        )
    collection_inserted = _commit_one(conn, _MONITOR_PRICE_SQL, params)
    tally.inserted += collection_inserted
    return {**tally.stats("changes_inserted"),
            "collection_changes_inserted": collection_inserted}


# --- collection monitor producer (Sprint C) -------------------------------


# The monitored-membership CTE shared by every detector below: each property in
# a collection with monitoring_enabled. status='active' skips merged-away properties.
# `monitor_since` is the anchor that makes every detector fire only for changes
# AFTER monitoring began for this (collection, property) pair: the later of when
# the property was added (collection_properties.added_at) and when the collection
# last had monitoring turned on (collections.monitoring_enabled_at, migration 230).
# A pre-membership change never notifies.
_MONITORED_CTE = (
    "monitored AS ("
    "  SELECT cp.collection_id, p.id AS property_id, "
    "         p.repr_listing_ref_id, "
    "         greatest(cp.added_at, coalesce(c.monitoring_enabled_at, cp.added_at)) "
    "           AS monitor_since "
    "  FROM collection_properties cp "
    "  JOIN collections c ON c.id = cp.collection_id AND c.monitoring_enabled = true "
    "  JOIN properties p ON p.id = cp.property_id AND p.status = 'active' "
    # Gate on the SURROGATE, not the legacy handle: post-Gate-2 a property whose
    # repr listing is a new non-sreality row has repr_listing_id NULL but
    # repr_listing_ref_id set — gating on the legacy column would silently drop
    # exactly those properties out of collection monitoring.
    "                   AND p.repr_listing_ref_id IS NOT NULL"
    ")"
)

# The monitor's price half (step price): replicated per monitored collection so each
# collection alerts independently. event_ref = the step's snapshot id.
_MONITOR_PRICE_SQL = (
    f"WITH {_CANONICAL_PRICE_STEPS}, {_MONITORED_CTE} "
    "INSERT INTO notification_dispatches "
    "  (source_kind, collection_id, property_id, listing_id, change_kind, "
    "   trigger_price_czk, prev_price_czk, event_ref) "
    "SELECT 'collection_monitor', m.collection_id, st.property_id, st.listing_id, "
    "       CASE WHEN st.price_czk < st.prev_price_czk "
    "            THEN 'price_drop' ELSE 'price_rise' END, "
    "       st.price_czk, st.prev_price_czk, st.snapshot_id::text "
    "FROM monitored m "
    "JOIN canonical_steps st ON st.property_id = m.property_id "
    "WHERE st.scraped_at > m.monitor_since "
    f"{ON_EVENT_CONFLICT}"
)

# inactive — the property went inactive (every listing delisted) and has STAYED inactive
# for INACTIVE_DWELL (F14). No snapshot; event_ref = the epoch of the latest child inactive_at
# (set on the flip, cleared on reactivation, so a re-inactivation is a fresh event).
_MONITOR_INACTIVE_SQL = (
    f"WITH {_MONITORED_CTE}, "
    "gone AS ("
    "  SELECT m.collection_id, m.property_id, m.repr_listing_ref_id, "
    "         max(l.inactive_at) AS inactive_at "
    "  FROM monitored m "
    "  JOIN properties p ON p.id = m.property_id AND p.is_active = false "
    "  JOIN listings l ON l.property_id = m.property_id "
    "  GROUP BY m.collection_id, m.property_id, m.repr_listing_ref_id, "
    "           m.monitor_since "
    "  HAVING max(l.inactive_at) > now() - %(monitor_window)s::interval "
    "     AND max(l.inactive_at) < now() - %(inactive_dwell)s::interval "
    "     AND max(l.inactive_at) > m.monitor_since"
    ") "
    "INSERT INTO notification_dispatches "
    "  (source_kind, collection_id, property_id, listing_id, change_kind, event_ref) "
    "SELECT 'collection_monitor', g.collection_id, g.property_id, "
    "       g.repr_listing_ref_id, 'inactive', "
    "       floor(extract(epoch FROM g.inactive_at))::bigint::text "
    "FROM gone g "
    f"{ON_EVENT_CONFLICT}"
)

# reactivated — a property we ALREADY alerted as inactive came back active. The prior
# 'inactive' dispatch is the durable "was dead" marker (listings.inactive_at is cleared on
# reactivation, so we can't read it), and only an event counts: both reads skip a row a merge
# marked a duplicate. It must itself postdate monitor_since (the LATERAL ON),
# so a reactivation fires only for a property that went inactive while being monitored. Its
# event_ref is the epoch of that inactive dispatch's dispatched_at: the pre-W3 key's last segment,
# which 652 backfilled, so a reactivation already told keeps its identity. This supersedes the
# program's "the id of the inactive dispatch" (PROGRAM.md § 3.2): the grammar already in production
# wins, and the epoch names the same inactive dispatch for its (collection, property).
_MONITOR_REACTIVATED_SQL = (
    f"WITH {_MONITORED_CTE}, "
    "back AS ("
    "  SELECT m.collection_id, m.property_id, m.repr_listing_ref_id, "
    "         nd.dispatched_at AS inactive_at "
    "  FROM monitored m "
    "  JOIN properties p ON p.id = m.property_id "
    "       AND p.is_active = true "
    "       AND p.last_seen_at > now() - %(monitor_window)s::interval "
    "  JOIN LATERAL ("
    "    SELECT dispatched_at FROM notification_dispatches "
    "    WHERE source_kind = 'collection_monitor' "
    "      AND collection_id = m.collection_id "
    "      AND property_id = m.property_id "
    "      AND change_kind = 'inactive' "
    "      AND duplicate_of IS NULL "
    "    ORDER BY dispatched_at DESC LIMIT 1"
    "  ) nd ON p.last_seen_at > nd.dispatched_at "
    "       AND nd.dispatched_at > m.monitor_since "
    "  WHERE NOT EXISTS ("
    "    SELECT 1 FROM notification_dispatches r "
    "    WHERE r.source_kind = 'collection_monitor' "
    "      AND r.collection_id = m.collection_id "
    "      AND r.property_id = m.property_id "
    "      AND r.change_kind = 'reactivated' "
    "      AND r.duplicate_of IS NULL "
    "      AND r.dispatched_at > nd.dispatched_at"
    "  )"
    ") "
    "INSERT INTO notification_dispatches "
    "  (source_kind, collection_id, property_id, listing_id, change_kind, event_ref) "
    "SELECT 'collection_monitor', b.collection_id, b.property_id, "
    "       b.repr_listing_ref_id, 'reactivated', "
    "       floor(extract(epoch FROM b.inactive_at))::bigint::text "
    "FROM back b "
    f"{ON_EVENT_CONFLICT}"
)

# new_source — a sibling listing introduced a NEW portal to the property (a cross-source
# sighting the dedup engine grouped in). event_ref = the introducing listing's id. Limitation
# (kept, program § 3.3): fires only when that listing's first_seen_at is inside the window (the
# common "a portal just listed it" case); a merge of an OLD listing won't fire.
_MONITOR_NEW_SOURCE_SQL = (
    f"WITH {_MONITORED_CTE}, "
    "src AS ("
    "  SELECT m.collection_id, m.monitor_since, l.property_id, "
    "         l.id AS listing_id, l.first_seen_at, "
    # The introducing ad must not change between passes, or its event_ref (and so the identity)
    # would: the legacy handle breaks a first-seen tie as before, the surrogate breaks the rest.
    "         row_number() OVER (PARTITION BY m.collection_id, l.property_id, l.source "
    "                            ORDER BY l.first_seen_at, l.sreality_id, l.id) AS rn, "
    "         min(l.first_seen_at) OVER (PARTITION BY m.collection_id, l.property_id) AS prop_first "
    "  FROM monitored m "
    "  JOIN listings l ON l.property_id = m.property_id"
    "), "
    # count(DISTINCT ...) OVER (...) is unsupported in Postgres; exactly
    # one row per source has rn=1, so a filtered count over the property
    # partition IS the distinct-source count.
    "src_counted AS ("
    "  SELECT src.*, "
    "         count(*) FILTER (WHERE rn = 1) "
    "           OVER (PARTITION BY collection_id, property_id) AS n_sources "
    "  FROM src"
    ") "
    "INSERT INTO notification_dispatches "
    "  (source_kind, collection_id, property_id, listing_id, change_kind, event_ref) "
    "SELECT 'collection_monitor', src.collection_id, src.property_id, "
    "       src.listing_id, 'new_source', src.listing_id::text "
    "FROM src_counted src "
    "WHERE src.rn = 1 AND src.n_sources >= 2 "
    "  AND src.first_seen_at > src.prop_first "
    "  AND src.first_seen_at > now() - %(monitor_window)s::interval "
    "  AND src.first_seen_at > src.monitor_since "
    f"{ON_EVENT_CONFLICT}"
)


def match_monitored_collections_once(conn: "psycopg.Connection") -> dict[str, int]:
    """Step monitor: the collection monitor's lifecycle and source detectors (Sprint C).

    For every property in a collection with `monitoring_enabled = true`, emit
    `source_kind='collection_monitor'` dispatches for `inactive` (gone for a day, F14) /
    `reactivated` (lifecycle) and `new_source` (a sibling listing appeared on another
    portal); its price detector is step price (`match_changes_once`). Each dispatch carries
    `collection_id` (the source, required by source_ck) and a per-event `event_ref` (the
    discriminator within the identity), so re-runs over an overlapping window are
    idempotent and a 2nd real change is its own event. Set-based: one INSERT...SELECT per
    kind across ALL monitored collections.

    A property in N monitored collections fires N times (once per collection): monitoring
    is per-collection by design (the operator chose which collections alert). Every
    detector is anchored on `monitored.monitor_since`, so a change that predates the
    operator starting to watch the property never notifies.

    `broker_change` is intentionally NOT emitted: there is no property-level broker nor a
    broker-change timestamp to key a stable event off (program § 3.2 drops the kind).
    """
    # Cheap early-out: nothing monitored, nothing to do.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM collections WHERE monitoring_enabled = true"
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return {"monitored_collections": 0, "events_inserted": 0}
    params = {"monitor_window": MONITOR_WINDOW, "inactive_dwell": INACTIVE_DWELL}
    # In this order, each committed before the next: reactivated reads the inactive rows.
    inserted = sum(_commit_one(conn, sql, params) for sql in (
        _MONITOR_INACTIVE_SQL, _MONITOR_REACTIVATED_SQL, _MONITOR_NEW_SOURCE_SQL))
    return {"monitored_collections": int(row[0]), "events_inserted": inserted}

