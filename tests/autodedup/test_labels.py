"""Judgements in, labels out: precedence, weights, the abstention, and the HT sample."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autodedup import labels as lb


def judgement(
    lo: int,
    hi: int,
    verdict: str | None,
    tier: str = "text",
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"lo": lo, "hi": hi, "tier": tier, "model": f"{tier}-model",
                            "stratum": extra.pop("stratum", "merge|model|praha|cross"),
                            "cost_usd": 0.001, "llm_call_id": 1}
    body.update(extra)
    if verdict is not None:
        inner: dict[str, Any] = {"verdict": verdict, "confidence": 0.8,
                                 "developer_project_suspected": extra.pop("developer", False)}
        for key in ("unanimous", "flagged", "votes"):
            if key in extra:
                inner[key] = body.pop(key)
        body["verdict"] = inner
    return body


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n\n", encoding="utf-8"
    )
    return path


def test_verdict_class_maps_three_verdicts_onto_two_classes_and_an_abstention() -> None:
    assert lb.verdict_class("same_property") == 1
    assert lb.verdict_class("different_property") == 0
    assert lb.verdict_class("same_building_different_unit") == 0
    assert lb.verdict_class("insufficient_evidence") is None


def test_load_judgements_skips_blank_lines(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "j.jsonl", [
        judgement(1, 2, "same_property"),
        judgement(3, 4, "different_property"),
    ])
    rows = lb.load_judgements(path)
    assert [row.key for row in rows] == [(1, 2), (3, 4)]
    assert all(row.usable for row in rows)


def test_pair_key_normalises_order() -> None:
    assert lb.pair_key(9, 2) == (2, 9)
    assert lb.pair_key(2, 9) == (2, 9)


def test_gold_outranks_vision_outranks_text() -> None:
    rows = [
        lb.parse_judgement(judgement(1, 2, "different_property", tier="text")),
        lb.parse_judgement(judgement(1, 2, "insufficient_evidence", tier="vision")),
        lb.parse_judgement(
            judgement(1, 2, "same_property", tier="gold", n_votes=3, unanimous=True)
        ),
        lb.parse_judgement(judgement(3, 4, "same_property", tier="text")),
        lb.parse_judgement(judgement(3, 4, "different_property", tier="vision")),
    ]
    out = lb.label_pairs(rows)
    assert out[(1, 2)].tier == "gold" and out[(1, 2)].y == 1
    assert out[(3, 4)].tier == "vision" and out[(3, 4)].y == 0
    text_first = lb.label_pairs(rows, precedence=("text", "vision", "gold"))
    assert text_first[(1, 2)].tier == "text" and text_first[(1, 2)].y == 0


def test_weights_are_the_four_module_constants() -> None:
    unanimous = lb.parse_judgement(
        judgement(1, 2, "same_property", tier="gold", n_votes=3, unanimous=True)
    )
    majority = lb.parse_judgement(
        judgement(1, 2, "same_property", tier="gold", n_votes=3, unanimous=False, flagged=True)
    )
    assert lb.label_weight(unanimous) == lb.WEIGHT_GOLD_UNANIMOUS == 1.0
    assert lb.label_weight(majority) == lb.WEIGHT_GOLD_MAJORITY == 0.67
    assert lb.label_weight(lb.parse_judgement(judgement(1, 2, "same_property", tier="vision"))) \
        == lb.WEIGHT_VISION == 0.6
    assert lb.label_weight(lb.parse_judgement(judgement(1, 2, "same_property", tier="text"))) \
        == lb.WEIGHT_TEXT == 0.3


def test_same_building_different_unit_is_a_negative_that_also_flags_must_not_link() -> None:
    rows = [lb.parse_judgement(judgement(1, 2, "same_building_different_unit"))]
    label = lb.label_pairs(rows)[(1, 2)]
    assert label.y == 0
    assert label.must_not_link is True
    other = lb.label_pairs(
        [lb.parse_judgement(judgement(1, 2, "different_property"))]
    )[(1, 2)]
    assert other.y == 0 and other.must_not_link is False


def test_insufficient_evidence_labels_the_pair_but_with_no_class() -> None:
    label = lb.label_pairs(
        [lb.parse_judgement(judgement(1, 2, "insufficient_evidence"))]
    )[(1, 2)]
    assert label.y is None and label.judged is False and label.verdict == "insufficient_evidence"


def test_a_lone_gold_vote_is_not_ground_truth_and_an_incomplete_row_is_not_a_label() -> None:
    vote = lb.parse_judgement(judgement(1, 2, "same_property", tier="gold"))
    incomplete = lb.parse_judgement(
        {"lo": 3, "hi": 4, "tier": "gold", "n_votes": 1, "incomplete": True, "cost_usd": 0.004}
    )
    assert vote.usable is False and incomplete.usable is False
    assert lb.label_pairs([vote, incomplete]) == {}
    aggregate = lb.parse_judgement(
        judgement(1, 2, "same_property", tier="gold", n_votes=3, unanimous=True)
    )
    assert aggregate.usable is True and aggregate.is_aggregate is True


def test_developer_project_suspected_rides_onto_the_label() -> None:
    row = judgement(1, 2, "different_property")
    row["verdict"]["developer_project_suspected"] = True
    label = lb.label_pairs([lb.parse_judgement(row)])[(1, 2)]
    assert label.developer_project_suspected is True


def test_labels_by_tier_keeps_every_tier_apart_and_a_rejudge_wins() -> None:
    rows = [
        lb.parse_judgement(judgement(1, 2, "same_property", tier="text")),
        lb.parse_judgement(judgement(1, 2, "different_property", tier="text")),
        lb.parse_judgement(judgement(1, 2, "same_property", tier="vision")),
    ]
    per_tier = lb.labels_by_tier(rows)
    assert per_tier["text"][(1, 2)].y == 0
    assert per_tier["vision"][(1, 2)].y == 1


def test_load_sample_reads_stratum_populations_and_yields_ht_weights(tmp_path: Path) -> None:
    payload = {
        "seed": 7,
        "tier": "text",
        "judge_version": "j1",
        "n_requested": 20,
        "n_selected": 3,
        "strata": {"thin": {"population": 100, "selected": 2},
                   "fat": {"population": 1000, "selected": 1}},
        "pairs": [
            {"lo": 1, "hi": 2, "stratum": "thin"},
            {"lo": 3, "hi": 4, "stratum": "thin"},
            {"lo": 5, "hi": 6, "stratum": "fat"},
        ],
    }
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    sample = lb.load_sample(path)
    assert sample.seed == 7 and sample.tier == "text" and sample.n_cohort == 1100
    assert sample.weight_of((1, 2)) == 50.0
    assert sample.weight_of((5, 6)) == 1000.0
    assert sample.weight_of((99, 100)) == 1.0  # never sampled: counts once, never zero
    assert sample.stratum_of((3, 4)) == "thin"


def test_load_sample_recomputes_a_missing_stratum_with_the_samplers_own_key(
    tmp_path: Path,
) -> None:
    payload = {
        "strata": {"merge|K-A|praha|cross": {"population": 8, "selected": 2}},
        "pairs": [{"lo": 1, "hi": 2, "zone": "merge", "certificate": "K-A",
                   "block": "praha", "cross_source": True, "feats": {}}],
    }
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    sample = lb.load_sample(path)
    assert sample.stratum_of((1, 2)) == "merge|K-A|praha|cross"
    assert sample.weight_of((1, 2)) == 4.0


def test_load_sample_accepts_the_n_selected_spelling(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"
    path.write_text(
        json.dumps({"strata": {"a": {"n_total": 50, "n_selected": 5}},
                    "pairs": [{"lo": 1, "hi": 2, "stratum": "a"}]}),
        encoding="utf-8",
    )
    assert lb.load_sample(path).weight_of((1, 2)) == 10.0


def test_sample_from_judgements_is_honestly_unweighted() -> None:
    rows = [
        lb.parse_judgement(judgement(1, 2, "same_property", stratum="merge|model|a|same")),
        lb.parse_judgement(judgement(3, 4, "different_property", stratum="merge|model|a|same")),
    ]
    sample = lb.sample_from_judgements(rows)
    assert sample.weight_of((1, 2)) == 1.0
    assert sample.strata["merge|model|a|same"].n_total == 2


def test_empty_sample_weights_everything_once() -> None:
    assert lb.EMPTY_SAMPLE.weight_of((1, 2)) == 1.0
    assert lb.EMPTY_SAMPLE.n_cohort == 0


def test_load_all_judgements_concatenates_files(tmp_path: Path) -> None:
    a = write_jsonl(tmp_path / "a.jsonl", [judgement(1, 2, "same_property")])
    b = write_jsonl(tmp_path / "b.jsonl", [judgement(3, 4, "different_property", tier="vision")])
    rows = lb.load_all_judgements([a, b])
    assert {row.tier for row in rows} == {"text", "vision"}


def test_a_malformed_line_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"lo": 1, "hi"\n', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        lb.load_judgements(path)


# --- the sampler's own key, and the honesty flags on the draw -------------------------------


def cli_sample_payload() -> dict[str, Any]:
    """What `harness judge-sample` writes: `_stratum` keys, `zone|side|block`."""
    return {
        "strata": {"merge|cross|praha": {"population": 400, "selected": 2},
                   "merge|same|brno": {"population": 40, "selected": 1}},
        "pairs": [
            {"lo": 1, "hi": 2, "zone": "merge", "cross_source": True, "block": "praha",
             "certificate": None, "feats": {}},
            {"lo": 3, "hi": 4, "zone": "merge", "cross_source": True, "block": "praha",
             "certificate": "K-A", "feats": {}},
            {"lo": 5, "hi": 6, "zone": "merge", "cross_source": False, "block": "brno",
             "certificate": None, "feats": {}},
        ],
    }


def test_load_sample_reads_the_cli_samplers_key_not_only_the_lanes(tmp_path: Path) -> None:
    # The two samplers key differently (`zone|side|block` vs `zone|cert|block|side`); recomputing
    # with the wrong one used to collapse every HT weight to 1.0 while still claiming weighting.
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(cli_sample_payload()), encoding="utf-8")
    sample = lb.load_sample(path)
    assert sample.stratum_fn == "stratum"
    assert sample.weight_of((1, 2)) == 200.0
    assert sample.weight_of((3, 4)) == 200.0  # the CLI key ignores the certificate, as it must
    assert sample.weight_of((5, 6)) == 40.0
    assert sample.is_weighted is True


def test_load_sample_refuses_strata_no_known_sampler_could_have_written(tmp_path: Path) -> None:
    payload = cli_sample_payload()
    payload["strata"] = {"invented|key": {"population": 400, "selected": 2}}
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="do not match any known sampler key"):
        lb.load_sample(path)


def test_a_sample_whose_strata_never_inflate_says_it_is_not_weighted(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"
    path.write_text(
        json.dumps({"strata": {"a": {"population": 4, "selected": 4}},
                    "pairs": [{"lo": 1, "hi": 2, "stratum": "a"}]}),
        encoding="utf-8",
    )
    sample = lb.load_sample(path)
    assert sample.weight_of((1, 2)) == 1.0
    assert sample.is_weighted is False
    assert sample.to_json()["is_weighted"] is False
    assert lb.EMPTY_SAMPLE.is_weighted is False


def test_sample_from_judgements_is_never_weighted() -> None:
    rows = [lb.parse_judgement(judgement(1, 2, "same_property", stratum="merge|model|a|same"))]
    sample = lb.sample_from_judgements(rows)
    assert sample.is_weighted is False and sample.stratum_fn == "stamped"


def test_weight_for_prefers_the_stratum_the_judgement_row_carries(tmp_path: Path) -> None:
    sample = lb.Sample(
        strata={"thin": lb.Stratum("thin", 1, 100), "fat": lb.Stratum("fat", 1, 2)},
        pair_stratum={(1, 2): "fat"},
    )
    assert sample.weight_for((1, 2), "thin") == 100.0  # the label's own stamp wins
    assert sample.weight_for((1, 2)) == 2.0
    assert sample.weight_for((9, 9), None) == 1.0
    assert sample.inflates("thin") is True and sample.inflates("nope") is False


def test_a_partial_precedence_keeps_vision_above_text() -> None:
    rows = [
        lb.parse_judgement(judgement(1, 2, "same_property", tier="text")),
        lb.parse_judgement(judgement(1, 2, "different_property", tier="vision")),
    ]
    # `--precedence gold` lists neither cheap tier; alphabetical order would put text first,
    # which inverts the tier weights (0.6 vision vs 0.3 text).
    out = lb.label_pairs(rows, precedence=("gold",))
    assert out[(1, 2)].tier == "vision" and out[(1, 2)].y == 0
    assert lb.effective_precedence(["text", "vision"], ("gold",)) == ["vision", "text"]
    assert lb.effective_precedence(["text", "operator"], ("operator",)) == ["operator", "text"]


# --- the operator tier ---------------------------------------------------------------------


def operator_row(lo: int, hi: int, verdict: str, source: str = lb.SOURCE_EXPLICIT,
                 **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "listing_lo": lo, "listing_hi": hi, "verdict": verdict, "source": source,
        "relation": verdict, "reasons": [], "note": None, "decided_by": "operator",
        "decided_at": "2026-09-18T10:30:00+00:00", "cluster_key": None,
        "must_not_link": False, "engine": None,
    }
    row.update(extra)
    return row


@pytest.mark.parametrize("verdict,expected", [
    ("same", 1),
    ("different", 0),
    ("same_building_different_unit", 0),
    ("same_project_different_unit", 0),
    ("unsure", None),
    ("nonsense", None),
])
def test_the_operator_verdict_maps_onto_the_binary_target(verdict: str,
                                                          expected: int | None) -> None:
    assert lb.operator_verdict_class(verdict) == expected


def test_unsure_is_dropped_rather_than_carried_as_an_abstention() -> None:
    rows = [lb.parse_operator_label(operator_row(1, 2, "unsure")),
            lb.parse_operator_label(operator_row(3, 4, "same"))]
    out = lb.operator_label_pairs(rows)
    assert set(out) == {(3, 4)}


def test_the_operator_outranks_gold() -> None:
    judged = [lb.parse_judgement(judgement(1, 2, "same_property", tier="gold",
                                           unanimous=True, votes=3))]
    judged[0].n_votes = 3
    operator = lb.operator_label_pairs(
        [lb.parse_operator_label(operator_row(1, 2, "different"))]
    )
    out = lb.label_pairs(judged, operator=operator)
    assert out[(1, 2)].tier == lb.OPERATOR_TIER
    assert out[(1, 2)].y == 0 and out[(1, 2)].source == lb.SOURCE_EXPLICIT
    # ... and the machine tiers are still there to be reported on their own.
    per_tier = lb.all_labels_by_tier(judged, operator=operator)
    assert per_tier["gold"][(1, 2)].y == 1
    assert per_tier[lb.OPERATOR_TIER][(1, 2)].y == 0


def test_a_precedence_that_omits_the_operator_still_ranks_it_first() -> None:
    # `--precedence gold` names only one tier; the operator must not fall to the alphabetical
    # tail, where it would rank below `gold` and `text` alike.
    assert lb.effective_precedence(
        ["text", "gold", lb.OPERATOR_TIER], ("gold",)
    ) == ["gold", lb.OPERATOR_TIER, "text"]
    assert lb.effective_precedence(["text", "gold", lb.OPERATOR_TIER]) == [
        lb.OPERATOR_TIER, "gold", "text"
    ]


def test_explicit_beats_implied_whatever_order_the_rows_arrive_in() -> None:
    explicit = operator_row(1, 2, "different")
    implied = operator_row(1, 2, "same", source=lb.SOURCE_IMPLIED, cluster_key=900)
    for rows in ([implied, explicit], [explicit, implied]):
        out = lb.operator_label_pairs([lb.parse_operator_label(r) for r in rows])
        assert out[(1, 2)].y == 0 and out[(1, 2)].source == lb.SOURCE_EXPLICIT


def test_implied_labels_can_be_excluded_or_weighted() -> None:
    rows = [lb.parse_operator_label(operator_row(1, 2, "same")),
            lb.parse_operator_label(
                operator_row(3, 4, "same", source=lb.SOURCE_IMPLIED, cluster_key=900))]
    both = lb.operator_label_pairs(rows)
    assert set(both) == {(1, 2), (3, 4)}
    assert both[(3, 4)].weight == lb.WEIGHT_OPERATOR

    only_explicit = lb.operator_label_pairs(rows, include_implied=False)
    assert set(only_explicit) == {(1, 2)}

    discounted = lb.operator_label_pairs(rows, implied_weight=0.25)
    assert discounted[(3, 4)].weight == 0.25
    assert discounted[(1, 2)].weight == lb.WEIGHT_OPERATOR


def test_both_shared_building_verdicts_are_must_not_links() -> None:
    rows = [lb.parse_operator_label(operator_row(1, 2, "same_building_different_unit")),
            lb.parse_operator_label(operator_row(3, 4, "same_project_different_unit")),
            lb.parse_operator_label(operator_row(5, 6, "different"))]
    out = lb.operator_label_pairs(rows)
    assert out[(1, 2)].must_not_link and out[(3, 4)].must_not_link
    assert not out[(5, 6)].must_not_link


def test_operator_labels_load_from_a_jsonl_artifact(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "operator_labels.jsonl", [
        operator_row(2, 1, "same"),  # out of order on purpose: the key is normalised
        operator_row(3, 4, "same", source=lb.SOURCE_IMPLIED, cluster_key=900,
                     engine={"score": 0.9, "zone": "band"}),
    ])
    rows = lb.load_all_operator_labels([path])
    assert [row.key for row in rows] == [(1, 2), (3, 4)]
    assert rows[1].is_implied and rows[1].engine["zone"] == "band"
    assert lb.operator_label_pairs(rows)[(1, 2)].to_json()["source"] == lb.SOURCE_EXPLICIT


# --- E299: the Browse-merge provenance and the group grain ------------------------------------


def test_explicit_beats_browse_merge_beats_implied_in_any_order() -> None:
    explicit = operator_row(1, 2, "different")
    merged = operator_row(1, 2, "same", source=lb.SOURCE_BROWSE_MERGE, merge_group_id="g-1")
    implied = operator_row(1, 2, "same", source=lb.SOURCE_IMPLIED, cluster_key=900)
    for rows in ([implied, merged, explicit], [explicit, merged, implied],
                 [merged, explicit, implied], [implied, explicit, merged]):
        out = lb.operator_label_pairs([lb.parse_operator_label(r) for r in rows])
        assert out[(1, 2)].y == 0 and out[(1, 2)].source == lb.SOURCE_EXPLICIT
    for rows in ([implied, merged], [merged, implied]):
        out = lb.operator_label_pairs([lb.parse_operator_label(r) for r in rows])
        assert out[(1, 2)].source == lb.SOURCE_BROWSE_MERGE


def test_browse_merge_labels_can_be_excluded_or_weighted_on_their_own() -> None:
    rows = [lb.parse_operator_label(operator_row(1, 2, "same")),
            lb.parse_operator_label(operator_row(
                3, 4, "same", source=lb.SOURCE_BROWSE_MERGE, merge_group_id="g-1")),
            lb.parse_operator_label(
                operator_row(5, 6, "same", source=lb.SOURCE_IMPLIED, cluster_key=900))]
    assert rows[1].is_browse_merge and rows[1].merge_group_id == "g-1"
    assert rows[0].merge_group_id is None
    assert set(lb.operator_label_pairs(rows, include_browse_merge=False)) == {(1, 2), (5, 6)}
    weighted = lb.operator_label_pairs(rows, browse_merge_weight=0.4, implied_weight=0.2)
    assert weighted[(3, 4)].weight == 0.4 and weighted[(5, 6)].weight == 0.2
    assert weighted[(1, 2)].weight == lb.WEIGHT_OPERATOR


def test_a_version_one_row_still_parses() -> None:
    """Format 2 only APPENDED `merge_group_id`; a row written before it reads as before."""
    row = operator_row(1, 2, "same")
    row.pop("merge_group_id", None)
    parsed = lb.parse_operator_label(row)
    assert parsed.merge_group_id is None and parsed.source == lb.SOURCE_EXPLICIT


def test_a_merge_pair_s_standing_decides_whether_it_is_still_same() -> None:
    assert lb.MergePair(1, 2, standing=lb.STANDING_BROWSE_MERGE, verdict="same").same
    assert lb.MergePair(1, 2, standing=lb.STANDING_UNRULED).same
    assert lb.MergePair(1, 2, standing=lb.STANDING_EXPLICIT, verdict="same").same
    assert not lb.MergePair(1, 2, standing=lb.STANDING_EXPLICIT, verdict="different").same
    assert not lb.MergePair(1, 2, standing=lb.STANDING_EXPLICIT, verdict="unsure").same
    assert not lb.MergePair(1, 2, standing=lb.STANDING_MUST_NOT_LINK).same
    assert not lb.MergePair(1, 2, standing=lb.STANDING_UNRULED, must_not_link=True).same


def test_misaligned_member_arrays_are_refused() -> None:
    with pytest.raises(ValueError):
        lb.parse_operator_merge({"merge_group_id": "g", "member_ids": [1, 2],
                                 "member_sides": [1]})
