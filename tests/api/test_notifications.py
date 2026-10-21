"""Hermetic tests for the notifications backend (Phase U2.7).

Covers WatchdogFilterSpec validation, the SQL-clause generator and the
three producers over fake connections; the worker's notifications lane, their
one caller, is tested in tests/scraper/test_realtime_worker.py.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError

from api.notifications import (
    ON_EVENT_CONFLICT,
    WatchdogFilterSpec,
    _build_match_clauses,
)


def test_filter_spec_defaults() -> None:
    """category_main is multiselect now: a blank-save watchdog carries no
    category_main constraint (matches every category). Deal type still
    defaults to rent."""
    spec = WatchdogFilterSpec()
    assert spec.category_main_in is None
    assert spec.category_type == "pronajem"


def test_filter_spec_bounds_must_be_an_ordered_box() -> None:
    """`bounds` is the SPA's MapBounds: west < east and south < north, or a 422 —
    an inverted box would match nothing and mute the watchdog without a word."""
    with pytest.raises(ValidationError):
        WatchdogFilterSpec(bounds={"west": 14.5, "south": 50.0, "east": 14.3, "north": 50.2})
    with pytest.raises(ValidationError):
        WatchdogFilterSpec(bounds={"west": 14.3, "south": 50.2, "east": 14.5, "north": 50.0})
    with pytest.raises(ValidationError):
        WatchdogFilterSpec(bounds={"west": 14.3, "south": 50.0, "east": 14.5})
    s = WatchdogFilterSpec(bounds={"west": 14.3, "south": 50.0, "east": 14.5, "north": 50.2})
    assert s.bounds is not None
    assert (s.bounds.west, s.bounds.north) == (14.3, 50.2)


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


def test_build_clauses_skips_unset_box() -> None:
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert not any("l.lat" in w or "l.lng" in w for w in where)
    assert not any(k.startswith("bbox_") for k in params)


def test_build_clauses_emits_the_map_box_when_set() -> None:
    """Rule 16: the box is the lat/lng band Browse's list (`queries.ts applyFilters`)
    and both stats RPCs (`bbox_*`) apply — no circle, no ST_DWithin, no own shape."""
    spec = WatchdogFilterSpec(bounds={"west": 14.40, "south": 50.06, "east": 14.44, "north": 50.10})
    where, params = _build_match_clauses(spec)
    assert "l.lng >= %(bbox_west)s" in where
    assert "l.lng <= %(bbox_east)s" in where
    assert "l.lat >= %(bbox_south)s" in where
    assert "l.lat <= %(bbox_north)s" in where
    assert not any("ST_DWithin" in w for w in where)
    # Anchored: `sl.geom` is the W5 consumer rule's own probe into listing_location,
    # which is not `properties_public.geom` and must not be flagged as one.
    assert not any(re.search(r"(?<![a-z_])l\.geom", w) for w in where)
    assert params["bbox_west"] == 14.40
    assert params["bbox_east"] == 14.44
    assert params["bbox_south"] == 50.06
    assert params["bbox_north"] == 50.10


def test_build_clauses_city_quality_is_one_obec_predicate() -> None:
    """W5 (migration 436): the matcher renders ONE `obec_id = ANY(...)` predicate.

    Before W5 this branch built a nested EXISTS tree against `curated_cities_public`
    joined on the precomputed `home_city_id`, and the same rule was implemented three
    times over (here, in `browse_stats_properties`, and in the SPA prefilter RPC).
    Migration 374's own header records two divergences found between those copies,
    including an operator-chosen op silently re-interpreted as `>=`.

    Now one SQL function owns rule evaluation and every consumer reduces to the same
    form — a form with nothing left to diverge on (rule 16).

    `ARRAY(SELECT ...)` and not `IN (SELECT ...)`: the former forces a
    once-per-statement InitPlan, the latter can degrade into the per-row correlated
    SubPlan that made this predicate cost 1,778,259 blocks.
    """
    spec = WatchdogFilterSpec(
        city_index_rules=[{"index_name": "safety", "value": 60, "op": ">="}],
    )
    where, params = _build_match_clauses(spec)
    city = [w for w in where if "curated_cities_matching" in w]
    assert len(city) == 1, f"expected exactly one city-quality predicate, got {city}"
    clause = city[0]
    assert "l.obec_id = ANY (ARRAY(SELECT curated_cities_matching(" in clause
    # The whole point: no nested EXISTS tree, no per-rule subquery, no interpolated op.
    assert "EXISTS" not in clause
    assert "curated_cities_public" not in clause
    assert "home_city_id" not in clause
    # The rules travel as ONE bound jsonb parameter, nothing string-interpolated.
    assert params["city_index_rules"].obj == [
        {"index_name": "safety", "value": 60, "op": ">="}
    ]


def test_build_clauses_near_city_proximity_is_retired_loudly() -> None:
    """A retired filter that silently returns everything is worse than one that errors.

    `near_city_proximity` had zero UI widgets, 0 of 7 filter_presets and 0 of 2
    notification_subscriptions, and was never load-tested. Dropping the branch without
    a raise would WIDEN every cohort that still carried the value.
    """
    spec = WatchdogFilterSpec(near_city_proximity={"radius_km": 10, "index_rules": []})
    with pytest.raises(ValueError, match="near_city_proximity is retired"):
        _build_match_clauses(spec)


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


def test_spec_model_dump_keeps_building_material_and_garden() -> None:
    """create/update_subscription persist `model_dump()`. Merge f2d7b359 dropped these
    fields from the model, so extra='ignore' discarded them on every save and the
    watchdog matched wider than the Browse view it was made from."""
    dumped = WatchdogFilterSpec(min_garden_area=100, building_material=["cihla"]).model_dump()
    assert dumped["min_garden_area"] == 100
    assert dumped["building_material"] == ["cihla"]


def test_build_clauses_building_material_ostatni_expands_to_the_rest_of_the_canon() -> None:
    from toolkit.filter_registry import COLUMN_CANONICAL_VALUES

    where, params = _build_match_clauses(WatchdogFilterSpec(building_material=["ostatni"]))
    assert "l.building_type = ANY(%(building_material)s)" in where
    assert params["building_material"] == [
        v for v in COLUMN_CANONICAL_VALUES["building_type"]
        if v not in {"cihla", "panel", "smisena"}
    ]
    # A blob saved before merge f2d7b359 holds one scalar bucket.
    assert WatchdogFilterSpec(building_material="cihla").building_material == ["cihla"]
    where, params = _build_match_clauses(WatchdogFilterSpec(building_material=[]))
    assert not any("building_type" in w for w in where)


def test_build_clauses_garden_area_bounds() -> None:
    spec = WatchdogFilterSpec(min_garden_area=100.0, max_garden_area=800.0)
    where, params = _build_match_clauses(spec)
    assert "l.garden_area >= %(min_garden_area)s" in where
    assert "l.garden_area <= %(max_garden_area)s" in where
    assert params["min_garden_area"] == 100.0
    assert params["max_garden_area"] == 800.0


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


def test_build_clauses_place_chips_are_one_code_predicate() -> None:
    """Rule 16, in its W3 form: the matcher tests a chip exactly the way Browse,
    Stats and the map do — `<level>_id = any(codes)`, one array per level.

    RED by: reintroducing a name ILIKE, a `place_search_text` arm or a `context`
    narrow here. The six copies of the old five-predicate tree are what let this
    matcher and Browse answer the same saved filter differently."""
    spec = WatchdogFilterSpec(
        districts=[
            {"name": "Jihlava", "level": "obec", "id": 586846},
            {"name": "okres Jihlava", "level": "okres", "id": 3707},
            {"name": "Kraj Vysočina", "level": "kraj", "id": 108},
            {"name": "Žižkov", "level": "cast_obce", "id": 490067},
            {"name": "Na Rybníčkách", "context": "Pardubice", "level": "ulice", "id": 666742},
        ],
    )
    where, params = _build_match_clauses(spec)
    clause = next(w for w in where if "district_codes" in w)
    assert clause == (
        "(l.region_id = ANY(%(district_codes_kraj)s) "
        "OR l.okres_id = ANY(%(district_codes_okres)s) "
        "OR l.obec_id = ANY(%(district_codes_obec)s) "
        "OR l.cast_obce_id = ANY(%(district_codes_cast_obce)s) "
        "OR l.ulice_id = ANY(%(district_codes_ulice)s))"
    )
    assert params["district_codes_obec"] == [586846]
    assert params["district_codes_okres"] == [3707]
    assert params["district_codes_kraj"] == [108]
    assert params["district_codes_cast_obce"] == [490067]
    assert params["district_codes_ulice"] == [666742]


def test_build_clauses_place_chips_read_no_text_column() -> None:
    spec = WatchdogFilterSpec(
        districts=[
            {"name": "Na Rybníčkách", "context": "Pardubice", "level": "ulice", "id": 666742},
            {"name": "Brno", "context": None, "level": "obec", "id": 582786},
        ],
    )
    where, _params = _build_match_clauses(spec)
    for clause in [w for w in where if "district_codes" in w]:
        assert "ILIKE" not in clause.upper()
        assert "place_search_text" not in clause
        assert "l.district" not in clause
        assert "'%'" not in clause


def test_build_clauses_a_street_chip_filters_at_its_street_never_its_town() -> None:
    spec = WatchdogFilterSpec(
        districts=[{"name": "Na Rybníčkách", "context": "Pardubice", "level": "ulice", "id": 666742}],
    )
    where, params = _build_match_clauses(spec)
    clause = next(w for w in where if "district_codes" in w)
    assert clause == "(l.ulice_id = ANY(%(district_codes_ulice)s))"
    assert params["district_codes_ulice"] == [666742]
    assert "district_codes_obec" not in params


def test_build_clauses_excluded_chip_is_negated() -> None:
    spec = WatchdogFilterSpec(
        districts=[{"name": "Praha", "level": "obec", "id": 554782, "excluded": True}],
    )
    where, params = _build_match_clauses(spec)
    clauses = [w for w in where if "district_codes" in w]
    # COALESCE: an exclude subtracts only what it MATCHES, so a listing with no
    # obec code survives it (the same answer the RPC and the board already give).
    assert clauses == [
        "NOT COALESCE(l.obec_id = ANY(%(district_codes_excl_obec)s), false)"
    ]
    assert params["district_codes_excl_obec"] == [554782]


def test_build_clauses_mixed_include_exclude() -> None:
    spec = WatchdogFilterSpec(
        districts=[
            {"name": "Praha", "level": "obec", "id": 554782},
            {"name": "Modřany", "level": "cast_obce", "id": 490017, "excluded": True},
        ],
    )
    where, params = _build_match_clauses(spec)
    inc = next(w for w in where if "district_codes_obec" in w)
    exc = next(w for w in where if "district_codes_excl_cast_obce" in w)
    assert not inc.startswith("NOT ")
    assert exc.startswith("NOT COALESCE(")
    assert params["district_codes_obec"] == [554782]
    assert params["district_codes_excl_cast_obce"] == [490017]


@pytest.mark.parametrize("chips", [
    [{"name": "Brno", "context": None}],
    ["Praha"],
    [{"name": "Lipová", "context": "Praha", "level": "locality", "id": 554782}],
    [{"name": "Praha", "level": "obec", "id": -1}],
    [{"name": "Praha", "level": "obec", "id": 554782}, {"name": "Jihlava"}],
], ids=["name only", "legacy string", "stale locality", "the old sentinel", "one bad of two"])
def test_a_spec_with_a_chip_without_a_code_is_refused(chips) -> None:
    """A chip is its (level, code) or nothing: a watchdog that silently matched
    nothing (the old sentinel) or the whole country (a widened guess) is no
    longer expressible, so it never reaches the matcher."""
    with pytest.raises(ValidationError):
        WatchdogFilterSpec(districts=chips)


@pytest.mark.parametrize("method,path", [
    ("post", "/notifications/subscriptions"),
    ("put", "/notifications/subscriptions/46a6005f-dc32-4d94-9ced-4d262b57ef6b"),
])
def test_the_routes_answer_a_chip_without_a_code_with_a_422(method, path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from api import main as api_main
    from api import notifications as nf
    from api import tenant_pool

    def never(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("a refused spec must not be written")

    monkeypatch.setattr(nf, "create_subscription", never)
    monkeypatch.setattr(nf, "update_subscription", never)
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: object()
    api_main.app.dependency_overrides[tenant_pool.require_account_id] = (
        lambda: UUID("11111111-1111-1111-1111-111111111111")
    )
    try:
        body = {"name": "x", "filter_spec": {"districts": [{"name": "Brno", "context": None}]}}
        res = getattr(TestClient(api_main.app), method)(path, json=body)
    finally:
        api_main.app.dependency_overrides.clear()
    assert res.status_code == 422, res.text
    assert "districts" in res.text


@pytest.mark.parametrize("method,path", [
    ("post", "/notifications/subscriptions"),
    ("put", "/notifications/subscriptions/46a6005f-dc32-4d94-9ced-4d262b57ef6b"),
])
def test_the_routes_answer_a_spec_the_compiler_refuses_with_a_422(method, path, monkeypatch) -> None:
    """A spec the model accepts but the matcher's compiler refuses (here a retired filter) is
    refused at write, one layer further in than the chip-without-code 422: stored, it would
    mute its watchdog every pass. A PUT that leaves the spec alone is not re-judged."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from api import main as api_main
    from api import notifications as nf
    from api import tenant_pool

    written: list[str] = []

    def never(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("a refused spec must not be written")

    monkeypatch.setattr(nf, "create_subscription", never)
    monkeypatch.setattr(nf, "update_subscription",
                        lambda *_a, **kw: written.append(kw["name"]) or {"id": "s"})
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: object()
    api_main.app.dependency_overrides[tenant_pool.require_account_id] = (
        lambda: UUID("11111111-1111-1111-1111-111111111111")
    )
    try:
        client = TestClient(api_main.app)
        body = {"name": "x", "filter_spec": {"near_city_proximity": {"radius_km": 10,
                                                                     "index_rules": []}}}
        if method == "put":
            monkeypatch.setattr(nf, "update_subscription", never)
        res = getattr(client, method)(path, json=body)
        assert res.status_code == 422, res.text
        assert res.json()["detail"].startswith("filter_spec: near_city_proximity is retired")
        if method == "put":
            monkeypatch.setattr(nf, "update_subscription",
                                lambda *_a, **kw: written.append(kw["name"]) or {"id": "s"})
            assert client.put(path, json={"name": "renamed"}).status_code == 200
            assert written == ["renamed"]
    finally:
        api_main.app.dependency_overrides.clear()


def test_the_write_check_is_the_matchers_own_compile() -> None:
    """One definition of what a spec may hold: the route's check is the matcher's compile, and
    what it raises is what the producers count as invalid."""
    from api import notifications as nf

    spec = WatchdogFilterSpec(near_city_proximity={"radius_km": 10, "index_rules": []})
    with pytest.raises(nf.SPEC_REJECTIONS):
        nf.check_filter_spec(spec)
    with pytest.raises(nf.SPEC_REJECTIONS):
        _build_match_clauses(spec)
    nf.check_filter_spec(WatchdogFilterSpec())


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
    assert "(l.condition = ANY(%(condition_match)s))" in where
    assert params["condition_match"] == ["novostavba", "po_rekonstrukci"]
    assert "condition_match_canon" not in params


def test_build_clauses_condition_unknown_sentinel() -> None:
    """The "Neuvedeno" chip: NULL or any value outside the canon, as on furnished."""
    from toolkit.filter_registry import COLUMN_CANONICAL_VALUES

    spec = WatchdogFilterSpec(condition_match=["novostavba", "__unknown__"])
    where, params = _build_match_clauses(spec)
    clauses = " ".join(where)
    assert ("l.condition = ANY(%(condition_match)s) OR "
            "(l.condition IS NULL OR NOT (l.condition = ANY(%(condition_match_canon)s)))") in clauses
    assert params["condition_match"] == ["novostavba"]
    assert params["condition_match_canon"] == list(COLUMN_CANONICAL_VALUES["condition"])


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


@pytest.mark.parametrize("fid", [
    "building_condition_level_min", "building_condition_level_max",
    "apartment_condition_level_min", "apartment_condition_level_max",
])
def test_build_clauses_parked_condition_levels_are_retired_loudly(fid: str) -> None:
    """Condition scoring is parked (rule 14, operator ruling 2026-10-20). A stored spec still
    deserialises, but a level bound raises (the matcher skips that subscription) rather than
    silently widening to every property; an unset one renders nothing."""
    with pytest.raises(ValueError, match=f"{fid} is retired"):
        _build_match_clauses(WatchdogFilterSpec(**{fid: 3}))
    where, params = _build_match_clauses(WatchdogFilterSpec())
    assert not any("condition_level" in w for w in where)
    assert not any("condition_level" in k for k in params)


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
        self.transactions = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def transaction(self) -> Any:
        # Each watchdog's INSERT is its own transaction (a savepoint inside the lane's step).
        import contextlib

        self.transactions += 1
        return contextlib.nullcontext()


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


def test_deleting_a_watchdog_retires_it_and_keeps_its_alerts() -> None:
    """F21: a delete is an UPDATE that stamps retired_at and switches it off — no DELETE, so the
    FK's cascade never takes the watchdog's alerts with it; a missing or already retired one
    changes nothing (the route's 404). List, get and update see live watchdogs only, and the
    producers read none that is retired."""
    from api import notifications as nf

    conn = _FakeConn([(lambda s: s.startswith("UPDATE notification_subscriptions"), [], 1)])
    assert nf.delete_subscription(conn, "s-1") is True  # type: ignore[arg-type]
    [(sql, params)] = conn.executed
    assert sql == ("UPDATE notification_subscriptions SET retired_at = now(), is_active = false "
                   "WHERE id = %s AND retired_at IS NULL") and params == ("s-1",)
    assert nf.delete_subscription(_FakeConn([]), "gone") is False  # type: ignore[arg-type]

    conn = _FakeConn([])
    nf.list_subscriptions(conn)  # type: ignore[arg-type]
    nf.list_subscriptions(conn, include_inactive=False)  # type: ignore[arg-type]
    nf.get_subscription(conn, "s-1")  # type: ignore[arg-type]
    nf.update_subscription(conn, "s-1", name="x")  # type: ignore[arg-type]
    listed, active, got, updated = (s for s, _ in conn.executed)
    assert listed.endswith("FROM notification_subscriptions WHERE retired_at IS NULL ORDER BY created_at DESC")
    assert "WHERE retired_at IS NULL AND is_active = true ORDER BY" in active
    assert got.endswith("FROM notification_subscriptions WHERE id = %s AND retired_at IS NULL")
    assert updated.endswith("WHERE id = %s AND retired_at IS NULL")
    assert nf._LIVE_WATCHDOGS_SQL.endswith("WHERE is_active = true AND retired_at IS NULL")


def test_the_delete_route_keeps_its_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from api import notifications as nf
    from api import tenant_pool
    from api.main import app

    retired: list[str] = []
    monkeypatch.setattr(nf, "delete_subscription", lambda _c, sid: retired.append(sid) or sid == "s-1")
    app.dependency_overrides[tenant_pool.tenant_conn] = lambda: object()
    try:
        client = TestClient(app)
        assert client.delete("/notifications/subscriptions/s-1").json() == {"deleted": True}
        assert client.delete("/notifications/subscriptions/s-2").status_code == 404
    finally:
        app.dependency_overrides.pop(tenant_pool.tenant_conn, None)
    assert retired == ["s-1", "s-2"]


def test_a_stored_spec_with_a_chip_without_a_code_mutes_only_its_watchdog() -> None:
    """Fail CLOSED: a stored row the model now refuses is counted invalid and
    dispatches nothing — never a cohort widened to the whole country — while the
    healthy watchdog in the same pass still matches. Migration 606 deleted the
    last such rows; this is the rail if one ever reappears."""
    dead = UUID("46a6005f-dc32-4d94-9ced-4d262b57ef6b")
    healthy = UUID("5b1e4c8e-0d1f-4a77-9c3e-2a6f1d0b9e10")
    since = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    conn = _FakeConn([
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [
                (dead, {"districts": [{"name": "Jihlava"}]}, since),
                (healthy, {"districts": [{"name": "Na Rybníčkách", "context": "Pardubice",
                                          "level": "ulice", "id": 666742}]},
                 since),
            ],
            2,
        ),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 2),
    ])
    stats = match_once(conn)  # type: ignore[arg-type]
    assert stats == {"subscriptions_evaluated": 2, "subscriptions_invalid": 1,
                     "subscriptions_failed": 0, "matches_inserted": 2}
    inserts = [p for sql, p in conn.executed if "INSERT INTO notification_dispatches" in sql]
    assert len(inserts) == 1 and inserts[0]["subscription_id"] == str(healthy)
    assert inserts[0]["district_codes_ulice"] == [666742]


