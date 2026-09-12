"""S4/S5 — position assignment, the registry-vs-pin cross-check, and admin assignment
(03 §3.6, §3.7).
"""

from __future__ import annotations

import dataclasses

import pytest

from location_data.resolver import core
from location_data.resolver import position as s4
from location_data.resolver.precision import DECLARED_CAP
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm


def _resolve(claims, *, mirror=None, ctx=None):
    return core.resolve(
        claims,
        ctx or mm.context(mirror),
        resolver_version=RESOLVER_VERSION,
        registry_version_id=7,
        policy_version="v1",
        collision_epoch_id=11,
    )


def _address_claims(**overrides):
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "coordinate", lat=overrides.get("lat", 50.10102),
                 lon=overrides.get("lon", 14.34804),
                 declared_precision_label=overrides.get("label", "gps"),
                 licence_class=overrides.get("licence", "portal")),
    ]
    return claims


# ------------------------------------------------------------------ S4 precedence


def test_registry_point_wins_over_the_portal_pin():
    resolution = _resolve(_address_claims())
    assert resolution.position.position_source == "registry_point"
    assert resolution.chosen_rule == "registry_point_wins"


def test_the_losing_pin_is_persisted_as_a_candidate_with_distance_to_pin_m():
    """There is no positions child table: the loser is a `location_resolution_candidates`
    row with `rejected_reason='lost_to_registry_point'`, and the distance is on BOTH rows."""
    resolution = _resolve(_address_claims())
    loser = next(c for c in resolution.candidates if c.target_kind == "coordinate_only")
    assert loser.rejected_reason == "lost_to_registry_point"
    assert loser.distance_to_pin_m == 0.0
    winner = next(c for c in resolution.candidates if c.rank == resolution.chosen_rank)
    assert winner.distance_to_pin_m is not None and winner.distance_to_pin_m < 10


def test_a_registry_pin_conflict_beyond_300_m_flags_and_caps_confidence():
    """`location_constants.registry_pin_conflict_m` = 300: flag, never silently pick."""
    resolution = _resolve(_address_claims(lat=50.2000, lon=14.4000))
    rules = {s.rule for s in resolution.contradiction_signals}
    assert "pin_registry_distance" in rules
    assert resolution.position.position_source == "registry_point"
    assert resolution.precision.match_confidence in ("low", "medium")
    signal = next(s for s in resolution.contradiction_signals if s.rule == "pin_registry_distance")
    assert signal.distance_m > 300


def test_an_ephemeral_coordinate_is_stored_as_a_candidate_and_never_wins():
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378,
                 licence_class="ephemeral_display_only"),
    ]
    resolution = _resolve(claims)
    assert resolution.position_licence_class != "ephemeral_display_only"
    rejected = [c for c in resolution.candidates if c.target_kind == "coordinate_only"]
    assert rejected and rejected[0].rejected_reason == "licence_ephemeral_inadmissible"


def test_a_declared_blurred_pin_becomes_portal_pin_blurred():
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378),
        mm.claim(3, "precision_declaration", declared_precision_label="municipality",
                 value_text="municipality"),
    ]
    resolution = _resolve(claims)
    assert resolution.position.position_source == "portal_pin_blurred"
    assert resolution.precision.blur_evidence in ("declared", "both")
    assert resolution.precision.granularity == "obec"


# The four labels W1-c's nine contracts added to the resolver's vocabulary. Each is one
# portal's own word, and an unknown label is NOT silently blurred ("cap, never certify"), so
# before these rows each of them resolved as a portal that declared nothing — bazos'
# `Přibližná lokalita` and maxima's drawn shapes among them.
#   (label, the portal and entry that emits it, the granularity it caps at, is it blurred?)
# Coarse -> fine, for the ceiling assertion below.
_GRANULARITY = ("country", "kraj", "okres", "obec", "cast_obce_or_quarter", "street",
                "street_segment", "building", "address_point")
