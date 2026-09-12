"""FILL — step 2 of 4: the hierarchy, joined off the bound ids, plus the pin election that
decides which coordinate the row publishes.

The rule FILL exists to state: **administrative names and codes come from the RÚIAN chain,
never from a claim.** Before W2-a this was S5, 295 lines with a registry branch, a
point-in-polygon branch, a sliver fallback, a `claimed`-beats-the-pin rule weighed against a
boundary distance, and a separate ČástObce point lookup. The chain read replaces all of it,
and the quarter now lands on EVERY branch instead of only on PIP (2026-09-11 audit,
resolver-v4: a street match lost its quarter).
"""

from __future__ import annotations

from location_data.resolver import bind as step_bind
from location_data.resolver import core
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm


def _resolve(claims, *, mirror=None):
    return core.resolve(
        claims, mm.context(mirror), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31",
    )


def _address_claims(**overrides):
    return [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "coordinate", lat=overrides.get("lat", 50.10102),
                 lon=overrides.get("lon", 14.34804),
                 declared_precision_label=overrides.get("label", "gps"),
                 licence_class=overrides.get("licence", "portal")),
    ]


# ------------------------------------------------------------------ the pin election


def test_the_registry_point_wins_over_the_portal_pin():
    resolution = _resolve(_address_claims())
    assert (resolution.lat, resolution.lon) == (50.10100, 14.34800)


def test_a_registry_pin_conflict_beyond_300_m_caps_the_confidence():
    """`REGISTRY_PIN_CONFLICT_M` = 300: flag, never silently pick. The registry point stays
    the position; the confidence is what says the two sources disagree."""
    resolution = _resolve(_address_claims(lat=50.2000, lon=14.4000))
    assert (resolution.lat, resolution.lon) == (50.10100, 14.34800)
    assert resolution.match_confidence in ("low", "medium")


def test_an_ephemeral_coordinate_can_never_become_the_position():
    """The Mapy class. Migration 384 made this a CHECK on three stores of record; W2-a moves
    the guard upstream into the claim read, and `bind.admissible` refuses it again."""
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=49.5936, lon=17.2987,
                 licence_class="ephemeral_display_only"),
    ]
    resolution = _resolve(claims)
    assert (resolution.lat, resolution.lon) != (49.5936, 17.2987)
    # It falls through to the obec's own definition point, which is a statement about the
    # TOWN and not a laundered Mapy coordinate.
    assert (resolution.lat, resolution.lon) == (50.0755, 14.4378)


def test_the_pin_is_chosen_by_declared_quality_not_by_claim_id():
    """A blurred coordinate that merely arrived FIRST used to become the position."""
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378,
                 declared_precision_label="municipality"),
        mm.claim(3, "coordinate", lat=50.10102, lon=14.34804, declared_precision_label="gps"),
    ]
    pin = step_bind.elect_pin(claims)
    assert pin is not None and pin.id == 3


def test_a_declared_blurred_pin_grades_at_the_obec_rung():
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378),
        mm.claim(3, "precision_declaration", declared_precision_label="municipality",
                 value_text="municipality"),
    ]
    resolution = _resolve(claims)
    assert resolution.granularity == "obec"
    assert resolution.match_confidence == "medium"


def test_a_bare_blur_hint_is_a_distinct_claim_type_with_no_declared_value():
    """bazos 'Přibližná lokalita' is a BINARY presence signal — folding it into
    `precision_declaration` would lose that it declares nothing."""
    claims = [
        mm.claim(1, "obec_name", value_text="Praha", source="bazos"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378, source="bazos"),
        mm.claim(3, "blur_hint", value_text="Přibližná lokalita", source="bazos"),
    ]
    resolution = _resolve(claims)
    assert resolution.granularity == "obec"