def test_match_once_reads_one_24h_window_from_active_since() -> None:
    """F1: ONE INSERT per live watchdog over `first_seen_at > greatest(now() − 24 h,
    active_since)` — a late-matching property under a day old still fires, a watchdog
    switched on alerts only on what appears afterwards (R6 (a)), and nothing advances:
    no cursor, no window upper bound, no lookback re-scan."""
    sub_id = UUID("46a6005f-dc32-4d94-9ced-4d262b57ef6b")
    since = datetime(2026, 10, 21, 9, 0, 0, tzinfo=timezone.utc)
    conn = _FakeConn([
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {"category_main": "byt", "category_type": "pronajem",
                       "districts": [{"name": "Praha", "level": "obec", "id": 554782}]},
              since)],
            1,
        ),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 3),
    ])

    stats = match_once(conn)  # type: ignore[arg-type]

    assert stats == {"subscriptions_evaluated": 1, "subscriptions_invalid": 0,
                     "subscriptions_failed": 0, "matches_inserted": 3}
    subs_sql = next(s for s, _ in conn.executed if "FROM notification_subscriptions" in s)
    assert subs_sql == ("SELECT id, filter_spec, active_since FROM notification_subscriptions "
                        "WHERE is_active = true AND retired_at IS NULL")
    [(sql, params)] = [(s, p) for s, p in conn.executed
                       if "INSERT INTO notification_dispatches" in s]
    assert ("l.first_seen_at > greatest(now() - %(new_window)s::interval, %(active_since)s)"
            in sql)
    assert params["new_window"] == "24 hours" and params["active_since"] == since
    assert params["subscription_id"] == str(sub_id)
    # The spec made it through the one compiler (rule 16).
    assert params["category_main_in"] == ["byt"]
    assert params["category_type"] == "pronajem"
    assert params["district_codes_obec"] == [554782]
    assert "l.obec_id = ANY(%(district_codes_obec)s)" in sql
    assert "'watchdog'" in sql and sql.endswith(ON_EVENT_CONFLICT)
    # Committed on its own under the bound: the INSERT's locks never outlive it.
    at = [s for s, _ in conn.executed].index(sql)
    assert conn.executed[at - 2] == ("SET LOCAL statement_timeout = '120s'", None)
    # The notification-events lock, SHARED, as its own statement just before the INSERT: the INSERT's
    # snapshot then comes after any merge's commit (toolkit.property_carriers states the protocol).
    from toolkit.property_carriers import NOTIFICATION_EVENTS_SHARED_LOCK_SQL
    assert conn.executed[at - 1] == (NOTIFICATION_EVENTS_SHARED_LOCK_SQL, None)
    assert conn.transactions == 1, "one transaction per watchdog"
    executed = " ".join(s for s, _ in conn.executed)
    for gone in ("last_matched_first_seen_at", "max(first_seen_at)", "UPDATE notification_subscriptions",
                 "image_lookback_minutes", "window_size"):
        assert gone not in executed, gone


