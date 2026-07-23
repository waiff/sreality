"""Hermetic tests for the notifications backend (Phase U2.7).

Covers WatchdogFilterSpec validation + the SQL-clause generator. The
matcher loop and FastAPI routes are integration-heavy (DB + asyncio
lifespan) and exercised via the live deployment, not here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from api.notifications import WatchdogFilterSpec, _build_match_clauses


def test_filter_spec_defaults() -> None:
    """category_main is multiselect now: a blank-save watchdog carries no
    category_main constraint (matches every category). Deal type still
    defaults to rent."""
    spec = WatchdogFilterSpec()
    assert spec.category_main_in is None
    assert spec.category_type == "pronajem"


def test_filter_spec_spatial_requires_all_three() -> None:
    """lat/lng/radius_m must be all set or all None — partial spatial
    filters silently broke the SQL in v1, so validate eagerly."""
    with pytest.raises(ValueError):
        WatchdogFilterSpec(lat=50.0, lng=14.0)  # radius_m missing
    with pytest.raises(ValueError):
        WatchdogFilterSpec(lat=50.0, radius_m=1000)  # lng missing

    # All three set is OK.
    s = WatchdogFilterSpec(lat=50.0, lng=14.0, radius_m=1000)
    assert s.lat == 50.0


def test_build_clauses_default_category_main_unconstrained_type_rent() -> None:
    # category_main is multiselect now; a blank spec carries no category_main
    # constraint (matches every category). Deal type still defaults to rent.
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert not any("l.category_main" in w for w in where)
    assert "category_main_in" not in params
    assert "l.category_type = %(category_type)s" in where
    assert params["category_type"] == "pronajem"


def test_build_clauses_emits_category_main_in_membership() -> None:
    spec = WatchdogFilterSpec(category_main_in=["byt", "dum"])
    where, params = _build_match_clauses(spec)
    assert "l.category_main = ANY(%(category_main_in)s)" in where
    assert params["category_main_in"] == ["byt", "dum"]


def test_build_clauses_migrates_legacy_scalar_category_main() -> None:
    # Specs saved before the multiselect split carry a scalar "category_main"
    # key; the before-validator lifts it to category_main_in so the saved
    # watchdog keeps matching instead of silently widening to all categories.
    spec = WatchdogFilterSpec(**{"category_main": "dum"})
    assert spec.category_main_in == ["dum"]
    where, params = _build_match_clauses(spec)
    assert "l.category_main = ANY(%(category_main_in)s)" in where
    assert params["category_main_in"] == ["dum"]


def test_build_clauses_skips_unset_spatial() -> None:
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert not any("ST_DWithin" in w for w in where)
    assert "radius_m" not in params


def test_build_clauses_emits_spatial_when_set() -> None:
    spec = WatchdogFilterSpec(lat=50.08, lng=14.42, radius_m=1500)
    where, params = _build_match_clauses(spec)
    assert any("ST_DWithin" in w for w in where)
    # properties_public exposes lat/lng, not the raw geom column, so the
    # spatial predicate builds the point from lat/lng (Slice 2b).
    assert any("l.lat IS NOT NULL" in w for w in where)
    assert any("ST_MakePoint(l.lng, l.lat)" in w for w in where)
    assert not any("l.geom" in w for w in where)
    assert params["lat"] == 50.08
    assert params["lng"] == 14.42
    assert params["radius_m"] == 1500


def test_build_clauses_city_quality_uses_latlng_not_geom() -> None:
    """City-index containment + near-city proximity watchdogs scan
    properties_public, which projects lat/lng but NOT the raw geom column.
    Every point in _city_quality_clauses must be built from l.lng/l.lat — a
    stray `l.geom` throws `column l.geom does not exist`, caught by the
    per-subscription try/except, so those watchdogs silently NEVER match
    (the Wave 3 detection fix; this guards against a geom regression)."""
    spec = WatchdogFilterSpec(
        city_index_rules=[{"index_name": "safety", "value": 60, "op": ">="}],
        near_city_proximity={"radius_km": 10, "index_rules": []},
    )
    where, params = _build_match_clauses(spec)
    city = [w for w in where if "curated_cities_public" in w]
    # both branches emitted: one EXISTS for city_index_rules, one for near_city_proximity
    assert len(city) == 2
    joined = " ".join(city)
    assert "l.geom" not in joined
    assert "ST_MakePoint(l.lng, l.lat)" in joined
    # polygon-containment fallback also builds the listing point from lat/lng
    assert "ST_Covers(b.geom, ST_SetSRID(ST_MakePoint(l.lng, l.lat), 4326))" in joined
    assert params["near_city_radius_m"] == 10000


def test_build_clauses_property_grain_derived_predicates() -> None:
    """The merged price-change filters (migration 173) render against the
    property-grain columns properties_public exposes; the window picks the
    precomputed count column."""
    spec = WatchdogFilterSpec(price_change_count_min=2)
    where, params = _build_match_clauses(spec)
    assert "l.price_change_count >= %(price_change_count_min)s" in where
    assert params["price_change_count_min"] == 2

    spec = WatchdogFilterSpec(price_change_count_min=3, price_change_window_days=90)
    where, params = _build_match_clauses(spec)
    assert "l.price_change_count_90d >= %(price_change_count_min)s" in where
    assert params["price_change_count_min"] == 3


def test_build_clauses_total_price_change_sign_flips_direction() -> None:
    """Negative threshold = 'dropped at least', positive = 'rose at least';
    zero is treated as unset."""
    spec = WatchdogFilterSpec(total_price_change_pct=-10.0)
    where, params = _build_match_clauses(spec)
    assert "l.total_price_change_pct <= %(total_price_change_pct)s" in where
    assert params["total_price_change_pct"] == -10.0

    spec = WatchdogFilterSpec(total_price_change_pct=5.0)
    where, params = _build_match_clauses(spec)
    assert "l.total_price_change_pct >= %(total_price_change_pct)s" in where

    spec = WatchdogFilterSpec(total_price_change_pct=0.0)
    where, params = _build_match_clauses(spec)
    assert not any("total_price_change_pct" in w for w in where)


def test_spec_ignores_retired_price_history_keys() -> None:
    """Stored specs predating migration 173 carry the per-direction keys;
    pydantic's extra='ignore' default must drop them without raising."""
    spec = WatchdogFilterSpec(**{
        "distinct_site_count_min": 2,
        "price_drop_count_min": 3,
        "price_rise_count_min": 1,
        "max_price_drop_pct_min": 10.0,
    })
    where, params = _build_match_clauses(spec)
    assert not any("distinct_site_count" in w for w in where)
    assert not any("price_drop_count" in w for w in where)
    assert "distinct_site_count_min" not in params


def test_build_clauses_handles_price_and_area_bounds() -> None:
    spec = WatchdogFilterSpec(
        min_price_czk=15_000,
        max_price_czk=30_000,
        min_area_m2=40.0,
        max_area_m2=80.0,
    )
    where, params = _build_match_clauses(spec)
    assert "l.price_czk >= %(min_price_czk)s" in where
    assert "l.price_czk <= %(max_price_czk)s" in where
    assert "l.area_m2 >= %(min_area_m2)s" in where
    assert "l.area_m2 <= %(max_area_m2)s" in where
    assert params["min_price_czk"] == 15_000
    assert params["max_price_czk"] == 30_000