_W1C_DECLARED_LABELS = (
    ("approximate_location", "bazos/bzs.det.blur_hint", "obec", True),
    ("linestring", "maxima/mx.det.map_geometry", "street", True),
    ("circle", "maxima/mx.det.map_geometry", "cast_obce_or_quarter", True),
)


@pytest.mark.parametrize(("label", "who", "capped", "blurred"), _W1C_DECLARED_LABELS)
def test_a_w1c_declared_label_caps_the_pin_at_the_rung_its_contract_documents(
        label: str, who: str, capped: str, blurred: bool) -> None:
    """Both halves, through the real S4 read and the real S6 assessment: the label is KNOWN
    (so `read_declared_precision` returns it rather than dropping it on the floor) and it
    caps at the rung the entry's own `precision_cap` documents. A label the ladder does not
    map takes the generic `blur_hint->street` fallback, which on bazos is LOOSER than the
    obec ceiling its contract declares and on maxima's circle TIGHTER than the quarter."""
    assert label in s4.KNOWN_DECLARED_LABELS, who
    assert (label in s4.BLURRED_DECLARED_LABELS) is blurred, who
    assert DECLARED_CAP[label] == capped, who

    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378),
        mm.claim(3, "precision_declaration", declared_precision_label=label,
                 value_text=label, blur_evidence="declared" if blurred else "none"),
    ]
    resolution = _resolve(claims)
    declared = s4.read_declared_precision(claims)
    assert declared.label == label, who
    assert declared.blurred is blurred, who
    # The cap is RECORDED by name, which is what proves the ladder mapped the label rather
    # than falling through: an unmapped blurred label is stamped `(unmapped)->street`.
    assert f"declared:{label}->{capped}" in resolution.precision.declared_caps, who
    assert "(unmapped)" not in " ".join(resolution.precision.declared_caps), who
    # A cap is a CEILING, never a floor. These claims state only a town, so the address
    # evidence already resolves at `obec` and a finer cap coarsens nothing — asserting the
    # granularity IS the cap would be asserting that a declaration can promote a pin.
    assert _GRANULARITY.index(resolution.precision.granularity) <= _GRANULARITY.index(capped)
    assert resolution.precision.granularity == "obec", who


def test_mmrealitys_accurate_ranks_the_pin_without_certifying_a_granularity():
    """`accurate` is the ONE W1-c label that is precise rather than blurred, and it is
    deliberately absent from `DECLARED_CAP`: membership in `PRECISE_DECLARED_LABELS` decides
    which of two sibling declarations wins the pin (`declared_for_coordinate` ranks it 0),
    while a cap row would additionally CERTIFY a granularity the portal's own flag does not
    predict. Being unmapped costs nothing — the entry documents an `address_point` ceiling,
    which is the finest rung, so capping at it would coarsen nothing anyway."""
    assert "accurate" in s4.PRECISE_DECLARED_LABELS
    assert "accurate" in s4.KNOWN_DECLARED_LABELS
    assert "accurate" not in s4.BLURRED_DECLARED_LABELS
    assert "accurate" not in DECLARED_CAP

    precise = mm.claim(2, "coordinate", lat=50.0755, lon=14.4378,
                       declared_precision_label="accurate")
    blurred = mm.claim(3, "coordinate", lat=50.1, lon=14.5,
                       declared_precision_label="regional", blur_evidence="declared")
    assert s4.declared_for_coordinate(precise).rank == 0
    assert s4.declared_for_coordinate(blurred).rank == 2
    resolution = _resolve([mm.claim(1, "obec_name", value_text="Praha"), precise, blurred])
    assert resolution.precision.blur_evidence == "none"
    # Nothing certified either: the granularity is whatever the ADDRESS evidence supports.
    assert not any(c.startswith("declared:accurate") for c in resolution.precision.declared_caps)