def test_a_quiet_pass_writes_nothing_and_never_touches_the_watchdog() -> None:
    """No matching property is the steady state: one INSERT that writes 0 rows, and the
    subscription row is never written (there is no cursor to move)."""
    conn = _FakeConn([
        (lambda s: "FROM app_settings" in s, [(False,)], 0),
        (lambda s: "FROM notification_subscriptions WHERE is_active" in s,
         [(UUID("00000000-0000-0000-0000-000000000001"), {},
           datetime(2030, 1, 1, tzinfo=timezone.utc))], 1),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 0),
    ])
    stats = match_once(conn)  # type: ignore[arg-type]
    assert stats["matches_inserted"] == 0
    assert not any("UPDATE notification_subscriptions" in s for s, _ in conn.executed)


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
    """One set-based INSERT per detector across all monitored collections — inactive,
    reactivated and new_source in step monitor, price_drop / price_rise (one CASE) in
    step price; every dispatch is source_kind='collection_monitor', idempotent on the one
    identity. broker_change is reserved, not emitted."""
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM collections WHERE monitoring_enabled = true" in s, [(2,)], 0),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 2),
    ]
    conn = _FakeConn(script)
    stats = match_monitored_collections_once(conn)  # type: ignore[arg-type]
    assert stats == {"monitored_collections": 2, "events_inserted": 6}
    assert conn.transactions == 3, "each detector commits before the next reads it"
    match_changes_once(conn)  # type: ignore[arg-type]

    inserts = [
        sql for sql, _ in conn.executed
        if "INSERT INTO notification_dispatches" in sql
    ]
    assert len(inserts) == 4
    joined = " ".join(inserts)
    assert "'collection_monitor'" in joined
    for kind in ("'price_drop'", "'price_rise'", "'inactive'", "'reactivated'", "'new_source'"):
        assert kind in joined
    assert "'cm:'" not in joined                   # no key grammar since W3
    assert joined.count(ON_EVENT_CONFLICT) == 4
    assert "'broker_change'" not in joined          # reserved, no clean signal yet


def test_new_source_names_one_introducing_ad_on_every_pass() -> None:
    """`new_source`'s event_ref is the introducing ad's id, so the ad the window picks must not
    change between passes or the identity would, and the same portal arrival would be told twice.
    The legacy handle breaks a first-seen tie as the pre-W3 key did; the surrogate breaks the
    rest (a non-sreality ad has no handle)."""
    from api import notifications as nf

    new_source = " ".join(nf._MONITOR_NEW_SOURCE_SQL.split())
    assert ("row_number() OVER (PARTITION BY m.collection_id, l.property_id, l.source "
            "ORDER BY l.first_seen_at, l.sreality_id, l.id) AS rn") in new_source
    assert "src.listing_id::text" in new_source


