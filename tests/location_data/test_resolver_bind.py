"""BIND — step 1 of 4: the three NAMED homonym regressions, and the ambiguity contract.

Czech obec names repeat heavily. The old system's answer was to refuse text→hierarchy
entirely and route everything through "geocode the text, then PIP the coordinate" — which
converts a naming problem into a precision problem, because the geocode of an ambiguous town
name IS the town centroid. BIND reverses it: resolve names locally and hierarchically inside
the constraining parent.

**The W2-a contract flip**: BIND returns ONE `Binding`, not a stored candidate set, and
`ambiguous` is a CONFIDENCE (`low`) rather than a first-class status routed to an operator
queue that does not exist. The 2026-09-11 audit measured what the queue cost: 24,601 bazos
rows were served an arbitrary id-ordered winner while the row was labelled `ambiguous`.
"""

from __future__ import annotations

from location_data.resolver import bind as step_bind
from location_data.resolver import core, normalize
from location_data.resolver.types import AddressPoint
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm


def _bind(claims, *, mirror=None, pin_is_precise=False):
    ctx = mm.context(mirror)
    binding, _ = step_bind.bind(
        claims, normalize.normalize_all(claims), ctx, pin_is_precise=pin_is_precise
    )
    return binding


def _resolve(claims, mirror=None):
    return core.resolve(
        claims, mm.context(mirror), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31",
    )


# ---------------------------------------------------- regression 1: Krásný Les (maxima)


def test_krasny_les_resolves_via_the_okres_claim():
    """maxima f60012522: the description states 'obec Krásný Les, okres Liberec' while the
    stored row carried obec Petrovice / okres Ústí nad Labem — the OTHER Krásný Les, ~100 km
    west. Five stored fields wrong at once."""
    binding = _bind([
        mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima"),
        mm.claim(3, "okres_name", value_text="Liberec", source="maxima",
                 extraction_method="regex_text"),
    ])
    assert binding.admin_unit_id == 3  # Krásný Les, okres Liberec
    assert binding.ambiguous is False


