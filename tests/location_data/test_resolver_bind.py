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


def test_krasny_les_resolves_via_the_cadastral_name_and_the_okres_claim():
    """maxima f60012522: the description states 'katastrální území Krásný Les u Frýdlantu,
    obec Krásný Les, okres Liberec' while the stored row carried obec Petrovice / okres Ústí
    nad Labem — the OTHER Krásný Les, ~100 km west. Five stored fields wrong at once."""
    binding = _bind([
        mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima"),
        mm.claim(2, "cadastral_territory_name", value_text="Krásný Les u Frýdlantu",
                 source="maxima", extraction_method="regex_text"),
        mm.claim(3, "okres_name", value_text="Liberec", source="maxima",
                 extraction_method="regex_text"),
    ])
    assert binding.admin_unit_id == 3  # Krásný Les, okres Liberec
    assert binding.ambiguous is False


def test_krasny_les_with_the_qualifier_alone_stays_ambiguous():
    """The qualifier lives on the cadastral name in the gazetteer, so the okres claim is the
    decisive one; without it the pair stays AMBIGUOUS rather than silently picking."""
    binding = _bind([
        mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima"),
        mm.claim(2, "homonym_qualifier", value_text="u Frýdlantu", source="maxima",
                 extraction_method="regex_text"),
    ])
    assert binding.ambiguous is True


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
    assert binding.granularity in ("street", "street_segment")
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


def test_without_a_pin_the_post_town_name_breaks_the_tie_still_at_low_confidence():
    claims = [
        mm.claim(1, "psc", value_text="67401", source="bazos"),
        mm.claim(2, "postal_town", value_text="674 01 Třebíč", source="bazos",
                 extraction_method="legacy_column"),
    ]
    binding = _bind(claims, mirror=_psc_mirror())
    assert binding.admin_unit_id == 22
    assert "postal_town" in binding.relaxations
    resolution = _resolve(claims, _psc_mirror())
    assert resolution.obec_kod == 590266
    assert resolution.match_confidence == "low"


def test_a_shared_psc_with_nothing_to_break_the_tie_is_ambiguous_and_still_answered():
    claims = [mm.claim(1, "psc", value_text="67401", source="bazos")]
    binding = _bind(claims, mirror=_psc_mirror())
    assert binding.ambiguous is True
    resolution = _resolve(claims, _psc_mirror())
    assert resolution.obec_kod in (590266, 545309)
    assert resolution.match_confidence == "low"


# ------------------------------------------------------------------- the deleted rung


def test_the_parcel_rung_is_gone():
    """R5 was unreachable — no portal states a cadastral parcel in a form that joins — and it
    was the only reader of `ruian_parcels`. A cadastral claim still QUALIFIES a homonym; it
    just cannot bind an entity of its own."""
    assert "R5" not in step_bind._RUNG_BASE_SCORE
    assert not hasattr(mm.default_mirror(), "parcels")
