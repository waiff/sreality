"""The one filter compiler (C4, rule 16), tested at its interface.

`compile_filter_where(values, grain)` is the only place a registry FilterDef becomes a
WHERE predicate for the Watchdog (`PROPERTIES_GRAIN`) and every estimation cohort
(`LISTINGS_GRAIN`). Per-filter output is pinned by the golden
(`tests/toolkit/test_filter_compiler_golden.py`); this file pins the module's contracts:
the kind templates, each hook, that every filter has exactly one implementation, the
agenda gate (rules 16 + 17), the retired-filter raise, and the text guards that keep the
census and the spatial rails covering the code that moved here.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

import toolkit.filter_registry as fr
from toolkit import filter_compiler as fc
from toolkit.filter_compiler import (
    LISTINGS_GRAIN,
    PROPERTIES_GRAIN,
    RETIRED_FILTERS,
    compile_filter_where,
)
from toolkit.measures import per_m2_sql, plot_area_sql

_ROOT = Path(__file__).resolve().parents[2]
_GRAINS = (LISTINGS_GRAIN, PROPERTIES_GRAIN)


def _ids(agenda: fr.Agenda) -> set[str]:
    return {f.id for f in fr.filters_for_agenda(agenda)}


def _comparable_fields() -> set[str]:
    from toolkit.comparables import ComparableFilters

    return {f.name for f in dataclasses.fields(ComparableFilters)}


def _spec_fields() -> set[str]:
    from api.notifications import WatchdogFilterSpec

    return set(WatchdogFilterSpec.model_fields)


# --- the kind templates ----------------------------------------------------------------


@pytest.mark.parametrize("grain", _GRAINS, ids=lambda g: str(g.agenda))
def test_each_kind_renders_its_template(grain: fc.FilterGrain) -> None:
    cases: list[tuple[dict[str, Any], str, dict[str, Any]]] = [
        ({"category_type": "prodej"}, "l.category_type = %(category_type)s",
         {"category_type": "prodej"}),
        ({"has_lift": False}, "l.has_lift = %(has_lift)s", {"has_lift": False}),
        ({"portals": ("sreality",)}, "l.source = ANY(%(portals)s)",
         {"portals": ["sreality"]}),
        ({"min_usable_area": 10}, "l.usable_area >= %(min_usable_area)s",
         {"min_usable_area": 10}),
        ({"building_condition_level_max": 0},
         "l.building_condition_level <= %(building_condition_level_max)s",
         {"building_condition_level_max": 0}),
        ({"furnished": ["ano", fr.UNKNOWN_FILTER_VALUE]},
         "(l.furnished = ANY(%(furnished)s) OR "
         "(l.furnished IS NULL OR NOT (l.furnished = ANY(%(furnished_canon)s))))",
         {"furnished": ["ano"], "furnished_canon": list(fr.FURNISHED_CANONICAL)}),
        ({"ownership": [fr.UNKNOWN_FILTER_VALUE]},
         "((l.ownership IS NULL OR NOT (l.ownership = ANY(%(ownership_canon)s))))",
         {"ownership_canon": list(fr.OWNERSHIP_CANONICAL)}),
    ]
    for values, clause, params in cases:
        assert compile_filter_where(values, grain) == ([clause], params), values


def test_the_rule_23_measures_resolve_per_relation() -> None:
    values = {"min_price_per_m2": 1.0, "max_estate_area": 2.0}
    assert compile_filter_where(values, LISTINGS_GRAIN)[0] == [
        f"{per_m2_sql('l')} >= %(min_price_per_m2)s",
        f"{plot_area_sql('l')} <= %(max_estate_area)s",
    ]
    assert compile_filter_where(values, PROPERTIES_GRAIN)[0] == [
        "l.price_per_m2 >= %(min_price_per_m2)s",
        f"{plot_area_sql('l')} <= %(max_estate_area)s",
    ]


@pytest.mark.parametrize("grain", _GRAINS, ids=lambda g: str(g.agenda))
@pytest.mark.parametrize("unset", [None, [], ()])
def test_an_unset_value_renders_nothing(grain: fc.FilterGrain, unset: Any) -> None:
    values = {"portals": unset, "min_usable_area": None, "has_lift": None, "furnished": unset}
    assert compile_filter_where(values, grain) == ([], {})


def test_the_kind_templates_are_the_shared_table() -> None:
    """The same table Browse's TS dispatch is tested against (registryQueryBuilder.test.ts)."""
    table = json.loads((_ROOT / "tests" / "fixtures" / "filter_sql_kinds.json").read_text())
    assert dict(fc._KIND_SQL) == {k: v["sql"] for k, v in table["kinds"].items()}
    kinds = {fr.sql_kind(f) for f in fr.all_filters()} - {None}
    assert kinds == set(table["kinds"])


