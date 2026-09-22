"""The card schema, its prompt contract, and the comparison that does the actual deciding."""

from __future__ import annotations

import json
from typing import Any

import pytest

from autodedup import fact_cards
from autodedup.fact_cards import UnitCard, card_conflicts, conflict_field, parse_card


def card(listing_id: int = 1, **fields: Any) -> UnitCard:
    payload: dict[str, Any] = {"listing_id": listing_id}
    payload.update(fields)
    return UnitCard.from_json(payload, listing_id)


# --- the schema the model is forced through ------------------------------------------------


def test_tool_schema_requires_every_property_so_a_null_is_an_ANSWER() -> None:
    schema = fact_cards.TOOL_SCHEMA["input_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_tool_schema_is_json_serialisable_and_names_its_tool() -> None:
    assert fact_cards.TOOL_SCHEMA["name"] == fact_cards.TOOL_NAME
    json.dumps(fact_cards.TOOL_SCHEMA)


def test_every_comparable_field_is_a_property_of_the_schema() -> None:
    properties = set(fact_cards.TOOL_SCHEMA["input_schema"]["properties"])
    for name in fact_cards.CONFLICT_FIELDS:
        assert name in properties, name


def test_strong_and_weak_fields_do_not_overlap_and_cover_the_conflict_set() -> None:
    assert not set(fact_cards.STRONG_FIELDS) & set(fact_cards.WEAK_FIELDS)
    assert set(fact_cards.CONFLICT_FIELDS) == set(fact_cards.STRONG_FIELDS) | set(
        fact_cards.WEAK_FIELDS
    )


# --- parsing what came back ----------------------------------------------------------------


def test_a_card_of_all_nulls_parses_and_carries_no_comparable_field() -> None:
    parsed = parse_card({name: None for name in fact_cards.CONFLICT_FIELDS}, 7)
    assert parsed.listing_id == 7
    assert parsed.filled() == ()


def test_missing_keys_are_null_not_an_error() -> None:
    parsed = parse_card({}, 9)
    assert parsed.unit_code is None and parsed.area_m2 is None
    assert parsed.is_menu is False


def test_numbers_arrive_as_strings_with_czech_decimal_commas() -> None:
    parsed = parse_card({"area_m2": "34,9", "floor": "2", "total_floors": "7"}, 1)
    assert parsed.area_m2 == pytest.approx(34.9)
    assert parsed.floor == 2 and parsed.total_floors == 7


def test_labelled_lists_fold_to_maps() -> None:
    parsed = parse_card({
        "other_areas": [{"label": "Terasa", "m2": 7.9}, {"label": "sklep", "m2": 3}],
        "accessories": [{"kind": "stání", "designator": "47"}],
    }, 1)
    assert parsed.other_areas == {"terasa": 7.9, "sklep": 3.0}
    assert parsed.accessories == {"stani": ["47"]}


def test_a_non_object_tool_input_is_a_refusal_not_a_card() -> None:
    with pytest.raises(fact_cards.CardParseError):
        parse_card(["nope"], 1)


def test_a_json_string_tool_input_is_decoded() -> None:
    parsed = parse_card(json.dumps({"unit_code": "B1.2.1"}), 3)
    assert parsed.unit_code == "B1.2.1"


def test_a_card_survives_the_artifact_round_trip_the_analysis_reads_it_back_through() -> None:
    first = parse_card({
        "unit_code": "B1.2.1", "floor": 1, "area_m2": 74.0,
        "other_areas": [{"label": "Terasa", "m2": 7.9}],
        "accessories": [{"kind": "stání", "designator": "47"}],
        "parcel_numbers": ["934/11"], "evidence": {"unit_code": "byt B1.2.1"},
    }, 1)
    again = UnitCard.from_json(json.loads(json.dumps(first.to_json())), 1)
    assert again.to_json() == first.to_json()
    assert card_conflicts(first, again) == []


def test_evidence_survives_a_round_trip_and_a_list_evidence_is_dropped() -> None:
    parsed = parse_card({"unit_code": "A", "evidence": {"unit_code": "byt A"}}, 1)
    assert parsed.to_json()["evidence"] == {"unit_code": "byt A"}
    assert parse_card({"evidence": ["nope"]}, 1).evidence == {}


# --- the rule the whole engine runs on -----------------------------------------------------


def test_absence_is_never_a_conflict() -> None:
    a = card(1, unit_code="B1.2.1", floor=2, area_m2=74.0, street="Litovelská")
    assert card_conflicts(a, card(2)) == []
    assert card_conflicts(card(2), a) == []


def test_two_empty_cards_do_not_conflict() -> None:
    assert card_conflicts(card(1), card(2)) == []


def test_comparison_is_symmetric() -> None:
    a = card(1, unit_code="B1.2.1", floor_raw="1. patro")
    b = card(2, unit_code="B2.2.1", floor_raw="2. patro")
    assert len(card_conflicts(a, b)) == len(card_conflicts(b, a)) == 2


# --- the forms four confirmations found ----------------------------------------------------


def test_the_confirmed_false_merge_B1_2_1_vs_B2_2_1_conflicts() -> None:
    reasons = card_conflicts(card(1, unit_code="B1.2.1"), card(2, unit_code="B2.2.1"))
    assert [conflict_field(r) for r in reasons] == ["unit_code"]


@pytest.mark.parametrize(
    "left,right",
    [
        ("Byt B1.2.1", "byt s označením B2.2.1"),
        ("F2.103", "F3.103"),
        ("H2-304", "H2-404"),
        ("A4/15", "A5/15"),
        ("JE24.000", "JE26.000"),
        ("5.13", "5.14"),
        ("II", "III"),
        ("A", "B"),
    ],
)
def test_every_printed_code_form_the_confirmations_found_conflicts(
    left: str, right: str
) -> None:
    assert card_conflicts(card(1, unit_code=left), card(2, unit_code=right))


@pytest.mark.parametrize("left,right", [("B1.2.1", "b1 2 1"), ("A4/15", "A4-15")])
def test_one_code_spelled_two_ways_is_not_a_conflict(left: str, right: str) -> None:
    assert card_conflicts(card(1, unit_code=left), card(2, unit_code=right)) == []


def test_a_coarser_code_never_splits_the_pair_it_is_a_prefix_of() -> None:
    assert card_conflicts(card(1, unit_code="B1"), card(2, unit_code="B1.2.1")) == []


def test_floors_written_in_prose_are_compared_after_normalisation() -> None:
    # `2. NP` and `1. patro` are the SAME floor on two portals' conventions; the model
    # normalises, so the card compares 1 with 1 and finds nothing.
    same = card_conflicts(
        card(1, floor=1, floor_raw="2. NP"), card(2, floor=1, floor_raw="1. patro")
    )
    assert same == []
    differ = card_conflicts(
        card(1, floor=1, floor_raw="2. NP"), card(2, floor=2, floor_raw="3. NP")
    )
    assert [conflict_field(r) for r in differ] == ["floor"]
    assert "2. NP" in differ[0] and "3. NP" in differ[0]


def test_floor_slack_can_be_widened_for_the_measurement() -> None:
    a, b = card(1, floor_raw="1. patro"), card(2, floor_raw="2. patro")
    assert card_conflicts(a, b)
    assert card_conflicts(a, b, floor_slack=1) == []


def test_a_printed_area_gap_beyond_tolerance_conflicts_and_rounding_does_not() -> None:
    assert card_conflicts(card(1, area_m2=28.2), card(2, area_m2=28.0)) == []
    reasons = card_conflicts(card(1, area_m2=159.5), card(2, area_m2=130.6))
    assert [conflict_field(r) for r in reasons] == ["area_m2"]


def test_the_bazos_degenerate_column_cannot_reach_the_card() -> None:
    # bazos stores the TERRACE in area_m2. The card reads the body instead, so the headline
    # areas agree and the terrace lands in its own labelled slot.
    a = card(1, area_m2=75.0, other_areas={"terasa": 12.0})
    b = card(2, area_m2=75.3, other_areas={"terasa": 12.0})
    assert card_conflicts(a, b) == []


def test_a_labelled_other_area_conflicts_only_against_its_own_label() -> None:
    a = card(1, other_areas={"terasa": 7.9, "sklep": 3.0})
    b = card(2, other_areas={"terasa": 20.0})
    reasons = card_conflicts(a, b)
    assert [conflict_field(r) for r in reasons] == ["other_areas"]
    assert "terasa" in reasons[0]


def test_disjoint_parcels_of_one_parcelling_conflict_and_a_shared_one_does_not() -> None:
    assert card_conflicts(
        card(1, parcel_numbers=["934/11"]), card(2, parcel_numbers=["935/4"])
    )
    assert card_conflicts(
        card(1, parcel_numbers=["2224", "729/121"]), card(2, parcel_numbers=["2224"])
    ) == []


def test_a_street_named_only_in_the_body_separates_two_adverts() -> None:
    reasons = card_conflicts(card(1, street="Litovelská"), card(2, street="28. října"))
    assert [conflict_field(r) for r in reasons] == ["street"]


def test_one_street_in_two_inflections_agrees() -> None:
    assert card_conflicts(card(1, street="Krapkova"), card(2, street="Krapkově")) == []


def test_the_serviced_office_tier_separates() -> None:
    reasons = card_conflicts(card(1, capacity_persons=1), card(2, capacity_persons=2))
    assert [conflict_field(r) for r in reasons] == ["capacity_persons"]


def test_offered_extent_one_room_versus_two_joined_rooms_separates() -> None:
    reasons = card_conflicts(card(1, rooms_offered=1), card(2, rooms_offered=2))
    assert [conflict_field(r) for r in reasons] == ["rooms_offered"]


def test_accessory_numbers_separate_within_a_kind_only() -> None:
    a = card(1, accessories={"stani": ["47"]})
    b = card(2, accessories={"stani": ["32"]})
    assert [conflict_field(r) for r in card_conflicts(a, b)] == ["accessories"]
    # A cellar number on one side and a parking number on the other share no kind, so
    # neither is evidence about the other.
    assert card_conflicts(card(1, accessories={"stani": ["47"]}),
                          card(2, accessories={"koje": ["25"]})) == []


def test_one_accessory_number_in_common_is_not_a_conflict() -> None:
    a = card(1, accessories={"stani": ["47", "6"]})
    b = card(2, accessories={"stani": ["6"]})
    assert card_conflicts(a, b) == []


# --- menus ---------------------------------------------------------------------------------


def test_a_menu_suppresses_unit_level_conflicts_but_not_place_level_ones() -> None:
    menu = card(1, is_menu=True, menu_units=["7", "8", "9"], area_m2=351.0,
                parcel_numbers=["7", "8", "9"], locality="Šťáhlavy")
    single = card(2, area_m2=349.0, parcel_numbers=["12"], locality="Šťáhlavy")
    fields = [conflict_field(r) for r in card_conflicts(menu, single)]
    assert "area_m2" not in fields
    assert "parcel_numbers" in fields


def test_a_menu_that_lists_the_other_side_s_code_does_not_conflict_on_it() -> None:
    menu = card(1, is_menu=True, menu_units=["A", "B"], unit_code="A")
    assert card_conflicts(menu, card(2, unit_code="B")) == []


# --- weak versus strong ---------------------------------------------------------------------


def test_project_name_is_a_weak_field_and_is_reported_separately() -> None:
    reasons = card_conflicts(card(1, project_name="Harfa Living"),
                             card(2, project_name="Rokytná Resort"))
    assert reasons and fact_cards.strong_conflicts(reasons) == []


def test_a_stated_unit_code_is_a_strong_field() -> None:
    reasons = card_conflicts(card(1, unit_code="A"), card(2, unit_code="B"))
    assert fact_cards.strong_conflicts(reasons) == reasons


def test_a_place_NAME_is_weak_because_two_portals_spell_it_two_ways() -> None:
    reasons = card_conflicts(card(1, street="Měděná"), card(2, street="Železná"))
    assert reasons and fact_cards.strong_conflicts(reasons) == []


# --- value hygiene: what fc1 wrote into value slots on 1,276 real adverts ------------------
#
# Every string in this block is a value gpt-5-nano actually returned in run 35672879363.


@pytest.mark.parametrize(
    "written",
    ["null", "NULL", "n/a", "N/A", "not provided", "none", "neuvedeno",
     "1+kk?", "B1.2.?", "Botič II?", "3+kk? no", "?",
     "Plzeň - Skvrny? text says Plzeň Skvrňany. The locality should be Plzeň",
     "2+kk? wait 1+1 is. The text says byt 1+1.",
     "Litice? text says Litic and Plzeň-Bory; likely Litic is town. Street null,",
     "Malá Homolka v Plzni, mezi Doudlevcemi a Radobyčicemi"],
)
def test_a_value_the_model_did_not_read_is_a_null(written: str) -> None:
    assert fact_cards.clean_value(written) is None


@pytest.mark.parametrize("written", ["B1.2.1", "3+1", "Plzeň - Skvrňany", "7. patro", "A4/15"])
def test_a_value_the_model_did_read_survives_hygiene(written: str) -> None:
    assert fact_cards.clean_value(written) == written


def test_the_string_null_can_never_conflict_with_a_stated_value() -> None:
    # fc1 wrote the four characters n-u-l-l into 233 unit_codes, and the comparison read them
    # as a stated fact: "unit_code: garáž vs null" split a hand-confirmed pair.
    assert card_conflicts(card(1, unit_code="garáž"), card(2, unit_code="null")) == []
    assert card_conflicts(card(1, floor_raw="2. NP"), card(2, floor_raw="null")) == []


# --- floors: parsed from the printed string, not from the model's integer -----------------


@pytest.mark.parametrize(
    "printed,expected",
    [
        ("7. patro", 7), ("7. patře", 7), ("3. patro", 3), ("1. patře", 1),
        ("první patro", 1), ("pátém patře", 5), ("patém patře", 5), ("třetím patře", 3),
        ("1. nadzemní podlaží", 0), ("1. nadzemním podlaží", 0), ("2. NP", 1),
        ("2.NP", 1), ("2NP", 1), ("4. NP", 3), ("6. nadzemní podlaží", 5),
        ("6. nadzemním podlaží", 5), ("III. NP", 2),
        ("přízemí", 0), ("zvýšeném přízemí", 0), ("parter", 0),
        ("suterén", -1), ("1. PP", -1), ("-1. patro", -1),
        ("mezonet 4./5. patro", 4), ("2. patro (v podkroví)", 2),
    ],
)
def test_every_printed_floor_form_parses_to_one_integer(printed: str, expected: int) -> None:
    assert fact_cards.parse_floor(printed) == expected


@pytest.mark.parametrize("printed", ["null", "3", "4/4", "nejvyšším podlaží", ""])
def test_a_floor_string_that_states_no_convention_is_not_a_floor(printed: str) -> None:
    assert fact_cards.parse_floor(printed) is None


@pytest.mark.parametrize(
    "left,right",
    [("7. patro", "7. patře"), ("6. nadzemní podlaží", "6. nadzemním podlaží"),
     ("2. NP", "1. patro"), ("přízemí", "1. NP"), ("2.NP", "2. nadzemním podlaží")],
)
def test_one_floor_declined_twice_is_not_a_conflict(left: str, right: str) -> None:
    assert card_conflicts(card(1, floor_raw=left), card(2, floor_raw=right)) == []


def test_the_models_own_floor_integer_is_not_evidence_of_a_floor() -> None:
    # It disagreed with the string printed beside it on 232 of 458 fc1 cards ("3. patro" -> 1).
    assert card_conflicts(card(1, floor=1), card(2, floor=7)) == []
    assert fact_cards.normalise(card(1, floor=4, floor_raw="3. patro")).floor == 3


# --- places: deaccented, de-prepositioned, inflection-tolerant ---------------------------


def test_place_tokens_drop_the_noun_the_preposition_and_the_punctuation() -> None:
    assert fact_cards.place_tokens("Plzeň – Skvrňany") == ("plzen", "skvrnany")
    assert fact_cards.place_tokens("ulici Univerzitní") == ("univerzitni",)
    assert fact_cards.place_tokens("Město Touškov") == ("touskov",)
    assert fact_cards.place_tokens("Olomouc-město") == ("olomouc",)


@pytest.mark.parametrize(
    "left,right",
    [
        ("Plzeň", "Plzni"), ("Plzeň - Skvrňany", "Plzeň Skvrňany"),
        ("Plzeň", "Plzeň 3"), ("Touškov", "Město Touškov"),
        ("Krapkova", "Krapkově"), ("ulici Univerzitní", "Univerzitní ulice"),
        ("Bohdalovice, Velké Hamry", "Velké Hamry"), ("Olomouc", "Olomouc-město"),
    ],
)
def test_one_place_printed_two_ways_is_not_a_conflict(left: str, right: str) -> None:
    assert card_conflicts(card(1, locality=left), card(2, locality=right)) == []


@pytest.mark.parametrize(
    "left,right",
    [
        ("Plzeň - Skvrňany", "Plzeň - Slovany"), ("Praha 9 - Hloubětín", "Praha 9 - Vysočany"),
        ("Olomouc", "Olomučany"), ("Dobřany", "Chotěšov"), ("Šternberk", "Šťáhlavy"),
    ],
)
def test_two_places_still_conflict_after_normalisation(left: str, right: str) -> None:
    assert card_conflicts(card(1, locality=left), card(2, locality=right))


def test_stem_tolerance_can_be_switched_off_for_the_measurement() -> None:
    a, b = card(1, locality="Plzeň"), card(2, locality="Plzni")
    assert card_conflicts(a, b) == []
    assert card_conflicts(a, b, stem_tolerance=False)


def test_a_street_named_only_in_the_body_still_separates_two_adverts() -> None:
    assert card_conflicts(card(1, street="Litovelská"), card(2, street="28. října"))


# --- dispositions ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written,expected",
    [("2+kk", "2+kk"), ("2kk", "2+kk"), ("3 kk", "3+kk"), ("2 + kk", "2+kk"),
     ("3KK", "3+kk"), ("1+ kk", "1+kk"), ("3+1", "3+1"),
     ("3+1 (přízemí, 1. patro, podkroví)", "3+1"), ("3+1+šatna", "3+1")],
)
def test_the_canonical_disposition_grammar(written: str, expected: str) -> None:
    assert fact_cards.canonical_disposition(written) == expected


