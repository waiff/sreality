"""Named presentations: one question, several ways of putting it.

The point of the mechanism is attribution — an arm that differs from the baseline in exactly
one appended paragraph, one tool-schema obligation or one frame selection. These tests pin
that each presentation changes ONLY its own thing, so a difference in verdicts can be read as
a difference in that thing.
"""

from __future__ import annotations

import pytest

from autodedup import judge, judge_lane, judge_prompts


def test_the_baseline_presentation_changes_nothing() -> None:
    view = judge.presentation("j2")
    assert judge.system_prompt(view) == judge.SYSTEM_PROMPT
    assert judge.tool_schema(view) is judge.TOOL_SCHEMA
    assert view.strategy is None
    assert judge.presentation(None) == view


def test_every_presentation_keeps_the_baseline_as_its_prefix() -> None:
    for name in judge.PRESENTATIONS:
        prompt = judge.system_prompt(judge.presentation(name))
        assert prompt.startswith(judge.SYSTEM_PROMPT), name


def test_j2b_appends_its_two_rules_and_nothing_else() -> None:
    view = judge.presentation("j2b")
    assert judge.system_prompt(view) == judge.SYSTEM_PROMPT + judge_prompts.J2B_ADDENDUM
    assert view.require_discriminator is False
    assert view.strategy is None
    assert judge.tool_schema(view) is judge.TOOL_SCHEMA


def test_the_strict_presentation_promotes_the_discriminator_into_required() -> None:
    schema = judge.tool_schema(judge.presentation("strict_discriminator"))
    assert "unit_discriminator" in schema["input_schema"]["required"]
    # The baseline schema is not mutated by building the strict one.
    assert "unit_discriminator" not in judge.TOOL_SCHEMA["input_schema"]["required"]
    # Only the obligation changes: the property itself was always there.
    assert (schema["input_schema"]["properties"]
            == judge.TOOL_SCHEMA["input_schema"]["properties"])


def test_the_j1_presentation_only_swaps_the_frame_selection() -> None:
    view = judge.presentation("j1_frames")
    assert view.strategy == "matched_first_j1"
    assert view.strategy in judge.STRATEGIES
    assert judge.system_prompt(view) == judge.SYSTEM_PROMPT
    assert judge.tool_schema(view) is judge.TOOL_SCHEMA


def test_an_unknown_presentation_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown presentation"):
        judge.presentation("j9")


# --- the lane carries it through ----------------------------------------------------------


def test_the_plan_takes_the_strategy_and_names_the_presentation() -> None:
    plan = judge_lane.apply_presentation(
        judge_lane.vote_plan("vision"), judge.presentation("j1_frames")
    )
    assert [vote.strategy for vote in plan] == ["matched_first_j1"]
    assert [vote.view for vote in plan] == ["j1_frames"]


def test_the_baseline_leaves_the_plan_untouched() -> None:
    plan = judge_lane.vote_plan("gold")
    applied = judge_lane.apply_presentation(plan, judge.presentation("j2"))
    assert applied == plan
    assert all(vote.view is None for vote in applied)


def test_a_model_swap_keeps_every_other_input() -> None:
    base = judge_lane.vote_plan("vision")[0]
    swapped = judge_lane.apply_presentation(
        (base,), judge.presentation("strict_discriminator"),
        "qwen3-vl-235b-a22b-instruct",
    )[0]
    assert swapped.model == "qwen3-vl-235b-a22b-instruct"
    assert (swapped.tier, swapped.n_images, swapped.called_for, swapped.strategy) == (
        base.tier, base.n_images, base.called_for, base.strategy
    )
    # `provider` is cleared so the id routes itself; a stale `oss` would send a DashScope id
    # to a rented pod.
    assert swapped.provider is None


def test_a_presentation_without_a_strategy_leaves_each_vote_on_its_own() -> None:
    # The gold plan deliberately runs two different strategies; a prompt-only presentation
    # must not collapse them into one, or the two votes stop being independent.
    applied = judge_lane.apply_presentation(
        judge_lane.vote_plan("gold"), judge.presentation("j2b")
    )
    assert [vote.strategy for vote in applied] == [
        "matched_first", "sequence_first", "matched_first"
    ]


def test_the_args_carry_the_presentation_and_the_model() -> None:
    parsed = judge_lane.parse_args({
        "export_run": "1", "tier": "vision", "n": "5", "max_usd": "1",
        "presentation": "j2b", "llm_model": "qwen3-vl-235b-a22b-instruct",
    })
    assert parsed.presentation == "j2b"
    assert parsed.llm_model == "qwen3-vl-235b-a22b-instruct"


# --- the j1 frame selection, restored ------------------------------------------------------


def test_the_j1_selection_does_not_pair_by_room() -> None:
    """The whole difference between the two arms, in one assertion.

    Given galleries where every room is photographed on both sides, j2 spends its slots on
    room pairs and j1 does not — so `paired_prefix`, which is what the prompt promises the
    model, is the thing the comparison is attributing a verdict difference to."""
    from tests.autodedup.test_judge import make_image, make_listing

    # The two galleries photograph the same three rooms in OPPOSITE order — the shape j2 was
    # built for, and the one where gallery order alone lines up kitchen against living room.
    rooms_a = ["kitchen", "bathroom", "living_room"]
    rooms_b = ["living_room", "bathroom", "kitchen"]
    images_a = [make_image(i + 1, 1, seq=i, tags=[(rooms_a[i], 0.9)]) for i in range(3)]
    images_b = [make_image(11 + i, 2, seq=i, tags=[(rooms_b[i], 0.9)]) for i in range(3)]
    args = (make_listing(id=1), images_a, make_listing(id=2), images_b, {})

    j2_left, j2_right = judge.select_images(*args, n_per_side=3)
    j1_left, j1_right = judge.select_images(
        *args, n_per_side=3, strategy="matched_first_j1"
    )
    assert judge.paired_prefix(j2_left, j2_right) == 3
    assert judge.paired_prefix(j1_left, j1_right) == 0
    assert [judge.room_key(img) for img in j1_right] == rooms_b
    # Both still spend the whole budget: this is a different selection, not a smaller one.
    assert (len(j1_left), len(j1_right)) == (3, 3)


def test_the_j1_selection_is_deterministic() -> None:
    from tests.autodedup.test_judge import make_image, make_listing

    images_a = [make_image(i + 1, 1, seq=i, tags=[("kitchen", 0.9)]) for i in range(4)]
    images_b = [make_image(11 + i, 2, seq=i, tags=[("bathroom", 0.9)]) for i in range(4)]
    args = (make_listing(id=1), images_a, make_listing(id=2), images_b, {})
    first = judge.select_images(*args, n_per_side=3, strategy="matched_first_j1")
    second = judge.select_images(*args, n_per_side=3, strategy="matched_first_j1")
    assert [img.image_id for img in first[0]] == [img.image_id for img in second[0]]
    assert [img.image_id for img in first[1]] == [img.image_id for img in second[1]]