def test_our_own_coords_stamp_is_not_the_portals_declared_precision():
    """`coords.source` ('page', 'carry_forward') grades OUR write path; read as a label it
    made a map VIEW CENTRE count as a precise pin for the homonym tie-break."""
    stamp = mm.claim(1, "precision_declaration", value_text="page",
                     extraction_method="legacy_column", source="maxima")
    assert step_bind.read_declared_precision([stamp]).label is None
    legend = mm.claim(2, "precision_declaration", source="idnes",
                      value_text="Na mapě zobrazujeme jen nemovitosti s přesnou adresou.")
    assert step_bind.read_declared_precision([legend]).label is None
    spoken = mm.claim(3, "precision_declaration", value_text="street", source="sreality")
    declared = step_bind.read_declared_precision([spoken])
    assert declared.label == "street" and declared.blurred


# ---------------------------------------------------------------------- the chain read


def test_the_chain_comes_from_the_join_not_from_geometry():
    resolution = _resolve(_address_claims())
    assert (resolution.obec_kod, resolution.obec_name) == (554782, "Praha")
    assert (resolution.okres_kod, resolution.kraj_kod) == (3100, 19)
    assert (resolution.okres_name, resolution.kraj_name) == (
        "Hlavní město Praha", "Hlavní město Praha"
    )


def test_the_quarter_lands_on_a_registry_bound_row():
    resolution = _resolve(_address_claims())
    assert (resolution.cast_obce_kod, resolution.cast_obce_name) == (490067, "Vokovice")


def test_the_quarter_lands_on_a_name_bound_row_too():
    """The W2-a fix: `cast_obce` used to be filled only on the point-in-polygon branch, so a
    quarter CLAIMED by name and matched by BIND was dropped on the floor."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "cast_obce_name", value_text="Vokovice"),
    ])
    assert (resolution.cast_obce_kod, resolution.cast_obce_name) == (490067, "Vokovice")
    assert (resolution.obec_kod, resolution.okres_kod, resolution.kraj_kod) == (554782, 3100, 19)


def test_a_street_match_carries_the_whole_chain():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "street_name", value_text="Slunečná"),
    ])
    assert resolution.ulice_kod == 103
    assert (resolution.obec_kod, resolution.okres_kod, resolution.kraj_kod) == (599212, 3804, 80)


def test_a_coordinate_only_listing_gets_its_town_from_point_in_polygon():
    resolution = _resolve([mm.claim(1, "coordinate", lat=50.0755, lon=14.4378, source="bazos")])
    assert resolution.obec_kod == 554782
    assert resolution.granularity == "obec"


def test_a_point_outside_every_polygon_keeps_its_coordinate_and_says_nothing_else():
    """No sliver fallback any more (`nearest_obec_within` went with the boundary reads). The
    honest answer for a pin in no obec is the pin and `unknown`, never a guessed town."""
    mirror = mm.default_mirror()
    mirror.obec_polygons = {}
    resolution = _resolve([mm.claim(1, "coordinate", lat=49.5000, lon=15.5000)], mirror=mirror)
    assert (resolution.lat, resolution.lon) == (49.5000, 15.5000)
    assert resolution.obec_kod is None
    assert resolution.granularity == "unknown"
    assert resolution.country_status == "cz"  # inside the state polygon


# ------------------------------------------------------- čp / čo / PSČ: preserve-if-null


def test_the_registry_wins_the_house_number_and_the_psc_where_it_has_them():
    resolution = _resolve(_address_claims())
    assert (resolution.house_number_cp, resolution.house_number_co) == ("487", "40")
    assert resolution.psc == "16000"


def test_a_claimed_house_number_survives_when_the_registry_has_none():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "house_number_cp", value_text="487/40"),
        mm.claim(3, "psc", value_text="160 00"),
    ])
    assert resolution.house_number_cp == "487"
    assert resolution.psc == "16000"


def test_the_orientation_number_keeps_its_letter():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "house_number_co", value_text="487/40a"),
    ])
    assert resolution.house_number_co == "40a"
