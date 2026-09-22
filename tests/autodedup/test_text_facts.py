"""The printed-fact parsers the benchmark and the rule floor now share (E60/E61, W8)."""

from __future__ import annotations

import pytest

from autodedup.dataset import Listing, Location, live_end_stamp
from autodedup.text_facts import (
    MAX_CODE_POPULATION,
    address_block_key,
    code_population,
    mask_codes,
    orientations,
    rare_codes,
    reference_codes,
    stated_areas,
    unit_designators,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Pro komunikaci uvádějte evidenční číslo zakázky N115423.", {"N115423"}),
        ("Ev. číslo: 657349", {"657349"}),
        ("evidenční číslo / ID zakázky: R2256", {"R2256"}),
        ("[ID 84553] Prodej bytu", {"84553"}),
        ("Cena 4 500 000 Kč, postaveno 1998", set()),
        ("Ev. č. 123", set()),  # shorter than MIN_CODE_LEN: a per-broker sequence number
    ],
)
def test_reference_codes(text: str, expected: set[str]) -> None:
    assert reference_codes(text) == expected


def test_mask_codes_hides_the_code_in_the_original_text() -> None:
    masked = mask_codes("Nabídka, ev. číslo: 657349, volejte")
    assert "657349" not in (masked or "")
    assert "[KOD]" in (masked or "")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("byt (č.3) v 1.NP", {"3"}),
        ("jednotka č. 12", {"12"}),
        ("s označením B36", {"B36"}),
        ("apartmán č. A311", {"A311"}),
        ("prodej jednotku 4+kk", set()),  # a disposition is never a unit designator
    ],
)
def test_unit_designators(text: str, expected: set[str]) -> None:
    assert unit_designators(text) == expected


def test_code_population_and_the_rarity_cap() -> None:
    population = code_population({i: "Ev. číslo: 03888" for i in range(MAX_CODE_POPULATION + 1)})
    assert population == {"03888": MAX_CODE_POPULATION + 1}
    assert rare_codes({"03888"}, population) == set()
    assert rare_codes({"03888"}, {"03888": MAX_CODE_POPULATION}) == {"03888"}


# --- stated areas -------------------------------------------------------------------------


def test_stated_areas_reads_the_text_not_the_column() -> None:
    assert stated_areas("byt 3+1 o velikosti 72m2 se zahrádkou 20,2 m²") == {72.0, 20.2}


def test_a_building_total_is_not_the_unit_area() -> None:
    """W7 wrong label 83275 x 412654: the sreality body prints only Polygon's 10 500 m²."""
    sreality = "Polygon je moderní budova o velikosti 10.500 m2 pronajímatelné plochy."
    ceskereality = (
        "Nabízíme k pronájmu moderní kancelářské prostory o velikosti 883 m2 v budově. "
        "Objekt disponuje celkovou plochou cca 10 500 m2."
    )
    assert stated_areas(sreality, 883.0) == set()
    assert stated_areas(ceskereality, 883.0) == {883.0}


def test_a_unit_area_beside_the_word_budova_survives() -> None:
    """Listing 464483: `cihlové budovy. S užitnou plochou 80 m2` names the UNIT, not the house."""
    body = "Byt ve 13. patře cihlové budovy. S užitnou plochou 80 m2 nabízí příjemné bydlení."
    assert stated_areas(body, 80.0) == {80.0}


def test_a_size_range_is_a_menu_not_a_fact() -> None:
    body = "Nabízíme pronájem obchodních prostor/ kanceláří od 55m2 do 900m2 v budově."
    assert stated_areas(body, 65.0) == set()


def test_an_enumeration_of_sizes_is_a_menu() -> None:
    body = "Nabídka coworkingových prostor – 10 m2, 20 m2, 30 m2, 50 m2, 60 m2, 75 m2."
    assert stated_areas(body, 25.0) == set()


def test_spaced_thousands_are_one_number_not_their_tail() -> None:
    """`10 500 m2` must never read as 500 — the truncation four portal parsers still carry."""
    assert stated_areas("celková plocha haly je 10 500 m2") == {10500.0}


def test_an_area_far_from_the_stored_one_is_not_this_unit() -> None:
    assert stated_areas("hala o výměře 900 m2, kancelář 65 m2", 65.0) == {65.0}
    assert stated_areas("sklep 4 m2, byt 60 m2", 60.0) == {60.0}


def test_stated_areas_without_a_stored_area_keeps_the_absolute_window() -> None:
    assert stated_areas("byt 60 m2 se sklepem 3 m2") == {60.0}


# --- place and window ---------------------------------------------------------------------


def _listing(**kwargs: object) -> Listing:
    base = dict(id=1, block="b")
    base.update(kwargs)
    return Listing(**base)  # type: ignore[arg-type]


def test_address_block_key_falls_back_through_the_grains() -> None:
    assert address_block_key(_listing(location=Location(ruian_adm_kod=555))) == "ruian:555"
    loose = _listing(location=Location(obec_kod=9, street_key="hartigova", house_number="1116/83"))
    assert address_block_key(loose) == "addr:9:hartigova:1116/83"
    assert address_block_key(_listing(location=Location(obec_kod=9))) == "obec:9"


def test_live_end_stamp_prefers_the_sighting_over_the_detection_stamp() -> None:
    gone = _listing(last_seen_at="2026-08-12T00:00:00+00:00",
                    inactive_at="2026-09-08T00:00:00+00:00", is_active=False)
    assert live_end_stamp(gone) == "2026-08-12T00:00:00+00:00"
    stampless = _listing(last_seen_at=None, inactive_at="2026-09-08T00:00:00+00:00",
                         is_active=False)
    assert live_end_stamp(stampless) == "2026-09-08T00:00:00+00:00"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Orientace obou pokojů je na jihovýchod a na oknech jsou žaluzie.", {"jihovychod"}),
        ("Orientace je na východ, na oknech s trojskly jsou žaluzie.", {"vychod"}),
        # A through-flat names two; the clause is read whole so neither wins.
        ("Byt je orientovaný na jih a západ, díky čemuž je prosvětlený.", {"jih", "zapad"}),
        # "na dvě světové strany" names no direction at all.
        ("Díky orientaci na dvě světové strany byt přirozeně větrá.", set()),
        # An abbreviation is a coin flip; the parser abstains.
        ("Nový byt 2+kk, orientovaný na J/Z, vybavený kuchyňskou linkou.", set()),
        # No keyword, no fact: a body names the direction of the park too.
        ("Sever (ložnice) je vaše oáza klidu, Jih patří obývacímu pokoji.", set()),
        ("", set()),
    ],
)
def test_orientations_reads_only_what_the_body_states_as_this_unit_s(
    text: str, expected: set[str]
) -> None:
    assert orientations(text) == expected


def test_orientations_prefers_the_longer_compass_stem() -> None:
    """`jihovýchod` contains `východ`; reading the short stem would merge two flats."""
    assert orientations("Byt je orientován na jihovýchod.") == {"jihovychod"}
    assert orientations("Byt je orientován na severozápad.") == {"severozapad"}
