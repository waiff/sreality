"""W4b error analysis: the groupings, the feature contrasts, the rule tables and the command."""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from autodedup import errors as err
from autodedup import harness
from autodedup.features import TXT_RARE_TOKENS
from autodedup.labels import (
    Sample,
    Stratum,
    label_pairs,
    load_all_judgements,
    parse_judgement,
)
from autodedup.settings import Settings

VERDICT_OF: dict[int | None, str] = {
    1: "same_property",
    0: "different_property",
    None: "insufficient_evidence",
}


def pair_row(
    lo: int,
    hi: int,
    *,
    zone: str = "merge",
    score: float = 0.99,
    certificate: str | None = None,
    block: str = "praha",
    cross: bool = True,
    reason: str = "model",
    veto: str | None = None,
    families: Sequence[str] = ("ATTR", "LOC"),
    absent: Sequence[str] = (),
    **feats: float,
) -> dict[str, Any]:
    body = {name: [value, True] for name, value in feats.items()}
    body.update({name: [0.0, False] for name in absent})
    return {
        "lo": lo,
        "hi": hi,
        "zone": zone,
        "score": score,
        "certificate": certificate,
        "veto": veto,
        "reason": reason,
        "block": block,
        "cross_source": cross,
        "source_pair": "bazos|sreality" if cross else "sreality|sreality",
        "families": list(families),
        "probes": ["attr_area"],
        "feats": body,
    }


def judgement(
    lo: int,
    hi: int,
    y: int | None,
    *,
    tier: str = "vision",
    stratum: str = "merge|K-A|praha|cross",
    discriminator: str = "cellar: no vs yes",
    evidence: Sequence[str] = ("floor 3 vs 8",),
) -> dict[str, Any]:
    return {
        "lo": lo,
        "hi": hi,
        "tier": tier,
        "model": "gpt-5-mini",
        "stratum": stratum,
        "cost_usd": 0.004,
        "llm_call_id": 1,
        "verdict": {
            "verdict": VERDICT_OF[y],
            "confidence": 0.9,
            "developer_project_suspected": False,
            "downgraded_from": None,
            "unit_discriminator": discriminator,
            "key_evidence": list(evidence),
            "contradicting_evidence": [],
        },
    }


