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

from location_data.resolver import bind as step_bind
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


# ------------------------------------------ W1-c: the labels the nine contracts emit
#
# An unknown label is deliberately NOT treated as blurred ("cap, never certify"), so each of
# these silently cost the portal the very signal its entry exists to publish until W1-c added
# it. They were lost again when `position.py`/`precision.py` were re-typed into
# `bind.py`/`grade.py`, which is what this block exists to stop happening a third time.
#   (label, the portal and entry that emits it, the granularity it caps at, is it blurred?)
_W1C_DECLARED_LABELS = (
    ("approximate_location", "bazos/bzs.det.blur_hint", "obec", True),
    ("linestring", "maxima/mx.det.map_geometry", "street", True),
    ("circle", "maxima/mx.det.map_geometry", "cast_obce_or_quarter", True),
)


@pytest.mark.parametrize(("label", "who", "capped", "blurred"), _W1C_DECLARED_LABELS)
def test_a_w1c_declared_label_caps_the_pin_at_the_rung_its_contract_documents(
        label: str, who: str, capped: str, blurred: bool) -> None:
    """Both halves, through the real read and the real grade: the label is KNOWN (so
    `read_declared_precision` returns it rather than dropping it on the floor) and it caps at
    the rung the entry's own `precision_cap` documents. A label the ladder does not map takes
    the generic blur fallback, which on bazos is LOOSER than the obec ceiling its contract
    declares and on maxima's circle TIGHTER than the quarter."""
    assert label in step_bind.KNOWN_DECLARED_LABELS, who
    assert (label in step_bind.BLURRED_DECLARED_LABELS) is blurred, who
    assert grade.DECLARED_CAP[label] == capped, who

    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "coordinate", lat=50.10102, lon=14.34804),
        mm.claim(4, "precision_declaration", declared_precision_label=label,
                 value_text=label, blur_evidence="declared" if blurred else "none"),
    ]
    declared = step_bind.read_declared_precision(claims)
    assert declared.label == label and declared.blurred is blurred, who
    # The address evidence reaches `address_point`; the declaration is a CEILING that
    # coarsens it to exactly the rung the contract documents — not to the generic fallback.
    resolution = core.resolve(
        claims, mm.context(), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31",
    )
    assert resolution.granularity == capped, who


def test_mmrealitys_accurate_ranks_the_pin_without_certifying_a_granularity():
    """`accurate` is the ONE W1-c label that is precise rather than blurred, and it is
    deliberately absent from `DECLARED_CAP`: membership in `PRECISE_DECLARED_LABELS` decides
    which of two sibling declarations wins the pin (`declared_rank` returns 0), while a cap
    row would additionally CERTIFY a granularity the portal's own flag does not predict."""
    assert "accurate" in step_bind.PRECISE_DECLARED_LABELS
    assert "accurate" in step_bind.KNOWN_DECLARED_LABELS
    assert "accurate" not in step_bind.BLURRED_DECLARED_LABELS
    assert "accurate" not in grade.DECLARED_CAP

    precise = mm.claim(2, "coordinate", lat=50.0755, lon=14.4378,
                       declared_precision_label="accurate")
    blurred = mm.claim(3, "coordinate", lat=50.1, lon=14.5,
                       declared_precision_label="regional", blur_evidence="declared")
    assert step_bind.declared_rank(precise) == 0
    assert step_bind.declared_rank(blurred) == 2
    assert step_bind.elect_pin([precise, blurred]).id == precise.id
    resolution = core.resolve(
        [mm.claim(1, "obec_name", value_text="Praha"), precise, blurred], mm.context(),
        resolver_version=RESOLVER_VERSION, registry_version="ruian:2026-07-31",
    )
    # Nothing certified either: the granularity is whatever the ADDRESS evidence supports.
    assert resolution.granularity == "obec"
    assert (resolution.lat, resolution.lon) == (50.0755, 14.4378)


def test_maximas_point_is_deliberately_unmapped_and_certifies_nothing():
    """The shape the portal draws for "here" rather than for an area. It is not blurred (its
    entry's `blurred_labels` names only LineString and Circle, W1-c R5), and it is not precise
    either — a marker is not a measurement — so it caps nothing and must not reach the
    unmapped-blurred fallback, which would coarsen every maxima Point pin to `street`."""
    assert "point" not in step_bind.KNOWN_DECLARED_LABELS
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "coordinate", lat=50.10102, lon=14.34804),
        mm.claim(4, "precision_declaration", declared_precision_label="point",
                 value_text="Point", blur_evidence="none"),
    ]
    declared = step_bind.read_declared_precision(claims)
    assert declared.blurred is False
    resolution = core.resolve(
        claims, mm.context(), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31",
    )
    assert resolution.granularity == "address_point"