def test_collection_monitor_gates_every_detector_on_monitor_since() -> None:
    """Each detector fires only for changes AFTER monitoring began for the
    (collection, property) pair. The shared `monitored` CTE computes the anchor
    `monitor_since = greatest(added_at, monitoring_enabled_at)`, and price /
    inactive / new_source gate their change-time on it (reactivated is gated
    transitively via the inactive dispatch). This is the false-positive the fix
    closes: a price drop that predates membership must not notify. 'inactive' also
    waits out the delist dwell (F14): a blink sends neither Delisted nor Relisted."""
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM collections WHERE monitoring_enabled = true" in s, [(1,)], 0),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 0),
    ]
    conn = _FakeConn(script)
    match_monitored_collections_once(conn)  # type: ignore[arg-type]
    match_changes_once(conn)  # type: ignore[arg-type]

    inserts = [
        (sql, params) for sql, params in conn.executed
        if "INSERT INTO notification_dispatches" in sql
    ]
    assert len(inserts) == 4
    anchor = (
        "greatest(cp.added_at, coalesce(c.monitoring_enabled_at, cp.added_at)) "
        "AS monitor_since"
    )
    for sql, _ in inserts:
        assert anchor in sql  # every detector shares the anchored CTE

    price_sql = next(s for s, _ in inserts if "'price_drop'" in s)
    assert "st.scraped_at > m.monitor_since" in price_sql

    # The inactive insert references 'inactive' but not 'reactivated'.
    inactive_sql, params = next(
        (s, p) for s, p in inserts if "'inactive'" in s and "'reactivated'" not in s
    )
    assert "max(l.inactive_at) > m.monitor_since" in inactive_sql
    assert "max(l.inactive_at) < now() - %(inactive_dwell)s::interval" in inactive_sql
    assert "max(l.inactive_at) > now() - %(monitor_window)s::interval" in inactive_sql
    assert params == {"monitor_window": "7 days", "inactive_dwell": "24 hours"}

    new_source_sql = next(s for s, _ in inserts if "'new_source'" in s)
    assert "src.first_seen_at > src.monitor_since" in new_source_sql
    # new_source must NOT use count(DISTINCT ...) OVER (...): Postgres rejects it
    # (0A000), which threw on every producer run pre-fix. The distinct-source
    # count is a filtered window count over the rn=1 rows instead.
    assert "count(DISTINCT" not in new_source_sql
    assert "count(*) FILTER (WHERE rn = 1)" in new_source_sql

    # reactivated is hardened: the prior 'inactive' dispatch it keys on must
    # itself postdate monitor_since (no leak off a pre-fix ungated dispatch).
    reactivated_sql = next(s for s, _ in inserts if "'reactivated'" in s)
    assert "nd.dispatched_at > m.monitor_since" in reactivated_sql


# notification_dispatches' columns after migration 656 (its $post$ pins the same 15).
_DISPATCH_COLUMNS = frozenset({
    "id", "subscription_id", "collection_id", "source_kind", "property_id", "listing_id", "change_kind",
    "trigger_price_czk", "prev_price_czk", "dispatched_at", "seen_at", "account_id", "event_ref",
    "actor_account_id", "duplicate_of"})


def test_every_producer_writes_only_the_columns_migration_656_keeps() -> None:
    """Producers write facts (notifications program, north star 1), never routing, a key, text or
    the legacy id: every INSERT, the share grant's included, names only the 15 columns migration 656
    keeps (it dropped channel, status, error_message, target_channels, dedupe_key,
    trigger_snapshot_id, sreality_id, estimation_run_id and message)."""
    from api import shares
    from api.notifications import match_changes_once

    sub_row = (UUID("46a6005f-dc32-4d94-9ced-4d262b57ef6b"), {},
               datetime(2026, 5, 14, tzinfo=timezone.utc))
    conn = _FakeConn([
        (lambda s: "FROM app_settings" in s, [], 0),
        (lambda s: "FROM notification_subscriptions WHERE is_active" in s, [sub_row], 1),
        (lambda s: "FROM collections WHERE monitoring_enabled = true" in s, [(1,)], 0),
    ])
    match_once(conn)  # type: ignore[arg-type]
    match_changes_once(conn)  # type: ignore[arg-type]
    match_monitored_collections_once(conn)  # type: ignore[arg-type]
    statements = [s for s, _ in conn.executed if "INSERT INTO notification_dispatches" in s]
    statements.append(" ".join(shares._INSERT_SHARE_DISPATCH_SQL.split()))
    columns = [re.search(r"INSERT INTO notification_dispatches \(([^)]*)\)", s) for s in statements]
    assert len(columns) == 7 and all(columns), statements
    for match in columns:
        named = {c.strip() for c in match.group(1).split(",")}
        assert named <= _DISPATCH_COLUMNS, named - _DISPATCH_COLUMNS
    from tests.migration_defs import MIGRATIONS
    post = " ".join((MIGRATIONS / "656_notifications_w6_prune.sql").read_text().split())
    kept = re.search(r"v_cols is distinct from array\[([^\]]*)\]", post)
    assert kept and set(re.findall(r"'(\w+)'", kept.group(1))) == _DISPATCH_COLUMNS


def test_the_unread_count_is_one_number() -> None:
    """Bell and badge count one visible set (program § 3.6): no per-source breakdown, no scope."""
    conn = _FakeConn([(lambda s: s.startswith("SELECT count(*) FROM notification_dispatches d"),
                       [(7,)], 0)])
    assert get_unread_count(conn) == {"unread_count": 7}  # type: ignore[arg-type]
    [(sql, params)] = conn.executed
    assert "GROUP BY" not in sql and "source_kind" not in sql and params is None


def test_dispatch_select_labels_a_share_from_its_actor_and_live_name_never_stored_text() -> None:
    """A share stores no text (F17, R11; migration 656 dropped `message`): the feed hands the SPA the
    sharer's email through share_party_emails(), spelled as api/shares.party_label spells it (NULL
    once no share joins the two accounts), the shared thing's live name and a pipeline's mode."""
    from api import shares
    from api.notifications import _DISPATCH_FROM, _DISPATCH_SELECT

    sql = " ".join(_DISPATCH_SELECT.split())
    assert "message" not in sql and "message" not in _DISPATCH_FROM
    assert (f"CASE WHEN d.actor_account_id IS NOT NULL THEN {shares.party_label('d.actor_account_id')} "
            "END AS actor_email") in sql
    assert ("CASE d.change_kind WHEN 'shared_collection' THEN c.name WHEN 'shared_dashboard' THEN "
            "(SELECT sd.name FROM stats_dashboards sd WHERE sd.id::text = d.event_ref) END AS share_name") in sql
    assert "CASE WHEN d.change_kind = 'shared_pipeline' THEN d.event_ref END AS share_mode" in sql


def test_dispatch_select_names_what_a_share_opens() -> None:
    """A share's bell row opens its collection, the owner's pipeline or the dashboard: the kind is
    its change_kind's suffix (`shared_<collection|pipeline|dashboard>`, migration 652) and its id
    is the row's own column, the one api/shares.py writes it to (F24: no key is parsed): the
    collection id, the owner (the actor), the dashboard id (the event_ref). Every other producer
    reads NULL for both. No row is left labelled 'shared' to fall back for: migration 653
    relabels any 652's re-run missed before it builds the identity."""
    import inspect

    from api import notifications as nf

    assert ("CASE WHEN d.source_kind = 'share' THEN split_part(d.change_kind, '_', 2) END AS share_kind, "
            "CASE d.change_kind WHEN 'shared_collection' THEN d.collection_id::text "
            "  WHEN 'shared_pipeline' THEN d.actor_account_id::text "
            "  WHEN 'shared_dashboard' THEN d.event_ref END AS share_ref") in nf._DISPATCH_SELECT
    assert "dedupe_key" not in nf._DISPATCH_SELECT + nf._DISPATCH_FROM
    assert "split_part(d.dedupe_key" not in inspect.getsource(nf)


def test_dispatch_feed_reads_listings_through_the_public_view() -> None:
    """Every listing column on the feed comes from `listings_public`, never from
    `listings`.

    This feed runs on the tenant pool — `SET LOCAL ROLE authenticated` — and
    `listings` has carried RLS with NO POLICY since migration 001, so the bare
    table is empty under that role and the whole projection arrived NULL. The
    owner-rights view returns the same rows with no WHERE of its own. It is also
    the only relation in reach that can publish `display_label`: migration 501
    revokes `listing_location` from `authenticated` outright, so a direct join
    would be a hard permission error rather than a silent blank.
    """
    from api.notifications import _DISPATCH_FROM, _LISTING_PROJECTION

    assert "LEFT JOIN listings_public lp ON lp.id = d.listing_id" in _DISPATCH_FROM
    assert "JOIN listings l " not in _DISPATCH_FROM
    # The place is the PROPERTY's location ad's (ruling R5), never the advert's own.
    assert "LEFT JOIN properties_public pp ON pp.property_id = d.property_id" in _DISPATCH_FROM
    assert "pp.display_label" in _LISTING_PROJECTION
    assert "lp.display_label" not in _LISTING_PROJECTION
    # No column may sneak back onto the RLS-dark table alias.
    assert " l." not in f" {_LISTING_PROJECTION}"