def test_an_ambiguous_bind_is_served_at_low_confidence_never_queued():
    claims = [mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima")]
    assert _bind(claims).ambiguous is True
    resolution = _resolve(claims)
    assert resolution.match_confidence == "low"
    assert resolution.obec_kod in (563943, 567931)  # served, deterministically


def test_a_psc_narrows_the_homonym_decisively():
    binding = _bind([
        mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima"),
        mm.claim(2, "psc", value_text="463 46", source="maxima"),
    ])
    assert binding.admin_unit_id == 3
    assert binding.ambiguous is False
    # The PSČ is an INDEPENDENT field agreeing with the name, so the pair grades `high`.
    assert set(binding.agreed) == {"obec", "psc"}


# ------------------------------------------------ regression 2: Bílovec (realitymix)


def test_bilovec_matches_through_the_deaccented_form():
    """realitymix 8375963/8375983: `locality_text: 'Bilovec'` — the diacritics-stripped form.
    The CZ municipality lookup missed it and a fuzzy STREET-level fallback landed in western
    Slovakia, ~180 km off, with obec/okres/region/ku_id all NULL."""
    binding = _bind([mm.claim(1, "obec_name", value_text="Bilovec", source="realitymix")])
    assert binding.admin_unit_id == 10
    assert binding.granularity == "obec"


def test_a_fuzzy_street_match_can_never_become_an_obec_resolution():
    """The Bílovec failure mode: a street-level fuzzy hit used as a municipality answer. The
    fuzzy rung produces a STREET binding — never an admin-unit one."""
    binding = _bind([
        mm.claim(1, "obec_name", value_text="Bilovec", source="realitymix"),
        mm.claim(2, "street_name", value_text="Slunecna", source="realitymix"),
    ])
    assert binding.rung in ("R2", "R3")
    assert binding.target_kind == "street"
    assert binding.granularity == "street"
    assert binding.obec_kod == 599212


# --------------------------------------------- regression 3: Bořislav 40 (GeocodeSOE)


def test_borislav_40_never_resolves_to_the_prague_street_that_contains_the_substring():
    """GeocodeSOE ranks `Nad Bořislavkou 487/40, Vokovice, Praha 6` FIRST for
    `SingleLine=Borislav 40` — a Prague street ~120 km from the correct village, every
    candidate scoring exactly 100. The obec constraint decides instead."""
    binding = _bind([
        mm.claim(1, "obec_name", value_text="Bořislav", source="maxima"),
        mm.claim(2, "house_number_cp", value_text="40", source="maxima"),
    ])
    assert binding.admin_unit_id == 15  # the village, not Praha
    assert binding.ruian_adm_kod != 21690278  # the Vokovice address point
    assert binding.ulice_kod != 101  # 'Nad Bořislavkou'


# ------------------------------------------- regression 4: a PSČ shared by several obce


def _psc_mirror() -> mm.MiniMirror:
    """Kraj Vysočina / okres Třebíč: Třebíč and Kožichovice both carry PSČ 674 01 (as do
    four more obce in production). Kožichovice's circle sits inside Třebíč's 5 km circle and
    has the LOWER obec code, so a code-ordered tie-break picks it first."""
    mirror = mm.default_mirror()
    mirror.units.extend([
        mm._unit(20, "kraj", 63, "Kraj Vysočina", "kraj vysocina", "k63"),
        mm._unit(21, "okres", 3710, "Třebíč", "trebic", "k63.o3710", parent=20),
        mm._unit(22, "obec", 590266, "Třebíč", "trebic", "k63.o3710.b590266", parent=21,
                 lat=49.2149, lon=15.8817, psc_set=("67401",)),
        mm._unit(23, "obec", 545309, "Kožichovice", "kozichovice", "k63.o3710.b545309",
                 parent=21, lat=49.1980, lon=15.9250, psc_set=("67401",)),
    ])
    mirror.points.extend([
        AddressPoint(kod_adm=44000001, obec_unit_id=22, obec_kod=590266, psc="67401",
                     lat=49.2149, lon=15.8817),
        AddressPoint(kod_adm=44000002, obec_unit_id=23, obec_kod=545309, psc="67401",
                     lat=49.1980, lon=15.9250),
    ])
    mirror.obec_polygons.update({
        590266: (49.2149, 15.8817, 5000.0),
        545309: (49.1980, 15.9250, 1200.0),
    })
    return mirror


def test_a_shared_psc_is_broken_by_the_pins_containing_obec_at_low_confidence():
    """bazos 18798695: 'Lokalita 674 01 Třebíč' and a link pin inside Třebíč. Six obce share
    the PSČ; before 2026-09-11 the PSČ branch skipped the qualifier ladder, the candidates
    tied at one score and the lowest admin_unit_id — Kožichovice — was served. Measured on
    24,601 bazos rows."""
    claims = [
        mm.claim(1, "psc", value_text="67401", source="bazos"),
        mm.claim(2, "coordinate", lat=49.2149, lon=15.8817, source="bazos"),
    ]
    binding = _bind(claims, mirror=_psc_mirror())
    assert binding.ambiguous is False
    assert binding.admin_unit_id == 22
    assert "coordinate_tiebreak_imprecise" in binding.relaxations
    resolution = _resolve(claims, _psc_mirror())
    assert resolution.obec_kod == 590266
    assert resolution.match_confidence == "low"  # a tie-break decided it, not a field


def test_the_pin_side_of_the_tie_wins_whichever_obec_it_lands_in():
    binding = _bind([
        mm.claim(1, "psc", value_text="67401", source="bazos"),
        mm.claim(2, "coordinate", lat=49.1980, lon=15.9250, source="bazos"),
    ], mirror=_psc_mirror())
    assert binding.ambiguous is False
    assert binding.admin_unit_id == 23


def test_a_pin_inside_neither_obec_takes_the_nearest_never_the_lowest_id():
    """v5.5 step 3: the pin REPLACES the lowest-`admin_unit_id` pick. 6 km west of Třebíč and
    inside neither circle, it is still nearer Třebíč than Kožichovice (the lower code) — and a
    nearest-town guess is served at `low`, never as a corroborated answer."""
    claims = [mm.claim(1, "psc", value_text="67401", source="bazos"),
              mm.claim(2, "coordinate", lat=49.2300, lon=15.8000, source="bazos")]
    binding = _bind(claims, mirror=_psc_mirror())
    assert (binding.admin_unit_id, binding.ambiguous) == (22, False)
    assert "coordinate_tiebreak_imprecise" in binding.relaxations
    assert _resolve(claims, _psc_mirror()).match_confidence == "low"


def test_a_shared_psc_with_nothing_to_break_the_tie_is_ambiguous_and_still_answered():
    claims = [mm.claim(1, "psc", value_text="67401", source="bazos")]
    binding = _bind(claims, mirror=_psc_mirror())
    assert binding.ambiguous is True
    resolution = _resolve(claims, _psc_mirror())
    assert resolution.obec_kod in (590266, 545309)
    assert resolution.match_confidence == "low"


# ------------------------------------------------------------- the v5.5 town rule (D4)


def _town_mirror(*, with_zlate_hory_ku: bool = True) -> mm.MiniMirror:
    """The W2 cases as the live register holds them (codes read 2026-09-30): Černotín is an
    obec near Přerov AND a část obce (and, in the name index, a KÚ) of Dnešice; Hory is an
    obec near Karlovy Vary AND a část obce and a KÚ of Oloví; "Zlaté Hory v Jeseníkách" is a
    KÚ of Zlaté Hory, 329 km from that Hory. Towns with a point carry an address point too."""
    mirror = mm.default_mirror()
    towns = {557668: (60, "Dnešice", 49.6056, 13.2655, ("33401", "33443")),
             513067: (63, "Černotín", 49.5271, 17.7717, ("75368",)),
             560588: (64, "Oloví", 50.2436, 12.5595, ("35709", "35707")),
             551651: (67, "Hory", 50.2100, 12.7800, ("36001",)),
             597996: (68, "Zlaté Hory", 50.2639, 17.3958, ("79376",))}
    for code, (uid, name, lat, lon, pscs) in towns.items():
        mirror.units.append(mm._unit(uid, "obec", code, name, normalize.normalize_match_key(name),
                                     f"b{code}", lat=lat, lon=lon, psc_set=pscs))
        mirror.obec_polygons[code] = (lat, lon, 4000.0)
        mirror.points.append(AddressPoint(kod_adm=uid, obec_unit_id=uid, obec_kod=code,
                                          psc=pscs[-1], lat=lat, lon=lon))
    parts = [(61, "cast_obce", 26778, "cernotin", 557668), (62, "katastralni_uzemi", 626775,
             "cernotin", 557668), (65, "cast_obce", 111007, "hory", 560588),
             (66, "katastralni_uzemi", 711004, "hory", 560588)]
    if with_zlate_hory_ku:
        parts.append((69, "katastralni_uzemi", 793191, "zlate hory v jesenikach", 597996))
    for uid, level, code, key, town in parts:
        mirror.units.append(mm._unit(uid, level, code, key, key, f"b{town}.x{code}",
                                     parent=towns[town][0]))
    return mirror


def _bazos(town: str, psc: str, pin: tuple[float, float] | None = None):
    claims = [mm.claim(1, "obec_name", value_text=town, source="bazos"),
              mm.claim(2, "psc", value_text=psc, source="bazos")]
    if pin is not None:
        claims.append(mm.claim(3, "coordinate", lat=pin[0], lon=pin[1], source="bazos"))
    return claims


def test_a_village_the_register_holds_as_a_part_binds_the_town_its_psc_serves():
    """Černotín is named; the only Černotín OBEC is near Přerov. At the část-obce and KÚ
    levels it climbs to Dnešice, and PSČ 334 43 is Dnešice's — so Dnešice binds, from the
    name, and the PSČ that discriminated counts as an agreeing field."""
    binding = _bind(_bazos("Černotín", "33443", pin=(49.6100, 13.2700)), mirror=_town_mirror())
    assert (binding.obec_kod, binding.rung) == (557668, "R4")
    assert "psc" in binding.agreed


def test_hory_binds_olovi_through_its_part_and_its_ku():
    binding = _bind(_bazos("Hory", "35707"), mirror=_town_mirror())
    assert (binding.obec_kod, binding.rung) == (560588, "R4")


def test_18841980_heals_through_the_ku_level():
    """The slug "zlaté-hory-v-jeseníkách" matches no obec. It split to a "Hory" 329 km away;
    it IS the KÚ of Zlaté Hory, whose obec carries PSČ 793 76."""
    claims = _bazos("zlaté-hory-v-jeseníkách", "79376", pin=(50.24194, 17.34158))
    binding = _bind(claims, mirror=_town_mirror())
    assert (binding.obec_kod, binding.rung) == (597996, "R4")


def test_a_composite_anchor_out_of_reach_falls_through_to_the_psc():
    """Without that KÚ the line anchors on "Hory": neither in the PSČ nor within 40 km of the
    pin, so it is refused and the PSČ answers — at R6, an inference."""
    claims = _bazos("zlaté-hory-v-jeseníkách", "79376", pin=(50.24194, 17.34158))
    binding = _bind(claims, mirror=_town_mirror(with_zlate_hory_ku=False))
    assert (binding.obec_kod, binding.rung) == (597996, "R6")


def test_a_named_town_the_pin_cannot_reach_is_refused_unless_the_psc_vouches():
    """No PSČ: a unique obec-level name 280 km from the pin is a namesake, not the town — the
    pin's own town answers (R7). Within 40 km, or with no pin, the name binds as ever."""
    praha, bilovec_pin = mm.claim(1, "obec_name", value_text="Praha"), (49.7573, 18.0158)
    pin = mm.claim(2, "coordinate", lat=bilovec_pin[0], lon=bilovec_pin[1])
    assert (_bind([praha, pin]).obec_kod, _bind([praha, pin]).rung) == (599212, "R7")
    assert _bind([praha]).obec_kod == 554782
    near = mm.claim(1, "obec_name", value_text="Bílovec")
    assert (_bind([near, pin]).obec_kod, _bind([near, pin]).rung) == (599212, "R4")


def test_a_name_that_binds_nothing_falls_through_to_the_part_then_the_psc():
    """The `elif` is open (step 4): an unknown town name no longer leaves a PSČ unread."""
    unknown = mm.claim(1, "obec_name", value_text="Neexistující")
    binding = _bind([unknown, mm.claim(2, "psc", value_text="74301")])
    assert (binding.obec_kod, binding.rung) == (599212, "R6")
    resolution = _resolve([unknown, mm.claim(2, "cast_obce_name", value_text="Vokovice"),
                           mm.claim(3, "psc", value_text="160 00")])
    assert (resolution.obec_kod, resolution.cast_obce_kod) == (554782, 490067)


# ------------------------------------------------ the pin BIND uses is the pin it publishes


def test_the_town_is_reverse_geocoded_from_the_pin_the_row_actually_publishes():
    """Two coordinate claims — a blurred one that arrived first and a precise one that did
    not. The town used to come from `collect_constraints`' FIRST coordinate by id while
    `geom` came from `elect_pin`'s best-DECLARED one, so the row shipped a town in Bílovec
    and a pin in Praha, 300 km apart. R7/R8 are pin-derived, so CHECK skips the containment
    test by design and nothing downstream could have caught it."""
    claims = [
        mm.claim(2, "coordinate", lat=49.7573, lon=18.0158,
                 declared_precision_label="municipality"),
        mm.claim(3, "coordinate", lat=50.0755, lon=14.4378, declared_precision_label="gps"),
    ]
    assert step_bind.elect_pin(claims).id == 3
    resolution = _resolve(claims)
    assert (resolution.lat, resolution.lon) == (50.0755, 14.4378)
    assert resolution.obec_kod == 554782  # Praha, the town the PUBLISHED pin is in
    assert resolution.disputed is None


def test_the_psc_tie_break_uses_the_elected_pin_too():
    """Same defect, the other branch that reads `constraints.pin`: with a shared PSČ the
    tie-break must consult the pin the row publishes, not whichever coordinate sorted first."""
    claims = [
        mm.claim(1, "psc", value_text="67401", source="bazos"),
        mm.claim(2, "coordinate", lat=49.1980, lon=15.9250, source="bazos",
                 declared_precision_label="municipality"),
        mm.claim(3, "coordinate", lat=49.2149, lon=15.8817, source="bazos",
                 declared_precision_label="gps"),
    ]
    resolution = _resolve(claims, _psc_mirror())
    assert (resolution.lat, resolution.lon) == (49.2149, 15.8817)
    assert resolution.obec_kod == 590266  # Třebíč, where the elected pin is


# --------------------------------------------------- a PSČ is one fact, not two agreeing ones


def test_a_psc_only_bind_is_low_confidence():
    """The obec was not named, it was LOOKED UP. Seeding the qualifier list with "psc" and
    then counting "obec" + "psc" as two independent fields was one fact counted twice, and it
    graded a bare postcode `high` — the same grade a town named and corroborated gets."""
    for psc, obec_kod in (("743 01", 599212), ("160 00", 554782)):
        resolution = _resolve([mm.claim(1, "psc", value_text=psc)])
        assert resolution.obec_kod == obec_kod, psc
        assert resolution.match_confidence == "low", psc
        assert resolution.granularity == "obec", psc


def test_a_named_town_corroborated_by_its_psc_still_grades_high():
    """The counterpart: two INDEPENDENT fields — the name and the postcode — agreeing on one
    obec is exactly what `high` is for."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima"),
        mm.claim(2, "psc", value_text="463 46", source="maxima"),
    ])
    assert resolution.obec_kod == 563943
    assert resolution.match_confidence == "high"


# ------------------------------------------- the margin compares like with like


def test_a_quarter_is_not_ambiguous_against_the_town_that_contains_it():
    """Reproduced: a town qualified by one field scored 45 + 5, tying the quarter below it at
    45 + 5, and the zero gap graded the row `low`. One answer containing another is not two
    answers, so the margin compares like with like."""
    resolution = _resolve([mm.claim(1, "obec_name", value_text="Praha"),
                           mm.claim(2, "cast_obce_name", value_text="Vokovice")])
    assert resolution.cast_obce_kod == 490067
    assert resolution.match_confidence == "high"


# ------------------------------------------------------- the region is the chain's last word


def test_an_okres_name_alone_binds_the_okres():
    """maxima and mmreality emit an okres name with no town. "We know the okres" is a real
    answer at a real rung, and strictly better than the `undetermined` an unbound region used
    to collapse to — the hierarchy above it fills by the ordinary chain read."""
    resolution = _resolve([mm.claim(1, "okres_name", value_text="Liberec", source="maxima")])
    assert (resolution.okres_kod, resolution.okres_name) == (3506, "Liberec")
    assert resolution.kraj_name == "Liberecký kraj"
    assert resolution.obec_kod is None
    assert resolution.granularity == "okres"
    assert resolution.match_confidence == "low"
    # And it is a COUNTRY determination: the gazetteer it came out of is the Czech one.
    assert (resolution.country_status, resolution.country_code) == ("cz", "CZ")


def test_a_kraj_name_alone_binds_the_kraj():
    resolution = _resolve([mm.claim(1, "kraj_name", value_text="Ústecký kraj")])
    assert (resolution.kraj_kod, resolution.kraj_name) == (42, "Ústecký kraj")
    assert resolution.granularity == "kraj"
    assert resolution.country_status == "cz"


def test_a_town_still_outranks_the_region_that_contains_it():
    """The region rung is the LAST one: it fires only when nothing finer bound."""
    resolution = _resolve([
        mm.claim(1, "okres_name", value_text="Liberec"),
        mm.claim(2, "obec_name", value_text="Krásný Les"),
    ])
    assert resolution.obec_kod == 563943
    assert resolution.granularity == "obec"


# ------------------------------------------------------------------- the deleted rung


def test_the_parcel_rung_is_gone():
    """R5 was unreachable — no portal states a cadastral parcel in a form that joins — and it
    was the only reader of `ruian_parcels`. A KÚ is a LOOKUP LEVEL of the town rule (v5.5); it
    binds no entity of its own."""
    assert "R5" not in step_bind._RUNG_BASE_SCORE
    assert not hasattr(mm.default_mirror(), "parcels")