def test_maximas_point_is_deliberately_unmapped_and_certifies_nothing():
    """The shape the portal draws for "here" rather than for an area. It is not blurred (its
    entry's `blurred_labels` names only LineString and Circle, W1-c R5), and it is not
    precise either — a marker is not a measurement — so it caps nothing and must not reach
    the unmapped-blurred fallback, which would coarsen every maxima Point pin to `street`."""
    assert "point" not in s4.KNOWN_DECLARED_LABELS
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378),
        mm.claim(3, "precision_declaration", declared_precision_label="point",
                 value_text="Point", blur_evidence="none"),
    ]
    declared = s4.read_declared_precision(claims)
    assert declared.blurred is False and declared.blur_evidence == "none"
    resolution = _resolve(claims)
    assert resolution.precision.blur_evidence == "none"
    assert not any("unmapped" in c for c in resolution.precision.declared_caps)


def test_a_bare_blur_hint_is_a_distinct_claim_type_with_no_declared_value():
    """bazos 'Přibližná lokalita' is a BINARY presence signal — 00 §2.2 rejects folding it
    into `precision_declaration`."""
    claims = [
        mm.claim(1, "obec_name", value_text="Praha", source="bazos"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378, source="bazos"),
        mm.claim(3, "blur_hint", value_text="Přibližná lokalita", source="bazos"),
    ]
    resolution = _resolve(claims)
    assert resolution.position.position_source == "portal_pin_blurred"
    assert resolution.precision.blur_evidence == "declared"


# ------------------------------------------------------------------ S5 assignment


def test_registry_first_the_chain_comes_from_the_join_not_from_geometry():
    resolution = _resolve(_address_claims())
    assert resolution.admin.method == "registry"
    assert resolution.admin.obec_kod == 554782
    assert resolution.admin.okres_kod == 3100
    assert resolution.admin.kraj_kod == 19


def test_cast_obce_membership_is_a_code_predicate_over_address_points():
    """ČástObce has NO polygon in RÚIAN — only a definition point (03 §3.7.4)."""
    resolution = _resolve(_address_claims())
    assert resolution.admin.cast_obce_kod == 490067
    assert resolution.admin.cast_obce_name == "Vokovice"


def test_pip_fallback_inherits_the_pins_position_source():
    """D5: PIP-derived admin carries the PIN's source, so a bad pin cannot silently
    relocate a listing."""
    claims = [mm.claim(1, "coordinate", lat=50.0755, lon=14.4378, source="bazos")]
    resolution = _resolve(claims)
    assert resolution.admin.method == "pip_containment"
    assert resolution.admin.position_source == resolution.position.position_source == "portal_pin"
    assert resolution.admin.obec_kod == 554782


def test_a_point_outside_every_polygon_but_within_the_sliver_tolerance():
    """250 m is the value migration 289 already chose, and the outcome is a POSITIVE status
    (`pip_nearest_within_n_m`), never a silent NULL obec."""
    mirror = mm.default_mirror()
    mirror.obec_polygons = {567639: (50.5794, 13.9200, 100.0)}
    mirror.cz_polygon = (49.8, 15.5, 300_000.0)
    claims = [mm.claim(1, "coordinate", lat=50.58120, lon=13.92000)]
    resolution = _resolve(claims, mirror=mirror)
    assert resolution.admin.method == "pip_nearest_within_n_m"
    assert resolution.admin.sliver_distance_m is not None
    assert 0 < resolution.admin.sliver_distance_m <= 250


def test_inside_cz_but_no_obec_at_any_tolerance_is_unresolved_sliver_not_null():
    mirror = mm.default_mirror()
    mirror.obec_polygons = {}
    claims = [mm.claim(1, "coordinate", lat=49.5000, lon=15.5000)]
    resolution = _resolve(claims, mirror=mirror)
    assert resolution.admin.method == "unresolved_sliver"
    assert resolution.position.lat is not None  # the coordinate is KEPT


