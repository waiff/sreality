"""FILL — step 2 of 4: the hierarchy, joined off the bound ids, plus the pin election that
decides which coordinate the row publishes and the registry point that places the rows the
pin election cannot (W2-a3, the last section).

The rule FILL exists to state: **administrative names and codes come from the RÚIAN chain,
never from a claim.** Before W2-a this was S5, 295 lines with a registry branch, a
point-in-polygon branch, a sliver fallback, a `claimed`-beats-the-pin rule weighed against a
boundary distance, and a separate ČástObce point lookup. The chain read replaces all of it,
and the quarter now lands on EVERY branch instead of only on PIP (2026-09-11 audit,
resolver-v4: a street match lost its quarter).
"""

from __future__ import annotations

from dataclasses import replace

from location_data.resolver import bind as step_bind
from location_data.resolver import core
from location_data.resolver import grade as step_grade
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


def test_a_pin_just_outside_a_polygon_still_gets_that_town_at_low_confidence():
    """BIND's LAST rung. A pin 30 m outside every obec polygon is a boundary artifact — a
    rounded coordinate, a simplified edge, a river bank — not a listing with no town, and
    rule 25 does not allow a Czech listing to have none. `PIP_SLIVER_TOLERANCE_M` is 250 m,
    the value `location_constants.pip_sliver_tolerance_m` carried before W2-b dropped it.

    It is NOT a dispute: nothing about the row contradicts anything else about it."""
    mirror = mm.default_mirror()
    # Bořislav's circle, shrunk so the pin below sits ~30 m outside it.
    mirror.obec_polygons = {567639: (50.5794, 13.9200, 100.0)}
    resolution = _resolve([mm.claim(1, "coordinate", lat=50.58057, lon=13.92000)], mirror=mirror)
    assert resolution.obec_kod == 567639
    assert resolution.match_confidence == "low"
    assert resolution.disputed is None
    assert resolution.granularity == "obec"
    assert (resolution.lat, resolution.lon) == (50.58057, 13.92000)


def test_a_pin_far_outside_every_polygon_binds_nothing():
    """The tolerance is a tolerance, not a nearest-neighbour search: 5 km out there is no
    boundary artifact to forgive, and inventing a town would be worse than saying nothing."""
    mirror = mm.default_mirror()
    mirror.obec_polygons = {567639: (50.5794, 13.9200, 100.0)}
    resolution = _resolve([mm.claim(1, "coordinate", lat=50.62450, lon=13.92000)], mirror=mirror)
    assert resolution.obec_kod is None
    assert resolution.granularity == "unknown"
    assert (resolution.lat, resolution.lon) == (50.62450, 13.92000)
    # The TOWN is undetermined; the COUNTRY is not, because the pin is demonstrably inside
    # the state polygon and throwing that away would be inventing an absence.
    assert resolution.country_status == "cz"
    mirror.cz_polygon = None
    mirror.obec_polygons = {}
    away = _resolve([mm.claim(1, "coordinate", lat=41.9, lon=12.5)], mirror=mirror)
    assert away.country_status == "undetermined"


def test_the_sliver_tolerance_is_the_constant_migration_289_chose():
    assert step_bind.PIP_SLIVER_TOLERANCE_M == 250.0


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


# ------------------------------------------- the position: a towned row always has one
#
# W2-a3. 8,706 of 29,892 towned rows (29 %, 2026-09-12 08:05Z) carried `geom NULL`, because
# the only position the resolver published was a portal pin and a row bound by NAME has
# none. The rule: the pin when one was admissible, else the finest bound unit's own registry
# point. The SIX cases below are the contract.


def test_a_town_bound_by_name_alone_is_placed_at_the_towns_own_point():
    """Case 1. The point is the boundary's inscribed-circle centre — inside the polygon by
    construction, which `ST_Centroid` is not for a concave obec. Nothing else moves: the
    granularity still says `obec` and the radius is still the obec constant, because the
    LEVEL is what tells a reader how coarse a position is."""
    resolution = _resolve([mm.claim(1, "obec_name", value_text="Bílovec")])
    assert (resolution.lat, resolution.lon) == (49.7573, 18.0158)
    assert resolution.granularity == "obec"
    assert resolution.uncertainty_radius_m == step_grade.RADIUS_M["obec"]
    assert resolution.disputed is None


def test_a_street_with_no_pin_falls_back_to_its_towns_point():
    """Case 2. `ruian_streets` carries no geometry in the mirror, so there is no street point
    to fall back TO — the town's is the finest thing that exists. The row still grades
    `street`: the address identity is what BIND matched, and the position is the half that is
    coarser than the level, not the other way round."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "street_name", value_text="Slunečná"),
    ])
    assert resolution.ulice_kod == 103
    assert resolution.granularity == "street"
    assert (resolution.lat, resolution.lon) == (49.7573, 18.0158)


def test_an_address_point_with_no_pin_publishes_the_address_points_own_point():
    """Case 3, and the one rung that was never broken: a bound address point has always been
    its own position. It is here so a future refactor cannot quietly coarsen it to the town."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
    ])
    assert resolution.ruian_adm_kod == 21690278
    assert (resolution.lat, resolution.lon) == (50.10100, 14.34800)
    assert resolution.granularity == "address_point"


