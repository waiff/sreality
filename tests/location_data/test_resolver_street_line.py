"""The street matcher (W18, v5.6): a street claim bound whole against the register.

W18 split bazos' headline on the portals' separators and bound it segment by segment; W3
retired that headline claim (the text reading names the street itself) and v5.6 deleted the
split. What stays is the matcher: exact inside the anchoring obec, in two tiers, fail-closed,
never a place of that town — and R3 for a typo when nothing matched exactly.
"""

from __future__ import annotations

from location_data.resolver import composite
from location_data.resolver import core
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

MLADA_BOLESLAV, KLADNO, PRAHA, OSTRAVA = 535419, 532053, 554782, 554821


def _resolve(claims, *, mirror=None):
    return core.resolve(
        claims, mm.context(mirror), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31",
    )


def _street(value: str, town: str = "Mladá Boleslav"):
    return _resolve([
        mm.claim(1, "obec_name", source="bazos", value_text=town),
        mm.claim(2, "street_name", source="bazos", value_text=value),
    ])


def _bind(value: str, obec_kods=(MLADA_BOLESLAV,)):
    return composite.resolve_street([value], obec_kods, mm.default_mirror())


def test_a_street_binds_inside_its_town_and_nowhere_else():
    """The constraining obec keeps a common street name from placing a listing 200 km away —
    the Krásný Les lesson one level down."""
    assert _street("Jiráskova").ulice_kod == 105
    assert _bind("Ke Křížku", (KLADNO,)).street is not None
    assert _bind("Ke Křížku", (MLADA_BOLESLAV,)).street is None
    orphan = _resolve([mm.claim(1, "street_name", source="bazos", value_text="Jiráskova")])
    assert (orphan.street_name, orphan.obec_kod) == (None, None)


def test_the_official_generic_word_is_matched_and_so_is_its_absence():
    """`ruian_streets.name_norm` keeps `náměstí` while S1 parses it off; both forms are keys,
    which is the 215 titles that bind only with the word kept."""
    for value in ("náměstí Míru", "nám. Míru", "Míru"):
        assert _bind(value).street.code == 108, value


def test_an_exact_full_name_match_wins_outright_over_the_tolerant_fold():
    """Kladno holds `náměstí Svobody` AND `Svobody`, both folding to `svobody`: on two tiers a
    correct exact claim reads as one candidate. The abbreviation reaches both through the
    fold and fails closed — and R3 never undoes that refusal, a tie is not a typo."""
    assert _street("náměstí Svobody", town="Kladno").ulice_kod == 112
    assert _street("Svobody", town="Kladno").ulice_kod == 113
    assert _bind("nám. Svobody", (KLADNO,)).reason == "ambiguous_streets"
    assert _street("nám. Svobody", town="Kladno").street_name is None


def test_a_claim_naming_the_town_or_one_of_its_parts_is_never_a_street():
    """76 register streets across 20 obce are spelled exactly like a část obce of their own
    town; the row keeps its town rather than gaining a street that may be a quarter."""
    assert _bind("Zábřeh", (OSTRAVA,)).street is None


def test_a_street_whose_name_IS_the_generic_word_still_binds():
    assert composite.street_match_keys("Nová ulice")[1] == frozenset({"nova ulice", "nova"})
    assert _street("Nová ulice", town="Kladno").street_name == "Nová ulice"
    assert _street("Na Ulici", town="Kladno").street_name == "Na Ulici"


def test_the_street_index_is_the_same_answer_as_folding_each_row():
    """The per-obec index is an ACCELERATOR and nothing else."""
    mirror = mm.default_mirror()
    built = composite.build_street_index(mirror.streets_in_obec(KLADNO))
    assert composite.street_index(mirror, KLADNO) == built
    assert set(built["svobody"]) == {
        s for s in mirror.streets_in_obec(KLADNO) if s.code in (112, 113)}


def test_a_house_number_claimed_in_its_own_field_still_reaches_the_address_point():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou"),
        mm.claim(3, "house_number_cp", value_text="487"),
        mm.claim(4, "house_number_co", value_text="40"),
    ])
    assert resolution.ruian_adm_kod == 21690278
    assert resolution.street_name == "Nad Bořislavkou"
    assert resolution.house_number_cp == "487"


def test_every_street_claim_keeps_the_trigram_rung_for_a_typo():
    """v5.6: no claim carries `claim_confidence: low` any more — W3's reading names the street
    itself, so R3 is what it was built for, a typo, on every portal alike."""
    assert _street("Slunecna", town="Bílovec").street_name == "Slunečná"
    assert _street("Slunečná", town="Bílovec").street_name == "Slunečná"