@pytest.mark.parametrize(
    "written",
    ["garsoniéra", "atypický", "4 pokoje", "nebytový prostor", "2+0", "Patrový",
     "1kk a 2kk", "2+1 (3kk)", "4+1 a 3+1 v domě", "null"],
)
def test_a_layout_outside_the_grammar_is_not_a_comparable_fact(written: str) -> None:
    assert fact_cards.canonical_disposition(written) is None


def test_one_disposition_printed_with_its_rooms_listed_is_not_a_conflict() -> None:
    assert card_conflicts(
        card(1, disposition="3+1 (přízemí, 1. patro, podkroví)"),
        card(2, disposition="3+1"),
    ) == []


# --- designators: a printed code, never a layout and never an object ---------------------


@pytest.mark.parametrize(
    "written,expected",
    [("B1.2.1", "b1.2.1"), ("Byt B1.2.1", "b1.2.1"),
     ("byt s označením B2.2.1", "b2.2.1"), ("F2.103", "f2.103"), ("5.13", "5.13"),
     ("A4/15", "a4/15"), ("H2-304", "h2-304"), ("JE24.000", "je24.000"),
     ("G4", "g4"), ("apartmán č. 4", "4"), ("Vila II", "ii"), ("jednotka A", "a"),
     ("C", "c"), ("8", "8"), ("garáž č. 3", "3")],
)
def test_a_printed_designator_is_accepted(written: str, expected: str) -> None:
    assert fact_cards.designator(written) == expected


