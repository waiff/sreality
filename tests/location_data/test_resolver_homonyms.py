"""S3 — the three NAMED regression tests of 03 §3.5.3, plus the ambiguity contract.

Czech obec names repeat heavily. The old system's answer was to refuse text→hierarchy
entirely and route everything through "geocode the text, then PIP the coordinate" — which
converts a naming problem into a precision problem, because the geocode of an ambiguous
town name IS the town centroid. D6 reverses it: resolve names locally and hierarchically
inside the constraining parent, and return a SCORED CANDIDATE SET.
"""

from __future__ import annotations

from location_data.resolver import candidates as s3
from location_data.resolver import core, normalize
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm


def _generate(claims, *, pin_is_precise=False):
    ctx = mm.context()
    normalized = normalize.normalize_all(claims)
    return s3.generate(claims, normalized, ctx, source="maxima", pin_is_precise=pin_is_precise)


def _resolve(claims, source="maxima"):
    return core.resolve(
        claims, mm.context(), resolver_version=RESOLVER_VERSION, registry_version_id=7,
        policy_version="v1", collision_epoch_id=11,
    )


# ---------------------------------------------------- regression 1: Krásný Les (maxima)


def test_krasny_les_resolves_via_the_cadastral_name_and_the_okres_claim():
    """maxima f60012522: the description states 'katastrální území Krásný Les u Frýdlantu,
    obec Krásný Les, okres Liberec' while the stored row carried obec Petrovice / okres
    Ústí nad Labem — the OTHER Krásný Les, ~100 km west. Five stored fields wrong at once."""
    claims = [
        mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima"),
        mm.claim(2, "cadastral_territory_name", value_text="Krásný Les u Frýdlantu",
                 source="maxima", extraction_method="regex_text"),
        mm.claim(3, "okres_name", value_text="Liberec", source="maxima",
                 extraction_method="regex_text"),
    ]
    candidate_set, _ = _generate(claims)
    winner = candidate_set.candidates[0]
    assert winner.admin_unit_id == 3  # Krásný Les, okres Liberec
    assert candidate_set.ambiguity_status == "resolved"


def test_krasny_les_with_the_qualifier_alone_still_resolves():
    claims = [
        mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima"),
        mm.claim(2, "homonym_qualifier", value_text="u Frýdlantu", source="maxima",
                 extraction_method="regex_text"),
    ]
    # The qualifier lives on the cadastral name in the gazetteer, so the okres claim is the
    # decisive one; without it the pair stays AMBIGUOUS rather than silently picking.
    candidate_set, _ = _generate(claims)
    assert candidate_set.ambiguity_status == "ambiguous"


def test_krasny_les_with_no_qualifier_is_ambiguous_never_a_silent_pick():
    claims = [mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima")]
    candidate_set, _ = _generate(claims)
    assert candidate_set.ambiguity_status == "ambiguous"
    assert {c.admin_unit_id for c in candidate_set.candidates} == {3, 7}
    assert _resolve(claims).status == "ambiguous"


def test_a_psc_narrows_the_homonym_decisively():
    claims = [
        mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima"),
        mm.claim(2, "psc", value_text="463 46", source="maxima"),
    ]
    candidate_set, _ = _generate(claims)
    assert [c.admin_unit_id for c in candidate_set.candidates][:1] == [3]


# ------------------------------------------------ regression 2: Bílovec (realitymix)


def test_bilovec_matches_through_value_ascii():
    """realitymix 8375963/8375983: `locality_text: 'Bilovec'` — the diacritics-stripped
    form. The CZ municipality lookup missed it and a fuzzy STREET-level fallback landed in
    western Slovakia, ~180 km off, with obec/okres/region/ku_id all NULL. All 16
    out-of-bbox realitymix rows share that one pin."""
    claims = [mm.claim(1, "obec_name", value_text="Bilovec", source="realitymix")]
    candidate_set, _ = _generate(claims)
    assert candidate_set.candidates[0].admin_unit_id == 10
    assert candidate_set.candidates[0].granularity == "obec"


def test_a_fuzzy_street_match_can_never_become_an_obec_resolution():
    """The Bílovec failure mode: a street-level fuzzy hit used as a municipality answer.
    R3 produces STREET candidates only — never an admin-unit one."""
    claims = [
        mm.claim(1, "obec_name", value_text="Bilovec", source="realitymix"),
        mm.claim(2, "street_name", value_text="Slunecna", source="realitymix"),
    ]
    candidate_set, _ = _generate(claims)
    for candidate in candidate_set.candidates:
        if candidate.rung == "R3":
            assert candidate.target_kind == "street"
            assert candidate.granularity in ("street", "street_segment")


# --------------------------------------------- regression 3: Bořislav 40 (GeocodeSOE)


def test_borislav_40_never_resolves_to_the_prague_street_that_contains_the_substring():
    """GeocodeSOE ranks `Nad Bořislavkou 487/40, Vokovice, Praha 6` FIRST for
    `SingleLine=Borislav 40` — a Prague street ~120 km from the correct village, every
    candidate scoring exactly 100. Rank 1 is never consumed; the obec constraint decides."""
    claims = [
        mm.claim(1, "obec_name", value_text="Bořislav", source="maxima"),
        mm.claim(2, "house_number_cp", value_text="40", source="maxima"),
    ]
    candidate_set, _ = _generate(claims)
    assert candidate_set.candidates[0].admin_unit_id == 15  # the village, not Praha
    for candidate in candidate_set.candidates:
        assert candidate.ruian_adm_kod != 21690278  # the Vokovice address point
        assert candidate.ulice_kod != 101  # 'Nad Bořislavkou'


def test_the_candidate_set_is_stored_complete_never_truncated_to_the_winner():
    claims = [mm.claim(1, "obec_name", value_text="Krásný Les", source="maxima")]
    candidate_set, _ = _generate(claims)
    assert len(candidate_set.candidates) == 2
    assert [c.rank for c in candidate_set.candidates] == [1, 2]