def test_a_validated_claim_beats_an_uncertain_pin_and_records_claimed():
    """03 §3.7.3 rule 2: when the pin's uncertainty exceeds the distance to the nearest
    boundary, the claimed locality wins. On bazos the two answers differ on 57.0 % of rows."""
    mirror = mm.default_mirror()
    # A pin just inside Praha's polygon edge, with a declared municipality-grade blur.
    mirror.obec_polygons[554782] = (50.0755, 14.4378, 1000.0)
    claims = [
        mm.claim(1, "coordinate", lat=50.08400, lon=14.43780, source="bazos"),
        mm.claim(2, "precision_declaration", declared_precision_label="municipality",
                 value_text="municipality", source="bazos"),
        mm.claim(3, "obec_name", value_text="Bílovec", source="bazos"),
    ]
    resolution = _resolve(claims, mirror=mirror)
    assert resolution.admin.method == "claimed"
    assert resolution.admin.obec_kod == 599212


def test_a_foreign_listing_skips_cz_resolution_but_keeps_its_pin():
    claims = [
        mm.claim(1, "address_line_verbatim", value_text="Benahavís, Španělsko", source="idnes"),
        mm.claim(2, "coordinate", lat=36.5090, lon=-4.8856, source="idnes"),
    ]
    resolution = _resolve(claims)
    assert resolution.status == "skipped_foreign"
    assert resolution.country.country_code == "ES"
    assert resolution.position.lat == 36.5090
    assert resolution.admin.method == "outside_country"


def test_distance_to_nearest_boundary_is_precomputed_for_the_membership_verdict():
    resolution = _resolve(_address_claims())
    assert resolution.admin.distance_to_nearest_boundary_m is not None


# ------------------------------------------- 2026-09-11 audit: ambiguity, labels, caps


def test_an_ambiguous_name_never_beats_the_pin_as_a_validated_claim():
    """Two Krásný Les, a pin inside Bílovec (neither of them): the tie cannot be broken, so
    the set stays `ambiguous` — and an ambiguous rank-1 is not a validated claim that §3.7.3
    rule 2 may prefer over point-in-polygon."""
    claims = [
        mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima"),
        mm.claim(2, "coordinate", lat=49.7573, lon=18.0158, source="maxima"),
    ]
    resolution = _resolve(claims)
    assert resolution.status == "ambiguous"
    assert resolution.admin.method == "pip_containment"
    assert resolution.admin.obec_kod == 599212


def test_our_own_coords_stamp_is_not_the_portals_declared_precision():
    """`coords.source` ('page', 'carry_forward') grades OUR write path; read as a label it
    made a map VIEW CENTRE count as a precise pin for the homonym tie-break."""
    stamp = mm.claim(1, "precision_declaration", value_text="page",
                     extraction_method="legacy_column", source="maxima")
    assert s4.read_declared_precision([stamp]).label is None
    legend = mm.claim(2, "precision_declaration", source="idnes",
                      value_text="Na mapě zobrazujeme jen nemovitosti s přesnou adresou.")
    assert s4.read_declared_precision([legend]).label is None
    spoken = mm.claim(3, "precision_declaration", value_text="street", source="sreality")
    declared = s4.read_declared_precision([spoken])
    assert declared.label == "street" and declared.blurred


def test_the_no_exact_address_disclaimer_caps_at_the_quarter_as_the_contract_declares():
    claims = _address_claims(label=None) + [
        mm.claim(4, "precision_declaration", source="idnes",
                 declared_precision_label="no_exact_address", blur_evidence="declared"),
    ]
    resolution = _resolve(claims)
    assert resolution.precision.granularity == "cast_obce_or_quarter"


def test_a_blurred_label_the_ladder_does_not_know_still_caps_at_street():
    claims = _address_claims(label=None) + [
        mm.claim(4, "precision_declaration", source="idnes",
                 declared_precision_label="fuzzy", blur_evidence="declared"),
    ]
    # (registry_point, street) is a shipped v1 pair (migration 491) the hand-written
    # fixture never needed before this cap existed.
    ctx = dataclasses.replace(mm.context(), uncertainty_policy=mm.v1_uncertainty_policy())
    resolution = _resolve(claims, ctx=ctx)
    rank = ctx.granularity_rank
    assert rank.rank(resolution.precision.granularity) <= rank.rank("street")