@pytest.mark.parametrize(
    "written",
    ["3+1", "2+kk", "1+kk", "1+KK", "2kk", "1+kk?", "Botič 1+kk?", "B1.2.?", "Botič II?",
     "garáž", "garáže", "Pozemek", "parcela s chatou?", "Dřevařská", "Tvarožník",
     "null", "NULL", "not provided", "n/a", "?", "bytového domu", "bytový dům",
     "BYDLENÍ V OLŠINKÁCH?", "Willerby Winchester"],
)
def test_what_is_not_a_designator_is_not_a_unit_code(written: str) -> None:
    assert fact_cards.designator(written) is None


def test_the_confirmed_codes_still_conflict_and_a_layout_in_the_slot_no_longer_does() -> None:
    assert card_conflicts(card(1, unit_code="Byt B1.2.1"),
                          card(2, unit_code="byt s označením B2.2.1"))
    assert card_conflicts(card(1, unit_code="3+1"), card(2, unit_code="2+kk")) == []
    assert card_conflicts(card(1, unit_code="garáž"), card(2, unit_code="Pozemek")) == []


def test_a_block_named_without_a_letter_names_no_block() -> None:
    assert fact_cards.designator("bytového domu") is None
    assert card_conflicts(card(1, building_block="bytového domu"),
                          card(2, building_block="budova B")) == []
    assert card_conflicts(card(1, building_block="budovy B"),
                          card(2, building_block="budova G"))


