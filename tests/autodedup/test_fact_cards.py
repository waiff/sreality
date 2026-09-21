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
    a = card(1, unit_code="B1.2.1", floor=1)
    b = card(2, unit_code="B2.2.1", floor=2)
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
    a, b = card(1, floor=1), card(2, floor=2)
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


def test_the_content_key_is_stable_and_text_sensitive() -> None:
    assert fact_cards.content_key("abc") == fact_cards.content_key("abc")
    assert fact_cards.content_key("abc") != fact_cards.content_key("abd")