def test_build_clauses_tri_state_amenities() -> None:
    spec = WatchdogFilterSpec(
        has_balcony=True,
        terrace=False,
        garage=None,  # explicit "any" — no clause
    )
    where, params = _build_match_clauses(spec)
    assert "l.has_balcony = %(has_balcony)s" in where
    assert "l.terrace = %(terrace)s" in where
    assert not any("l.garage = " in w for w in where)
    assert params["has_balcony"] is True
    assert params["terrace"] is False


def test_build_clauses_dispositions_use_any() -> None:
    """Dispositions are a multi-select; SQL uses ANY() for index-friendly
    lookups against `l.disposition`."""
    spec = WatchdogFilterSpec(dispositions=["2+kk", "2+1", "3+kk"])
    where, params = _build_match_clauses(spec)
    assert "l.disposition = ANY(%(dispositions)s)" in where
    assert params["dispositions"] == ["2+kk", "2+1", "3+kk"]


def test_build_clauses_subtype_use_any() -> None:
    """Subtype is a portal-agnostic multi-select; ANY() against l.subtype."""
    spec = WatchdogFilterSpec(subtype=["rodinny_dum", "vila"])
    where, params = _build_match_clauses(spec)
    assert "l.subtype = ANY(%(subtype)s)" in where
    assert params["subtype"] == ["rodinny_dum", "vila"]


def test_build_clauses_no_subtype_by_default() -> None:
    """Default spec leaves subtype unset → no subtype clause emitted."""
    where, params = _build_match_clauses(WatchdogFilterSpec())
    assert "subtype" not in params
    assert not any("l.subtype" in c for c in where)


def test_build_clauses_district_chip_without_context() -> None:
    """A chip with `context=None` produces a single (district ILIKE name OR
    place-text ILIKE name) clause — same shape as the migration 067
    behaviour, preserved for picks at the municipality / okres / kraj
    level (where there's nothing finer to narrow against). Free-text
    matching reads `place_search_text` (street + locality, migration 182),
    never bare `locality` — bazos stores the street outside locality."""
    spec = WatchdogFilterSpec(
        districts=[{"name": "okres Jihlava", "context": None}],
    )
    where, params = _build_match_clauses(spec)
    district_clause = next(w for w in where if "district_name_0" in w)
    # Wildcards live in the bound VALUE, not as inline SQL '%' literals —
    # a bare '%' in the query string is a malformed psycopg placeholder and
    # raised at execute time, silently killing every matcher pass.
    assert "l.district ILIKE %(district_name_0)s" in district_clause
    assert "l.place_search_text ILIKE %(district_name_0)s" in district_clause
    assert "l.locality ILIKE" not in district_clause
    assert "'%'" not in district_clause
    assert " AND " not in district_clause
    assert params["district_name_0"] == "%okres Jihlava%"
    assert "district_ctx_0" not in params


def test_build_clauses_district_chip_with_context_anding_the_narrow() -> None:
    """A chip with a parent municipality narrows the name match — the
    fix for 'Edvarda Beneše · Plzeň' no longer dragging in the streets
    of the same name in Olomouc / Hradec Králové. Generates the same
    AND'd predicate browse_stats applies (migration 074)."""
    spec = WatchdogFilterSpec(
        districts=[{"name": "Edvarda Beneše", "context": "Plzeň"}],
    )
    where, params = _build_match_clauses(spec)
    district_clause = next(w for w in where if "district_name_0" in w)
    assert "%(district_name_0)s" in district_clause
    assert "%(district_ctx_0)s" in district_clause
    assert " AND " in district_clause
    assert params["district_name_0"] == "%Edvarda Beneše%"
    assert params["district_ctx_0"] == "%Plzeň%"


def test_build_clauses_district_multiple_chips_are_or_joined() -> None:
    """Two chips OR'd: the cohort matches either (Plzeň-narrowed) or
    (Olomouc-narrowed). Matches the per-chip OR Browse uses."""
    spec = WatchdogFilterSpec(
        districts=[
            {"name": "Edvarda Beneše", "context": "Plzeň"},
            {"name": "Edvarda Beneše", "context": "Olomouc"},
        ],
    )
    where, params = _build_match_clauses(spec)
    district_clause = next(w for w in where if "district_name_0" in w)
    assert "%(district_name_0)s" in district_clause
    assert "%(district_name_1)s" in district_clause
    assert " OR " in district_clause
    assert params["district_ctx_0"] == "%Plzeň%"
    assert params["district_ctx_1"] == "%Olomouc%"


def test_build_clauses_district_no_inline_percent_literals() -> None:
    """Regression: the district ILIKE clauses must carry NO inline SQL '%'
    wildcards. psycopg scans the query string for `%`-placeholders, so a bare
    '%' (as in the old `ILIKE '%' || %(name)s || '%'`) is a malformed
    placeholder that raises ProgrammingError at execute time. That raise sat
    outside the per-subscription guard in match_once, so it silently zeroed
    the entire watchdog feed for every watchdog with a district chip — 0
    dispatches despite real matches. Wildcards must live in the bound VALUE."""
    spec = WatchdogFilterSpec(
        districts=[{"name": "Jihlava", "context": "Vysočina"}],
    )
    where, params = _build_match_clauses(spec)
    district_clause = next(w for w in where if "district_name_0" in w)
    # The only '%' in the SQL must be inside %(...)s placeholders; none bare.
    assert "'%'" not in district_clause
    assert "||" not in district_clause
    # Wildcards moved into the parameter values instead.
    assert params["district_name_0"] == "%Jihlava%"
    assert params["district_ctx_0"] == "%Vysočina%"


def test_build_clauses_district_excluded_chip_is_negated() -> None:
    """An excluded chip becomes a NOT (...) group that subtracts its
    matches. With every chip excluded there is no positive include group —
    only the negation. Mirrors Browse's `not.or(...)` and browse_stats'
    EXCLUDE gate (migration 146)."""
    spec = WatchdogFilterSpec(
        districts=[{"name": "Praha", "context": None, "excluded": True}],
    )
    where, params = _build_match_clauses(spec)
    district_clauses = [w for w in where if "district_name_0" in w]
    assert len(district_clauses) == 1
    assert district_clauses[0].startswith("NOT (")
    assert "l.district ILIKE %(district_name_0)s" in district_clauses[0]
    assert params["district_name_0"] == "%Praha%"
    assert "'%'" not in district_clauses[0]  # wildcards stay in the value


def test_build_clauses_district_mixed_include_exclude() -> None:
    """Include + exclude chips emit two WHERE entries — an OR'd include
    group AND a NOT(...) exclude group — keeping the matcher in lockstep
    with Browse (queries.ts) and browse_stats (migration 146). Params are
    keyed by original chip position regardless of the split."""
    spec = WatchdogFilterSpec(
        districts=[
            {"name": "Praha", "context": None},
            {"name": "Modřany", "context": None, "excluded": True},
        ],
    )
    where, params = _build_match_clauses(spec)
    inc = next(w for w in where if "district_name_0" in w)
    exc = next(w for w in where if "district_name_1" in w)
    assert not inc.startswith("NOT (")
    assert exc.startswith("NOT (")
    assert params["district_name_0"] == "%Praha%"
    assert params["district_name_1"] == "%Modřany%"


