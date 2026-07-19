"""FastAPI routes for the Location Audit surface.

Mounted under `/location-audit/*`, admin-gated by `require_admin` (is_admin
claim; the legacy operator token passes during the dual-auth window). This is a
READ-ONLY inspection surface: it browses every listing's address / coordinate /
admin-hierarchy fields side by side, with the acquisition method for the two
fields whose provenance actually varies per row (coordinate + street), so the
operator can see coverage gaps and hunt for portal signals the parser drops.

Two endpoints:
- `GET /location-audit`        — paginated, filtered listing rows (small columns
  only; the per-row raw_json keys it does read are three shallow `->>` lookups,
  bounded to one page, never a full-table scan — see the raw_json note below).
- `GET /location-audit/{sreality_id}/raw` — one row's full `raw_json` (a single
  PK detoast; NEVER selected in the list query, per migration 234's incident).

No migration: every column read here already exists on `listings`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api import dependencies as deps
from toolkit.publication import (
    BYT_GEO_ELIGIBLE_PREDICATE,
    GEO_ELIGIBLE_PREDICATE,
    STREET_ELIGIBLE_PREDICATE,
    eligible_predicate,
)

router = APIRouter(prefix="/location-audit", tags=["location-audit"])

# Dedup reachability — the engine's OWN "can any of the three passes reach this
# listing?" gate (`toolkit.publication.eligible_predicate`, parity-tested against
# the engine SQL, so this can't drift). All column-only (street/disposition/
# category_main/geom/obec_id/area) → safe + fast in a WHERE, unlike raw_json.
# A listing failing this never becomes a dedup candidate: it lacks the data for
# every pass — street+disposition (byt), geo+area (dům/pozemek/komerční/ostatní),
# or byt-geo (street-less byt with coord+area+disposition). The geo + byt-geo
# arms are active-only by construction (the street arm is not — inactive
# street+disposition rows still merge, to preserve price history on delisting).
_DEDUP_REACHABLE_SQL = eligible_predicate("l")


# ---------------------------------------------------------------------------
# Presence-filter allowlist. UI key -> the SQL predicate that means "this field
# is populated". The KEYS are the injection-safe surface: request `has`/`missing`
# CSVs are matched against this fixed dict, never interpolated, so an unknown key
# is ignored rather than reaching the query. Every value is a bare column test on
# the `l` alias — no bind params needed (presence is not value-dependent).
#
# DELIBERATELY column-only — no `raw_json ->> ...` predicate here. A jsonb-key
# test in the WHERE detoasts the full payload of every row the filter scans
# (measured: a single `inaccuracy_type IS NOT NULL` filter over sreality timed
# out — the migration-234 incident class). The three raw_json-derived signals
# (coords_source / inaccuracy_type / accurate) are still SELECTED for the bounded
# page (≤200 rows → ≤200 detoasts, cheap) and shown per listing; they are just
# not server-filterable.
# ---------------------------------------------------------------------------
_PRESENCE_SQL: dict[str, str] = {
    "geom": "l.geom IS NOT NULL",
    "street": "(l.street IS NOT NULL AND l.street <> '')",
    "house_number": "(l.house_number IS NOT NULL AND l.house_number <> '')",
    "zip": "(l.zip IS NOT NULL AND l.zip <> '')",
    "street_id": "l.street_id IS NOT NULL",
    "street_name_key": "(l.street_name_key IS NOT NULL AND l.street_name_key <> '')",
    "geo_cell_key": "(l.geo_cell_key IS NOT NULL AND l.geo_cell_key <> '')",
    "street_source": "l.street_source IS NOT NULL",
    "locality": "(l.locality IS NOT NULL AND l.locality <> '')",
    "district": "(l.district IS NOT NULL AND l.district <> '')",
    "obec": "(l.obec IS NOT NULL AND l.obec <> '')",
    "okres": "(l.okres IS NOT NULL AND l.okres <> '')",
    "region": "(l.region IS NOT NULL AND l.region <> '')",
    "obec_id": "l.obec_id IS NOT NULL",
    "okres_id": "l.okres_id IS NOT NULL",
    "region_id": "l.region_id IS NOT NULL",
    "locality_district_id": "l.locality_district_id IS NOT NULL",
    "locality_region_id": "l.locality_region_id IS NOT NULL",
    "locality_municipality_id": "l.locality_municipality_id IS NOT NULL",
    "locality_quarter_id": "l.locality_quarter_id IS NOT NULL",
    "locality_ward_id": "l.locality_ward_id IS NOT NULL",
}

# The field keys the UI can present as a presence toggle (order = display order).
PRESENCE_FIELDS: tuple[str, ...] = tuple(_PRESENCE_SQL.keys())

# Structured-passthrough portals: the street text is a structured portal field
# (not mined from free text), even when no numeric street_id accompanies it.
_STRUCTURED_STREET_PORTALS = frozenset({"sreality", "bezrealitky", "mmreality"})


def _geom_method(source: str, geom_present: bool, coords_source: str | None) -> str | None:
    """How the stored coordinate was acquired. CoordResolver portals stamp
    `raw_json.coords.source` only when the pin is NOT page-native; bazos stamps
    its own 3-tier tag (street / link / locality); the fully-native portals
    (sreality, mmreality, bezrealitky) never stamp, so an absent tag on a present
    geom is page-native."""
    if not geom_present:
        return None
    if source == "bazos":
        return {
            "street": "geocoded_street",
            "link": "map_link_pin",
            "locality": "geocoded_town",
        }.get(coords_source or "", "page_native")
    if coords_source in (None, "", "page", "portal_pin"):
        return "page_native"
    if coords_source == "carry_forward":
        return "carry_forward"
    if coords_source == "geocode":
        return "geocoded"
    return coords_source  # unknown future tag surfaces raw rather than hiding


def _street_method(
    source: str,
    street_present: bool,
    street_source: str | None,
    street_id_present: bool,
) -> str | None:
    """How the stored street was acquired: RÚIAN coord→street resolver,
    (future) LLM enrichment, a structured portal field, or free-text mining."""
    if not street_present:
        return None
    if street_source == "resolver":
        return "ruian_resolver"
    if street_source == "llm":
        return "llm"
    if street_id_present:
        return "structured_id"
    if source in _STRUCTURED_STREET_PORTALS:
        return "structured_text"
    return "free_text"


# Column projection for the list query — deliberately excludes raw_json itself
# (large + TOASTed; see migration 234). The three `->>` reads below are shallow
# key lookups; bounded to one page they detoast at most `limit` rows, never the
# whole table.
_LIST_SELECT = """
    l.sreality_id, l.source, l.source_id_native, l.source_url,
    l.category_main, l.category_type, l.category_sub_cb,
    l.is_active, l.last_seen_at, l.inactive_at,
    ST_Y(l.geom::geometry) AS lat, ST_X(l.geom::geometry) AS lon,
    l.street, l.house_number, l.zip, l.street_id, l.street_name_key, l.street_source,
    l.locality, l.district,
    l.obec, l.okres, l.region, l.obec_id, l.okres_id, l.region_id,
    l.locality_district_id, l.locality_region_id, l.locality_municipality_id,
    l.locality_quarter_id, l.locality_ward_id,
    l.geo_cell_key, l.geocode_attempted_at, l.coord_street_attempt_version,
    (l.raw_json -> 'coords' ->> 'source')            AS coords_source,
    (l.raw_json -> 'locality' ->> 'inaccuracy_type') AS inaccuracy_type,
    (l.raw_json ->> 'accurate')                      AS accurate
