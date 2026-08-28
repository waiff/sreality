"""The two renderings of one definition.

The load-bearing test here is the leak test. The operator reported being confused
by `counts` / `does_not_count` / `confusable_with` / `leave_out_when` — the storage
vocabulary — and the card exists so those words never reach a person labeling.
That is a property nobody can eyeball once the renderer grows, so it is asserted.
"""

from __future__ import annotations

import json
from typing import Any

from toolkit import tag_definition_render as r

# Exercises every field at once, including a reference to a tag that no longer
# exists (36 is resolved, 999 deliberately is not).
FULL: dict[str, Any] = {
    "means": "An image of a bathroom or public shower room.",
    "counts": ["A bathroom with a shower", "A bathroom with a bathtub", "Public showers"],
    "does_not_count": [
        {"case": "An image taken from well above ground level", "goes_to_tag_id": 4},
        {"case": "A door to a building that is not obviously residential",
         "goes_to_tag_id": None},
    ],
    "confusable_with": [
        {"tag_id": 36, "tell": "a solo toilet with no hint of a bathroom around it"},
        {"tag_id": 999, "tell": "an orphaned reference"},
    ],
    "leave_out_when": "you cannot tell a bathroom sink-room from a public toilet",
    "referenced_tags": [
        {"tag_id": 4, "label": "exterier - letecký snímek"},
        {"tag_id": 36, "label": "interier - wc"},
    ],
}


# --- the leak rail ----------------------------------------------------------


def test_the_card_never_shows_a_storage_field_name() -> None:
    card = r.render_card(FULL, tag_label="interier - koupelna")
    blob = json.dumps(card, ensure_ascii=False)
    for name in r.STORAGE_FIELD_NAMES:
        assert name not in blob, f"card leaked the storage field name {name!r}"


def test_the_card_keys_are_not_named_after_the_columns() -> None:
    # A card whose keys mirror the row would leak the vocabulary through the UI's
    # own markup even when the VALUES are clean.
    assert set(r.render_card(FULL, tag_label="x")) == {
        "tag_label", "headline", "count_it", "dont_count_it", "cant_tell",
    }


# --- the card ---------------------------------------------------------------


def test_the_headline_is_a_question() -> None:
    card = r.render_card(FULL, tag_label="interier - koupelna")
    assert card["headline"].endswith("?")
    assert "interier - koupelna" in card["headline"]


def test_a_definition_with_only_a_means_still_has_something_to_count() -> None:
    # The common early shape. An empty "count it" would read as "nothing counts".
    card = r.render_card({"means": "Bedroom - a room with a bed."}, tag_label="ložnice")
    assert card["count_it"] == ["Bedroom - a room with a bed."]


def test_both_kinds_of_exclusion_land_in_one_list() -> None:
    # does_not_count vs confusable_with is a property of how the RULE works, not of
    # what the labeler does — in both cases the answer is no.
    card = r.render_card(FULL, tag_label="interier - koupelna")
    assert len(card["dont_count_it"]) == 4


def test_an_exclusion_says_where_the_photo_belongs_instead() -> None:
    card = r.render_card(FULL, tag_label="interier - koupelna")
    assert any("exterier - letecký snímek" in line for line in card["dont_count_it"])
    assert any("interier - wc" in line for line in card["dont_count_it"])


def test_an_unresolvable_reference_degrades_to_its_bare_text() -> None:
    # _referenced_tags drops ids that no longer exist by design, so a definition
    # can point at a deleted tag. That must not crash or render an empty quote.
    card = r.render_card(FULL, tag_label="interier - koupelna")
    assert "an orphaned reference" in card["dont_count_it"]
    assert '““' not in json.dumps(card) and '""' not in json.dumps(card)


def test_a_redirect_with_no_target_keeps_the_case_alone() -> None:
    card = r.render_card(FULL, tag_label="interier - koupelna")
    assert "A door to a building that is not obviously residential" in card["dont_count_it"]


def test_leave_out_when_is_the_only_cant_tell_entry() -> None:
    card = r.render_card(FULL, tag_label="interier - koupelna")
    assert card["cant_tell"] == [
        "you cannot tell a bathroom sink-room from a public toilet"
    ]


def test_a_missing_leave_out_when_is_an_empty_list_not_a_blank_line() -> None:
    assert r.render_card({"means": "x"}, tag_label="t")["cant_tell"] == []


def test_an_empty_definition_still_renders() -> None:
    # Nothing here should require a field to exist: definitions are authored
    # incrementally, and a half-written one must still preview.
    card = r.render_card({}, tag_label="garáž")
    assert card["headline"].endswith("?")
    assert card["count_it"] == [] and card["dont_count_it"] == []


# --- the prompt -------------------------------------------------------------


def test_the_prompt_keeps_the_distinction_the_card_collapses() -> None:
    # The model acts on them differently: a confusable names a rival to weigh, a
    # does-not-count is unconditional.
    prompt = r.render_prompt(FULL, tag_label="interier - koupelna")
    assert "DOES NOT COUNT:" in prompt and "EASILY CONFUSED WITH:" in prompt


def test_the_prompt_forbids_a_no_that_is_really_a_preference() -> None:
    # The operator's own rule from the brief: a tag whose subject is clearly present
    # must never be marked negative merely because another tag fits better.
    prompt = r.render_prompt(FULL, tag_label="x")
    assert "Never answer no because a different tag fits better" in prompt


def test_the_prompt_asks_for_the_three_states_the_store_holds() -> None:
    prompt = r.render_prompt(FULL, tag_label="x")
    assert "yes / no / unsure" in prompt


def test_the_prompt_names_the_rival_tag_by_label() -> None:
    assert 'vs "interier - wc"' in r.render_prompt(FULL, tag_label="x")


def test_the_prompt_omits_sections_a_definition_does_not_have() -> None:
    prompt = r.render_prompt({"means": "A kitchen."}, tag_label="kuchyně")
    assert "COUNTS AS THIS TAG:" not in prompt
    assert "DOES NOT COUNT:" not in prompt
    assert "MEANS: A kitchen." in prompt


# --- one source ------------------------------------------------------------


def test_both_renderings_carry_the_same_counts() -> None:
    # The point of one source: a rule the operator reads must be the same rule the
    # model is given. A separately-authored guide would drift within a week.
    card = r.render_card(FULL, tag_label="interier - koupelna")
    prompt = r.render_prompt(FULL, tag_label="interier - koupelna")
    for entry in FULL["counts"]:
        assert entry in card["count_it"]
        assert entry in prompt


def test_both_renderings_carry_every_exclusion_case() -> None:
    card = json.dumps(r.render_card(FULL, tag_label="t"), ensure_ascii=False)
    prompt = r.render_prompt(FULL, tag_label="t")
    for entry in FULL["does_not_count"]:
        assert entry["case"] in card and entry["case"] in prompt
    for entry in FULL["confusable_with"]:
        assert entry["tell"] in card and entry["tell"] in prompt


def test_whitespace_is_normalised_in_both() -> None:
    messy = {"means": "  a  bathroom\n\twith   space  ", "counts": ["  a  tub  ", "  "]}
    assert r.render_card(messy, tag_label="t")["count_it"] == ["a tub"]
    assert "a bathroom with space" in r.render_prompt(messy, tag_label="t")
