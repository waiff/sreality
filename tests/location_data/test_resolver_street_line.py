"""W18 — a street stated inside a LINE, bound segment by segment against the register.

A portal states a street inside a line as readily as it states a quarter inside one. bazos'
headline is "Prodej bytu 3+1, ul. Jiráskova, Mladá Boleslav" and its parser's own reading is
"Kladno - Dubí, Ke Křížku", so the answer is the one W9 already gave for composite
localities: split on the separators the portals write, match each segment against the
REGISTER inside the anchoring town, and fail closed when more than one thing matches.

The measurements this is built on (2026-09-16): of 20,909 cued titles, 18,226 anchor to an
obec and 14,659 (80.1 %) bind exactly to a RÚIAN street of that obec; 215 more bind only
because the generic word survived ("náměstí Míru", "třída Václava Klementa"); 24 lines name
two distinct street codes inside one obec and are refused; 21,930 titles sit on bazos' 60
character cap and a truncated stem never binds.
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


def _line(value: str, town: str = "Mladá Boleslav"):
    """One bazos row as the lane delivers it: the town off the href, the street claim as
    `street_token` leaves it (the generic wrapper gone, everything else intact)."""
    return _resolve([
        mm.claim(1, "obec_name", source="bazos", value_text=town),
        mm.claim(2, "street_name", source="bazos", value_text=value),
    ])


def _bind(value: str, obec_kods=(MLADA_BOLESLAV,)):
    return composite.resolve_street([value], obec_kods, mm.default_mirror())


# ------------------------------------------------------------------ the operator's own ad

def test_the_operators_title_binds_the_street_it_names():
    """THE CASE. bazos caps this title at 60 characters, so the town is cut to "Mladá
    Bolesl" and only the href gives the obec — which is exactly why the town is claimed
    from the href and the street from the line."""
    resolution = _line("Prodej bytu 3+1 s lodžií, 86 m2, ul. Jiráskova, Mladá Bolesl")
    assert resolution.street_name == "Jiráskova"
    assert resolution.ulice_kod == 105
    assert resolution.obec_kod == MLADA_BOLESLAV


def test_the_parsers_own_reading_binds_the_same_street():
    """`/coords/street` is the first surface and it is usually a bare name, which takes the
    ordinary R2 path rather than the line binder. Same answer, one rung either way."""
    assert _line("Jiráskova").ulice_kod == 105


# ------------------------------------------------------------------ the segment rules

def test_a_segment_carrying_a_house_number_reaches_the_address_point():
    """"ul. 28. října 12, Ostrava": the number belongs to the SEGMENT, not to the end of the
    line, so the binder takes it off the segment that bound and R1 uses it."""
    bound = _bind("ul. 28. října 12, Ostrava", (OSTRAVA,))
    assert bound.street.code == 110
    assert bound.cislo_domovni == 12
    resolution = _resolve([
        mm.claim(1, "obec_name", source="bazos", value_text="Ostrava"),
        mm.claim(2, "street_name", source="bazos", value_text="28. října 12, Ostrava"),
    ])
    assert resolution.street_name == "28. října"
    assert resolution.house_number_cp == "12"


def test_the_official_generic_word_is_matched_and_so_is_its_absence():
    """`ruian_streets.name_norm` keeps `náměstí` (the loader strips only a leading
    `ulice`/`ul.`) while S1 parses it off. Neither form alone is the key, so both are tried —
    which is the 215 titles that bind only with the word kept."""
    assert _bind("náměstí Míru, Mladá Boleslav").street.code == 108
    assert _bind("nám. Míru, Mladá Boleslav").street.code == 108
    assert _bind("Míru, Mladá Boleslav").street.code == 108


def test_a_dash_is_a_separator_and_the_last_segment_is_the_street():
    """"Kladno - Dubí, Ke Křížku" is the shape `_trailer_street_quarter` writes: a town, a
    quarter and a street in one string, on two different separators."""
    bound = _bind("Kladno - Dubí, Ke Křížku", (KLADNO,))
    assert bound.street.code == 106
    assert _line("Kladno - Dubí, Ke Křížku", town="Kladno").street_name == "Ke Křížku"


def test_two_distinct_streets_in_one_line_bind_nothing():
    """FAIL CLOSED, and it is the whole reason this is a binder and not a regex: a line
    naming two streets is two answers and neither is the listing's. 24 lines in the corpus
    are this shape."""
    bound = _bind("Sokolovská, Ke Křížku, Kladno", (KLADNO,))
    assert bound.street is None
    assert bound.reason == "ambiguous_streets"
    assert _line("Sokolovská, Ke Křížku, Kladno", town="Kladno").street_name is None


def test_two_spellings_of_one_street_are_one_answer():
    """Distinctness is keyed on the register's CODE, not on the string: a line that says the
    same street twice has named one street."""
    bound = _bind("náměstí Míru, nám. Míru, Mladá Boleslav")
    assert bound.street.code == 108


def test_a_segment_naming_the_town_or_one_of_its_parts_is_never_a_street():
    """76 register streets across 20 obce are spelled exactly like a část obce of their own
    town. On a line there is nothing to tell them apart, so the segment is dropped — the
    row keeps its town rather than gaining a street that may be a quarter."""
    assert _bind("Ostrava, Zábřeh", (OSTRAVA,)).street is None
    assert _bind("Mladá Boleslav, Jiráskova").street.code == 105


def test_a_title_cut_at_the_sixty_character_cap_binds_nothing():
    """21,930 bazos titles sit exactly on the cap, cut mid-word. There is no prefix matching
    anywhere in this lane: a truncated stem is ambiguous by nature ("ul. Vršo" could be
    Vršovická, Vršovců or Vršovské náměstí), and guessing is what W18 exists to stop."""
    assert _bind("Prodej bytu 2+kk, ul. Vršo").street is None
    assert _line("Prodej bytu 2+kk, ul. Vršo").street_name is None


def test_prose_in_a_line_never_reaches_the_trigram_rung():
    """R3 exists for ONE claimed name with a typo in it. Run over the segments of a title it
    would fuzzy-match "Prodej bytu" against a street, so a line binds exactly or not at
    all."""
    assert _line("Prodej bytu 3+1 s lodžií, 86 m2, Mladá Bolesl").street_name is None
    assert _line("Nový 2 pokojový byt, Praha 8", town="Praha").street_name is None


def test_an_inflected_form_does_not_match_the_register_exactly():
    """bazos writes "Livornské ulici" — the locative of `Livornská`. The generic word comes
    off and what is left is not the register's string, so the EXACT match fails. Czech
    inflection is out of scope for W18 and is deliberately not guessed at: R3 is off for
    lines, and a single inflected token can only ever reach it at `low` confidence."""
    assert composite.street_keys("Livornské ulici") == frozenset({"livornske"})
    assert _bind("Livornské ulici, Praha", (PRAHA,)).street is None


def test_the_binder_is_scoped_to_the_anchoring_obec():
    """`Ke Křížku` is a Kladno street. The same line under a different town binds nothing —
    the constraining obec is what keeps a common street name from placing a listing 200 km
    away, which is the Krásný Les lesson applied one level down."""
    assert _bind("Kladno - Dubí, Ke Křížku", (KLADNO,)).street is not None
    assert _bind("Kladno - Dubí, Ke Křížku", (MLADA_BOLESLAV,)).street is None


def test_a_line_with_no_anchoring_town_binds_no_street_at_all():
    resolution = _resolve([
        mm.claim(1, "street_name", source="bazos",
                 value_text="Prodej bytu 3+1, Jiráskova, Mladá Bolesl"),
    ])
    assert resolution.street_name is None
    assert resolution.obec_kod is None