def test_build_clauses_obec_chip_matches_obec_id() -> None:
    """A resolved obec pick matches by stable obec_id — NOT a name ILIKE — so
    picking obec 'Jihlava' can't drag in its same-named okres."""
    spec = WatchdogFilterSpec(
        districts=[{"name": "Jihlava", "level": "obec", "id": 586846}],
    )
    where, params = _build_match_clauses(spec)
    clause = next(w for w in where if "district_id_0" in w)
    assert clause == "(l.obec_id = %(district_id_0)s)"
    assert params["district_id_0"] == 586846
    assert "district_name_0" not in params  # no name ILIKE for a resolved chip


def test_build_clauses_okres_and_kraj_chips_match_their_id_columns() -> None:
    spec = WatchdogFilterSpec(
        districts=[
            {"name": "okres Jihlava", "level": "okres", "id": 3707},
            {"name": "Kraj Vysočina", "level": "kraj", "id": 108},
        ],
    )
    where, params = _build_match_clauses(spec)
    inc = next(w for w in where if "district_id_0" in w)
    assert "l.okres_id = %(district_id_0)s" in inc
    assert "l.region_id = %(district_id_1)s" in inc
    assert params["district_id_0"] == 3707
    assert params["district_id_1"] == 108


def test_build_clauses_locality_chip_narrows_to_containing_obec() -> None:
    """A street/POI pick matches its containing obec_id AND a place-text
    ILIKE — scoped to the municipality, no cross-city street collisions."""
    spec = WatchdogFilterSpec(
        districts=[
            {"name": "Edvarda Beneše", "level": "locality", "id": 554791},
        ],
    )
    where, params = _build_match_clauses(spec)
    clause = next(w for w in where if "district_id_0" in w)
    assert "l.obec_id = %(district_id_0)s" in clause
    assert "l.place_search_text ILIKE %(district_name_0)s" in clause
    assert params["district_id_0"] == 554791
    assert params["district_name_0"] == "%Edvarda Beneše%"
    assert "'%'" not in clause  # wildcards stay in the bound value


def test_build_clauses_locality_chip_never_matches_bare_locality() -> None:
    """Regression for the invisible-bazos-listing bug: bazos stores the town
    in `locality` and the street in `street`, so a street pick matched on
    bare `locality` can never see a bazos listing. Free-text place matching
    must go through `place_search_text` (street + locality, migration 182)
    in EVERY chip branch — street pick, legacy fallback, include and
    exclude alike."""
    spec = WatchdogFilterSpec(
        districts=[
            {"name": "Pezinská", "level": "locality", "id": 535419},
            {"name": "Pezinská", "level": "locality", "id": 535419,
             "excluded": True},
            {"name": "Brno", "context": "Jihomoravský kraj"},
        ],
    )
    where, _params = _build_match_clauses(spec)
    chip_clauses = [w for w in where if "district_name_" in w]
    assert chip_clauses, "expected district chip clauses"
    for clause in chip_clauses:
        assert "l.locality ILIKE" not in clause
    assert any("l.place_search_text ILIKE" in w for w in chip_clauses)


def test_build_clauses_unresolved_chip_falls_back_to_name_match() -> None:
    """A chip with no level/id (legacy saved filter) keeps the name-ILIKE
    predicate across district/place_search_text/okres/region — never
    breaks, and pre-#409 street chips gain street matching too."""
    spec = WatchdogFilterSpec(districts=[{"name": "Brno", "context": None}])
    where, params = _build_match_clauses(spec)
    clause = next(w for w in where if "district_name_0" in w)
    assert "l.okres ILIKE %(district_name_0)s" in clause
    assert "l.place_search_text ILIKE %(district_name_0)s" in clause
    assert "district_id_0" not in params


def test_filter_spec_lifts_legacy_string_districts() -> None:
    """Pre-migration-070 request bodies passing `districts: ["Praha"]`
    still validate — the field_validator lifts each string to a
    `{name, context: None}` chip so the matcher only sees the new
    shape. Covers the deploy window between the API redeploy and the
    backfill running."""
    spec = WatchdogFilterSpec(districts=["Praha", "okres Jihlava"])
    assert spec.districts is not None
    assert len(spec.districts) == 2
    assert spec.districts[0].name == "Praha"
    assert spec.districts[0].context is None
    assert spec.districts[1].name == "okres Jihlava"
    assert spec.districts[1].context is None


def test_build_clauses_enumerated_columns() -> None:
    # A bare string still coerces to a single-element list (backward-compat).
    spec = WatchdogFilterSpec(furnished="ano", ownership=["osobni", "druzstevni"])
    assert spec.furnished == ["ano"]
    where, params = _build_match_clauses(spec)
    clauses = " ".join(where)
    assert "l.furnished = ANY(%(furnished)s)" in clauses
    assert "l.ownership = ANY(%(ownership)s)" in clauses
    assert params["furnished"] == ["ano"]
    assert params["ownership"] == ["osobni", "druzstevni"]


def test_build_clauses_furnished_unknown_sentinel() -> None:
    spec = WatchdogFilterSpec(furnished=["__unknown__"])
    where, params = _build_match_clauses(spec)
    clauses = " ".join(where)
    assert "l.furnished IS NULL OR NOT (l.furnished = ANY(%(furnished_canon)s))" in clauses
    assert params["furnished_canon"] == ["ano", "ne", "castecne"]


def test_build_clauses_condition_match() -> None:
    """condition_match feeds ANY() against l.condition — the same
    pattern toolkit/comparables uses, so Browse / Watchdog stay
    aligned with the analytical surfaces."""
    spec = WatchdogFilterSpec(
        condition_match=["novostavba", "po_rekonstrukci"],
    )
    where, params = _build_match_clauses(spec)
    assert "l.condition = ANY(%(condition_match)s)" in where
    assert params["condition_match"] == ["novostavba", "po_rekonstrukci"]


def test_build_clauses_condition_match_empty_list_drops_clause() -> None:
    """An empty list behaves like None — no WHERE clause emitted.
    Matches how `dispositions` already behaves."""
    spec = WatchdogFilterSpec(condition_match=[])
    where, params = _build_match_clauses(spec)
    assert not any("l.condition" in w for w in where)
    assert "condition_match" not in params


def test_build_clauses_categoryless_spec() -> None:
    """An operator who explicitly clears the category filters gets a
    spec with no category WHERE clauses — the watchdog matches every
    category. Defaults narrow; explicit None widens."""
    spec = WatchdogFilterSpec(category_main_in=None, category_type=None)
    where, params = _build_match_clauses(spec)
    assert not any("l.category_main" in w for w in where)
    assert not any("l.category_type" in w for w in where)
    assert "category_main_in" not in params
    assert "category_type" not in params


def test_build_clauses_condition_level_maximums() -> None:
    spec = WatchdogFilterSpec(
        building_condition_level_max=3,
        apartment_condition_level_max=2,
    )
    where, params = _build_match_clauses(spec)
    assert any("building_condition_level <= %(building_condition_level_max)s" in w for w in where)
    assert any("apartment_condition_level <= %(apartment_condition_level_max)s" in w for w in where)
    assert params["building_condition_level_max"] == 3
    assert params["apartment_condition_level_max"] == 2