def test_a_foreign_row_keeps_no_position():
    """Case 4. Nothing Czech bound, so there is no registry point to place it at — and a
    foreign listing placed at a Czech unit would be worse than an unplaced one."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Benahavís"),
        mm.claim(2, "country", value_text="Španělsko"),
    ])
    assert resolution.country_status == "foreign"
    assert (resolution.lat, resolution.lon) == (None, None)


def test_an_undetermined_row_keeps_no_position():
    """Case 5. `undetermined` is the state that says "we have nothing"; inventing a position
    for it would be the one thing it must never mean."""
    resolution = _resolve([mm.claim(1, "obec_name", value_text="Nikdejov")])
    assert resolution.country_status == "undetermined"
    assert resolution.granularity == "unknown"
    assert (resolution.lat, resolution.lon) == (None, None)


def test_an_admissible_pin_still_wins_over_the_unit_point():
    """Case 6. FILL places what BIND did not place — it never re-places. A portal pin is a
    statement about THIS listing; a unit point is a statement about its town."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "coordinate", lat=49.7600, lon=18.0200),
    ])
    assert (resolution.lat, resolution.lon) == (49.7600, 18.0200)


def test_a_quarter_takes_its_towns_point_because_ruian_draws_no_quarter_polygon():
    """The walk, and why it is a walk and not `chain[0]`: `cast_obce` and `momc` are the two
    levels a Czech listing most often names by hand, and RÚIAN has no polygon for either —
    so the finest bound unit is precisely the one with no point of its own."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "cast_obce_name", value_text="Vokovice"),
    ])
    assert resolution.cast_obce_kod == 490067
    assert resolution.granularity == "cast_obce_or_quarter"
    assert (resolution.lat, resolution.lon) == (50.0755, 14.4378)


def test_a_region_only_listing_is_placed_at_the_region_it_named():
    """BIND's last rung (R9) publishes a position too — an okres-grade point with a 25 km
    radius is a worse answer than a town and a better one than no answer at all."""
    resolution = _resolve([mm.claim(1, "okres_name", value_text="Nový Jičín")])
    assert resolution.granularity == "okres"
    assert (resolution.lat, resolution.lon) == (49.5944, 18.0103)
    assert resolution.uncertainty_radius_m == step_grade.RADIUS_M["okres"]


def test_the_unit_point_comes_from_the_registry_and_never_from_a_claim():
    """The purity half of the rule: FILL reads the point off the chain the REGISTRY answers.
    A mirror whose boundaries were never loaded has no point to give, and the row keeps
    `geom NULL` — the same answer it had before W2-a3 — rather than a guessed one."""
    mirror = mm.default_mirror()
    mirror.units = [replace(u, lat=None, lon=None) for u in mirror.units]
    resolution = _resolve([mm.claim(1, "obec_name", value_text="Bílovec")], mirror=mirror)
    assert resolution.obec_kod == 599212
    assert (resolution.lat, resolution.lon) == (None, None)


def test_an_obec_with_no_point_climbs_to_its_okres_rather_than_publishing_nothing():
    """The same walk the quarter takes, one level up. It is bounded at the kraj: `stat` is on
    every chain and placing a listing at the centre of the country would be an answer about
    nothing."""
    mirror = mm.default_mirror()
    mirror.units = [
        replace(u, lat=None, lon=None) if u.level == "obec" else u for u in mirror.units
    ]
    resolution = _resolve([mm.claim(1, "obec_name", value_text="Bílovec")], mirror=mirror)
    assert resolution.granularity == "obec"
    assert (resolution.lat, resolution.lon) == (49.5944, 18.0103)  # okres Nový Jičín


def test_the_walk_stops_above_the_kraj():
    """The state polygon IS on every chain in the live mirror — `ruian_load` wires obec → POU
    → ORP → okres → kraj → region soudržnosti → stát, and the boundary loader draws all of
    them — so the walk has to REFUSE the last two rather than run out of rows. A listing at
    the centre of the Czech Republic carrying an obec's 1 km radius is a worse answer than no
    position."""
    mirror = mm.default_mirror()
    mirror.units = [
        replace(u, lat=None, lon=None) if u.level in ("obec", "okres", "kraj") else u
        for u in mirror.units
    ] + [
        mm._unit(90, "region_soudrznosti", 80, "Moravskoslezsko", "moravskoslezsko", "t1.r80",
                parent=91, lat=49.5, lon=17.5),
        mm._unit(91, "stat", 1, "Česko", "cesko", "t1", lat=49.8, lon=15.5),
    ]
    mirror.units = [
        replace(u, parent_id=90) if u.unit_id == 8 else u for u in mirror.units
    ]
    resolution = _resolve([mm.claim(1, "obec_name", value_text="Bílovec")], mirror=mirror)
    assert resolution.obec_kod == 599212
    assert (resolution.lat, resolution.lon) == (None, None)