def test_feed_mf_yield_is_the_number_the_matcher_filtered_on() -> None:
    """The feed shows the MF yield of the dispatch's PROPERTY, read off the same
    `properties_public` row, by the same key, that the matcher's yield bound was
    evaluated against — so a row that fired on `yield >= 4` can never show a
    different yield next to it (the listing-grain column it replaced could)."""
    from api import notifications as nf

    where, _ = _build_match_clauses(WatchdogFilterSpec(min_mf_gross_yield_pct=4.0))
    assert "l.mf_gross_yield_pct >= %(min_mf_gross_yield_pct)s" in where
    insert = nf._WATCHDOG_NEW_SQL
    assert "FROM properties_public l " in insert
    assert "SELECT %(subscription_id)s, 'watchdog', l.property_id," in insert

    assert "LEFT JOIN properties_public pp ON pp.property_id = d.property_id" in nf._DISPATCH_FROM
    assert "pp.mf_gross_yield_pct" in nf._LISTING_PROJECTION
    assert "lp.mf_" not in nf._LISTING_PROJECTION


def test_mark_all_seen_scoped_filters_by_source() -> None:
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "UPDATE notification_dispatches d SET seen_at" in s, [], 5),
    ]
    conn = _FakeConn(script)
    assert mark_all_seen(conn) == 5  # type: ignore[arg-type]
    assert mark_all_seen(conn, source_kind="watchdog") == 5  # type: ignore[arg-type]
    scoped_sql = [
        s for s, _ in conn.executed if "UPDATE notification_dispatches" in s
    ][-1]
    assert "AND d.source_kind = %s" in scoped_sql


# --- VISIBLE: one fragment for every in-app read, count and update (program § 3.6) ---


def test_visible_is_one_fragment_whose_three_arms_hold_on_the_service_role() -> None:
    """Not a marked repeat, not dismissed by the event's OWN account, not from a retired watchdog.
    The account is named, never left to the caller's policy: W4's fan-out reads on the service
    role, which bypasses RLS, so `property_dismissals_public` (no account column) would there
    hide every account's alert once anyone dismissed the property."""
    from api.notifications import VISIBLE_SQL

    arms = [a.strip() for a in VISIBLE_SQL.split(" AND ", 1)]
    assert arms[0] == "d.duplicate_of IS NULL"
    assert ("NOT EXISTS (SELECT 1 FROM property_dismissals pd WHERE pd.property_id = d.property_id "
            "AND pd.account_id = d.account_id AND pd.lifted_at IS NULL)") in VISIBLE_SQL
    assert VISIBLE_SQL.endswith(
        "AND (d.subscription_id IS NULL OR EXISTS (SELECT 1 FROM notification_subscriptions ws "
        "WHERE ws.id = d.subscription_id AND ws.retired_at IS NULL))")
    assert "property_dismissals_public" not in VISIBLE_SQL


def test_every_in_app_read_count_and_update_carries_visible() -> None:
    """The badge, the feed, its total, mark-all-seen, mark-seen and each watchdog's dispatch
    count must agree on what is visible — a count that includes rows the list hides is the
    badge-vs-list divergence migrations 351/378 exist to close, and a bell that counts a merge's
    marked repeat or a retired watchdog's alert would count an alert the feed never shows."""
    from api.notifications import (
        _RESOLVE_DISPATCH_SQL, VISIBLE_SQL, get_subscription, list_dispatches, list_subscriptions,
        mark_dispatch_seen,
    )

    resolve = " ".join(_RESOLVE_DISPATCH_SQL.split())
    conn = _FakeConn([(lambda s: s == resolve, [("d-1",)], 1)])
    get_unread_count(conn)  # type: ignore[arg-type]
    mark_all_seen(conn)  # type: ignore[arg-type]
    mark_all_seen(conn, source_kind="watchdog")  # type: ignore[arg-type]
    list_dispatches(conn)  # type: ignore[arg-type]
    list_dispatches(conn, subscription_id="s-1", seen="unseen")  # type: ignore[arg-type]
    get_subscription(conn, "s-1")  # type: ignore[arg-type]
    list_subscriptions(conn)  # type: ignore[arg-type]
    mark_dispatch_seen(conn, "d-1")  # type: ignore[arg-type]
    # mark-seen's first statement only resolves the id it was handed (to the row a merge marked it
    # a duplicate of); the row it stamps and returns is VISIBLE-checked like every other.
    reads = [s for s, _ in conn.executed if "notification_dispatches" in s and s != resolve]
    assert len(reads) == 11
    for sql in reads:
        assert VISIBLE_SQL in sql, sql
    totals = [s for s in reads if s.startswith("SELECT count(*) FROM notification_dispatches d")]
    assert len(totals) == 3  # the badge, and the two feed reads' first-page totals
    updates = [s for s in reads if s.startswith("UPDATE notification_dispatches d SET seen_at")]
    assert len(updates) == 3  # mark-all-seen twice, mark-seen once


def test_a_hidden_dispatch_is_a_404_on_mark_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeat, a dismissed property's or a retired watchdog's dispatch is not visible, so
    marking it seen changes nothing and finds nothing: the route answers 404 (program § 3.5:
    the tap then lands on /notifications)."""
    from fastapi.testclient import TestClient

    from api import tenant_pool
    from api.main import app
    from api.notifications import VISIBLE_SQL, mark_dispatch_seen

    conn = _FakeConn([
        (lambda s: s.startswith("SELECT coalesce(d.duplicate_of, d.id)"), [("d-1",)], 1),
        (lambda s: s.startswith("UPDATE notification_dispatches d"), [], 0),
    ])
    assert mark_dispatch_seen(conn, "d-1") is None  # type: ignore[arg-type]
    _resolve, update, fetch = (s for s, _ in conn.executed)
    assert update == ("UPDATE notification_dispatches d SET seen_at = now() "
                      f"WHERE d.id = %s AND d.seen_at IS NULL AND {VISIBLE_SQL}")
    assert fetch.endswith(f"WHERE d.id = %s AND {VISIBLE_SQL}")

    app.dependency_overrides[tenant_pool.tenant_conn] = lambda: _FakeConn([])
    try:
        res = TestClient(app).post("/notifications/dispatches/d-1/mark-seen")
    finally:
        app.dependency_overrides.pop(tenant_pool.tenant_conn, None)
    assert res.status_code == 404 and res.json() == {"detail": "dispatch not found"}


def test_mark_seen_follows_a_duplicate_to_the_row_it_duplicates() -> None:
    """A push tapped for an alert a merge later marked a duplicate lands on the survivor's alert:
    the id resolves to coalesce(duplicate_of, id) first, and THAT row is stamped and returned; an
    id that resolves to nothing (another account's, under RLS) is the route's 404."""
    from api.notifications import _RESOLVE_DISPATCH_SQL, mark_dispatch_seen

    kept = "22222222-2222-2222-2222-222222222222"
    conn = _FakeConn([
        (lambda s: s.startswith("SELECT coalesce(d.duplicate_of, d.id)"), [(UUID(kept),)], 1),
        (lambda s: s.startswith("UPDATE notification_dispatches d"), [], 1),
    ])
    mark_dispatch_seen(conn, "11111111-1111-1111-1111-111111111111")  # type: ignore[arg-type]
    (resolve, asked), (_update, stamped), (_fetch, read) = conn.executed
    assert resolve == ("SELECT coalesce(d.duplicate_of, d.id) FROM notification_dispatches d "
                       "WHERE d.id = %s") == " ".join(_RESOLVE_DISPATCH_SQL.split())
    assert asked == ("11111111-1111-1111-1111-111111111111",)
    assert stamped == read == (kept,)

    nothing = _FakeConn([])
    assert mark_dispatch_seen(nothing, "foreign") is None  # type: ignore[arg-type]
    assert len(nothing.executed) == 1, "nothing is stamped for an id that resolves to nothing"


def test_reactivated_is_anchored_on_events_never_on_a_merges_duplicates() -> None:
    """A merge re-points the retired property's 'inactive' (and 'reactivated') rows and marks the
    ones the survivor already holds; such a duplicate must neither anchor a 'reactivated' (the
    LATERAL's latest 'inactive') nor stand in for one already told (the NOT EXISTS)."""
    from api import notifications as nf

    sql = " ".join(nf._MONITOR_REACTIVATED_SQL.split())
    lateral = sql.split("JOIN LATERAL (", 1)[1].split(") nd ON", 1)[0]
    assert "AND change_kind = 'inactive' AND duplicate_of IS NULL ORDER BY dispatched_at DESC LIMIT 1" in lateral
    told = sql.split("WHERE NOT EXISTS (", 1)[1].split(") ) INSERT", 1)[0]
    assert "AND r.change_kind = 'reactivated' AND r.duplicate_of IS NULL AND r.dispatched_at > nd.dispatched_at" in told


def test_detection_never_consults_dismissals() -> None:
    """Suppressing an INSERT would lose the event for good on undo: a 'new' identity is
    once-ever and the `reactivated` detector keys off a prior `inactive` dispatch, so no
    producer statement reads a dismissal."""
    import inspect

    from api import notifications as nf

    for fn in (nf.match_once, nf.match_changes_once, nf.match_monitored_collections_once):
        assert "dismiss" not in inspect.getsource(fn), fn.__name__
    for sql in (nf._MONITORED_CTE, nf._CANONICAL_PRICE_STEPS, nf._IMAGE_GATE_SQL,
                nf._WATCHDOG_NEW_SQL, nf._WATCHDOG_PRICE_SQL, nf._MONITOR_PRICE_SQL,
                nf._MONITOR_INACTIVE_SQL, nf._MONITOR_REACTIVATED_SQL,
                nf._MONITOR_NEW_SOURCE_SQL):
        assert "dismiss" not in sql


def test_match_once_skips_invalid_filter_spec() -> None:
    """A subscription whose filter_spec doesn't validate (e.g. corrupt
    legacy row) must NOT crash the whole matcher pass — log + skip."""
    good_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    bad_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [
                # Invalid: a map box with west >= east.
                (bad_id, {"bounds": {"west": 14.5, "south": 50.0, "east": 14.0, "north": 50.2}}, since),
                # Valid spec
                (good_id, {}, since),
            ],
            2,
        ),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 1),
    ]
    conn = _FakeConn(script)

    stats = match_once(conn)  # type: ignore[arg-type]

    # Both rows counted as "evaluated" — the broken one was inspected
    # and skipped as invalid, and the surviving one fired one dispatch.
    assert stats == {"subscriptions_evaluated": 2, "subscriptions_invalid": 1,
                     "subscriptions_failed": 0, "matches_inserted": 1}