def test_build_clauses_condition_level_minimums() -> None:
    """Watchdog must honour the new condition-level filters so the
    operator can say 'alert me when a level-5 apartment shows up'."""
    spec = WatchdogFilterSpec(
        building_condition_level_min=4,
        apartment_condition_level_min=5,
    )
    where, params = _build_match_clauses(spec)
    assert any("building_condition_level >= %(building_condition_level_min)s" in w for w in where)
    assert any("apartment_condition_level >= %(apartment_condition_level_min)s" in w for w in where)
    assert params["building_condition_level_min"] == 4
    assert params["apartment_condition_level_min"] == 5


def test_build_clauses_condition_level_minimums_omitted_by_default() -> None:
    """Absent filters add no clauses — NULL rows would otherwise be
    excluded by an unintended `IS NOT NULL` check."""
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert not any("building_condition_level" in w for w in where)
    assert not any("apartment_condition_level" in w for w in where)
    assert "building_condition_level_min" not in params
    assert "apartment_condition_level_min" not in params


# --- match_once with a fake psycopg connection ---------------------------


from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from api.notifications import (
    get_unread_count,
    mark_all_seen,
    match_monitored_collections_once,
    match_once,
)


class _FakeCursor:
    """Minimal cursor that returns canned rows in sequence.

    `_FakeConn.results` is a list of (predicate, rows) pairs; the cursor
    finds the first predicate that matches the executed SQL and returns
    its `rows`. Insert/UPDATE SQL is captured in `_FakeConn.executed`
    so the test can assert on what the matcher emitted.
    """

    def __init__(self, conn: "_FakeConn"):
        self._conn = conn
        self._last_rows: list[tuple[Any, ...]] = []
        self.rowcount = 0
        self.description: list[tuple[str]] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        sql_norm = " ".join(sql.split())
        self._conn.executed.append((sql_norm, params))
        for predicate, rows, rowcount in self._conn.script:
            if predicate(sql_norm):
                self._last_rows = rows
                self.rowcount = rowcount
                return
        self._last_rows = []
        self.rowcount = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._last_rows[0] if self._last_rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._last_rows)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _FakeConn:
    def __init__(
        self,
        script: list[tuple[Any, list[tuple[Any, ...]], int]],
    ):
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def test_create_subscription_stamps_account_id() -> None:
    """create_subscription must write account_id into the INSERT — it is NOT NULL
    since migration 364. The route resolves it from the caller's JWT via
    tenant_pool.resolve_account_id and passes it here; None would be a
    NotNullViolation by design (the route 400s first)."""
    from api.notifications import create_subscription

    acct = UUID("11111111-1111-1111-1111-111111111111")
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (
            lambda s: "INSERT INTO notification_subscriptions" in s,
            [(UUID("22222222-2222-2222-2222-222222222222"),)],
            1,
        ),
    ]
    conn = _FakeConn(script)
    create_subscription(
        conn, name="Test", filter_spec=WatchdogFilterSpec(), account_id=acct
    )
    insert_sql, insert_params = conn.executed[0]
    assert "INSERT INTO notification_subscriptions" in insert_sql
    assert "account_id" in insert_sql
    assert acct in insert_params


def test_matcher_lease_cas_acquires_and_yields() -> None:
    """The single-runner lease (migration 366) is a mig-279-shape CAS
    UPDATE ... RETURNING: a returned row means we hold it, no row means another
    runner does. Guards against N replicas fanning out N× market-wide scans."""
    from api.notifications import _try_matcher_lease

    acquired = _FakeConn(
        [(lambda s: "UPDATE notification_matcher_lease" in s, [(1,)], 1)]
    )
    assert _try_matcher_lease(acquired, "matcher:me") is True
    sql, params = acquired.executed[0]
    assert "notification_matcher_lease" in sql
    assert "RETURNING" in sql.upper()
    assert params["holder"] == "matcher:me"

    held = _FakeConn(
        [(lambda s: "UPDATE notification_matcher_lease" in s, [], 0)]
    )
    assert _try_matcher_lease(held, "matcher:me") is False


def test_match_once_uses_per_subscription_cursor() -> None:
    """The matcher reads `last_matched_first_seen_at` per subscription
    and injects it as the `cursor` clause — proves the per-subscription
    cursor column drives the SQL, not the old global watermark."""
    sub_id = UUID("46a6005f-dc32-4d94-9ced-4d262b57ef6b")
    cursor_ts = datetime(2026, 5, 14, 13, 57, 48, tzinfo=timezone.utc)
    upper_ts = datetime(2026, 5, 15, 21, 0, 0, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        # app_settings reads for interval / window / image gate — default all.
        (lambda s: "FROM app_settings" in s, [], 0),
        # Active subscriptions query
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {"category_main": "byt", "category_type": "pronajem", "districts": ["Praha"]}, cursor_ts, ["email", "in_app"])],
            1,
        ),
        # Window upper-bound query (published_at-keyed since migration 273's gate)
        (lambda s: "SELECT max(published_at), count(*) FROM" in s, [(upper_ts, 5)], 0),
        # Gate-pending lookback INSERT (default-on gate) — nothing released.
        (
            lambda s: "INSERT INTO notification_dispatches" in s
            and "image_lookback_minutes" in s,
            [],
            0,
        ),
        # Window INSERT dispatches — return rowcount 3 (idempotent dedup)
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 3),
        # Cursor advance UPDATE — rowcount 1
        (lambda s: "UPDATE notification_subscriptions SET last_matched_first_seen_at" in s, [], 1),
    ]
    conn = _FakeConn(script)

    stats = match_once(conn)  # type: ignore[arg-type]

    assert stats == {
        "subscriptions_evaluated": 1,
        "matches_inserted": 3,
        "gate_lookback_inserted": 0,
        "listings_in_window": 5,
        "cursors_advanced": 1,
    }

    # Verify the WHERE clause references the per-subscription cursor
    # parameter, not a global watermark.
    window_query = next(
        (sql, p) for sql, p in conn.executed if "max(published_at), count(*)" in sql
    )
    sql, params = window_query
    # New-dispatch detection keys on published_at (migration 273), not first_seen_at;
    # the cursor column keeps its name but now holds a published_at watermark.
    assert "l.published_at > %(cursor)s" in sql
    assert isinstance(params, dict) and params["cursor"] == cursor_ts

    # And that the filter spec made it through too. The district chip
    # without a context lands as a single ILIKE-OR pair under the
    # `district_name_0` placeholder (matching browse_stats' migration
    # 069 predicate); the row's legacy string form is lifted by
    # `WatchdogFilterSpec._lift_legacy_districts` before SQL build.
    # The stored spec carries a legacy scalar category_main; the before-validator
    # lifts it to category_main_in and the matcher emits an = ANY membership.
    assert params["category_main_in"] == ["byt"]
    assert params["category_type"] == "pronajem"
    assert params["district_name_0"] == "%Praha%"
    assert "district_ctx_0" not in params  # null context = no narrow
    assert "l.district ILIKE %(district_name_0)s" in sql

    # The 'new' dispatch INSERT writes the unified event shape: source_kind,
    # a per-property dedupe_key, ON CONFLICT on that key (migration 206), and the
    # producer-stamped target_channels (migration 208) — the subscription's
    # channels minus the always-implicit 'in_app'.
    insert_sql, insert_params = next(
        (sql, p) for sql, p in conn.executed
        if "INSERT INTO notification_dispatches" in sql
    )
    assert "'watchdog'" in insert_sql
    assert ":new:' || l.property_id::text" in insert_sql
    assert "ON CONFLICT (dedupe_key)" in insert_sql
    assert "%(target_channels)s::text[]" in insert_sql
    assert insert_params["target_channels"] == ["email"]