# --- the hooks, called directly --------------------------------------------------------


def _hook(fid: str, value: Any, values: dict[str, Any] | None = None) -> tuple[list[str], dict]:
    params: dict[str, Any] = {}
    return fc._HOOKS[fid](fid, value, {fid: value, **(values or {})}, params), params


def test_price_bounds_keep_no_price_rows_only_when_asked() -> None:
    assert _hook("min_price_czk", 5) == (["l.price_czk >= %(min_price_czk)s"],
                                         {"min_price_czk": 5})
    assert _hook("max_price_czk", 5, {"include_no_price": True}) == (
        ["(l.price_czk is null or l.price_czk <= %(max_price_czk)s)"], {"max_price_czk": 5})
    assert _hook("min_price_czk", None, {"include_no_price": True}) == ([], {})
    assert _hook("include_no_price", True) == ([], {})


@pytest.mark.parametrize("window", [None, 30, 90, 365])
def test_the_price_change_window_picks_the_count_column(window: int | None) -> None:
    col = fr.PRICE_CHANGE_COUNT_COLUMNS[window]
    assert _hook("price_change_count_min", 2, {"price_change_window_days": window}) == (
        [f"l.{col} >= %(price_change_count_min)s"], {"price_change_count_min": 2})
    assert _hook("price_change_window_days", window) == ([], {})


def test_total_price_change_is_signed_and_zero_is_no_bound() -> None:
    assert _hook("total_price_change_pct", 0) == ([], {})
    assert _hook("total_price_change_pct", -5)[0] == [
        "l.total_price_change_pct <= %(total_price_change_pct)s"]
    assert _hook("total_price_change_pct", 5)[0] == [
        "l.total_price_change_pct >= %(total_price_change_pct)s"]


def test_seen_days_bounds_are_inverted_against_their_affix() -> None:
    assert _hook("last_seen_max_days", 3)[0] == [
        "l.last_seen_at >= now() - make_interval(days => %(last_seen_max_days)s)"]
    assert _hook("last_seen_min_days", 3)[0] == [
        "l.last_seen_at <= now() - make_interval(days => %(last_seen_min_days)s)"]
    assert _hook("first_seen_max_days", 3)[0] == [
        "l.first_seen_at >= now() - make_interval(days => %(first_seen_max_days)s)"]
    assert _hook("first_seen_min_days", 3)[0] == [
        "l.first_seen_at <= now() - make_interval(days => %(first_seen_min_days)s)"]


def test_tom_bounds_read_the_one_tom_expression() -> None:
    assert _hook("tom_days_min", 5) == ([f"{fc._TOM_EXPR} >= %(tom_days_min)s"],
                                        {"tom_days_min": 5})
    assert _hook("tom_days_max", 9)[0] == [f"{fc._TOM_EXPR} <= %(tom_days_max)s"]