def _raise_for_poison(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the compile raise for any spec carrying the poison price bound."""
    import api.notifications as nf

    real = nf._build_match_clauses

    def fake(spec: WatchdogFilterSpec) -> Any:
        if spec.min_price_czk == 666:
            raise ValueError("a stored spec the compile refuses")
        return real(spec)

    monkeypatch.setattr(nf, "_build_match_clauses", fake)


def test_a_spec_the_compiler_refuses_is_invalid_never_failed(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A spec the compile refuses (ValueError / LookupError) fails the same way every pass: it
    is counted invalid, which never reddens the lane's step, and the other subscription still
    matches."""
    _raise_for_poison(monkeypatch)
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [
                (UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"), {"min_price_czk": 666}, since),
                (UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"), {}, since),
            ],
            2,
        ),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 1),
    ]
    stats = match_once(_FakeConn(script))  # type: ignore[arg-type]
    assert stats == {"subscriptions_evaluated": 2, "subscriptions_invalid": 1,
                     "subscriptions_failed": 0, "matches_inserted": 1}


def test_a_watchdog_whose_statement_fails_rolls_back_alone() -> None:
    """Each watchdog's INSERT is its own transaction: a statement that raises (a timeout,
    a future spec field's bad SQL) is counted failed (the lane reddens its step) and the
    next watchdog still matches."""
    import psycopg

    bad, good = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"), UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)

    class _Raising(_FakeCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            super().execute(sql, params)
            if "INSERT INTO" in sql and params["subscription_id"] == str(bad):
                raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    conn = _FakeConn([
        (lambda s: "FROM app_settings" in s, [], 0),
        (lambda s: "FROM notification_subscriptions WHERE is_active" in s,
         [(bad, {}, since), (good, {}, since)], 2),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 4),
    ])
    conn.cursor = lambda: _Raising(conn)  # type: ignore[method-assign]
    stats = match_once(conn)  # type: ignore[arg-type]
    assert stats == {"subscriptions_evaluated": 2, "subscriptions_invalid": 0,
                     "subscriptions_failed": 1, "matches_inserted": 4}
    assert conn.transactions == 2


def test_match_changes_once_counts_a_refused_spec_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.notifications import match_changes_once

    _raise_for_poison(monkeypatch)
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [
                (UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"), {"min_price_czk": 666}, since),
                (UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"), {}, since),
            ],
            2,
        ),
        (lambda s: "INSERT INTO notification_dispatches" in s and "'watchdog'" in s, [], 1),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 2),
    ]
    stats = match_changes_once(_FakeConn(script))  # type: ignore[arg-type]
    assert stats == {"subscriptions_evaluated": 2, "subscriptions_invalid": 1,
                     "subscriptions_failed": 0, "changes_inserted": 3,
                     "collection_changes_inserted": 2}


# --- images-first publication gate (Wave C-4) ----------------------------


from api.notifications import (
    IMAGE_GATE_ZERO_ROWS_FLOOR_MINUTES,
    ImageGateSettings,
    _read_bool_setting,
)


def _gate_script(sub_id: UUID, since: datetime) -> list[tuple[Any, list[tuple[Any, ...]], int]]:
    return [
        # app_settings all default → gate ON (default true), timeout 30.
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {}, since)],
            1,
        ),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 1),
    ]


def test_match_once_image_gate_holds_new_properties_without_losing_them() -> None:
    """Default-on gate: the window INSERT requires a stored image (or a release arm).
    A held property is still inside the 24 h window on the next pass, so the first
    pass after its release dispatches it — the hold never loses it, and no separate
    lookback exists."""
    sub_id = UUID("00000000-0000-0000-0000-00000000000a")
    conn = _FakeConn(_gate_script(sub_id, datetime(2026, 7, 1, tzinfo=timezone.utc)))
    stats = match_once(conn)  # type: ignore[arg-type]

    [(sql, params)] = [(s, p) for s, p in conn.executed if "INSERT INTO notification_dispatches" in s]
    # The three release arms, set-based inside the INSERT's WHERE:
    assert "gi.storage_path IS NOT NULL" in sql                                   # (a)
    assert "make_interval(mins => %(image_timeout_minutes)s)" in sql              # (b)
    assert "make_interval(mins => %(image_zero_rows_floor_minutes)s)" in sql      # (c)
    assert "AND NOT EXISTS" in sql
    assert params["image_timeout_minutes"] == 30
    assert params["image_zero_rows_floor_minutes"] == IMAGE_GATE_ZERO_ROWS_FLOOR_MINUTES
    assert "image_lookback_minutes" not in sql
    assert stats["matches_inserted"] == 1


def test_match_once_gate_flag_off_restores_old_behavior() -> None:
    """`notifications_new_requires_image` = false → the one window INSERT carries no gate SQL."""
    sub_id = UUID("00000000-0000-0000-0000-00000000000c")
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        # Every app_settings read returns false-y → the gate flag reads False.
        (lambda s: "FROM app_settings" in s, [(False,)], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {}, datetime(2026, 7, 1, tzinfo=timezone.utc))],
            1,
        ),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 2),
    ]
    conn = _FakeConn(script)
    stats = match_once(conn)  # type: ignore[arg-type]
    [sql] = [s for s, _ in conn.executed if "INSERT INTO notification_dispatches" in s]
    assert "storage_path" not in sql and "image_timeout_minutes" not in sql
    assert stats["matches_inserted"] == 2