# --- numbers: areas, counts, accessories, house numbers ----------------------------------


def test_a_printed_area_that_rounds_to_the_same_metre_is_not_a_conflict() -> None:
    assert card_conflicts(card(1, area_m2=74.5), card(2, area_m2=75.0)) == []
    assert card_conflicts(card(1, area_m2=159.5), card(2, area_m2=130.6))


def test_a_two_square_metre_headline_area_is_a_misread_not_a_fact() -> None:
    assert fact_cards.normalise(card(1, area_m2=2.0)).area_m2 is None
    assert card_conflicts(card(1, area_m2=50.0), card(2, area_m2=2.0)) == []


def test_an_area_of_zero_is_not_a_stated_area() -> None:
    assert fact_cards.normalise(card(1, other_areas={"lodzie": 0.0})).other_areas == {}
    assert card_conflicts(card(1, other_areas={"lodzie": 2.0}),
                          card(2, other_areas={"lodzie": 0.0})) == []


def test_a_count_of_zero_is_not_a_count() -> None:
    assert card_conflicts(card(1, rooms_offered=3), card(2, rooms_offered=0)) == []
    assert card_conflicts(card(1, rooms_offered=3), card(2, rooms_offered=2))


def test_an_accessory_the_advert_did_not_number_is_not_an_accessory() -> None:
    assert card_conflicts(card(1, accessories={"skrin": ["předsíni"]}),
                          card(2, accessories={"skrin": ["v předsíni"]})) == []
    assert card_conflicts(card(1, accessories={"garazove stani": ["ne"]}),
                          card(2, accessories={"garazove stani": ["3"]})) == []
    assert card_conflicts(card(1, accessories={"stani": ["47"]}),
                          card(2, accessories={"stani": ["32"]}))


