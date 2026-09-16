"""W2 Task A — text normalisation, sketching and Czech fact extraction."""

from __future__ import annotations

import pytest

from autodedup.normalize import (
    MASK64,
    _simhash64_wide,
    attr_token,
    canonical_attr,
    disposition_norm,
    normalize_text,
    numeric_facts,
    rare_tokens,
    shingles,
    simhash64,
    simhash_bands,
    token_hash,
    tokens,
)


def test_normalize_text_deaccents_lowers_and_folds_punctuation() -> None:
    assert normalize_text("Prodej bytu 3+kk, Turnov — 68 m²!") == "prodej bytu 3 kk turnov 68 m2"


def test_normalize_text_handles_none_and_empty() -> None:
    assert normalize_text(None) == ""
    assert normalize_text("   ") == ""


def test_tokens_is_idempotent_over_normalisation() -> None:
    raw = "Náměstí 28. října, Praha"
    assert tokens(raw) == tokens(normalize_text(raw))
    assert tokens(raw) == ["namesti", "28", "rijna", "praha"]


def test_token_hash_is_stable_and_64_bit() -> None:
    first = token_hash("turnov")
    assert first == token_hash("turnov")
    assert 0 <= first <= MASK64
    assert token_hash("turnov") != token_hash("turnove")


def test_shingles_are_hashed_trigrams() -> None:
    toks = ["a", "b", "c", "d"]
    assert len(shingles(toks)) == 2
    assert shingles(toks) == shingles(list(toks))
    assert shingles(toks, k=2) != shingles(toks, k=3)


def test_shingles_of_a_short_document_yield_one_gram() -> None:
    assert len(shingles(["a", "b"])) == 1
    assert shingles([]) == set()


def test_shingles_reject_a_non_positive_k() -> None:
    with pytest.raises(ValueError):
        shingles(["a"], k=0)


def test_simhash_is_stable_signed_and_order_independent() -> None:
    toks = tokens("prodej bytu 3+kk v Turnove s vyhledem do zahrady")
    first = simhash64(toks)
    assert first == simhash64(toks)
    assert first == simhash64(list(reversed(toks)))
    assert -(1 << 63) <= first < (1 << 63)
    assert simhash64([]) == 0


def test_simhash_moves_little_for_a_near_duplicate_document() -> None:
    base = tokens("prodej bytu 3+kk v Turnove " * 12)
    tweaked = tokens("prodej bytu 3+kk v Turnove " * 12 + " s balkonem")
    distance = ((simhash64(base) & MASK64) ^ (simhash64(tweaked) & MASK64)).bit_count()
    assert distance <= 8


def test_simhash_bands_split_the_hash_low_to_high() -> None:
    value = 0x1111222233334444
    bands = simhash_bands(value, 4, 16)
    assert bands == [(0, 0x4444), (1, 0x3333), (2, 0x2222), (3, 0x1111)]


def test_simhash_bands_mask_a_signed_hash() -> None:
    signed = -1
    assert simhash_bands(signed, 4, 16) == [(band, 0xFFFF) for band in range(4)]


def test_simhash_bands_reject_an_oversized_split() -> None:
    with pytest.raises(ValueError):
        simhash_bands(1, 5, 16)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3+kk", "3+kk"),
        ("3 + KK", "3+kk"),
        ("4+1", "4+1"),
        ("3+2", "3+2"),  # the scrapers extract (\\d)\\+(kk|\\d) — a stored 3+2 must stay known
        ("2 + 3", "2+3"),
        ("Byt 2+kk s balkonem", "2+kk"),
        ("garsoniéra", "1+kk"),
        ("Garsonka", "1+kk"),
        ("atypický", "atypicky"),
        ("pokoj", "pokoj"),
        ("", None),
        (None, None),
        ("rodinný dům", None),
    ],
)
def test_disposition_norm_table(raw: str | None, expected: str | None) -> None:
    assert disposition_norm(raw) == expected


def test_numeric_facts_on_a_czech_listing_line() -> None:
    facts = numeric_facts("byt 3+kk, 68 m², 4. patro, cena 5 990 000 Kč")
    assert facts == {("rooms", 3.0), ("m2", 68.0), ("floor", 4.0), ("kc", 5990000.0)}


def test_numeric_facts_read_decimal_commas_and_ground_floor() -> None:
    facts = numeric_facts("Plocha 68,5 m2, přízemí, 12 500 Kč/měsíc")
    assert ("m2", 68.5) in facts
    assert ("floor", 0.0) in facts
    assert ("kc", 12500.0) in facts


def test_numeric_facts_read_dot_grouped_thousands() -> None:
    # "10.000 Kč" is ordinary portal spelling; the old pattern read it as ("kc", 0.0), a
    # fabricated numeral that decide.py turns into a hard auto-reject.
    assert numeric_facts("Cena 10.000 Kč měsíčně") == {("kc", 10000.0)}
    assert numeric_facts("Cena 10 000 Kč měsíčně") == {("kc", 10000.0)}
    assert ("kc", 3913.0) in numeric_facts("nájem 3.913 Kč")
    assert ("kc", 5000.0) in numeric_facts("5.000Kč")
    assert ("kc", 12500.5) in numeric_facts("12 500,50 Kč")


def test_simhash_lane_accumulation_matches_the_per_bit_reference() -> None:
    toks = tokens("prodej bytu 3+kk v Turnove s vyhledem do zahrady " * 7)
    assert simhash64(toks) == _simhash64_wide(toks)


def test_numeric_facts_read_a_unit_number_but_not_a_disposition() -> None:
    assert ("unit", 12.0) in numeric_facts("jednotka č. 12 ve 2. NP")
    assert ("floor", 2.0) in numeric_facts("jednotka č. 12 ve 2. NP")
    assert not [fact for fact in numeric_facts("byt 3+kk") if fact[0] == "unit"]