def test_collection_monitor_noop_when_nothing_monitored() -> None:
    """No monitored collection → cheap early-out, no INSERT attempted."""
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM collections WHERE monitoring_enabled = true" in s, [(0,)], 0),
    ]
    conn = _FakeConn(script)
    stats = match_monitored_collections_once(conn)  # type: ignore[arg-type]
    assert stats == {"monitored_collections": 0, "events_inserted": 0}
    assert not any(
        "INSERT INTO notification_dispatches" in s for s, _ in conn.executed
    )


def test_collection_monitor_emits_the_five_clean_kinds() -> None:
    """One set-based INSERT per detector across all monitored collections;
    every dispatch is source_kind='collection_monitor', collection-scoped
    dedupe, ON CONFLICT idempotent. broker_change is reserved, not emitted."""
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM collections WHERE monitoring_enabled = true" in s, [(2,)], 0),
        (lambda s: "FROM app_settings" in s, [], 0),  # window default
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 2),
    ]
    conn = _FakeConn(script)
    stats = match_monitored_collections_once(conn)  # type: ignore[arg-type]
    assert stats["monitored_collections"] == 2
    # 4 INSERT statements (price_drop + price_rise share one CASE insert) x rowcount 2.
    assert stats["events_inserted"] == 8

    inserts = [
        sql for sql, _ in conn.executed
        if "INSERT INTO notification_dispatches" in sql
    ]
    assert len(inserts) == 4
    joined = " ".join(inserts)
    assert "'collection_monitor'" in joined
    for kind in ("'price_drop'", "'price_rise'", "'inactive'", "'reactivated'", "'new_source'"):
        assert kind in joined
    assert "'cm:'" in joined                       # collection-scoped dedupe prefix
    assert joined.count("ON CONFLICT (dedupe_key)") == 4
    assert "'broker_change'" not in joined          # reserved, no clean signal yet


def test_collection_monitor_dedupe_keys_survive_a_null_sreality_id() -> None:
    """No dedupe_key may concatenate a bare `sreality_id` (R2/Gate 2).

    `dedupe_key` is NOT NULL and `||` yields NULL if ANY operand is NULL, so a
    post-Gate-2 listing (sreality_id NULL) would make the whole key NULL and abort
    the ENTIRE collection-monitor pass with a not-null violation — every collection
    silently stops notifying, not just the one row. The `new_source` detector was
    the one keyed this way; it now COALESCEs onto the surrogate. This test pins the
    invariant for every detector so a future one can't reintroduce it."""
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM collections WHERE monitoring_enabled = true" in s, [(1,)], 0),
        (lambda s: "FROM app_settings" in s, [], 0),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 0),
    ]
    conn = _FakeConn(script)
    match_monitored_collections_once(conn)  # type: ignore[arg-type]

    for sql, _ in conn.executed:
        if "INSERT INTO notification_dispatches" not in sql:
            continue
        # The dedupe_key expression is the tail of the SELECT list; any bare
        # sreality_id concatenated into it is the bug.
        assert "|| src.sreality_id::text" not in sql
        assert "|| st.sreality_id::text" not in sql
        assert "|| l.sreality_id::text" not in sql
        if "new_source" in sql:
            # Positive form: the surrogate is the NULL-safe fallback.
            assert "coalesce(src.sreality_id::text, 'l' || src.listing_id::text)" in sql


def test_collection_monitor_gates_every_detector_on_monitor_since() -> None:
    """Each detector fires only for changes AFTER monitoring began for the
    (collection, property) pair. The shared `monitored` CTE computes the anchor
    `monitor_since = greatest(added_at, monitoring_enabled_at)`, and price /
    inactive / new_source gate their change-time on it (reactivated is gated
    transitively via the inactive dispatch). This is the false-positive the fix
    closes: a price drop that predates membership must not notify."""
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM collections WHERE monitoring_enabled = true" in s, [(1,)], 0),
        (lambda s: "FROM app_settings" in s, [], 0),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 0),
    ]
    conn = _FakeConn(script)
    match_monitored_collections_once(conn)  # type: ignore[arg-type]

    inserts = [
        sql for sql, _ in conn.executed
        if "INSERT INTO notification_dispatches" in sql
    ]
    assert len(inserts) == 4
    anchor = (
        "greatest(cp.added_at, coalesce(c.monitoring_enabled_at, cp.added_at)) "
        "AS monitor_since"
    )
    for sql in inserts:
        assert anchor in sql  # every detector shares the anchored CTE

    price_sql = next(s for s in inserts if "'price_drop'" in s)
    assert "st.scraped_at > st.monitor_since" in price_sql

    # The inactive insert references 'inactive' but not 'reactivated'.
    inactive_sql = next(
        s for s in inserts if "'inactive'" in s and "'reactivated'" not in s
    )
    assert "max(l.inactive_at) > m.monitor_since" in inactive_sql

    new_source_sql = next(s for s in inserts if "'new_source'" in s)
    assert "src.first_seen_at > src.monitor_since" in new_source_sql
    # new_source must NOT use count(DISTINCT ...) OVER (...): Postgres rejects it
    # (0A000), which threw on every producer run pre-fix. The distinct-source
    # count is a filtered window count over the rn=1 rows instead.
    assert "count(DISTINCT" not in new_source_sql
    assert "count(*) FILTER (WHERE rn = 1)" in new_source_sql

    # reactivated is hardened: the prior 'inactive' dispatch it keys on must
    # itself postdate monitor_since (no leak off a pre-fix ungated dispatch).
    reactivated_sql = next(s for s in inserts if "'reactivated'" in s)
    assert "nd.dispatched_at > m.monitor_since" in reactivated_sql


def test_get_unread_count_breaks_down_by_source() -> None:
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (
            lambda s: "GROUP BY source_kind" in s,
            [("watchdog", 4), ("collection_monitor", 3)],
            0,
        ),
    ]
    conn = _FakeConn(script)
    out = get_unread_count(conn)  # type: ignore[arg-type]
    assert out == {
        "watchdog": 4, "collection_monitor": 3, "system_health": 0,
        "total": 7, "unread_count": 7,
    }
    scoped = get_unread_count(conn, source_kind="collection_monitor")  # type: ignore[arg-type]
    assert scoped["unread_count"] == 3


def test_get_unread_count_sums_system_health_into_total() -> None:
    # total sums EVERY kind, not a hardcoded watchdog+collection_monitor — the bug
    # that previously dropped system_health alerts from the nav badge.
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (
            lambda s: "GROUP BY source_kind" in s,
            [("watchdog", 4), ("collection_monitor", 3), ("system_health", 2)],
            0,
        ),
    ]
    conn = _FakeConn(script)
    out = get_unread_count(conn)  # type: ignore[arg-type]
    assert out["system_health"] == 2
    assert out["total"] == 9
    assert out["unread_count"] == 9
    scoped = get_unread_count(conn, source_kind="system_health")  # type: ignore[arg-type]
    assert scoped["unread_count"] == 2


def test_dispatch_select_projects_message() -> None:
    # The feed row carries d.message so system_health alerts render their verbatim text.
    from api.notifications import _DISPATCH_SELECT

    assert "d.message" in _DISPATCH_SELECT


