"""GRADE — step 3 of 4: table-driven, because the rule IS a table.

`match_confidence` answers ONE question: how many INDEPENDENT fields agreed with the entity
BIND landed on. Not "how good is the coordinate" — that is the radius, and the two are
deliberately separate (a declared-`gps` pin with no street text still grades by its
obec-level identity, because a street filter and a dedup key ask about the ADDRESS).

Before W2-a `match_confidence` was a hardcoded per-rung constant and the agreement evidence
(`Candidate.component_match`) was empty on every pin-positioned row — measured 2026-09-11,
resolver-13 — so the number said "which branch ran", not "how sure are we".
"""

from __future__ import annotations

import pytest

from location_data.resolver import core, grade
from location_data.resolver.bind import DeclaredPrecision
from location_data.resolver.types import (
    DEFAULT_GRANULARITY_RANK,
    Binding,
    GranularityRank,
    Position,
)
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

RANK = GranularityRank()
NO_DECLARATION = DeclaredPrecision(label=None, blurred=False, claim_ids=())


def _binding(**overrides) -> Binding:
    values = {
        "target_kind": "admin_unit", "granularity": "obec", "rung": "R4",
        "agreed": ("obec",), "relaxations": (),
    }
    values.update(overrides)
    return Binding(**values)  # type: ignore[arg-type]


def _graded(binding: Binding, position: Position | None = None, declared=NO_DECLARATION):
    return grade.grade(
        binding, position or Position(None, None, "none"), declared=declared, rank=RANK
    )


# ------------------------------------------------------------- agreement -> confidence


@pytest.mark.parametrize(
    "agreed, expected",
    [
        ((), "low"),                                  # nothing agreed; an inference
        (("obec",), "medium"),                        # one field
        (("obec", "psc"), "high"),                    # two INDEPENDENT fields
        (("obec", "psc", "okres"), "high"),           # more of the same verdict
    ],
)
def test_the_agreement_tally_is_the_confidence(agreed, expected):
    assert _graded(_binding(agreed=agreed)).match_confidence == expected


def test_an_address_point_whose_pin_corroborates_it_is_exact():
    position = Position(50.101, 14.348, "registry_point", registry_pin_distance_m=4.0)
    binding = _binding(target_kind="address_point", granularity="address_point",
                       agreed=("house_number", "street", "obec"))
    assert _graded(binding, position).match_confidence == "exact"


def test_a_pin_that_disagrees_with_the_registry_point_by_more_than_300_m_caps_at_medium():
    """`REGISTRY_PIN_CONFLICT_M`: the registry point stays the position and the confidence
    says the two sources are telling different stories. Flag, never silently pick."""
    position = Position(50.101, 14.348, "registry_point", registry_pin_distance_m=1_200.0)
    binding = _binding(target_kind="address_point", granularity="address_point",
                       agreed=("house_number", "street", "obec"))
    assert _graded(binding, position).match_confidence == "medium"


def test_a_tie_break_qualifier_caps_at_low_however_many_fields_agreed():
    """Six obce share PSČ 674 01. An honest low-confidence answer beats an arbitrary one —
    but it may never read as a corroborated one."""
    for qualifier in ("coordinate_tiebreak_imprecise", "postal_town"):
        binding = _binding(agreed=("obec", "psc"), relaxations=(qualifier,))
        assert _graded(binding).match_confidence == "low", qualifier


def test_an_ambiguous_bind_is_low_confidence_and_never_a_queue():
    """The contract flip of W2-a: `ambiguous` used to be a first-class status routed to an
    operator queue that does not exist."""
    assert _graded(_binding(agreed=("obec", "psc"), ambiguous=True)).match_confidence == "low"


def test_nothing_bound_is_low():
    assert _graded(Binding("none", "unknown", "none")).match_confidence == "low"


def test_a_blurred_declaration_caps_at_medium():
    declared = DeclaredPrecision(label="municipality", blurred=True, claim_ids=(9,))
    assert _graded(_binding(agreed=("obec", "psc")), declared=declared).match_confidence == "medium"


# --------------------------------------------------------------------- radius per level


def test_every_granularity_label_has_a_radius():
    """A missing level would raise mid-drain on the one listing that reached it."""
    assert set(grade.RADIUS_M) == set(DEFAULT_GRANULARITY_RANK)


def test_the_radius_is_monotone_in_the_rung():
    """Coarser identity, wider circle — with no exceptions, or a containment filter would
    admit a row it should have excluded."""
    ordered = sorted(DEFAULT_GRANULARITY_RANK, key=lambda g: DEFAULT_GRANULARITY_RANK[g])
    radii = [grade.RADIUS_M[g] for g in ordered if g != "unknown"]
    assert radii == sorted(radii, reverse=True)


@pytest.mark.parametrize(
    "granularity, metres",
    [("address_point", 10.0), ("street", 300.0), ("obec", 1_000.0), ("unknown", 250_000.0)],
)
def test_the_seeded_values_survive_the_policy_tables_deletion(granularity, metres):
    """These are migration 383's own v1 numbers, transcribed, not re-guessed."""
    assert grade.radius_m(granularity) == metres


def test_the_row_carries_the_radius_of_the_granularity_it_publishes():
    resolution = core.resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        ],
        mm.context(), resolver_version=RESOLVER_VERSION, registry_version="ruian:2026-07-31",
    )
    assert resolution.granularity == "address_point"
    assert resolution.uncertainty_radius_m == grade.RADIUS_M["address_point"]


# --------------------------------------------------------------------- the declared cap


@pytest.mark.parametrize(
    "label, expected",
    [("municipality", "obec"), ("no_exact_address", "cast_obce_or_quarter"),
     ("approximate", "street"), ("gps", "address_point")],
)
def test_a_declared_label_is_an_upper_bound_never_a_certification(label, expected):
    declared = DeclaredPrecision(label=label, blurred=label != "gps", claim_ids=(1,))
    binding = _binding(target_kind="address_point", granularity="address_point",
                       agreed=("house_number", "street", "obec"))
    assert _graded(binding, declared=declared).granularity == expected


def test_a_blurred_label_the_ladder_does_not_know_still_caps_at_street():
    declared = DeclaredPrecision(label="fuzzy", blurred=True, claim_ids=(1,))
    binding = _binding(target_kind="address_point", granularity="address_point")
    assert _graded(binding, declared=declared).granularity == "street"