def test_a_house_number_missing_its_second_part_is_the_same_house() -> None:
    assert card_conflicts(card(1, house_number="2842/1"), card(2, house_number="2842")) == []
    assert card_conflicts(card(1, house_number="č.p. 467"), card(2, house_number="467")) == []
    assert card_conflicts(card(1, house_number="2842/1"), card(2, house_number="2842/2"))


# --- orientation -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written,expected",
    [("jihozápad", "jz"), ("jiho-západ", "jz"), ("JZ", "jz"), ("jih", "j"),
     ("jižní", "j"), ("již", "j"), ("Severní orientace", "s"), ("severovýchod", "sv"),
     ("jihovýchodně", "jv"), ("východní", "v")],
)
def test_one_compass_direction_written_many_ways(written: str, expected: str) -> None:
    assert fact_cards.canonical_orientation(written) == expected


@pytest.mark.parametrize(
    "written",
    ["sever jih", "orientace do klidného vnitrobloku – byt je velmi světlý a tichý", "null"],
)
def test_an_orientation_that_is_not_one_direction_is_null(written: str) -> None:
    assert fact_cards.canonical_orientation(written) is None


def test_one_orientation_spelled_two_ways_is_not_a_conflict() -> None:
    assert card_conflicts(card(1, orientation="jihozápad"),
                          card(2, orientation="jiho-západ")) == []
    assert card_conflicts(card(1, orientation="jihozápad"), card(2, orientation="jih"))


