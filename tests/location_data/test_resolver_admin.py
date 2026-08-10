"""S4/S5 — position assignment, the registry-vs-pin cross-check, and admin assignment
(03 §3.6, §3.7).
"""

from __future__ import annotations

from location_data.resolver import core
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