def test_numeric_facts_of_empty_text() -> None:
    assert numeric_facts("") == set()


def test_rare_tokens_are_the_low_document_frequency_ones() -> None:
    df = {"prodej": 40, "turnov": 9, "vinohradska": 2, "jednotka": 1}
    toks = ["prodej", "turnov", "vinohradska", "jednotka", "prodej"]
    assert rare_tokens(toks, df, 2) == {"vinohradska", "jednotka"}


def test_rare_tokens_treat_an_unseen_token_as_rare() -> None:
    assert rare_tokens(["novotoken"], {}, 2) == {"novotoken"}


def test_attr_token_is_one_spelling_per_stored_value() -> None:
    assert attr_token("Ve výstavbě (hrubá stavba)") == "ve_vystavbe_hruba_stavba"
    assert attr_token("  Velmi Dobrý ") == "velmi_dobry"
    assert attr_token(True) == "true" and attr_token(False) == "false"
    assert attr_token(None) == "" and attr_token(4) == "4"


def test_canonical_attr_folds_portal_synonyms_onto_one_token() -> None:
    """Measured on the W4 cohort (2026-09-16): each alias is a spelling the portals use for ONE
    fact, and each lifts the contradiction ratio between judged duplicates and non-duplicates."""
    assert canonical_attr("condition", "ve_vystavbe_(hruba_stavba)") == "ve_vystavbe"
    assert canonical_attr("condition", "urceny_k_demolici") == "k_demolici"
    assert canonical_attr("condition", "velmi_dobry") == canonical_attr("condition", "dobry")
    assert canonical_attr("building_type", "zdeny") == "cihla"
    assert canonical_attr("furnished", "ano") == canonical_attr("furnished", "castecne")
    assert canonical_attr("furnished", "ne") == "nevybaveno"


def test_canonical_attr_reads_an_unknown_value_as_absence() -> None:
    """`jiná` / `neuvedeno` is what a portal says when it does not know; E12 says that is
    absence, never a mismatch. bazos also writes a CONDITION into the building-type slot."""
    assert canonical_attr("building_type", "jina") is None
    assert canonical_attr("building_type", "novostavba") is None
    assert canonical_attr("condition", "neuvedeno") is None
    assert canonical_attr("ownership", None) is None
    assert canonical_attr("condition", "") is None


def test_canonical_attr_passes_an_unlisted_value_through() -> None:
    """A closed alias map, not a closed vocabulary: a portal's new value still compares."""
    assert canonical_attr("building_type", "Panel") == "panel"
    assert canonical_attr("ownership", "osobní") == "osobni"
    assert canonical_attr("condition", "novy_zvlastni_stav") == "novy_zvlastni_stav"
    assert canonical_attr("energy_rating", "B") == "b"


# The full per-slot vocabulary of the W4 cohort (export 35096363646, 5,402 listings, 2026-09-16),
# every distinct `attr_token` with its row count. `ATTR_ALIASES` is keyed on EXACT tokens, so a
# portal that relabels falls through to a silent contradiction rather than to an error — this
# fixture is the tripwire: re-run the census when a portal is added and update both together.
COHORT_ATTR_VOCABULARY: dict[str, dict[str, int]] = {
    "condition": {
        "velmi_dobry": 1938, "novostavba": 1007, "dobry": 733, "po_rekonstrukci": 437,
        "pred_rekonstrukci": 98, "ve_vystavbe": 85, "projekt": 20, "spatny": 7,
        "v_rekonstrukci": 6, "ve_vystavbe_hruba_stavba": 6, "k_demolici": 3,
        "urceny_k_demolici": 2, "udrzovany": 1,
    },
    "building_type": {
        "cihla": 2203, "smisena": 754, "skelet": 583, "panel": 515, "jina": 123, "drevo": 61,
        "montovana": 23, "kamen": 13, "zdeny": 1, "novostavba": 1,
    },
    "furnished": {"castecne": 813, "ano": 702, "ne": 692},
    "ownership": {"osobni": 2673, "druzstevni": 261, "statni": 14},
}

# What each slot means AFTER canonicalisation: the closed set the comparison is allowed to see.
CANONICAL_ATTR_VOCABULARY: dict[str, set[str]] = {
    "condition": {
        "novostavba", "dobry", "po_rekonstrukci", "pred_rekonstrukci",
        "ve_vystavbe", "projekt", "spatny", "v_rekonstrukci", "k_demolici",
    },
    "building_type": {"cihla", "smisena", "skelet", "panel", "drevo", "montovana", "kamen"},
    "furnished": {"vybaveno", "nevybaveno"},
    "ownership": {"osobni", "druzstevni", "statni"},
}


def test_the_alias_map_covers_the_whole_measured_vocabulary() -> None:
    """Every spelling the cohort contains folds onto a KNOWN canonical token or onto absence.

    The map is pinned to one census; without this test a portal's relabel would be invisible."""
    for slot, values in COHORT_ATTR_VOCABULARY.items():
        canonical = {canonical_attr(slot, value) for value in values}
        assert canonical - {None} == CANONICAL_ATTR_VOCABULARY[slot], slot
    # `velmi_dobry` is 1,938 of the cohort's 4,343 condition values and it is NOT a grade any
    # more: the alias retires it onto `dobry`. That is a domain judgement, the largest one in the
    # map, and this assertion is where it is written down.
    assert canonical_attr("condition", "velmi_dobry") == "dobry"
    assert "velmi_dobry" not in CANONICAL_ATTR_VOCABULARY["condition"]