def test_building_material_expands_buckets_like_the_spa() -> None:
    self_named = {"cihla", "panel", "smisena"}
    other = [v for v in fr.COLUMN_CANONICAL_VALUES["building_type"] if v not in self_named]
    assert fr.building_material_values(["ostatni"]) == other
    assert fr.building_material_values(["nonsense"]) == other   # an unknown bucket is ostatni
    assert fr.building_material_values(["panel", "cihla", "panel"]) == ["panel", "cihla"]
    assert fr.building_material_values(["cihla", "ostatni"]) == ["cihla", *other]
    assert _hook("building_material", ["panel"]) == (
        ["l.building_type = ANY(%(building_material)s)"], {"building_material": ["panel"]})
    assert _hook("building_material", []) == ([], {})


def test_city_index_rules_is_one_obec_predicate_with_a_jsonb_param() -> None:
    rule = {"index_name": "celkove_hodnoceni", "op": ">=", "value": 6}
    where, params = _hook("city_index_rules", [rule])
    assert where == [
        "l.obec_id = ANY (ARRAY(SELECT curated_cities_matching(%(city_index_rules)s::jsonb)))"]
    assert isinstance(params["city_index_rules"], Jsonb)
    assert params["city_index_rules"].obj == [rule]
    assert _hook("city_index_rules", []) == ([], {})


# --- one implementation per filter -----------------------------------------------------


@pytest.mark.parametrize("grain", _GRAINS, ids=lambda g: str(g.agenda))
def test_every_filter_on_a_grain_has_exactly_one_implementation(grain: fc.FilterGrain) -> None:
    for f in fr.filters_for_agenda(grain.agenda):
        homes = [
            fr.sql_kind(f) is not None and f.id not in fc._HOOKS,
            f.id in fc._HOOKS,
            f.id in grain.caller_renders,
        ]
        assert sum(homes) == 1, f"{f.id}: {homes} (kind, hook, caller-rendered)"


def test_the_hooks_are_exactly_the_irregular_filters_no_adapter_renders() -> None:
    both = _ids(fr.Agenda.COMPARABLES) | _ids(fr.Agenda.WATCHDOG)
    irregular = {fid for fid in both if fr.sql_kind(fr.REGISTRY[fid]) is None}
    rendered = LISTINGS_GRAIN.caller_renders | PROPERTIES_GRAIN.caller_renders
    assert set(fc._HOOKS) == irregular - rendered


def test_every_model_field_is_partitioned() -> None:
    """Together with the test above, this makes both LookupError paths unreachable."""
    assert _comparable_fields() <= (
        _ids(fr.Agenda.COMPARABLES) | LISTINGS_GRAIN.caller_renders)
    assert _spec_fields() <= (
        _ids(fr.Agenda.WATCHDOG) | PROPERTIES_GRAIN.caller_renders | set(RETIRED_FILTERS))


# --- the gate (rules 16 + 17) and the retired raise ------------------------------------


_CITY_IDS = [f.id for f in fr.all_filters() if f.category == fr.CATEGORY_CITY_QUALITY]


@pytest.mark.parametrize("fid", _CITY_IDS)
def test_every_city_quality_filter_raises_on_the_cohort(fid: str) -> None:
    value: Any = [{"index_name": "x", "op": ">=", "value": 1}] if fid == "city_index_rules" else 1
    with pytest.raises(ValueError, match=r"^rule 17 violation"):
        compile_filter_where({fid: value}, LISTINGS_GRAIN)


def test_an_off_agenda_filter_raises_an_agenda_violation() -> None:
    with pytest.raises(ValueError, match=r"^agenda violation: min_mf_gross_yield_pct"):
        compile_filter_where({"min_mf_gross_yield_pct": 4.0}, LISTINGS_GRAIN)
    with pytest.raises(ValueError, match=r"^agenda violation: category_main "):
        compile_filter_where({"category_main": "byt"}, PROPERTIES_GRAIN)


def test_no_stored_watchdog_spec_can_trip_the_gate() -> None:
    for fid in _spec_fields() - PROPERTIES_GRAIN.caller_renders:
        f = fr.REGISTRY.get(fid)
        if f is not None:
            assert fr.Agenda.WATCHDOG in f.agendas, fid