"""

# Derived dedup-reachability booleans, appended to the page SELECT. `dedup_reachable`
# matches the filter's predicate exactly; the three arm booleans show WHICH pass (if
# any) the row qualifies for. The arm predicates are the canonical exports, so the
# display can never disagree with the engine's own eligibility.
_DEDUP_COLS = f"""
    ,({_DEDUP_REACHABLE_SQL}) AS dedup_reachable
    ,({STREET_ELIGIBLE_PREDICATE}) AS elig_street
    ,({GEO_ELIGIBLE_PREDICATE}) AS elig_geo
    ,({BYT_GEO_ELIGIBLE_PREDICATE}) AS elig_byt_geo
"""


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _build_where(
    source: str | None,
    category_main: str | None,
    active: str | None,
    has: list[str],
    missing: list[str],
    dedup: str | None,
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if source:
        clauses.append("l.source = %(source)s")
        params["source"] = source
    if category_main:
        clauses.append("l.category_main = %(category_main)s")
        params["category_main"] = category_main
    if active == "active":
        clauses.append("l.is_active = true")
    elif active == "inactive":
        clauses.append("l.is_active = false")
    if dedup == "reachable":
        clauses.append(f"({_DEDUP_REACHABLE_SQL})")
    elif dedup == "unreachable":
        clauses.append(f"NOT ({_DEDUP_REACHABLE_SQL})")
    for key in has:
        pred = _PRESENCE_SQL.get(key)
        if pred:
            clauses.append(pred)
    for key in missing:
        pred = _PRESENCE_SQL.get(key)
        if pred:
            clauses.append(f"NOT {pred}")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


@router.get("")
def list_location_audit(
    source: str | None = None,
    category_main: str | None = None,
    active: str | None = Query(default=None, pattern="^(active|inactive)$"),
    has: str | None = Query(
        default=None,
        description="CSV of location field keys that MUST be populated (see PRESENCE_FIELDS).",
    ),
    missing: str | None = Query(
        default=None,
        description="CSV of location field keys that MUST be empty.",
    ),
    dedup: str | None = Query(
        default=None,
        pattern="^(reachable|unreachable)$",
        description="'reachable' = the dedup engine can reach this listing through some "
        "pass; 'unreachable' = it never becomes a candidate (insufficient data on every "
        "pass). Omit for both.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """One page of listings with their full location-field inventory, the per-row
    acquisition method for coordinate + street, and dedup reachability. Read-only."""
    has_keys = [k for k in (has.split(",") if has else []) if k.strip() in _PRESENCE_SQL]
    miss_keys = [k for k in (missing.split(",") if missing else []) if k.strip() in _PRESENCE_SQL]
    where, params = _build_where(source, category_main, active, has_keys, miss_keys, dedup)

    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM listings l {where}", params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""
            SELECT {_LIST_SELECT}{_DEDUP_COLS}
            FROM listings l
            {where}
            ORDER BY l.last_seen_at DESC NULLS LAST, l.sreality_id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {**params, "limit": limit, "offset": offset},
        )
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    data: list[dict[str, Any]] = []
    for r in rows:
        source_v = r["source"]
        geom_present = r["lat"] is not None and r["lon"] is not None
        street_present = bool(r["street"])
        accurate_raw = r["accurate"]
        data.append(
            {
                "sreality_id": r["sreality_id"],
                "source": source_v,
                "source_id_native": r["source_id_native"],
                "source_url": r["source_url"],
                "category_main": r["category_main"],
                "category_type": r["category_type"],
                "category_sub_cb": r["category_sub_cb"],
                "is_active": r["is_active"],
                "last_seen_at": _iso(r["last_seen_at"]),
                "inactive_at": _iso(r["inactive_at"]),
                "lat": r["lat"],
                "lon": r["lon"],
                "street": r["street"],
                "house_number": r["house_number"],
                "zip": r["zip"],
                "street_id": r["street_id"],
                "street_name_key": r["street_name_key"],
                "street_source": r["street_source"],
                "locality": r["locality"],
                "district": r["district"],
                "obec": r["obec"],
                "okres": r["okres"],
                "region": r["region"],
                "obec_id": r["obec_id"],
                "okres_id": r["okres_id"],
                "region_id": r["region_id"],
                "locality_district_id": r["locality_district_id"],
                "locality_region_id": r["locality_region_id"],
                "locality_municipality_id": r["locality_municipality_id"],
                "locality_quarter_id": r["locality_quarter_id"],
                "locality_ward_id": r["locality_ward_id"],
                "geo_cell_key": r["geo_cell_key"],
                "geocode_attempted_at": _iso(r["geocode_attempted_at"]),
                "coord_street_attempt_version": r["coord_street_attempt_version"],
                "coords_source": r["coords_source"],
                "inaccuracy_type": r["inaccuracy_type"],
                "accurate": (
                    None if accurate_raw is None else str(accurate_raw).lower() == "true"
                ),
                "geom_method": _geom_method(source_v, geom_present, r["coords_source"]),
                "street_method": _street_method(
                    source_v, street_present, r["street_source"], r["street_id"] is not None
                ),
                "dedup_reachable": r["dedup_reachable"],
                "elig_street": r["elig_street"],
                "elig_geo": r["elig_geo"],
                "elig_byt_geo": r["elig_byt_geo"],
            }
        )

    return {
        "data": data,
        "total": total,
        "returned": len(data),
        "limit": limit,
        "offset": offset,
    }


@router.get("/raw")
def get_location_audit_raw(
    sreality_id: int = Query(
        ...,
        description="listings PK. A query param, not a path segment, because non-sreality "
        "portals use NEGATIVE synthetic PKs and Starlette's int path convertor "
        "(`[0-9]+`) would 404 on the leading minus.",
    ),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """One listing's full `raw_json` — the original captured portal payload, for
    spotting fields the parser doesn't yet use. Single-row PK detoast: cheap, and
    kept out of the list query on purpose. Admin-gated (raw_json can carry broker
    PII, which is why the anon `listings_public` view drops it)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sreality_id, source, source_id_native, source_url,
                   category_main, category_type, last_seen_at, raw_json
            FROM listings
            WHERE sreality_id = %(id)s
            """,
            {"id": sreality_id},
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    return {
        "sreality_id": row[0],
        "source": row[1],
        "source_id_native": row[2],
        "source_url": row[3],
        "category_main": row[4],
        "category_type": row[5],
        "last_seen_at": _iso(row[6]),
        "raw_json": row[7],
    }