def test_the_image_gate_hold_always_ends_inside_the_window() -> None:
    """A hold past the 24 h window would let a held property leave it unreleased: the
    timeout is clamped below the window, with passes to spare for the release."""
    from api.notifications import (
        IMAGE_GATE_TIMEOUT_CEILING_MINUTES,
        NEW_WINDOW,
        _load_image_gate_settings,
    )

    def gate(rows: list[tuple[Any, ...]]) -> ImageGateSettings:
        conn = _FakeConn([(lambda s: "FROM app_settings" in s, rows, 0)])
        return _load_image_gate_settings(conn)  # type: ignore[arg-type]

    assert NEW_WINDOW == "24 hours"
    assert IMAGE_GATE_TIMEOUT_CEILING_MINUTES * 60 + 2 * 300 < 24 * 3600
    assert gate([]) == ImageGateSettings(enabled=True, timeout_minutes=30)
    assert gate([(5000,)]).timeout_minutes == IMAGE_GATE_TIMEOUT_CEILING_MINUTES
    assert gate([(0,)]).timeout_minutes == 1


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
    """Step price: ONE canonical-ad step fragment feeds a `price_drop` per (matching
    watchdog x drop step after its active_since), keyed per snapshot, and then the
    collection monitor's price_drop / price_rise over the same fragment."""
    from api import notifications as nf

    sub_id = UUID("12121212-1212-1212-1212-121212121212")
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {"category_main": "byt", "category_type": "pronajem"}, since)],
            1,
        ),
        (lambda s: "INSERT INTO notification_dispatches" in s and "'watchdog'" in s, [], 2),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 1),
    ]
    conn = _FakeConn(script)

    stats = match_changes_once(conn)  # type: ignore[arg-type]

    assert stats == {"subscriptions_evaluated": 1, "subscriptions_invalid": 0,
                     "subscriptions_failed": 0, "changes_inserted": 3,
                     "collection_changes_inserted": 1}
    assert conn.transactions == 2, "the watchdog's INSERT and the collections' each commit"
    watchdog, collection = [(s, p) for s, p in conn.executed
                            if "INSERT INTO notification_dispatches" in s]
    fragment = " ".join(nf._CANONICAL_PRICE_STEPS.split())
    for sql, params in (watchdog, collection):
        assert sql.startswith(f"WITH {fragment}")
        assert params["price_window"] == "7 days"
        assert sql.endswith(ON_EVENT_CONFLICT)
    sql, params = watchdog
    assert "'price_drop'" in sql and "'price_rise'" not in sql
    assert "st.price_czk < st.prev_price_czk AND st.scraped_at > %(active_since)s" in sql
    assert params["active_since"] == since and params["category_type"] == "pronajem"
    assert "JOIN properties_public l ON l.property_id = st.property_id" in sql
    sql, params = collection
    assert "JOIN canonical_steps st ON st.property_id = m.property_id" in sql
    assert "WHERE st.scraped_at > m.monitor_since" in sql


def test_match_changes_once_with_no_live_watchdog_runs_only_the_collection_half() -> None:
    conn = _FakeConn([(lambda s: "INSERT INTO notification_dispatches" in s, [], 0)])
    stats = match_changes_once(conn)  # type: ignore[arg-type]
    assert stats == {"subscriptions_evaluated": 0, "subscriptions_invalid": 0,
                     "subscriptions_failed": 0, "changes_inserted": 0,
                     "collection_changes_inserted": 0}
    inserts = [s for s, _ in conn.executed if "INSERT INTO notification_dispatches" in s]
    assert len(inserts) == 1 and "'collection_monitor'" in inserts[0]




# --- one event identity, continuous with history (notifications W3) ----------------------
#
# From W3 the identity index (EVENT_IDENTITY, migration 653) is the one "same event?", and
# nothing writes dedupe_key. A row written before W3 carries the identity migration 652
# backfilled from its key; a producer re-running over the same event must render the SAME
# identity, or the event would be told twice. So each producer statement's columns are
# RENDERED against fixture rows by the small evaluator below and compared with what 652 reads
# off the pre-W3 key grammar, spelled out in Python.

import math as _math

_TOKEN = re.compile(r"\s*('(?:[^']|'')*'|%\(\w+\)s|\|\||::|[(),<]|[A-Za-z_][\w.]*)")


def _tokens(sql: str) -> list[str]:
    out, i = [], 0
    while i < len(sql):
        m = _TOKEN.match(sql, i)
        if not m:
            if sql[i:].strip() == "":
                break
            raise AssertionError(f"cannot tokenize at {sql[i:i + 40]!r}")
        out.append(m.group(1))
        i = m.end()
    return out


class _Expr:
    """Evaluates the producers' SELECT-list subset: literals, params, column refs, `||`, `::text`
    / `::bigint`, floor(), extract(epoch FROM …), coalesce(a, b), CASE WHEN a < b THEN … ELSE … END."""

    def __init__(self, toks: list[str], row: dict[str, Any], params: dict[str, Any]) -> None:
        self.toks, self.i, self.row, self.params = toks, 0, row, params

    def peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, want: str | None = None) -> str:
        tok = self.toks[self.i]
        assert want is None or tok.upper() == want.upper(), (want, tok)
        self.i += 1
        return tok

    def concat(self) -> Any:
        parts = [self.cast()]
        while self.peek() == "||":
            self.take()
            parts.append(self.cast())
        if len(parts) == 1:
            return parts[0]
        return None if any(p is None for p in parts) else "".join(str(p) for p in parts)

    def cast(self) -> Any:
        value = self.atom()
        while self.peek() == "::":
            self.take()
            kind = self.take().lower()
            if value is not None:
                value = {"text": str, "bigint": int}[kind](value)
        return value

    def atom(self) -> Any:
        tok = self.take()
        if tok.startswith("'"):
            return tok[1:-1].replace("''", "'")
        if tok.startswith("%("):
            return self.params[tok[2:-2]]
        upper = tok.upper()
        if upper == "NULL":
            return None
        if upper == "CASE":
            self.take("WHEN")
            left = self.cast()
            self.take("<")
            right = self.cast()
            self.take("THEN")
            yes = self.concat()
            self.take("ELSE")
            no = self.concat()
            self.take("END")
            return yes if left < right else no
        if self.peek() == "(":
            self.take("(")
            if upper == "EXTRACT":
                self.take("epoch")
                self.take("FROM")
                value = self.cast().timestamp()
            elif upper == "FLOOR":
                value = _math.floor(self.concat())
            elif upper == "COALESCE":
                first = self.concat()
                self.take(",")
                second = self.concat()
                value = first if first is not None else second
            else:
                raise AssertionError(f"unknown function {tok}")
            self.take(")")
            return value
        return self.row[tok.split(".")[-1]]


def _select_list(select: str) -> str:
    """The SELECT list: everything before the first FROM outside parentheses and literals."""
    depth, quoted = 0, False
    for i, ch in enumerate(select):
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch in "()":
            depth += 1 if ch == "(" else -1
        elif not quoted and depth == 0 and select.startswith(" FROM ", i):
            return select[:i]
    raise AssertionError("no FROM")