# --- the normalised card -----------------------------------------------------------------


def test_normalising_twice_changes_nothing_the_first_pass_did_not() -> None:
    printed = card(
        1, unit_code="Byt B1.2.1", floor_raw="3. patro", floor=1, area_m2=74.0,
        locality="Plzeň – Skvrňany", street="ul. Litovelská", disposition="2 + kk",
        orientation="jiho-západ", house_number="č.p. 467",
        other_areas={"terasa": 7.9, "lodzie": 0.0},
        accessories={"stani": ["47", "ne"]},
    )
    once = fact_cards.normalise(printed)
    assert fact_cards.normalise(once).to_json() == once.to_json()
    assert once.floor == 3 and once.unit_code == "b1.2.1"
    assert once.disposition == "2+kk" and once.orientation == "jz"
    assert once.other_areas == {"terasa": 7.9} and once.accessories == {"stani": ["47"]}


def test_the_conflict_is_reported_with_what_the_advert_printed() -> None:
    reasons = card_conflicts(card(1, unit_code="Byt B1.2.1"),
                             card(2, unit_code="byt s označením B2.2.1"))
    assert reasons == ["unit_code: Byt B1.2.1 vs byt s označením B2.2.1"]


def test_the_printed_card_is_never_mutated_by_a_comparison() -> None:
    printed = card(1, unit_code="3+1", floor_raw="3. patro", floor=1)
    before = printed.to_json()
    card_conflicts(printed, card(2, unit_code="2+kk", floor_raw="4. patro", floor=1))
    assert printed.to_json() == before


