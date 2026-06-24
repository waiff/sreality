"""Parity + invariant tests for the canonical filter registry.

The registry is the source of truth; these tests are the safety net.
If any of the existing hand-written classes drift from the registry
this suite fails immediately.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import fields as dc_fields
from pathlib import Path

import pytest

from toolkit import filter_registry as fr


# --- shape invariants -----------------------------------------------------


def test_registry_is_nonempty() -> None:
    assert len(fr.REGISTRY) > 0


def test_every_filter_has_at_least_one_agenda() -> None:
    for f in fr.all_filters():
        assert f.agendas, f"{f.id} declares no agendas"


def test_registry_keys_match_filter_ids() -> None:
    for key, f in fr.REGISTRY.items():
        assert key == f.id, f"registry key {key!r} != FilterDef.id {f.id!r}"


def test_ui_control_matches_type() -> None:
    """A LOCATION ui_control must pair with a LOCATION type, and vice versa.

    Other control / type combinations don't have hard invariants today,
    but the location composite is special — both the codegen and the
    React FilterForm dispatch on it together.
    """
    for f in fr.all_filters():
        is_loc_control = f.ui_control == fr.UiControl.LOCATION
        is_loc_type = f.type == fr.FilterType.LOCATION
        assert is_loc_control == is_loc_type, (
            f"{f.id}: ui_control LOCATION ↔ type LOCATION mismatch"
        )


def test_enum_constraints_match_enum_values() -> None:
    """When both `constraints['enum']` and `enum_values` are set they must agree."""
    for f in fr.all_filters():
        if not f.enum_values or not f.constraints:
            continue
        declared = f.constraints.get("enum")
        if declared is None:
            continue
        from_options = [o.value for o in f.enum_values]
        assert list(declared) == from_options, (
            f"{f.id}: constraints.enum {declared} != enum_values "
            f"{from_options}"
        )


def test_price_change_count_columns_match_window_enum() -> None:
    """The window→precomputed-column map and the `price_change_window_days`
    enum are the single source for the same set of windows — they must agree,
    or Browse/Watchdog read a column the UI can never select (or vice versa).
    A new window added to one without the other silently breaks the filter."""
    enum_windows = sorted(fr.by_id("price_change_window_days").constraints["enum"])
    mapped_windows = sorted(k for k in fr.PRICE_CHANGE_COUNT_COLUMNS if k is not None)
    assert enum_windows == mapped_windows, (
        f"price_change_window_days enum {enum_windows} != "
        f"PRICE_CHANGE_COUNT_COLUMNS keys {mapped_windows}"
    )


def test_pg_columns_subset_of_known_listings_columns() -> None:
    """Every column-backed filter points at a real listings or
    property-grain column.

    Hard-coded list mirrored from the migrations under `migrations/`.
    Update when the schema gains a new listings column; the registry
    refers to it so this test guards the inverse direction
    (registry-to-schema drift).
    """
    known = {
        "category_main", "category_type", "category_sub_cb",
        # Migration 152 — portal-agnostic normalized property sub-type.
        "subtype",
        "disposition", "condition", "building_type", "energy_rating",
        "furnished", "ownership",
        "has_balcony", "has_lift", "has_parking",
        "terrace", "cellar", "garage", "parking_lots",
        "price_czk", "area_m2", "estate_area", "usable_area",
        "garden_area",
        "district",
        "locality_district_id", "locality_region_id",
        # Migration 072 — derived condition scores.
        "building_condition_level", "apartment_condition_level",
        # Derived on listings_public (price_czk / NULLIF(area_m2, 0));
        # toolkit / matcher re-render the expression against the raw
        # columns. The PostgREST path treats it as a real column.
        "price_per_m2",
        # Migration 133 — MF gross rental yield % (sale apartments).
        "mf_gross_yield_pct",
        # Property-grain derived columns (migrations 091/095/173), exposed via
        # properties_public and filtered by the Browse property-grain RPC.
        # Not on `listings` — they aggregate across a property's children.
        # (The merged price-change filters are synthetic — pg_column=None —
        # because the window picks among four count columns at query time.)
        "distinct_site_count", "price_drop_count", "price_rise_count",
        "max_price_drop_pct",
        # Source portal (migration 091); exposed on listings_public and
        # properties_public, filtered by the `portals` multiselect.
        "source",
        # Property-grain city-proximity columns (migration 142), precomputed by
        # recompute_city_proximity and exposed on properties_public. home_obec_pop
        # backs Min/Max Population; the near_* columns back polygon-edge proximity.
        "home_obec_pop",
        "near_pop_5km", "near_pop_15km",
        "near_jobs_5km", "near_jobs_15km",
        "near_youth_5km", "near_youth_15km",
        "near_overall_5km", "near_overall_15km",
    }
    for f in fr.all_filters():
        if f.pg_column is None:
            continue
        assert f.pg_column in known, (
            f"{f.id}: pg_column {f.pg_column!r} is not a known listings column"
        )


# --- parity with hand-written classes (today's source of truth) -----------


def test_comparable_filters_fields_covered_by_registry() -> None:
    """Every ComparableFilters field exists in the registry under the
    same id."""
    from toolkit.comparables import ComparableFilters

    registry_ids = set(fr.REGISTRY.keys())
    for f in dc_fields(ComparableFilters):
        assert f.name in registry_ids, (
            f"ComparableFilters.{f.name} has no entry in REGISTRY — "
            f"add it to toolkit/filter_registry.py"
        )


def test_watchdog_filter_spec_fields_covered_by_registry() -> None:
    """Every WatchdogFilterSpec field exists in the registry.

    Pydantic field names match the registry ids 1:1 (Watchdog uses
    `min_price_czk`, `min_area_m2`, etc., same as the canonical
    naming).
    """
    from api.notifications import WatchdogFilterSpec

    registry_ids = set(fr.REGISTRY.keys())
    for name in WatchdogFilterSpec.model_fields:
        # `dispositions`, `districts`, lat/lng/radius_m are watchdog
        # specifics; lat/lng/radius_m are sub-fields of the composite
        # `location` filter, so allow them.
        if name in {"lat", "lng", "radius_m"}:
            assert "location" in registry_ids, (
                "WatchdogFilterSpec uses a center+radius spatial "
                "filter but the registry has no 'location' entry"
            )
            continue
        assert name in registry_ids, (
            f"WatchdogFilterSpec.{name} has no entry in REGISTRY — "
            f"add it to toolkit/filter_registry.py"
        )


def test_browse_agenda_includes_location() -> None:
    """Browse must offer the composite location filter."""
    browse_ids = {f.id for f in fr.filters_for_agenda(fr.Agenda.BROWSE)}
    assert "location" in browse_ids


def test_every_filter_with_a_pg_column_is_visible_in_some_user_agenda() -> None:
    """Sanity: column-backed filters that aren't reachable from any
    agenda are dead weight."""
    for f in fr.all_filters():
        if f.pg_column is None:
            continue
        assert f.agendas, f"{f.id} has a pg_column but no agendas"


# --- JSON serialisation ---------------------------------------------------


def test_registry_to_json_roundtrip_is_stable() -> None:
    """Calling twice produces identical output (no nondeterministic
    ordering)."""
    a = json.dumps(fr.registry_to_json(), sort_keys=False)
    b = json.dumps(fr.registry_to_json(), sort_keys=False)
    assert a == b


def test_registry_to_json_includes_every_filter() -> None:
    payload = fr.registry_to_json()
    ids_in_json = {f["id"] for f in payload["filters"]}
    assert ids_in_json == set(fr.REGISTRY.keys())


def test_visibility_argument_attaches_per_filter_visibility() -> None:
    payload = fr.registry_to_json(
        visibility={("browse", "min_price_czk"): False},
    )
    by_id = {f["id"]: f for f in payload["filters"]}
    assert by_id["min_price_czk"]["visibility"]["browse"] is False
    # Other agendas for the same filter stay enabled (the default).
    assert by_id["min_price_czk"]["visibility"]["watchdog"] is True


# --- min/max pair invariants ---------------------------------------------


def test_min_max_pairs_have_companion() -> None:
    """Every min-side range filter declares the matching max sibling.

    `FilterForm` pairs them into one slider / inputs row. A missing
    companion would render as an orphan number input — usable but
    user-confusing. Three pairing patterns are valid:

        min_X         ↔ max_X         (min_price_czk ↔ max_price_czk)
        X_min         ↔ X_max         (tom_days_min  ↔ tom_days_max)
        X_min_Y       ↔ X_max_Y       (last_seen_min_days ↔ last_seen_max_days)

    One-sided filters (`min_X` with no upper bound, e.g.
    `min_parking_lots` = "at least N parking spots") opt out by
    living in `ONE_SIDED_MINS` below. FilterForm renders those as a
    single number input via the unpaired-range fallthrough.
    """
    import re
    ONE_SIDED_MINS = frozenset({
        "min_parking_lots",
        # Merged price-change threshold (migration 173) — min-only by design;
        # its window modifier is a separate single_select, not a max side.
        "price_change_count_min",
        # City-proximity (migration 142) — "at least N within radius"; an upper
        # bound is meaningless, so these are min-only by design.
        "near_pop_5km_min",
        "near_pop_15km_min",
        "near_jobs_5km_min",
        "near_jobs_15km_min",
        "near_youth_5km_min",
        "near_youth_15km_min",
        "near_overall_5km_min",
        "near_overall_15km_min",
    })
    ids = set(fr.REGISTRY.keys())
    for fid in ids:
        if fid in ONE_SIDED_MINS:
            continue
        if fid.startswith("min_"):
            companion = "max_" + fid[len("min_"):]
            assert companion in ids, (
                f"{fid} declared without companion {companion}"
            )
        elif fid.endswith("_min"):
            companion = fid[:-len("_min")] + "_max"
            assert companion in ids, (
                f"{fid} declared without companion {companion}"
            )
        else:
            middle = re.match(r"^(.+)_min_(.+)$", fid)
            if middle:
                companion = f"{middle.group(1)}_max_{middle.group(2)}"
                assert companion in ids, (
                    f"{fid} declared without companion {companion}"
                )


# --- JSON Schema renderer -------------------------------------------------


def test_to_jsonschema_property_carries_description_from_registry() -> None:
    """The agent reads `description` from the JSON schema; assert it
    matches the registry verbatim so every operator-tunable change
    flows through."""
    prop = fr.to_jsonschema_property(fr.by_id("min_price_czk"))
    assert prop["type"] == "integer"
    assert prop["description"] == fr.description("min_price_czk")
    assert prop["minimum"] == 0


def test_to_jsonschema_property_includes_enum_for_enum_filters() -> None:
    prop = fr.to_jsonschema_property(fr.by_id("category_main"))
    assert prop["type"] == "string"
    # Constraints['enum'] surfaces as the JSON Schema enum keyword.
    assert "byt" in prop["enum"]


def test_to_jsonschema_property_list_filter_has_items() -> None:
    prop = fr.to_jsonschema_property(fr.by_id("dispositions"))
    assert prop["type"] == "array"
    assert prop["items"]["type"] == "string"
    # When enum_values is set, the items schema constrains values.
    assert "1+kk" in prop["items"]["enum"]


def test_to_jsonschema_properties_returns_visible_filters_for_agenda() -> None:
    props = fr.to_jsonschema_properties(fr.Agenda.COMPARABLES)
    # Every COMPARABLES filter appears, except the composite LOCATION.
    for fid in [
        "radius_m", "min_price_czk", "category_main",
        "has_balcony", "ownership", "min_garden_area",
    ]:
        assert fid in props, f"{fid} missing from COMPARABLES properties"
    assert "location" not in props


def test_agent_find_comparables_relaxed_descriptions_come_from_registry() -> None:
    """End-to-end: build the agent's tool registry, assert the
    find_comparables_relaxed filter descriptions match registry strings."""
    from api.agent import _build_tool_registry
    tools = _build_tool_registry()
    schema = tools["find_comparables_relaxed"].input_schema
    for fid in ("min_price_czk", "has_balcony", "category_main", "ownership"):
        assert schema["properties"][fid]["description"] == fr.description(fid)


# --- codegen drift --------------------------------------------------------


def test_codegen_check_passes() -> None:
    """`scripts/generate_filter_registry.py --check` must pass.

    If you've just edited `filter_registry.py`, re-run the script and
    commit the updated `frontend/src/lib/filterRegistry.generated.ts`.
    """
    root = Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "generate_filter_registry.py"), "--check"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            "Filter registry codegen is stale.\n"
            "Run: python scripts/generate_filter_registry.py\n"
            "and commit the updated frontend/src/lib/filterRegistry.generated.ts\n\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )


def test_mf_gross_yield_filter_registered():
    """The MF gross yield filter is a Browse+Watchdog float range on the
    mf_gross_yield_pct column (migration 133)."""
    for fid in ("min_mf_gross_yield_pct", "max_mf_gross_yield_pct"):
        f = fr.REGISTRY[fid]
        assert f.pg_column == "mf_gross_yield_pct"
        assert f.type == fr.FilterType.FLOAT
        assert f.ui_control == fr.UiControl.RANGE_SLIDER
        assert f.agendas == frozenset({fr.Agenda.BROWSE, fr.Agenda.WATCHDOG})
        assert fr.Agenda.COMPARABLES not in f.agendas
        assert fr.Agenda.ESTIMATION not in f.agendas


def test_category_main_multiselect_split():
    """category_main is split like dispositions/disposition_match: the
    multi-select `category_main_in` is Browse+Watchdog only, while the
    scalar `category_main` is the analytical single-category anchor.

    The two MUST NOT both reach a UI agenda (FilterForm would render two
    category controls) nor both reach an analytical agenda (a list-valued
    category would corrupt the comparable cohort)."""
    multi = fr.REGISTRY["category_main_in"]
    assert multi.pg_column == "category_main"
    assert multi.type == fr.FilterType.STRING_LIST
    assert multi.ui_control == fr.UiControl.MULTISELECT
    assert multi.agendas == frozenset({fr.Agenda.BROWSE, fr.Agenda.WATCHDOG})
    # STRING_LIST renders its enum under items.enum; a stray
    # constraints['enum'] would also emit an invalid top-level enum.
    assert not multi.constraints
    assert multi.enum_values == fr.CATEGORY_MAIN_OPTIONS

    scalar = fr.REGISTRY["category_main"]
    assert scalar.type == fr.FilterType.STRING
    assert fr.Agenda.BROWSE not in scalar.agendas
    assert fr.Agenda.WATCHDOG not in scalar.agendas
    assert fr.Agenda.COMPARABLES in scalar.agendas
    assert fr.Agenda.ESTIMATION in scalar.agendas
    # The two never co-occupy an agenda.
    assert scalar.agendas.isdisjoint(multi.agendas)