def test_mark_all_seen_scoped_filters_by_source() -> None:
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "UPDATE notification_dispatches SET seen_at" in s, [], 5),
    ]
    conn = _FakeConn(script)
    assert mark_all_seen(conn) == 5  # type: ignore[arg-type]
    assert mark_all_seen(conn, source_kind="watchdog") == 5  # type: ignore[arg-type]
    scoped_sql = [
        s for s, _ in conn.executed if "UPDATE notification_dispatches" in s
    ][-1]
    assert "AND source_kind = %s" in scoped_sql


def test_match_once_skips_subscription_with_no_listings() -> None:
    """An empty window (no listings past the cursor) is the steady
    state. The matcher should record zero dispatches and zero cursor
    advances rather than blow up trying to UPDATE with a NULL value.
    (Image gate scripted OFF — the gate's empty-window lookback re-scan
    has its own test below.)"""
    sub_id = UUID("00000000-0000-0000-0000-000000000001")
    cursor_ts = datetime(2030, 1, 1, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        # Every app_settings read returns false-y: interval/window fall back to
        # their floors and the image-gate flag reads False (gate off).
        (lambda s: "FROM app_settings" in s, [(False,)], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {}, cursor_ts, [])],
            1,
        ),
        # Window query returns (NULL, 0) — no fresh listings.
        (lambda s: "SELECT max(published_at), count(*) FROM" in s, [(None, 0)], 0),
    ]
    conn = _FakeConn(script)

    stats = match_once(conn)  # type: ignore[arg-type]

    assert stats["subscriptions_evaluated"] == 1
    assert stats["matches_inserted"] == 0
    assert stats["cursors_advanced"] == 0
    # Critically: no INSERT or UPDATE was attempted.
    assert not any("INSERT INTO notification_dispatches" in sql for sql, _ in conn.executed)
    assert not any(
        "UPDATE notification_subscriptions" in sql for sql, _ in conn.executed
    )


def test_match_once_skips_invalid_filter_spec() -> None:
    """A subscription whose filter_spec doesn't validate (e.g. corrupt
    legacy row) must NOT crash the whole matcher pass — log + skip."""
    good_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    bad_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    cursor_ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    upper_ts = datetime(2026, 5, 15, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [
                # Invalid: partial spatial filter (no radius).
                (bad_id, {"lat": 50.0, "lng": 14.0}, cursor_ts, []),
                # Valid spec
                (good_id, {}, cursor_ts, []),
            ],
            2,
        ),
        (lambda s: "SELECT max(published_at), count(*) FROM" in s, [(upper_ts, 1)], 0),
        (
            lambda s: "INSERT INTO notification_dispatches" in s
            and "image_lookback_minutes" in s,
            [],
            0,
        ),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 1),
        (lambda s: "UPDATE notification_subscriptions SET last_matched_first_seen_at" in s, [], 1),
    ]
    conn = _FakeConn(script)

    stats = match_once(conn)  # type: ignore[arg-type]

    # Both rows counted as "evaluated" — the broken one was inspected
    # and skipped, but the surviving one fired one dispatch.
    assert stats["subscriptions_evaluated"] == 2
    assert stats["matches_inserted"] == 1


# --- images-first publication gate (Wave C-4) ----------------------------


from api.notifications import (
    IMAGE_GATE_ZERO_ROWS_FLOOR_MINUTES,
    ImageGateSettings,
    _read_bool_setting,
)


def _gate_script(
    sub_id: UUID, cursor_ts: datetime, upper: tuple[Any, Any],
) -> list[tuple[Any, list[tuple[Any, ...]], int]]:
    return [
        # app_settings all default → gate ON (default true), timeout 30.
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {}, cursor_ts, [])],
            1,
        ),
        (lambda s: "SELECT max(published_at), count(*) FROM" in s, [upper], 0),
        (
            lambda s: "INSERT INTO notification_dispatches" in s
            and "image_lookback_minutes" in s,
            [],
            1,
        ),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 0),
        (lambda s: "UPDATE notification_subscriptions SET last_matched_first_seen_at" in s, [], 1),
    ]


def test_match_once_image_gate_holds_new_properties_without_losing_them() -> None:
    """Default-on gate: the window INSERT requires a stored image (or a release
    arm), the CURSOR still advances past held properties (the window/upper query
    carries NO gate — one photoless CDN can't stall the feed), and the
    gate-pending lookback re-scan re-considers already-passed properties so a
    held property is dispatched once released — never lost."""
    sub_id = UUID("00000000-0000-0000-0000-00000000000a")
    cursor_ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    upper_ts = datetime(2026, 7, 2, tzinfo=timezone.utc)

    conn = _FakeConn(_gate_script(sub_id, cursor_ts, (upper_ts, 2)))
    stats = match_once(conn)  # type: ignore[arg-type]

    # Phase 1 (window upper / cursor advance) is gate-free by design.
    window_sql, _ = next(
        (sql, p) for sql, p in conn.executed
        if "max(published_at), count(*)" in sql
    )
    assert "storage_path" not in window_sql

    inserts = [
        (sql, p) for sql, p in conn.executed
        if "INSERT INTO notification_dispatches" in sql
    ]
    assert len(inserts) == 2  # window insert + lookback re-scan

    window_insert_sql, window_params = inserts[0]
    assert "image_lookback_minutes" not in window_insert_sql
    # The three release arms, set-based inside the INSERT's WHERE:
    assert "gi.storage_path IS NOT NULL" in window_insert_sql            # (a)
    assert (
        "make_interval(mins => %(image_timeout_minutes)s)"
        in window_insert_sql
    )                                                                     # (b)
    assert "make_interval(mins => %(image_zero_rows_floor_minutes)s)" in window_insert_sql
    assert "AND NOT EXISTS" in window_insert_sql                          # (c)
    assert window_params["image_timeout_minutes"] == 30
    assert (
        window_params["image_zero_rows_floor_minutes"]
        == IMAGE_GATE_ZERO_ROWS_FLOOR_MINUTES
    )

    lookback_sql, lookback_params = inserts[1]
    # Re-scan targets properties the cursor already passed, bounded by the
    # lookback window, under the same gate; dedupe_key keeps it idempotent.
    assert "l.published_at <= %(cursor)s" in lookback_sql
    assert "make_interval(mins => %(image_lookback_minutes)s)" in lookback_sql
    assert "gi.storage_path IS NOT NULL" in lookback_sql
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in lookback_sql
    assert lookback_params["image_lookback_minutes"] == 60  # max(60, 2×30)

    # Cursor advanced past the (possibly held) window — the lookback re-scan is
    # what keeps held properties reachable, not a stalled cursor.
    assert any(
        "UPDATE notification_subscriptions SET last_matched_first_seen_at" in sql
        for sql, _ in conn.executed
    )
    assert stats["cursors_advanced"] == 1
    assert stats["matches_inserted"] == 1       # the released lookback property
    assert stats["gate_lookback_inserted"] == 1


