"""Stratification: the arithmetic that makes an enriched exam still measurable.

The whole point of enrichment is that it is NOT a filter. Every assertion here is
ultimately about that: no stratum sampled at zero, no probability of zero, and no
model failure allowed to masquerade as "the screener saw nothing".
"""

from __future__ import annotations

import pytest

from toolkit import exam_screening as es


# --- parsing the screener ---------------------------------------------------


def test_a_plain_json_reply_parses() -> None:
    assert es.parse_guess('{"ids": [22, 46]}', valid_ids={22, 46}) == [22, 46]


def test_a_fenced_reply_parses() -> None:
    # Models wrap JSON in code fences unprompted; refusing that would turn a normal
    # answer into an error and push a real negative into the error bucket.
    assert es.parse_guess('```json\n{"ids": [22]}\n```', valid_ids={22, 46}) == [22]


def test_prose_around_the_object_is_tolerated() -> None:
    assert es.parse_guess('Sure! {"ids": [46]} hope that helps',
                          valid_ids={22, 46}) == [46]


def test_an_invented_tag_id_is_dropped() -> None:
    assert es.parse_guess('{"ids": [22, 999]}', valid_ids={22, 46}) == [22]


def test_an_empty_guess_is_a_real_answer_not_an_error() -> None:
    # "None of these apply" is evidence, and it is the stratum that keeps the exam
    # honest about what the screener misses.
    assert es.parse_guess('{"ids": []}', valid_ids={22, 46}) == []


def test_an_unparseable_reply_raises_rather_than_returning_empty() -> None:
    # An empty guess and a failure look identical downstream and mean opposite
    # things. Conflating them fills screen_none with model failures.
    with pytest.raises(ValueError):
        es.parse_guess("I could not see the image", valid_ids={22})


def test_a_reply_without_an_ids_list_raises() -> None:
    with pytest.raises(ValueError):
        es.parse_guess('{"tags": [22]}', valid_ids={22})


def test_duplicate_ids_collapse() -> None:
    assert es.parse_guess('{"ids": [22, 22, 46]}', valid_ids={22, 46}) == [22, 46]


# --- the partition ----------------------------------------------------------


def test_every_screened_image_lands_in_exactly_one_stratum() -> None:
    screens = [(1, [22]), (2, []), (3, [22, 46]), (4, [17])]
    stratum_of, sizes = es.assign_strata(screens)
    assert set(stratum_of) == {1, 2, 3, 4}
    assert sum(sizes.values()) == 4


def test_a_multi_guess_image_goes_to_the_rarest_guessed_tag() -> None:
    # 46 is guessed three times, 17 once. An image that might be either belongs in
    # the 17 stratum: that is the tag the enrichment budget exists to reach.
    screens = [(1, [46]), (2, [46]), (3, [46]), (4, [17, 46])]
    stratum_of, _ = es.assign_strata(screens)
    assert stratum_of[4] == "screen_hit:17"


def test_the_partition_is_stable_across_runs() -> None:
    # Ties break on tag id, so the same screen always yields the same strata —
    # otherwise a re-run would silently reassign probabilities.
    screens = [(1, [22, 46]), (2, [22]), (3, [46])]
    first, _ = es.assign_strata(screens)
    second, _ = es.assign_strata(list(reversed(screens)))
    assert first == second


def test_images_the_screener_saw_nothing_in_form_their_own_stratum() -> None:
    _, sizes = es.assign_strata([(1, []), (2, []), (3, [22])])
    assert sizes[es.SCREEN_NONE] == 2


# --- allocation -------------------------------------------------------------


def _sizes(**kw: int) -> dict[str, int]:
    return dict(kw)


def test_the_none_stratum_always_keeps_a_share() -> None:
    # THE load-bearing assertion. Sampling screen_none at zero would measure recall
    # only over what the screener already found, so the probe would be graded on
    # the half it was handed.
    sizes = {"screen_hit:17": 40, "screen_hit:22": 900, es.SCREEN_NONE: 3000}
    quota = es.allocate_stratified({}, sizes, total=150)
    assert quota[es.SCREEN_NONE] > 0


def test_a_present_stratum_is_never_rounded_away_to_zero() -> None:
    # Filtering by arithmetic is still filtering.
    sizes = {"screen_hit:17": 10, es.SCREEN_NONE: 5}
    quota = es.allocate_stratified({}, sizes, total=4, none_share=0.01)
    assert quota[es.SCREEN_NONE] >= 1


def test_hit_strata_are_spread_evenly_not_proportionally() -> None:
    # Proportional allocation would reproduce the corpus imbalance the enrichment
    # exists to correct — the rare tag would stay rare in the exam too.
    sizes = {"screen_hit:17": 40, "screen_hit:22": 2000, es.SCREEN_NONE: 100}
    quota = es.allocate_stratified({}, sizes, total=100)
    assert abs(quota["screen_hit:17"] - quota["screen_hit:22"]) <= 1


def test_allocation_never_asks_for_more_than_a_stratum_holds() -> None:
    sizes = {"screen_hit:17": 3, "screen_hit:22": 500, es.SCREEN_NONE: 500}
    quota = es.allocate_stratified({}, sizes, total=150)
    assert quota["screen_hit:17"] <= 3


def test_every_allocated_stratum_gets_a_usable_probability() -> None:
    sizes = {"screen_hit:17": 40, "screen_hit:22": 900, es.SCREEN_NONE: 3000}
    quota = es.allocate_stratified({}, sizes, total=150)
    probs = es.inclusion_probabilities(quota, sizes)
    assert set(probs) == set(quota)
    # Zero would make 1/p undefined; above one would mean drawing more than exists.
    assert all(0 < p <= 1 for p in probs.values())


def test_the_rare_stratum_is_sampled_far_harder_than_the_common_one() -> None:
    # This IS the enrichment, expressed as odds: a garage-ish image is many times
    # likelier to reach the exam than a bathroom-ish one, which is exactly why the
    # weighting exists.
    sizes = {"screen_hit:17": 40, "screen_hit:22": 2000, es.SCREEN_NONE: 3000}
    quota = es.allocate_stratified({}, sizes, total=150)
    probs = es.inclusion_probabilities(quota, sizes)
    assert probs["screen_hit:17"] > probs["screen_hit:22"] * 5


def test_allocation_of_nothing_is_empty_not_a_crash() -> None:
    assert es.allocate_stratified({}, {}, total=150) == {}


# --- the prompt -------------------------------------------------------------


def test_the_prompt_lists_every_tag_with_its_id() -> None:
    tags = [{"id": 22, "label": "interier - koupelna"}, {"id": 17, "label": "garáž"}]
    prompt = es.build_prompt(tags)
    assert "22: interier - koupelna" in prompt and "17: garáž" in prompt


def test_the_prompt_is_tuned_for_recall() -> None:
    # A false hit costs one slot; a miss costs coverage of the tag entirely.
    prompt = es.build_prompt([{"id": 1, "label": "x"}])
    assert "MIGHT" in prompt
    assert "missed" in prompt.lower()


def test_the_prompt_allows_an_empty_answer() -> None:
    # Without this the screener is pushed to guess, and screen_none — the stratum
    # that keeps the exam honest — would be starved.
    assert "empty list" in es.build_prompt([{"id": 1, "label": "x"}])