# --- the prompt -------------------------------------------------------------------------------


def test_prompt_text_runs_the_judges_scrubber_so_no_contact_crosses_the_wire() -> None:
    text, truncated = fact_cards.prompt_text(
        "Volejte 777 123 456 nebo pište na jan.novak@example.cz, byt B1.2.1"
    )
    assert text is not None and not truncated
    assert "777 123 456" not in text and "example.cz" not in text
    assert "B1.2.1" in text


def test_prompt_text_caps_a_runaway_body_and_says_so() -> None:
    text, truncated = fact_cards.prompt_text("a" * 9000, max_chars=500)
    assert truncated and text is not None and len(text) == 500


def test_an_empty_or_whitespace_body_is_no_text_at_all() -> None:
    assert fact_cards.prompt_text(None) == (None, False)
    assert fact_cards.prompt_text("   \n ") == (None, False)


def test_the_cap_is_generous_enough_that_a_card_is_not_a_judges_digest() -> None:
    assert fact_cards.TEXT_MAX_CHARS >= 4000


def test_build_messages_names_the_listing_and_carries_the_text() -> None:
    messages = fact_cards.build_messages(4242, "byt B1.2.1 ve 2. NP", truncated=False)
    assert len(messages) == 1 and messages[0]["role"] == "user"
    blob = messages[0]["content"][0]["text"]
    assert "4242" in blob and "B1.2.1" in blob
    assert fact_cards.TRUNCATED_NOTE not in blob


