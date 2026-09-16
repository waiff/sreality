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
    assert composite.street_keys("Livornské ulici") == frozenset(
        {"livornske", "livornske ulici"})
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


# ------------------------------------------------- the two tiers, and what they are for

def test_an_exact_full_name_match_wins_outright_over_the_tolerant_fold():
    """Kladno holds `náměstí Svobody` AND `Svobody`, and both fold to `svobody`. On one flat
    key set a correct exact claim reads as two candidates and fails closed; on two tiers it
    reads as one. 45 register keys across 32 obce are this shape."""
    assert _bind("náměstí Svobody, Kladno", (KLADNO,)).street.code == 112
    assert _bind("Svobody, Kladno", (KLADNO,)).street.code == 113
    # The ABBREVIATION matches neither exactly, so it reaches both rows through the tolerant
    # fold and fails closed. That is the honest answer and not a gap to paper over: this lane
    # does not hold a table saying `nám.` means `náměstí`, and inventing one to break a tie
    # between two real streets of the same town is the guess W18 exists to refuse. Where the
    # town holds only ONE of the pair the same abbreviation binds it (`nám. Míru` above).
    ambiguous = _bind("nám. Svobody, Kladno", (KLADNO,))
    assert ambiguous.street is None and ambiguous.reason == "ambiguous_streets"
    # ...and the refusal is not undone by the fuzzy rung: a TIE is not a typo, so R3 never
    # runs over the candidates the exact binder just refused.
    assert _line("nám. Svobody, Kladno", town="Kladno").street_name is None


def test_a_value_with_no_separator_takes_the_identical_path():
    """There is ONE binder. A claim with no separator is one segment, so "is this the same
    street" is answered the same way whether or not the portal happened to write a comma —
    including the refusal: the abbreviation that reaches both Kladno rows binds nothing here
    too, where the single-name path used to pick the lower ulice_kod and say nothing."""
    assert _line("náměstí Svobody", town="Kladno").ulice_kod == 112
    assert _line("Svobody", town="Kladno").ulice_kod == 113
    assert _line("nám. Svobody", town="Kladno").street_name is None
    assert _bind("nám. Svobody", (KLADNO,)).reason == "ambiguous_streets"


def test_a_street_whose_name_IS_the_generic_word_still_binds():
    """`Nová ulice`, `Na Ulici`, `I. ulice` — the register spells the generic word into the
    name on those, so the UNFOLDED spelling is a match key of its own. It is also why the
    CLAIM layer strips only the LEADING wrapper: the exact key is taken from the stored value,
    and `Nová ulice` folded down to `Nová` at intake can never bind afterwards."""
    assert composite.street_keys("Nová ulice") == frozenset({"nova ulice", "nova"})
    assert _line("Nová ulice", town="Kladno").street_name == "Nová ulice"
    assert _line("Na Ulici", town="Kladno").street_name == "Na Ulici"
    assert _bind("Prodej bytu, Nová ulice, Kladno", (KLADNO,)).street.code == 114


def test_the_street_index_is_the_same_answer_as_folding_each_row():
    """The per-obec index is an ACCELERATOR and nothing else: `CachedRegistryView` memoizes it
    per run so a four-segment Praha title stops re-folding ~10,000 street names four times
    over, and a view that does not offer one builds the identical dict."""
    mirror = mm.default_mirror()
    built = composite.build_street_index(mirror.streets_in_obec(KLADNO))
    assert composite.street_index(mirror, KLADNO) == built
    assert set(built["svobody"]) == {
        s for s in mirror.streets_in_obec(KLADNO) if s.code in (112, 113)}


def test_a_line_number_never_attaches_to_a_street_bound_from_another_claim():
    """A listing naming `Nad Bořislavkou` and, in a SEPARATE claim, a line reading
    "Livornská 5" published `Nad Bořislavkou 5` at `street_segment` grain, because the line's
    number was written back onto the listing-wide constraints and then lent to whatever street
    the ranking picked. A number belongs to the segment that bound ITS street.

    Two claims naming two different streets is also two answers, so the binder refuses both —
    and the number goes with them rather than surviving on a row with no street at all."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou"),
        mm.claim(3, "street_name", value_text="Prodej bytu 2+kk, Livornská 5, Praha"),
    ])
    assert resolution.street_name is None
    assert resolution.house_number_cp is None
    assert resolution.obec_name == "Praha"


def test_a_house_number_claimed_in_its_own_field_still_reaches_the_address_point():
    """The other direction, and the one that must NOT be broken by the rule above: a portal
    that states the street and the číslo in SEPARATE fields is stating both about the same
    listing, so the number is the listing's and R1 uses it."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou"),
        mm.claim(3, "house_number_cp", value_text="487"),
        mm.claim(4, "house_number_co", value_text="40"),
    ])
    assert resolution.ruian_adm_kod == 21690278
    assert resolution.street_name == "Nad Bořislavkou"
    assert resolution.house_number_cp == "487"


# --------------------------------- the trigram rung, and the claims allowed to reach it

def _headline(value: str, town: str = "Bílovec"):
    """A bazos-shaped claim: the contract declares the title `claim_confidence: low`, meaning
    *a headline, not an address field*."""
    return _resolve([
        mm.claim(1, "obec_name", source="bazos", value_text=town),
        mm.claim(2, "street_name", source="bazos", value_text=value, claim_confidence="low"),
    ])


def _address_field(value: str, town: str = "Bílovec"):
    """A structured street field from a portal that states one. No declaration — R3 as ever."""
    return _resolve([
        mm.claim(1, "obec_name", value_text=town),
        mm.claim(2, "street_name", value_text=value, claim_confidence=None),
    ])


def test_a_headline_is_never_matched_by_similarity():
    """THE defect this rule closes. A trigram run over prose binds a street the ad never
    named: "Byt Slunečná" scores 1.0 against Slunečná, while "Prodej domu Slunečná" scores
    0.429 and binds nothing — so coverage depended on how long the seller's title was, and a
    title that happened to score bound a wrong street. A headline may buy an EXACT register
    match and nothing else."""
    assert _headline("Byt Slunečná").street_name is None
    assert _headline("Prodej domu Slunečná").street_name is None
    # ...while the exact path is untouched: the same headline naming the street outright binds.
    assert _headline("Slunečná").street_name == "Slunečná"
    assert _headline("Prodej bytu 2+kk, Slunečná, Bílovec").street_name == "Slunečná"


def test_a_structured_street_field_keeps_the_trigram_rung():
    """The other half: a portal that states a street IN A STREET FIELD gets R3 exactly as
    before, because there the fuzziness is a typo and not prose. The contract declares the
    quality; the resolver obeys it, and no rule names a portal."""
    assert _address_field("Byt Slunečná").street_name == "Slunečná"
    assert _address_field("Slunecna").street_name == "Slunečná"


def test_the_operators_own_title_still_binds_with_the_fuzzy_rung_off():
    """The ruling's own listing, through the low-confidence path end to end."""
    resolution = _resolve([
        mm.claim(1, "obec_name", source="bazos", value_text="Mladá Boleslav"),
        mm.claim(2, "street_name", source="bazos", claim_confidence="low",
                 value_text="Prodej bytu 3+1 s lodžií, 86 m2, ul. Jiráskova, Mladá Bolesl"),
    ])
    assert resolution.street_name == "Jiráskova"
    assert resolution.ulice_kod == 105