def write_run(
    tmp_path: Path, rows: Sequence[dict[str, Any]], settings: Settings | None = None
) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(run_dir / harness.PAIRS_FILE, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    (run_dir / harness.RUN_FILE).write_text(
        json.dumps({"settings": (settings or Settings()).to_dict()}), encoding="utf-8"
    )
    return run_dir


def write_judgements(path: Path, rows: Sequence[dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8"
    )
    return path


def analyse(
    rows: Sequence[dict[str, Any]],
    judgements: Sequence[dict[str, Any]],
    *,
    sample: Sample | None = None,
    top: int = err.DEFAULT_TOP,
) -> err.ErrorReport:
    parsed = [parse_judgement(row) for row in judgements]
    return err.analyse(rows, label_pairs(parsed), parsed, sample, top=top)


# --- false merges ----------------------------------------------------------------------------


def test_false_merges_group_by_certificate_block_and_side_with_their_precision() -> None:
    rows = [
        pair_row(1, 2, certificate="K-A", interior_match_ratio=0.8),
        pair_row(1, 3, certificate="K-A", interior_match_ratio=0.7),
        pair_row(1, 4, certificate="K-A", interior_match_ratio=0.0),
        pair_row(1, 5, certificate="K-A", block="brno", interior_match_ratio=0.0),
        pair_row(1, 6, certificate="K-C", interior_match_ratio=0.9),
    ]
    judgements = [
        judgement(1, 2, 1), judgement(1, 3, 1), judgement(1, 4, 0),
        judgement(1, 5, 0), judgement(1, 6, 1),
    ]
    groups = analyse(rows, judgements).sections["false_merges"]
    keys = {group["group"]: group for group in groups}
    assert set(keys) == {"K-A|praha|cross", "K-A|brno|cross"}
    praha = keys["K-A|praha|cross"]
    assert (praha["n_right"], praha["n_wrong"]) == (2, 1)
    assert praha["precision"] == pytest.approx(2 / 3)
    # the brno group has no true merge to compare against, but it is still reported
    assert keys["K-A|brno|cross"]["precision"] == 0.0


def test_a_listed_false_merge_carries_the_judge_verdict_and_its_top_features() -> None:
    rows = [
        pair_row(1, 2, certificate="K-A", interior_match_ratio=0.8, rare_token_overlap=4.0),
        pair_row(1, 4, certificate="K-A", score=0.9, interior_match_ratio=0.0,
                 rare_token_overlap=0.0),
    ]
    judgements = [
        judgement(1, 2, 1),
        judgement(1, 4, 0, discriminator="floor 2 vs 7", evidence=("balcony yes vs no", "x")),
    ]
    group = analyse(rows, judgements).sections["false_merges"][0]
    listed = group["pairs"][0]
    assert (listed["lo"], listed["hi"]) == (1, 4)
    assert listed["verdict"] == "different_property"
    assert listed["unit_discriminator"] == "floor 2 vs 7"
    assert listed["key_evidence"] == "balcony yes vs no"
    assert listed["tier"] == "vision"
    assert len(listed["top_features"]) == err.TOP_FEATURES
    names = [entry["feature"] for entry in listed["top_features"]]
    assert "interior_match_ratio" in names
    present = {entry["feature"]: entry for entry in listed["top_features"]}
    assert present["interior_match_ratio"]["present"] is True
    assert present["interior_match_ratio"]["value"] == 0.0


def test_the_pair_listing_is_worst_first_and_honours_top() -> None:
    rows = [pair_row(1, 2, certificate="K-A", interior_match_ratio=0.9)]
    judgements = [judgement(1, 2, 1)]
    for index, score in enumerate((0.96, 0.99, 0.97), start=10):
        rows.append(pair_row(1, index, certificate="K-A", score=score, interior_match_ratio=0.0))
        judgements.append(judgement(1, index, 0))
    group = analyse(rows, judgements, top=2).sections["false_merges"][0]
    assert [pair["score"] for pair in group["pairs"]] == [0.99, 0.97]


def test_an_abstention_stays_out_of_the_precision_denominator() -> None:
    rows = [
        pair_row(1, 2, certificate="K-A", interior_match_ratio=0.9),
        pair_row(1, 3, certificate="K-A", interior_match_ratio=0.0),
        pair_row(1, 4, certificate="K-A", interior_match_ratio=0.5),
        pair_row(1, 5, certificate="K-A", interior_match_ratio=0.5),
    ]
    judgements = [judgement(1, 2, 1), judgement(1, 3, 0), judgement(1, 4, None)]
    group = analyse(rows, judgements).sections["false_merges"][0]
    assert (group["n_judged"], group["n_abstain"], group["n_unlabelled"]) == (2, 1, 1)
    assert group["precision"] == pytest.approx(0.5)
    assert group["n_pairs"] == 4


# --- feature contrasts -----------------------------------------------------------------------


def test_the_contrast_ranks_the_separating_feature_first_and_reports_both_means() -> None:
    rows: list[dict[str, Any]] = []
    judgements: list[dict[str, Any]] = []
    for index in range(10, 16):
        rows.append(pair_row(1, index, certificate="K-A",
                             interior_match_ratio=0.8, len_ratio=0.5))
        judgements.append(judgement(1, index, 1))
    for index in range(20, 26):
        rows.append(pair_row(1, index, certificate="K-A",
                             interior_match_ratio=0.1, len_ratio=0.5))
        judgements.append(judgement(1, index, 0))
    contrast = analyse(rows, judgements).sections["false_merges"][0]["contrast"]
    top = contrast[0]
    assert top["feature"] == "interior_match_ratio"
    assert top["mean_pos"] == pytest.approx(0.8)
    assert top["mean_neg"] == pytest.approx(0.1)
    assert top["presence_pos"] == 1.0 and top["presence_neg"] == 1.0
    assert abs(top["std_diff"]) > 1.0
    flat = {entry["feature"]: entry for entry in contrast}["len_ratio"]
    assert flat["std_diff"] == pytest.approx(0.0)


def test_a_feature_present_on_one_side_only_reports_presence_not_a_zero_mean() -> None:
    rows = [
        pair_row(1, 2, certificate="K-A", interior_match_ratio=0.9, rare_token_overlap=3.0),
        pair_row(1, 3, certificate="K-A", interior_match_ratio=0.1,
                 absent=("rare_token_overlap",)),
    ]
    judgements = [judgement(1, 2, 1), judgement(1, 3, 0)]
    contrast = analyse(rows, judgements).sections["false_merges"][0]["contrast"]
    entry = {item["feature"]: item for item in contrast}["rare_token_overlap"]
    assert entry["mean_pos"] == pytest.approx(3.0)
    assert entry["mean_neg"] is None
    assert entry["presence_pos"] == 1.0 and entry["presence_neg"] == 0.0
    assert entry["std_diff"] is None
    assert entry["presence_diff"] == pytest.approx(1.0)


def test_a_perfectly_separating_binary_feature_still_gets_a_standardised_difference() -> None:
    rows: list[dict[str, Any]] = []
    judgements: list[dict[str, Any]] = []
    for index in range(10, 14):
        rows.append(pair_row(1, index, certificate="K-A", same_source=0.0))
        judgements.append(judgement(1, index, 1))
    for index in range(20, 24):
        rows.append(pair_row(1, index, certificate="K-A", same_source=1.0))
        judgements.append(judgement(1, index, 0))
    contrast = analyse(rows, judgements).sections["false_merges"][0]["contrast"]
    entry = {item["feature"]: item for item in contrast}["same_source"]
    assert entry["std_diff"] == pytest.approx(-2.0)


# --- false rejects and the band ---------------------------------------------------------------


def test_false_rejects_group_by_block_and_side_and_name_what_refused_them() -> None:
    rows = [
        pair_row(1, 2, zone="reject", score=0.10, reason="model"),
        pair_row(1, 3, zone="reject", score=0.05, reason="auto_reject:numeral_conflict"),
        pair_row(1, 4, zone="veto", score=0.0, reason="guard:floor", veto="floor"),
        pair_row(1, 5, zone="reject", score=0.02, reason="model", cross=False),
        pair_row(1, 6, zone="reject", score=0.01, reason="model"),
    ]
    judgements = [
        judgement(1, 2, 1, stratum="reject|none|praha|cross"),
        judgement(1, 3, 1, stratum="reject|none|praha|cross"),
        judgement(1, 4, 1, stratum="reject|none|praha|cross"),
        judgement(1, 5, 0, stratum="reject|none|praha|same"),
        judgement(1, 6, 0, stratum="reject|none|praha|cross"),
    ]
    groups = analyse(rows, judgements).sections["false_rejects"]
    assert [group["group"] for group in groups] == ["praha|cross"]
    group = groups[0]
    assert (group["n_wrong"], group["n_right"]) == (3, 1)
    assert group["rules"] == {
        "auto_reject:numeral_conflict": 1, "guard_veto:floor": 1, "model": 1
    }
    # the most confidently refused pair is listed first
    assert [pair["score"] for pair in group["pairs"]] == [0.0, 0.05, 0.10]
    assert group["pairs"][0]["refusal"] == "guard_veto:floor"


def test_band_composition_is_per_block_plus_an_all_row_with_contrasts() -> None:
    rows = [
        pair_row(1, 2, zone="band", score=0.7, interior_match_ratio=0.9),
        pair_row(1, 3, zone="band", score=0.6, interior_match_ratio=0.1),
        pair_row(1, 4, zone="band", block="brno", score=0.5, interior_match_ratio=0.2),
        pair_row(1, 5, zone="band", block="brno", score=0.5, interior_match_ratio=0.2),
    ]
    judgements = [judgement(1, 2, 1), judgement(1, 3, 0), judgement(1, 4, None)]
    composition = analyse(rows, judgements).sections["band_composition"]
    assert composition[0]["block"] == "(all)"
    assert composition[0]["n_pairs"] == 4
    assert composition[0]["n_positive"] == 1 and composition[0]["n_negative"] == 1
    assert composition[0]["positive_share"] == pytest.approx(0.5)
    brno = {entry["block"]: entry for entry in composition}["brno"]
    assert (brno["n_judged"], brno["n_abstain"], brno["n_unlabelled"]) == (0, 1, 1)
    top = composition[0]["contrast"][0]
    assert top["feature"] == "interior_match_ratio"


# --- candidate rules -------------------------------------------------------------------------


def ka_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Four K-A merges: the two true ones carry interior matches, the two false ones do not."""
    rows = [
        pair_row(1, 2, certificate="K-A", interior_match_ratio=0.8, rare_token_overlap=3.0,
                 n_images_min=6.0, catalog_ratio_max=0.1, overlap_days=0.0, same_source=1.0),
        pair_row(1, 3, certificate="K-A", interior_match_ratio=0.6, rare_token_overlap=0.0,
                 n_images_min=4.0, catalog_ratio_max=0.0, overlap_days=12.0, same_source=0.0),
        pair_row(1, 4, certificate="K-A", interior_match_ratio=0.0, rare_token_overlap=0.0,
                 n_images_min=0.0, catalog_ratio_max=1.0, overlap_days=30.0, same_source=1.0),
        pair_row(1, 5, certificate="K-A", interior_match_ratio=0.2, rare_token_overlap=1.0,
                 n_images_min=3.0, catalog_ratio_max=0.5, overlap_days=8.0, same_source=1.0),
    ]
    judgements = [
        judgement(1, 2, 1), judgement(1, 3, 1), judgement(1, 4, 0), judgement(1, 5, 0)
    ]
    return rows, judgements


def test_the_ka_rule_table_prices_every_clause_and_every_pair_of_clauses() -> None:
    rows, judgements = ka_rows()
    table = analyse(rows, judgements).sections["candidate_rules_ka"]
    by_rule = {entry["rule"]: entry for entry in table}
    assert by_rule["(base)"]["n"] == 4
    assert by_rule["(base)"]["precision"] == pytest.approx(0.5)
    assert by_rule["a"]["n"] == 2 and by_rule["a"]["precision"] == pytest.approx(1.0)
    assert by_rule["a"]["kept_true_share"] == pytest.approx(1.0)
    assert by_rule["b"]["n"] == 1 and by_rule["b"]["precision"] == pytest.approx(1.0)
    assert by_rule["b"]["kept_true_share"] == pytest.approx(0.5)
    assert by_rule["c"]["n"] == 3
    assert by_rule["d"]["n"] == 2 and by_rule["d"]["precision"] == pytest.approx(1.0)
    assert by_rule["a+d"]["n"] == 2 and by_rule["a+d"]["precision"] == pytest.approx(1.0)
    assert by_rule["b+d"]["n"] == 1
    assert set(by_rule) == {"(base)", "a", "b", "c", "d",
                            "a+b", "a+c", "a+d", "b+c", "b+d", "c+d"}
    assert by_rule["a"]["wilson_lb"] < 1.0  # n=2 at 100% is not evidence of 100%


def test_an_absent_feature_never_satisfies_a_candidate_clause() -> None:
    rows, judgements = ka_rows()
    rows.append(pair_row(1, 6, certificate="K-A",
                         absent=("interior_match_ratio", "overlap_days"), same_source=1.0))
    judgements.append(judgement(1, 6, 0))
    by_rule = {entry["rule"]: entry
               for entry in analyse(rows, judgements).sections["candidate_rules_ka"]}
    assert by_rule["(base)"]["n"] == 5
    assert by_rule["a"]["n"] == 2  # unknown is not ">= 0.5"
    assert by_rule["d"]["n"] == 2  # unknown overlap does not assert "never overlapped"


def test_the_catalog_clause_refuses_a_pair_whose_gallery_is_all_stock() -> None:
    rows = [
        pair_row(1, 2, certificate="K-A", n_images_min=0.0, catalog_ratio_max=1.0),
        pair_row(1, 3, certificate="K-A", n_images_min=2.0, catalog_ratio_max=0.4),
    ]
    judgements = [judgement(1, 2, 0), judgement(1, 3, 1)]
    by_rule = {entry["rule"]: entry
               for entry in analyse(rows, judgements).sections["candidate_rules_ka"]}
    assert by_rule["c"]["n"] == 1
    assert by_rule["c"]["precision"] == pytest.approx(1.0)


def test_the_model_threshold_tables_cut_by_score_and_reuse_the_same_clauses() -> None:
    rows = [
        pair_row(1, 2, score=0.999, interior_match_ratio=0.9),
        pair_row(1, 3, score=0.985, interior_match_ratio=0.0),
        pair_row(1, 4, score=0.975, interior_match_ratio=0.0),
        pair_row(1, 5, certificate="K-A", score=0.999, interior_match_ratio=0.0),
    ]
    judgements = [judgement(1, 2, 1), judgement(1, 3, 0), judgement(1, 4, 0),
                  judgement(1, 5, 0)]
    tables = analyse(rows, judgements).sections["model_thresholds"]
    assert [table["threshold"] for table in tables] == list(err.MODEL_THRESHOLDS)
    by_threshold = {table["threshold"]: {row["rule"]: row for row in table["rows"]}
                    for table in tables}
    # the certificate merge is NOT a model merge and never enters these tables
    assert by_threshold[0.97]["(base)"]["n"] == 3
    assert by_threshold[0.98]["(base)"]["n"] == 2
    assert by_threshold[0.99]["(base)"]["n"] == 1
    assert by_threshold[0.99]["(base)"]["precision"] == pytest.approx(1.0)
    assert by_threshold[0.97]["a"]["n"] == 1


def test_horvitz_thompson_precision_follows_the_stratum_weights() -> None:
    rows = [
        pair_row(1, 2, certificate="K-A", interior_match_ratio=0.9),
        pair_row(1, 3, certificate="K-A", interior_match_ratio=0.1),
    ]
    judgements = [
        judgement(1, 2, 1, stratum="rare"), judgement(1, 3, 0, stratum="common"),
    ]
    sample = Sample(strata={"rare": Stratum("rare", 1, 1), "common": Stratum("common", 1, 9)})
    report = analyse(rows, judgements, sample=sample)
    group = report.sections["false_merges"][0]
    assert group["precision"] == pytest.approx(0.5)
    assert group["precision_ht"] == pytest.approx(0.1)
    assert report.sections["summary"]["sample_weighted"] is True


# --- the command ------------------------------------------------------------------------------


def test_the_errors_command_writes_both_reports_and_prints_the_tables(tmp_path: Path) -> None:
    rows, judgements = ka_rows()
    rows.append(pair_row(1, 9, zone="reject", score=0.02))
    judgements.append(judgement(1, 9, 1, stratum="reject|none|praha|cross"))
    rows.append(pair_row(1, 8, zone="band", score=0.6))
    judgements.append(judgement(1, 8, 0, stratum="band|model|praha|cross"))
    run_dir = write_run(tmp_path, rows)
    path = write_judgements(tmp_path / "j.jsonl", judgements)

    out = io.StringIO()
    code = harness.main(
        ["errors", str(run_dir), "--judgements", str(path), "--out", str(tmp_path / "e"),
         "--top", "2"],
        out=out,
    )
    assert code == 0
    text = out.getvalue()
    assert "false merges" in text and "K-A|praha|cross" in text
    assert "K-A merges under candidate rules" in text
    assert "model-only merges by score cut" in text

    payload = json.loads((tmp_path / "e" / "errors.json").read_text(encoding="utf-8"))
    assert payload["summary"]["merge"]["n_wrong"] == 2
    assert payload["false_rejects"][0]["group"] == "praha|cross"
    assert len(payload["false_merges"][0]["pairs"]) == 2
    assert {row["rule"] for row in payload["candidate_rules_ka"]} >= {"(base)", "a", "a+d"}
    markdown = (tmp_path / "e" / "errors.md").read_text(encoding="utf-8")
    assert "## Candidate rules on K-A merges" in markdown
    assert "## Band composition" in markdown


def test_the_errors_command_defaults_its_output_to_the_run_directory(tmp_path: Path) -> None:
    rows, judgements = ka_rows()
    run_dir = write_run(tmp_path, rows)
    path = write_judgements(tmp_path / "j.jsonl", judgements)
    assert harness.main(
        ["errors", str(run_dir), "--judgements", str(path)], out=io.StringIO()
    ) == 0
    assert (run_dir / "errors.json").is_file() and (run_dir / "errors.md").is_file()


def test_the_errors_command_exits_one_on_a_missing_run_or_unusable_judgements(
    tmp_path: Path,
) -> None:
    rows, judgements = ka_rows()
    run_dir = write_run(tmp_path, rows)
    path = write_judgements(tmp_path / "j.jsonl", judgements)
    assert harness.main(["errors", str(tmp_path / "nope"), "--judgements", str(path)],
                        out=io.StringIO()) == 1
    assert harness.main(["errors", str(run_dir), "--judgements", str(tmp_path / "nope.jsonl")],
                        out=io.StringIO()) == 1
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert harness.main(["errors", str(run_dir), "--judgements", str(empty)],
                        out=io.StringIO()) == 1


def test_a_higher_tier_judgement_wins_the_label_the_listing_shows(tmp_path: Path) -> None:
    rows = [
        pair_row(1, 2, certificate="K-A", interior_match_ratio=0.9),
        pair_row(1, 3, certificate="K-A", interior_match_ratio=0.1),
    ]
    text = write_judgements(
        tmp_path / "text.jsonl",
        [judgement(1, 2, 1, tier="text"), judgement(1, 3, 1, tier="text")],
    )
    vision = write_judgements(
        tmp_path / "vision.jsonl",
        [judgement(1, 3, 0, tier="vision", discriminator="lift yes vs no")],
    )
    judgements = load_all_judgements([text, vision])
    report = err.analyse(rows, label_pairs(judgements), judgements, None)
    group = report.sections["false_merges"][0]
    assert (group["n_right"], group["n_wrong"]) == (1, 1)
    listed = group["pairs"][0]
    assert (listed["lo"], listed["hi"], listed["tier"]) == (1, 3, "vision")
    assert listed["unit_discriminator"] == "lift yes vs no"


# --- what the contrast RANK is allowed to be bought with ---------------------------------


def test_a_feature_one_class_never_has_outranks_a_mere_value_gap() -> None:
    """Total presence separation has no std_diff at all; ranking on value alone buried it."""
    rows: list[dict[str, Any]] = []
    judgements: list[dict[str, Any]] = []
    for offset, index in enumerate(range(10, 16)):
        rows.append(pair_row(1, index, certificate="K-A", rare_token_overlap=3.0,
                             len_ratio=(0.5 if offset % 2 else 0.4)))
        judgements.append(judgement(1, index, 1))
    for offset, index in enumerate(range(20, 26)):
        rows.append(pair_row(1, index, certificate="K-A", absent=("rare_token_overlap",),
                             len_ratio=(0.45 if offset % 2 else 0.35)))
        judgements.append(judgement(1, index, 0))
    contrast = analyse(rows, judgements).sections["false_merges"][0]["contrast"]
    assert contrast[0]["feature"] == "rare_token_overlap"
    assert contrast[0]["std_diff"] is None and contrast[0]["presence_diff"] == 1.0
    ranked = [entry["feature"] for entry in contrast]
    gap = {entry["feature"]: entry for entry in contrast}["len_ratio"]
    assert gap["std_diff"] is not None and abs(gap["std_diff"]) > 0.5
    assert ranked.index("rare_token_overlap") < ranked.index("len_ratio")


def test_a_one_against_one_comparison_cannot_outrank_a_well_observed_one() -> None:
    """1-vs-1 standardises to a constant +-2.0: an artefact of the pooling, not a measurement.

    The well-observed feature here separates only WEAKLY, so the thin reading still wins on raw
    magnitude and can be kept out of the top only by the support test."""
    rows: list[dict[str, Any]] = []
    judgements: list[dict[str, Any]] = []
    for offset, index in enumerate(range(10, 18)):
        feats: dict[str, float] = {"len_ratio": 0.55 if offset % 2 else 0.45}
        if index == 10:
            feats["dist_norm"] = 0.0
        rows.append(pair_row(1, index, certificate="K-A", **feats))
        judgements.append(judgement(1, index, 1))
    for offset, index in enumerate(range(20, 28)):
        feats = {"len_ratio": 0.50 if offset % 2 else 0.40}
        if index == 20:
            feats["dist_norm"] = 1.0
        rows.append(pair_row(1, index, certificate="K-A", **feats))
        judgements.append(judgement(1, index, 0))
    contrast = analyse(rows, judgements).sections["false_merges"][0]["contrast"]
    by_feature = {entry["feature"]: entry for entry in contrast}
    thin, thick = by_feature["dist_norm"], by_feature["len_ratio"]
    assert thin["std_diff"] == pytest.approx(-2.0)
    assert (thin["n_present_pos"], thin["n_present_neg"]) == (1, 1)
    assert abs(thin["std_diff"]) > abs(thick["std_diff"])
    assert thin["separation"] > thick["separation"]  # magnitude alone would rank it first
    assert thin["supported"] is False and thick["supported"] is True
    assert contrast[0]["feature"] == "len_ratio"
    ranked = [entry["feature"] for entry in contrast]
    assert ranked.index("len_ratio") < ranked.index("dist_norm")


def test_the_contrast_table_prints_the_counts_behind_each_side() -> None:
    rows, judgements = ka_rows()
    markdown = analyse(rows, judgements).to_markdown()
    header = [line for line in markdown.splitlines() if line.startswith("| feature |")][0]
    assert "| n + |" in header and "| n - |" in header


# --- the markdown actually has to render as markdown -------------------------------------


def table_rows(markdown: str) -> list[tuple[str, str]]:
    lines = markdown.splitlines()
    return [
        (line, lines[index + 1])
        for index, line in enumerate(lines[:-1])
        if line.startswith("|")
        and lines[index + 1].startswith("|")
        and set(lines[index + 1].replace(" ", "")) <= set("|-:")
    ]


def n_cells(row: str) -> int:
    parts: list[str] = [""]
    escaped = False
    for character in row.strip():
        if escaped:
            parts[-1] += character
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            parts.append("")
        else:
            parts[-1] += character
    return len(parts) - 2


def test_every_markdown_table_has_a_delimiter_row_matching_its_header() -> None:
    rows, judgements = ka_rows()
    rows.append(pair_row(1, 8, zone="band", score=0.6, interior_match_ratio=0.5))
    judgements.append(judgement(1, 8, 0, stratum="band|model|praha|cross"))
    rows.append(pair_row(1, 9, zone="reject", score=0.02, interior_match_ratio=0.9))
    judgements.append(judgement(1, 9, 1, stratum="reject|none|praha|cross"))
    tables = table_rows(analyse(rows, judgements).to_markdown())
    assert tables
    for header, delimiter in tables:
        assert n_cells(header) == n_cells(delimiter), header


def test_a_group_key_reaches_the_table_as_one_escaped_cell() -> None:
    rows, judgements = ka_rows()
    markdown = analyse(rows, judgements).to_markdown()
    assert "| K-A\\|praha\\|cross |" in markdown
    assert "| certificate / block / side |" in markdown


# --- recall is priced against ONE denominator ---------------------------------------------


def test_raising_the_score_cut_costs_recall_against_a_fixed_denominator() -> None:
    rows = [
        pair_row(1, 2, score=0.999, interior_match_ratio=0.9),
        pair_row(1, 3, score=0.975, interior_match_ratio=0.9),
        pair_row(1, 4, score=0.975, interior_match_ratio=0.0),
    ]
    judgements = [judgement(1, 2, 1), judgement(1, 3, 1), judgement(1, 4, 0)]
    tables = analyse(rows, judgements).sections["model_thresholds"]
    by_threshold = {table["threshold"]: {row["rule"]: row for row in table["rows"]}
                    for table in tables}
    low, high = by_threshold[0.97]["(base)"], by_threshold[0.99]["(base)"]
    assert (low["n_positive"], high["n_positive"]) == (2, 1)
    assert low["kept_true_share"] == pytest.approx(1.0)
    assert high["kept_true_share"] == pytest.approx(0.5)
    # the per-rung figure is kept, but beside the fixed one, never instead of it
    assert high["kept_true_share_local"] == pytest.approx(1.0)


def test_the_ka_table_prices_recall_against_its_own_judged_set() -> None:
    rows, judgements = ka_rows()
    by_rule = {entry["rule"]: entry
               for entry in analyse(rows, judgements).sections["candidate_rules_ka"]}
    assert by_rule["(base)"]["kept_true_share"] == pytest.approx(1.0)
    assert by_rule["b"]["n_positive"] == 1
    assert by_rule["b"]["kept_true_share"] == pytest.approx(0.5)


def test_the_score_ladder_is_anchored_on_the_runs_own_t_hi() -> None:
    assert err.threshold_ladder(0.97) == [0.97, 0.98, 0.99, 0.995]
    assert err.threshold_ladder(0.99) == [0.99, 0.995]
    assert err.threshold_ladder(0.999) == [0.999]
    assert err.threshold_ladder(None) == list(err.MODEL_THRESHOLDS)


def test_the_command_drops_rungs_below_the_cut_the_pairs_were_zoned_under(
    tmp_path: Path,
) -> None:
    rows, judgements = ka_rows()
    run_dir = write_run(tmp_path, rows, Settings(t_hi=0.99))
    path = write_judgements(tmp_path / "j.jsonl", judgements)
    assert harness.main(["errors", str(run_dir), "--judgements", str(path)],
                        out=io.StringIO()) == 0
    payload = json.loads((run_dir / "errors.json").read_text(encoding="utf-8"))
    assert payload["summary"]["t_hi"] == 0.99
    assert [table["threshold"] for table in payload["model_thresholds"]] == [0.99, 0.995]


def test_an_explicit_threshold_overrides_the_ladder(tmp_path: Path) -> None:
    rows, judgements = ka_rows()
    run_dir = write_run(tmp_path, rows)
    path = write_judgements(tmp_path / "j.jsonl", judgements)
    assert harness.main(
        ["errors", str(run_dir), "--judgements", str(path), "--threshold", "0.5",
         "--threshold", "0.8"],
        out=io.StringIO(),
    ) == 0
    payload = json.loads((run_dir / "errors.json").read_text(encoding="utf-8"))
    assert [table["threshold"] for table in payload["model_thresholds"]] == [0.5, 0.8]


# --- a promise that held is evidence too ---------------------------------------------------


def test_a_certificate_that_never_erred_is_reported_instead_of_vanishing() -> None:
    rows = [
        pair_row(1, 2, certificate="K-B", interior_match_ratio=0.9),
        pair_row(1, 3, certificate="K-B", interior_match_ratio=0.8),
        pair_row(1, 4, certificate="K-A", interior_match_ratio=0.0),
    ]
    judgements = [judgement(1, 2, 1), judgement(1, 3, 1), judgement(1, 4, 0)]
    report = analyse(rows, judgements)
    assert [group["group"] for group in report.sections["false_merges"]] == ["K-A|praha|cross"]
    clean = report.sections["clean_merge_groups"]
    assert [group["group"] for group in clean] == ["K-B|praha|cross"]
    assert (clean[0]["n_judged"], clean[0]["precision"]) == (2, 1.0)
    assert clean[0]["wilson_lb"] < 1.0
    assert "K-B\\|praha\\|cross" in report.to_markdown()


def test_an_unjudged_group_is_neither_an_error_nor_a_clean_group() -> None:
    rows = [
        pair_row(1, 2, certificate="K-A", interior_match_ratio=0.9),
        pair_row(1, 3, certificate="K-D", block="brno", interior_match_ratio=0.9),
    ]
    report = analyse(rows, [judgement(1, 2, 1)])
    assert report.sections["false_merges"] == []
    assert [group["group"] for group in report.sections["clean_merge_groups"]] == [
        "K-A|praha|cross"
    ]


# --- weights and sentinels announce themselves ---------------------------------------------


def test_the_summary_counts_the_judged_pairs_whose_stratum_never_inflated() -> None:
    rows = [
        pair_row(1, 2, certificate="K-A", interior_match_ratio=0.9),
        pair_row(1, 3, certificate="K-A", interior_match_ratio=0.1),
    ]
    judgements = [judgement(1, 2, 1, stratum="rare"), judgement(1, 3, 0, stratum="flat")]
    sample = Sample(strata={"rare": Stratum("rare", 1, 9), "flat": Stratum("flat", 1, 1)})
    summary = analyse(rows, judgements, sample=sample).sections["summary"]
    assert summary["sample_weighted"] is True
    assert summary["n_labelled_without_sample_weight"] == 1
    without = analyse(rows, judgements).sections["summary"]
    assert without["n_labelled_without_sample_weight"] == 2


def test_a_block_named_like_the_pooled_row_is_not_folded_into_it() -> None:
    rows = [
        pair_row(1, 2, zone="band", score=0.6, block=err.ALL_BLOCKS, interior_match_ratio=0.9),
        pair_row(1, 3, zone="band", score=0.6, block="brno", interior_match_ratio=0.1),
    ]
    judgements = [judgement(1, 2, 1), judgement(1, 3, 0)]
    composition = analyse(rows, judgements).sections["band_composition"]
    pooled = [entry for entry in composition if entry["pooled"]]
    named = [entry for entry in composition
             if entry["block"] == err.ALL_BLOCKS and not entry["pooled"]]
    assert len(pooled) == 1 and pooled[0]["n_pairs"] == 2
    assert len(named) == 1 and named[0]["n_pairs"] == 1


def test_the_rare_token_clause_tracks_the_feature_constant_it_mirrors() -> None:
    rule = {candidate.code: candidate for candidate in err.CANDIDATE_RULES}["b"]
    assert rule.text == f"rare_token_overlap >= {TXT_RARE_TOKENS:g}"


def test_a_listed_pair_shows_the_reference_it_was_scored_against() -> None:
    rows = [
        pair_row(1, 2, certificate="K-A", interior_match_ratio=0.8, rare_token_overlap=4.0),
        pair_row(1, 3, certificate="K-A", interior_match_ratio=0.8, rare_token_overlap=4.0),
        pair_row(1, 4, certificate="K-A", interior_match_ratio=0.0,
                 absent=("rare_token_overlap",)),
    ]
    judgements = [judgement(1, 2, 1), judgement(1, 3, 1), judgement(1, 4, 0)]
    markdown = analyse(rows, judgements).to_markdown()
    assert "interior_match_ratio=0.000 (vs 0.800)" in markdown
    assert "rare_token_overlap=absent (present 100%)" in markdown