def test_build_messages_marks_a_truncated_body() -> None:
    blob = fact_cards.build_messages(1, "x", truncated=True)[0]["content"][0]["text"]
    assert fact_cards.TRUNCATED_NOTE in blob


def test_the_system_prompt_forbids_guessing_and_demands_verbatim_evidence() -> None:
    prompt = fact_cards.SYSTEM_PROMPT
    assert "never guess" in prompt.lower() or "Never infer" in prompt
    assert "VERBATIM" in prompt
    assert "EXTRACTOR, not a judge" in prompt
    # The ground-floor convention is the one normalisation the model is asked to perform.
    assert "GROUND = 0" in prompt


def test_both_prompt_versions_stay_available_and_fc2_is_the_default() -> None:
    # A card is cached for ever on (listing_id, content hash, prompt version), so fc1 must
    # still be askable: the 1,276 cards already paid for were read under it.
    assert set(fact_cards.PROMPTS) == {"fc1", "fc2"}
    assert fact_cards.PROMPT_VERSION == "fc2"
    assert fact_cards.SYSTEM_PROMPT is fact_cards.PROMPTS["fc2"].system
    assert fact_cards.TOOL_SCHEMA is fact_cards.PROMPTS["fc2"].tool_schema
    assert fact_cards.prompt_for("fc1").system != fact_cards.prompt_for("fc2").system


def test_an_unknown_prompt_version_is_refused_rather_than_silently_defaulted() -> None:
    with pytest.raises(SystemExit, match="unknown prompt_version"):
        fact_cards.prompt_for("fc9")


def test_fc2_names_the_four_things_that_are_not_values() -> None:
    prompt = fact_cards.PROMPTS["fc2"].system
    assert "A FIELD IS A VALUE OR IT IS null" in prompt
    for negative in ("3+1", "garáž", "question mark", "text says", "n-u-l-l"):
        assert negative in prompt, negative
    assert "NEVER a layout" in prompt and "DESIGNATOR of ONE unit" in prompt


def test_fc2_keeps_the_forced_schema_contract_and_tightens_it() -> None:
    schema = fact_cards.PROMPTS["fc2"].tool_schema["input_schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert "pattern" in schema["properties"]["unit_code"]
    assert schema["properties"]["disposition"]["pattern"] == r"^[0-9]+\+(?:kk|1)$"
    assert None in schema["properties"]["orientation"]["enum"]
    for name in fact_cards.ORIENTATIONS:
        assert fact_cards.canonical_orientation(name) is not None, name
    evidence = schema["properties"]["evidence"]
    assert set(evidence["required"]) == set(evidence["properties"])
    assert all(
        spec["type"] == ["string", "null"] for spec in evidence["properties"].values()
    )
    json.dumps(fact_cards.PROMPTS["fc2"].tool_schema)


def test_fc1_carries_none_of_fc2s_constraints_so_the_paid_cards_stay_reproducible() -> None:
    encoded = json.dumps(fact_cards.PROMPTS["fc1"].tool_schema)
    assert "pattern" not in encoded and "enum" not in encoded
    assert "required" not in fact_cards.PROMPTS["fc1"].tool_schema[
        "input_schema"]["properties"]["evidence"]


def test_the_content_key_is_stable_and_text_sensitive() -> None:
    assert fact_cards.content_key("abc") == fact_cards.content_key("abc")
    assert fact_cards.content_key("abc") != fact_cards.content_key("abd")
