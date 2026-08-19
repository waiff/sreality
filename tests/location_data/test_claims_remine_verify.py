"""The W3 gate verifier's own verdict logic.

The SQL is exercised against production, not here — what these tests hold is the part
that decides PASS from FAIL, because a verifier that cannot fail is not a gate.
"""

from __future__ import annotations

import pytest

from location_data.claims_remine import COORDINATE_HISTORY_SOURCES, W3_HISTORY_COMPLETENESS
from location_data.claims_remine_verify import (
    SERIES_CLAIM_TYPES,
    _VERSION_LIKE,
    check_completeness,
    check_licence,
    check_series,
)


def _completeness_rows(**by_source: str) -> list[dict]:
    return [
        {"source": s, "history_completeness": v, "claims": 1}
        for s, v in by_source.items()
    ]


def test_completeness_passes_when_every_source_carries_its_mapped_stamp():
    rows = _completeness_rows(**W3_HISTORY_COMPLETENESS)
    result = check_completeness(rows)
    assert result["passed"]
    assert not result["unexpected"]


def test_completeness_fails_when_a_text_only_portal_claims_full_history():
    rows = _completeness_rows(sreality="full", idnes="full")
    result = check_completeness(rows)
    assert not result["passed"]
    assert result["unexpected"] == {"idnes": ["full"]}


def test_completeness_fails_when_sreality_is_downgraded_to_text_only():
    result = check_completeness(_completeness_rows(sreality="locality_text_only"))
    assert not result["passed"]
    assert result["unexpected"] == {"sreality": ["locality_text_only"]}


def test_completeness_fails_on_a_source_carrying_two_different_stamps():
    rows = [
        {"source": "bazos", "history_completeness": "locality_text_only", "claims": 9},
        {"source": "bazos", "history_completeness": "full", "claims": 1},
    ]
    result = check_completeness(rows)
    assert not result["passed"]
    assert result["unexpected"] == {"bazos": ["full"]}


def test_every_non_coordinate_portal_is_mapped_to_text_only():
    text_only = {
        s for s, v in W3_HISTORY_COMPLETENESS.items() if v == "locality_text_only"
    }
    assert text_only == set(W3_HISTORY_COMPLETENESS) - COORDINATE_HISTORY_SOURCES
    assert len(text_only) == 8


def _licence_row(**kw) -> dict:
    return {
        "licence_class": "portal", "claim_type": "locality_text",
        "source": "sreality", "claims": 1, **kw,
    }


def test_licence_passes_on_a_clean_corpus():
    rows = [
        _licence_row(),
        _licence_row(claim_type="coordinate", source="sreality"),
        _licence_row(source="idnes"),
    ]
    assert check_licence(rows)["passed"]


def test_licence_fails_when_a_class_e_value_reached_the_table():
    rows = [_licence_row(licence_class="ephemeral_display_only", claims=7)]
    result = check_licence(rows)
    assert not result["passed"]
    assert result["ephemeral_display_only_claims"] == 7


def test_licence_fails_on_a_coordinate_from_a_portal_that_has_no_coordinate_history():
    """The indirect ladder breach: the only source of such a value is the live geom."""
    rows = [_licence_row(claim_type="coordinate", source="bezrealitky", claims=3)]
    result = check_licence(rows)
    assert not result["passed"]
    assert result["coordinate_claims_from_non_coordinate_portals"] == [
        {"source": "bezrealitky", "claims": 3}
    ]


def _series(listings: int, changed: int, returned: int = 0) -> dict:
    return {
        "listings": listings,
        "listings_changed": changed,
        "listings_changed_twice": 0,
        "listings_returned_to_a_prior_value": returned,
        "max_changes": 1 if changed else 0,
    }


def test_series_passes_when_a_value_actually_moved():
    result = check_series({"coordinate": _series(1000, 42, returned=7)})
    assert result["passed"]
    assert result["listings_whose_value_changed"] == 42
    assert result["listings_that_returned_to_a_prior_value"] == 7


def test_series_fails_when_the_corpus_is_flat():
    """Existence is not the gate. A series where nothing ever moved shows nothing."""
    result = check_series({"coordinate": _series(1000, 0)})
    assert not result["passed"]
    assert result["listings_with_a_series"] == 1000


def test_series_fails_when_there_is_no_series_at_all():
    assert not check_series({"coordinate": _series(0, 0)})["passed"]


def test_series_sums_across_claim_types():
    result = check_series({
        "coordinate": _series(10, 0),
        "precision_declaration": _series(5, 2),
    })
    assert result["passed"]
    assert result["listings_with_a_series"] == 15


def test_the_version_pattern_matches_the_lane_and_not_its_neighbours():
    """`claims_remine_archive@1` is a DIFFERENT lane sharing the name's prefix."""
    import re

    rx = re.compile("^" + _VERSION_LIKE.replace("%", ".*") + "$")
    assert rx.match("claims_remine@1")
    assert rx.match("claims_remine@2")
    assert not rx.match("claims_intake@1")


def test_series_claim_types_are_the_two_that_carry_geometry_and_precision():
    assert SERIES_CLAIM_TYPES == ("coordinate", "precision_declaration")