def test_match_once_gate_lookback_runs_even_when_window_is_empty() -> None:
    """A held property must not be stranded waiting for unrelated new
    inventory: with NO fresh listings past the cursor (upper NULL) the
    gate-pending lookback INSERT still runs — that's how a gated property gets
    dispatched after its images land even on a quiet market."""
    sub_id = UUID("00000000-0000-0000-0000-00000000000b")
    cursor_ts = datetime(2026, 7, 1, tzinfo=timezone.utc)

    conn = _FakeConn(_gate_script(sub_id, cursor_ts, (None, 0)))
    stats = match_once(conn)  # type: ignore[arg-type]

    inserts = [
        sql for sql, _ in conn.executed
        if "INSERT INTO notification_dispatches" in sql
    ]
    assert len(inserts) == 1
    assert "image_lookback_minutes" in inserts[0]
    # No window → no cursor advance (unchanged), but the release still fired.
    assert not any(
        "UPDATE notification_subscriptions" in sql for sql, _ in conn.executed
    )
    assert stats["cursors_advanced"] == 0
    assert stats["matches_inserted"] == 1
    assert stats["gate_lookback_inserted"] == 1


def test_match_once_gate_flag_off_restores_old_behavior() -> None:
    """`notifications_new_requires_image` = false → exactly one ungated window
    INSERT, no lookback re-scan, no gate SQL anywhere."""
    sub_id = UUID("00000000-0000-0000-0000-00000000000c")
    cursor_ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    upper_ts = datetime(2026, 7, 2, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        # Every app_settings read returns false-y → the gate flag reads False.
        (lambda s: "FROM app_settings" in s, [(False,)], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {}, cursor_ts, [])],
            1,
        ),
        (lambda s: "SELECT max(published_at), count(*) FROM" in s, [(upper_ts, 2)], 0),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 2),
        (lambda s: "UPDATE notification_subscriptions SET last_matched_first_seen_at" in s, [], 1),
    ]
    conn = _FakeConn(script)
    stats = match_once(conn)  # type: ignore[arg-type]

    inserts = [
        sql for sql, _ in conn.executed
        if "INSERT INTO notification_dispatches" in sql
    ]
    assert len(inserts) == 1
    assert "storage_path" not in inserts[0]
    assert "image_lookback_minutes" not in inserts[0]
    assert stats["matches_inserted"] == 2
    assert stats["gate_lookback_inserted"] == 0
    assert stats["cursors_advanced"] == 1


def test_image_gate_lookback_always_covers_the_timeout() -> None:
    """The re-scan window must exceed the longest possible hold, or a property
    gated the whole way would leave the lookback before its release."""
    assert ImageGateSettings(True, 30).lookback_minutes == 60
    assert ImageGateSettings(True, 45).lookback_minutes == 90
    assert ImageGateSettings(True, 120).lookback_minutes == 240
    assert ImageGateSettings(True, 5).lookback_minutes == 60  # floor


def test_read_bool_setting_parses_jsonb_shapes() -> None:
    def read(rows: list[tuple[Any, ...]], default: bool = True) -> bool:
        conn = _FakeConn([(lambda s: "FROM app_settings" in s, rows, 0)])
        return _read_bool_setting(conn, "k", default=default)  # type: ignore[arg-type]

    assert read([], default=True) is True         # absent → default
    assert read([], default=False) is False
    assert read([(True,)]) is True
    assert read([(False,)]) is False
    assert read([("false",)]) is False
    assert read([("TRUE",)]) is True
    assert read([(0,)]) is False
    assert read([(1,)]) is True


# --- match_changes_once (the property-change matcher, Slice 2b) ----------


from api.notifications import match_changes_once


def test_match_changes_once_emits_price_drop_for_matching_subs() -> None:
    """The change matcher resolves recent per-snapshot price drops once, then
    fires a `price_drop` dispatch per (matching subscription x drop snapshot),
    deduped by the per-snapshot dedupe_key so each genuine cut is its own
    event. Provenance (snapshot id + new/prev price) rides the unnest join."""
    sub_id = UUID("12121212-1212-1212-1212-121212121212")
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        # window-days app_settings read → default
        (lambda s: "FROM app_settings" in s, [], 0),
        # recent price-drop steps: (property_id, snapshot_id, price, prev)
        (
            lambda s: "FROM steps" in s,
            [(101, 5001, 4_900_000, 5_000_000), (102, 5002, 2_400_000, 2_500_000)],
            0,
        ),
        # active subscriptions
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {"category_main": "byt", "category_type": "pronajem"}, ["email"])],
            1,
        ),
        # INSERT price_drop dispatches — 2 inserted
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 2),
    ]
    conn = _FakeConn(script)

    stats = match_changes_once(conn)  # type: ignore[arg-type]

    assert stats["subscriptions_evaluated"] == 1
    assert stats["price_drops_in_window"] == 2
    assert stats["changes_inserted"] == 2

    insert_sql, insert_params = next(
        (sql, p) for sql, p in conn.executed
        if "INSERT INTO notification_dispatches" in sql
    )
    assert "'price_drop'" in insert_sql
    # Per-snapshot dedup + the unnest provenance join replace the old
    # ANY(drop_ids) + composite ON CONFLICT.
    assert "ON CONFLICT (dedupe_key)" in insert_sql
    assert ":price_drop:' || d.snapshot_id::text" in insert_sql
    assert "JOIN unnest(" in insert_sql
    assert insert_params["drop_pids"] == [101, 102]
    assert insert_params["drop_sids"] == [5001, 5002]
    assert insert_params["drop_prices"] == [4_900_000, 2_400_000]
    assert insert_params["drop_prevs"] == [5_000_000, 2_500_000]
    # Producer-stamped delivery routing (migration 208) — subscription channels
    # minus the always-implicit 'in_app'.
    assert "%(target_channels)s::text[]" in insert_sql
    assert insert_params["target_channels"] == ["email"]


def test_match_changes_once_noops_when_no_recent_drops() -> None:
    """No recent price drops → no subscription scan, no inserts."""
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM app_settings" in s, [], 0),
        (lambda s: "FROM steps" in s, [], 0),
    ]
    conn = _FakeConn(script)

    stats = match_changes_once(conn)  # type: ignore[arg-type]

    assert stats == {
        "subscriptions_evaluated": 0,
        "price_drops_in_window": 0,
        "changes_inserted": 0,
    }
    assert not any(
        "INSERT INTO notification_dispatches" in sql for sql, _ in conn.executed
    )


# --- kickoff (Run estimation) regression --------------------------------------


from api.notifications import _insert_pending_run


def test_insert_pending_run_does_not_reference_nonexistent_columns() -> None:
    """Regression: estimation_runs has NO category_main / category_type
    columns. The kickoff INSERT must not name them (doing so 500s the
    `/dispatches/{id}/estimate` endpoint, which silently no-ops the
    'Run estimation' button). category_main/type ride in input_spec instead."""
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "INSERT INTO estimation_runs" in s, [(4242,)], 1),
    ]
    conn = _FakeConn(script)

    spec = {
        "lat": 50.0, "lng": 14.0, "area_m2": 60.0, "disposition": "2+kk",
        "floor": 3, "exclude_ids": [99],
        "category_main": "byt", "category_type": "prodej",
    }
    run_id = _insert_pending_run(
        conn,  # type: ignore[arg-type]
        listing_id=99, spec=spec, estimate_kind="sale",
    )
    assert run_id == 4242

    insert_sql, params = next(
        (sql, p) for sql, p in conn.executed if "INSERT INTO estimation_runs" in sql
    )
    # The two phantom columns must NOT appear in the INSERT column list.
    cols_clause = insert_sql.split("VALUES")[0]
    assert "category_main" not in cols_clause
    assert "category_type" not in cols_clause
    # And the category survives inside the input_spec jsonb param instead.
    spec_param = next(p for p in params if isinstance(p, str) and '"category_main"' in p)
    assert '"category_type": "prodej"' in spec_param