def _rendered(sql: str, row: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """column -> value the INSERT's SELECT list renders for one fixture row."""
    sql = " ".join(sql.split())
    head, select = sql.split("INSERT INTO notification_dispatches (", 1)[1].split(") SELECT ", 1)
    columns = [c.strip() for c in head.split(",")]
    expr = _Expr([*_tokens(_select_list(select)), "FROM"], row, params)
    values = []
    for n, _ in enumerate(columns):
        values.append(expr.concat())
        expr.take("," if n < len(columns) - 1 else "FROM")
    return dict(zip(columns, values))


_SUB = "46a6005f-dc32-4d94-9ced-4d262b57ef6b"
_GONE_AT = datetime(2026, 10, 19, 8, 30, 15, 999_000, tzinfo=timezone.utc)
_FLAGGED_AT = datetime(2026, 10, 20, 9, 0, 0, tzinfo=timezone.utc)


def _pre_w3(kind: str, r: dict[str, Any]) -> str:
    """The dedupe_key grammar every row written before W3 carries (migration 206, Sprint C)."""
    if kind == "new":
        return f"wd:{_SUB}:new:{r['property_id']}"
    if kind == "wd_price":
        return f"wd:{_SUB}:price_drop:{r['snapshot_id']}"
    if kind == "cm_price":
        step = "price_drop" if r["price_czk"] < r["prev_price_czk"] else "price_rise"
        return f"cm:{r['collection_id']}:{step}:{r['snapshot_id']}"
    if kind in ("inactive", "reactivated"):
        return (f"cm:{r['collection_id']}:{kind}:{r['property_id']}:"
                f"{int(_math.floor(r['inactive_at'].timestamp()))}")
    if kind == "new_source":
        handle = r["sreality_id"] if r["sreality_id"] is not None else f"l{r['listing_id']}"
        return f"cm:{r['collection_id']}:new_source:{handle}"
    raise AssertionError(kind)


_PRICE_ROW = {"property_id": 101, "listing_id": 9001, "price_czk": 4_900_000,
              "prev_price_czk": 5_000_000, "snapshot_id": 5001, "collection_id": 7}


@pytest.mark.parametrize(("statement", "kind", "row", "event_ref"), [
    ("_WATCHDOG_NEW_SQL", "new", {"property_id": 101, "listing_id": 9001, "price_czk": 4_900_000},
     None),
    ("_WATCHDOG_PRICE_SQL", "wd_price", _PRICE_ROW, "5001"),
    ("_MONITOR_PRICE_SQL", "cm_price", _PRICE_ROW, "5001"),
    ("_MONITOR_PRICE_SQL", "cm_price",
     {**_PRICE_ROW, "price_czk": 5_100_000, "snapshot_id": 5002}, "5002"),
    ("_MONITOR_INACTIVE_SQL", "inactive",
     {"collection_id": 7, "property_id": 101, "repr_listing_ref_id": 9001,
      "inactive_at": _GONE_AT}, str(int(_GONE_AT.timestamp()))),
    ("_MONITOR_REACTIVATED_SQL", "reactivated",
     {"collection_id": 7, "property_id": 101, "repr_listing_ref_id": 9001,
      "inactive_at": _FLAGGED_AT}, str(int(_FLAGGED_AT.timestamp()))),
    ("_MONITOR_NEW_SOURCE_SQL", "new_source",
     {"collection_id": 7, "property_id": 101, "listing_id": 402678, "sreality_id": 3141592},
     "402678"),
    ("_MONITOR_NEW_SOURCE_SQL", "new_source",
     {"collection_id": 7, "property_id": 101, "listing_id": 402678, "sreality_id": None},
     "402678"),
    # A legacy handle is signed (2026-10-21: the one live new_source row keys on -248427).
    ("_MONITOR_NEW_SOURCE_SQL", "new_source",
     {"collection_id": 7, "property_id": 101, "listing_id": 402678, "sreality_id": -248427},
     "402678"),
], ids=["new", "wd-price", "cm-drop", "cm-rise", "inactive", "reactivated", "new-source",
        "new-source-no-sreality", "new-source-legacy-handle"])
def _backfilled_652(kind: str, key: str, row: dict[str, Any]) -> str | None:
    """event_ref as migration 652 (and 653's re-run of it) fills it from a pre-W3 key."""
    seg = key.split(":")
    if kind in ("wd_price", "cm_price"):
        return seg[3]  # 652 reads trigger_snapshot_id, which every price key ends in
    if kind in ("inactive", "reactivated"):
        return seg[4]
    if kind == "new_source":
        # 'l<id>' is the listing id; a legacy handle maps once through listings.sreality_id.
        return seg[3][1:] if seg[3].startswith("l") else str(row["listing_id"])
    return None


@pytest.mark.parametrize(("statement", "kind", "row", "event_ref"), [
    ("_WATCHDOG_NEW_SQL", "new", {"property_id": 101, "listing_id": 9001, "price_czk": 4_900_000},
     None),
    ("_WATCHDOG_PRICE_SQL", "wd_price", _PRICE_ROW, "5001"),
    ("_MONITOR_PRICE_SQL", "cm_price", _PRICE_ROW, "5001"),
    ("_MONITOR_PRICE_SQL", "cm_price",
     {**_PRICE_ROW, "price_czk": 5_100_000, "snapshot_id": 5002}, "5002"),
    ("_MONITOR_INACTIVE_SQL", "inactive",
     {"collection_id": 7, "property_id": 101, "repr_listing_ref_id": 9001,
      "inactive_at": _GONE_AT}, str(int(_GONE_AT.timestamp()))),
    # event_ref = the epoch of the inactive dispatch it answers, as the key always was (it
    # supersedes the program's "the id of the inactive dispatch": production's grammar wins).
    ("_MONITOR_REACTIVATED_SQL", "reactivated",
     {"collection_id": 7, "property_id": 101, "repr_listing_ref_id": 9001,
      "inactive_at": _FLAGGED_AT}, str(int(_FLAGGED_AT.timestamp()))),
    ("_MONITOR_NEW_SOURCE_SQL", "new_source",
     {"collection_id": 7, "property_id": 101, "listing_id": 402678, "sreality_id": 3141592},
     "402678"),
    ("_MONITOR_NEW_SOURCE_SQL", "new_source",
     {"collection_id": 7, "property_id": 101, "listing_id": 402678, "sreality_id": None},
     "402678"),
    # A legacy handle is signed (2026-10-21: the one live new_source row keys on -248427).
    ("_MONITOR_NEW_SOURCE_SQL", "new_source",
     {"collection_id": 7, "property_id": 101, "listing_id": 402678, "sreality_id": -248427},
     "402678"),
], ids=["new", "wd-price", "cm-drop", "cm-rise", "inactive", "reactivated", "new-source",
        "new-source-no-sreality", "new-source-legacy-handle"])
def test_every_producer_renders_the_identity_its_pre_w3_row_carries(
    statement: str, kind: str, row: dict[str, Any], event_ref: str | None,
) -> None:
    from api import notifications as nf

    out = _rendered(getattr(nf, statement), row, {"subscription_id": _SUB})
    key = _pre_w3(kind, row)
    seg = key.split(":")
    assert out["event_ref"] == event_ref == _backfilled_652(kind, key, row)
    if seg[0] == "wd":
        assert (out["source_kind"], out["subscription_id"]) == ("watchdog", seg[1])
    else:
        assert (out["source_kind"], str(out["collection_id"])) == ("collection_monitor", seg[1])
    assert out["change_kind"] == seg[2]
    assert out["property_id"] == row["property_id"]
    if kind in ("new", "inactive", "reactivated"):
        assert str(out["property_id"]) == seg[3], "the key's property is the row's"
    # The rest of the identity is the trigger's (account_id) or NULL: a detector has no actor,
    # and a row has one source, so the other source column is NULL (source_ck).
    other = "collection_id" if seg[0] == "wd" else "subscription_id"
    assert {c for c in nf.EVENT_IDENTITY if c not in out} == {"account_id", "actor_account_id", other}


def _conflict_target(sql: str) -> tuple[str, ...]:
    m = re.search(r"ON CONFLICT \(([^)]*)\) WHERE duplicate_of IS NULL DO NOTHING$", sql)
    assert m, sql[-200:]
    return tuple(c.strip() for c in m.group(1).split(","))


def test_every_insert_conflicts_on_the_one_identity_and_writes_no_key() -> None:
    """Each producer statement and the share grant end in ON CONFLICT (EVENT_IDENTITY) WHERE
    duplicate_of IS NULL DO NOTHING, the partial unique index migration 653 builds, and none
    writes the retired key or the snapshot column (a price row's snapshot is its event_ref)."""
    from api import notifications as nf
    from api import shares

    statements = [nf._WATCHDOG_NEW_SQL, nf._WATCHDOG_PRICE_SQL, nf._MONITOR_PRICE_SQL,
                  nf._MONITOR_INACTIVE_SQL, nf._MONITOR_REACTIVATED_SQL,
                  nf._MONITOR_NEW_SOURCE_SQL, shares._INSERT_SHARE_DISPATCH_SQL]
    for sql in statements:
        sql = " ".join(sql.split())
        head = sql.split("INSERT INTO notification_dispatches (", 1)[1].split(")", 1)[0]
        columns = {c.strip() for c in head.split(",")}
        assert "event_ref" in columns, sql
        assert not columns & {"dedupe_key", "trigger_snapshot_id"}, sql
        assert "dedupe_key" not in sql and "trigger_snapshot_id" not in sql, sql
        assert _conflict_target(sql) == nf.EVENT_IDENTITY
        assert sql.count("ON CONFLICT") == 1
    assert "actor_account_id" in " ".join(shares._INSERT_SHARE_DISPATCH_SQL.split())


def test_event_identity_is_the_index_migration_653_builds() -> None:
    """EVENT_IDENTITY, in index order, is notification_dispatches_identity_uq: NULLS NOT DISTINCT
    (a NULL source, property or event_ref is one value) and partial on the unmarked rows."""
    from api import notifications as nf
    from tests.migration_defs import MIGRATIONS

    sql = " ".join((MIGRATIONS / "653_notifications_w3_event_identity.sql").read_text().split())
    m = re.search(r"create unique index concurrently if not exists notification_dispatches_identity_uq "
                  r"on public\.notification_dispatches \(([^)]*)\) nulls not distinct "
                  r"where duplicate_of is null;", sql)
    assert m, "653 no longer builds the identity index this test reads"
    assert tuple(c.strip() for c in m.group(1).split(",")) == nf.EVENT_IDENTITY
    # The repeat marking (a) partitions on the same eight columns, so it marks exactly what the
    # index would refuse.
    part = re.search(r"partition by ([^)]*?) order by dispatched_at, id\)", sql)
    assert part and tuple(c.strip() for c in part.group(1).split(",")) == nf.EVENT_IDENTITY
    # And the $post$ check names them in the same order.
    post = re.search(r"v_cols is distinct from array\[([^\]]*)\]", sql)
    assert post and tuple(c.strip(" '") for c in post.group(1).split(",")) == nf.EVENT_IDENTITY


def test_the_change_kinds_the_producers_write_are_the_check_652_allows() -> None:
    """Every change_kind a producer renders or a share writes is in migration 652's CHECK, and 652
    keeps every value it found."""
    import inspect

    from api import shares
    from tests.migration_defs import MIGRATIONS

    sql = " ".join((MIGRATIONS / "652_notifications_w2a_event_columns.sql").read_text().split())
    check = sql.split("add constraint notification_dispatches_change_kind_ck check (", 1)[1]
    allowed = set(re.findall(r"'(\w+)'", check.split(");", 1)[0]))
    share_kinds = set(re.findall(r'"(shared_\w+)"', inspect.getsource(shares.grant)))
    assert share_kinds == {"shared_collection", "shared_dashboard", "shared_pipeline"}
    produced = {"new", "price_drop", "price_rise", "inactive", "reactivated", "new_source"}
    assert produced | share_kinds <= allowed
    assert {"shared", "system_alert", "broker_change"} <= allowed, "652 drops nothing"
