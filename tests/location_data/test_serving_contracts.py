"""05 §5.5.2 as code: every declared feature floor is well-formed against the resolver's own
vocabularies, `meets_floor` compares by rank (never string order), and an undeclared
feature raises instead of failing open."""

from __future__ import annotations

import pytest

from location_data import serving_contracts as sc
from location_data.resolver.types import (
    DEFAULT_GRANULARITY_RANK,
    MATCH_CONFIDENCE_ORDER,
    GranularityRank,
)


def test_every_floor_uses_a_ranked_granularity_and_a_known_confidence() -> None:
    # §5.5.2: "every flag named here must exist and be NOT NULL". A floor spelled with a
    # rung that has no rank row, or a confidence that isn't a label, is a NULL by another name.
    for feature, floor in sc.FEATURE_FLOORS.items():
        assert floor.grain in sc.GRAINS, feature
        assert floor.min_granularity in DEFAULT_GRANULARITY_RANK, feature
        assert floor.min_confidence == sc.ANY_CONFIDENCE or (
            floor.min_confidence in MATCH_CONFIDENCE_ORDER
        ), feature
        assert floor.extra_gates, feature


def test_the_table_transcribes_the_design_row_by_row() -> None:
    # The design's 21 rows minus the two that declare no floor (see the module docstring),
    # plus the path C row added by operator ruling on 2026-09-10, minus the four W2-b
    # deleted with the columns they gated on (the two property-grain rows, which read
    # `property_location_current`, and the building/parcel dedup rungs, whose registry keys
    # the answer table does not carry).
    expected = {
        "map_pin": ("listing", "building", "high"),
        "map_circle": ("listing", "obec", "any"),
        "radius_search": ("listing", "street_segment", "medium"),
        "drawn_polygon_search": ("listing", "street_segment", "medium"),
        "admin_filter": ("listing", "obec", "any"),
        "cast_obce_filter": ("listing", "cast_obce_or_quarter", "medium"),
        "street_filter": ("listing", "street", "medium"),
        "dedup_rung_0a": ("listing", "address_point", "high"),
        "dedup_tier_1": ("listing", "street", "medium"),
        "dedup_tier_2": ("listing", "street_segment", "medium"),
        # + the one row ruled after the design was written: path C's "same town" rung
        # (NEW DEDUP ledger 2026-09-10; docs/design/location-serving-contract.md §3).
        "dedup_path_c": ("listing", "obec", "any"),
        "comparables_estimation": ("listing", "street", "medium"),
        "per_street_price_stats": ("property", "street", "high"),
        "per_obec_price_stats": ("property", "obec", "any"),
        "watchdog_geo_rule": ("listing", "obec", "any"),
        "chrome_extension_overlay": ("listing", "building", "high"),
    }
    assert {
        k: (v.grain, v.min_granularity, v.min_confidence) for k, v in sc.FEATURE_FLOORS.items()
    } == expected


def test_undeclared_feature_raises_never_defaults() -> None:
    with pytest.raises(KeyError, match="no §5.5.2 floor declared for feature 'browse_list'"):
        sc.floor_for("browse_list")
    with pytest.raises(KeyError):
        sc.meets_floor("browse_list", granularity="address_point", match_confidence="exact")


def test_map_pin_needs_building_and_high() -> None:
    ok = dict(granularity="building", match_confidence="high")
    assert sc.meets_floor("map_pin", **ok)
    assert sc.meets_floor("map_pin", granularity="address_point", match_confidence="exact")
    assert not sc.meets_floor("map_pin", granularity="street", match_confidence="exact")
    assert not sc.meets_floor("map_pin", granularity="building", match_confidence="medium")


def test_any_confidence_floor_is_granularity_only() -> None:
    assert sc.meets_floor("watchdog_geo_rule", granularity="obec", match_confidence="low")
    assert not sc.meets_floor("watchdog_geo_rule", granularity="okres", match_confidence="exact")


def test_comparison_is_by_rank_not_by_string_order() -> None:
    # "street" < "street_segment" alphabetically AND by rank, but "parcel" < "street" as a
    # string while ranking finer — a string comparison would get this row backwards.
    assert sc.meets_floor("street_filter", granularity="parcel", match_confidence="medium")
    assert sc.meets_floor("dedup_rung_0a", granularity="address_point", match_confidence="high")
    assert not sc.meets_floor("dedup_rung_0a", granularity="building", match_confidence="high")


def test_a_loaded_rank_table_overrides_the_offline_default() -> None:
    # The DB table is the authority (01 §0.4); a caller with a loaded mapping passes it.
    inverted = GranularityRank({**DEFAULT_GRANULARITY_RANK, "obec": 95, "building": 40})
    assert sc.meets_floor("map_pin", granularity="obec", match_confidence="high", rank=inverted)
    assert not sc.meets_floor("map_pin", granularity="obec", match_confidence="high")


def test_a5_default_is_include_and_badge_and_a_declared_value() -> None:
    # Operator decision 2026-09-09. Changing the default is a decision, not a refactor —
    # this test is where that shows up as a diff.
    assert sc.FILTER_DEFAULT_SEMANTICS == "include_and_badge"
    assert sc.FILTER_DEFAULT_SEMANTICS in sc.FILTER_SEMANTICS
    assert sc.FILTER_SEMANTICS == ("include_and_badge", "strict_with_toggle")


def test_confidence_at_least_walks_the_declared_order() -> None:
    assert sc.confidence_at_least("exact", "high")
    assert sc.confidence_at_least("medium", "medium")
    assert not sc.confidence_at_least("low", "medium")
    assert sc.confidence_at_least("low", sc.ANY_CONFIDENCE)