import api.notifications as nf


def test_kickoff_always_runs_a_rent_estimate_even_for_a_sale_listing(monkeypatch) -> None:
    """The watchdog 'Estimate rent' action runs a RENTAL estimate regardless of
    the subject listing's own category_type — a sale flat gets a "what would it
    rent for" figure. So input_spec must carry category_type='pronajem' and the
    run's estimate_kind must be 'rent', even when the listing is 'prodej'."""
    monkeypatch.setattr(
        nf, "_fetch_dispatch",
        lambda conn, did: {
            "sreality_id": 12345, "listing_id": 987, "estimation_run_id": None,
        },
    )
    monkeypatch.setattr(
        nf, "_resolve_listing_for_estimate",
        lambda conn, lid: {
            "listing_id": lid, "sreality_id": 12345,
            "lat": 50.08, "lng": 14.42, "area_m2": 62.0, "disposition": "2+kk",
            "floor": 3, "category_main": "byt", "category_type": "prodej",
            "price_czk": 4_500_000, "price_unit": "czk",
        },
    )
    monkeypatch.setattr(nf, "_link_dispatch_run", lambda conn, did, rid: None)

    captured: dict[str, Any] = {}

    def _fake_insert(conn, *, listing_id, spec, estimate_kind):
        captured["listing_id"] = listing_id
        captured["spec"] = spec
        captured["estimate_kind"] = estimate_kind
        return 777

    monkeypatch.setattr(nf, "_insert_pending_run", _fake_insert)

    _dispatch, run_id = nf.kickoff_estimation_for_dispatch(object(), "d-1")  # type: ignore[arg-type]

    assert run_id == 777
    assert captured["listing_id"] == 987
    assert captured["estimate_kind"] == "rent"
    # Forces a rental comparable cohort even though the subject is 'prodej'.
    assert captured["spec"]["category_type"] == "pronajem"
    assert captured["spec"]["category_main"] == "byt"
    # The subject is excluded from its own cohort on the surrogate arm (the only
    # one that can exclude a NULL-sreality listing).
    assert captured["spec"]["exclude_listing_ids"] == [987]


def test_kickoff_null_sreality_dispatch_resolves_on_the_surrogate(monkeypatch) -> None:
    """Post-Gate-2 a listing has sreality_id NULL. The kickoff must resolve it on
    listing_id, not `int(dispatch["sreality_id"])` (which raised TypeError -> 500),
    and must exclude the subject from its own cohort on the surrogate arm."""
    monkeypatch.setattr(
        nf, "_fetch_dispatch",
        lambda conn, did: {
            "sreality_id": None, "listing_id": 555, "estimation_run_id": None,
        },
    )
    monkeypatch.setattr(
        nf, "_resolve_listing_for_estimate",
        lambda conn, lid: {
            "listing_id": lid, "sreality_id": None,
            "lat": 49.2, "lng": 16.6, "area_m2": 70.0, "disposition": "3+kk",
            "floor": 2, "category_main": "byt", "category_type": "pronajem",
            "price_czk": 20_000, "price_unit": "czk",
        },
    )
    monkeypatch.setattr(nf, "_link_dispatch_run", lambda conn, did, rid: None)

    captured: dict[str, Any] = {}

    def _fake_insert(conn, *, listing_id, spec, estimate_kind):
        captured["listing_id"] = listing_id
        captured["spec"] = spec
        return 42

    monkeypatch.setattr(nf, "_insert_pending_run", _fake_insert)

    _dispatch, run_id = nf.kickoff_estimation_for_dispatch(object(), "d-2")  # type: ignore[arg-type]

    assert run_id == 42
    assert captured["listing_id"] == 555
    # The surrogate arm carries the exclusion; the legacy arm is empty (no
    # sreality_id to exclude), NOT [None] — a NULL there would empty the cohort.
    assert captured["spec"]["exclude_listing_ids"] == [555]
    assert captured["spec"]["exclude_ids"] == []


def test_kickoff_listing_less_dispatch_schedules_nothing(monkeypatch) -> None:
    """A system_health alert has no listing at all (sreality_id AND listing_id
    NULL — 35 such rows live today). It must not crash on int(None); it simply
    has nothing to estimate."""
    monkeypatch.setattr(
        nf, "_fetch_dispatch",
        lambda conn, did: {
            "sreality_id": None, "listing_id": None, "estimation_run_id": None,
        },
    )

    def _boom(*a, **k):  # resolving a listing must never be attempted
        raise AssertionError("must not resolve a listing for a listing-less dispatch")

    monkeypatch.setattr(nf, "_resolve_listing_for_estimate", _boom)

    dispatch, run_id = nf.kickoff_estimation_for_dispatch(object(), "d-3")  # type: ignore[arg-type]

    assert run_id is None
    assert dispatch["listing_id"] is None


def test_run_pending_never_excludes_a_null_into_the_cohort_filter(monkeypatch) -> None:
    """Regression for the empty-cohort trap: the old default
    `exclude_ids=[sreality_id]` put [None] into the filter for a NULL-sreality
    subject, and `l.sreality_id <> ALL(ARRAY[NULL])` is NULL for EVERY row, so
    the whole comparable cohort silently emptied. On rehydration each arm now
    falls back only to an id that exists.

    _update_run_terminal, estimate_yield and load_filter_defaults are imported
    lazily from their own modules inside run_pending_estimation (cycle-avoidance),
    so they are patched at the SOURCE, not on `nf`."""
    import api.estimate_yield as ey_mod
    import api.estimation_runs as er_mod

    captured: dict[str, Any] = {}

    def _fake_estimate(conn, target, filters, _client=None, **kw):
        captured["exclude_ids"] = list(target.exclude_ids)
        captured["exclude_listing_ids"] = list(target.exclude_listing_ids)
        return {"data": {}}

    monkeypatch.setattr(ey_mod, "estimate_yield", _fake_estimate)
    monkeypatch.setattr(er_mod, "_update_run_terminal", lambda *a, **k: None)

    class _Defaults:
        radius_m = 1000
        area_band_pct = 0.2
        disposition_match = "exact"
        lifecycle = "active"
        def max_age_days_for(self, _kind): return 30
    monkeypatch.setattr(er_mod, "load_filter_defaults", lambda conn: _Defaults())

    # input_sreality_id NULL, input_spec (no exclude_* keys), estimate_kind,
    # input_listing_id — the classic post-Gate-2 pending row.
    monkeypatch.setattr(nf.scraper_db, "connect", lambda: _FakeConn([
        (lambda s: "FROM estimation_runs WHERE id" in s,
         [(None, {"lat": 49.0, "lng": 16.0, "area_m2": 55.0,
                  "disposition": "2+kk", "category_main": "byt",
                  "category_type": "pronajem"}, "rent", 555)], 1),
        (lambda s: "UPDATE estimation_runs SET status = 'running'" in s, [], 1),
    ]))

    nf.run_pending_estimation(999)

    # The subject's NULL sreality_id must NOT reach exclude_ids; the surrogate
    # arm carries the self-exclusion instead.
    assert None not in captured.get("exclude_ids", [])
    assert captured.get("exclude_ids") == []
    assert captured.get("exclude_listing_ids") == [555]