@pytest.mark.parametrize("value", [{}, {"radius_km": 10, "index_rules": []}])
def test_a_retired_filter_raises_even_when_empty(value: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="near_city_proximity is retired"):
        compile_filter_where({"near_city_proximity": value}, PROPERTIES_GRAIN)
    compile_filter_where({"near_city_proximity": None}, PROPERTIES_GRAIN)


def test_an_unregistered_key_is_a_programming_error() -> None:
    with pytest.raises(LookupError, match="not registered filters"):
        compile_filter_where({"no_such_filter": 1}, PROPERTIES_GRAIN)
    compile_filter_where({"no_such_filter": None}, PROPERTIES_GRAIN)


# --- agendas, basis, nullable ----------------------------------------------------------


def test_one_grain_serves_comparables_and_estimation() -> None:
    assert _ids(fr.Agenda.COMPARABLES) == _ids(fr.Agenda.ESTIMATION)


def test_velocity_compiles_under_comparables_on_purpose() -> None:
    """The agent's velocity tool passes the estimation's base filters, which carry the six
    tom/seen bounds VELOCITY does not declare: gating on VELOCITY would raise there and
    silently change agent estimates (api/agent.py turns the raise into a tool error)."""
    assert _ids(fr.Agenda.COMPARABLES) - _ids(fr.Agenda.VELOCITY) == {
        "tom_days_min", "tom_days_max", "last_seen_min_days", "last_seen_max_days",
        "first_seen_min_days", "first_seen_max_days",
    }
    assert LISTINGS_GRAIN.agenda is fr.Agenda.COMPARABLES


def test_basis_is_inseparable_from_the_measure() -> None:
    based = [f for f in fr.all_filters() if f.basis is not None]
    assert based
    for f in based:
        assert LISTINGS_GRAIN.exprs[f.pg_column] == per_m2_sql("l"), f.id
        assert f.pg_column not in PROPERTIES_GRAIN.exprs, f.id


@pytest.mark.parametrize("grain", _GRAINS, ids=lambda g: str(g.agenda))
def test_a_null_deal_type_is_no_constraint(grain: fc.FilterGrain) -> None:
    where, params = compile_filter_where({"category_type": None}, grain)
    assert not any("category_type" in w for w in where)
    assert "category_type" not in params
    _, params = compile_filter_where({"category_type": "pronajem", "portals": ["sreality"]}, grain)
    assert "any" not in params.values()


# --- params never collide with the adapters' or statements' own keys -------------------


def test_compiled_param_keys_are_disjoint_from_every_caller_key() -> None:
    emitted = set(fr.REGISTRY) | {f"{fid}_canon" for fid in fr.REGISTRY}
    callers = {
        "cursor", "upper", "window_size", "image_lookback_minutes", "subscription_id",
        "target_channels", "drop_pids", "drop_sids", "drop_prices", "drop_prevs",
        "transport_types", "anchor_radius_m", "corridor_m", "lat", "lng", "disposition",
        "disposition_loose", "area_min", "area_max", "floor_min", "floor_max",
        "exclude_listing_ids",
    }
    assert not emitted & callers
    assert not {k for k in emitted if k.startswith("district_codes")}
    # Caller-rendered ids the compiler never emits, even though they are FilterDefs.
    for fid in ("radius_m", "max_age_days"):
        assert fid in LISTINGS_GRAIN.caller_renders
        _, params = compile_filter_where({fid: 1}, LISTINGS_GRAIN)
        assert params == {}


# --- text guards -----------------------------------------------------------------------


def test_the_module_text_stays_out_of_the_spatial_and_measure_rails() -> None:
    from tests.api.test_watchdog_browse_one_measure import _HAND_TYPED_DIVISION

    text = (_ROOT / "toolkit" / "filter_compiler.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "ll.geom" not in code
    assert "dismiss" not in code.lower()
    assert not re.search(r"K[čc]\s*/\s*m", code)
    assert not _HAND_TYPED_DIVISION.search(code)
