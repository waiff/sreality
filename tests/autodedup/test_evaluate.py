"""The Wilson bound, the cluster split, the fit and its command, and SW1's M1-M5 / D83 read."""

from __future__ import annotations

import gzip
import io
import json
import os
import random
from pathlib import Path
from typing import Any, Sequence

import pytest

from autodedup import evaluate as ev
from autodedup import harness
from autodedup.labels import Label
from autodedup.model import LogisticModel
from autodedup.settings import Settings

# The W3 fake-judge outputs live in the session scratchpad, not in the repo: the smoke test runs
# against them when they are there and skips in CI, which never has them.
REAL_ARTIFACTS = Path(
    os.environ.get(
        "AUTODEDUP_W3_ARTIFACTS",
        "/tmp/claude-1000/-home-hejtm-dev-sreality/"
        "e6b78d33-4258-44e0-b4ff-c53da4ed8c50/scratchpad",
    )
)


def pair_row(
    lo: int,
    hi: int,
    zone: str = "merge",
    score: float = 0.99,
    certificate: str | None = None,
    block: str = "praha",
    cross: bool = True,
    **feats: float,
) -> dict[str, Any]:
    # E45 is a gate on the pair, not on the cut: a fixture row with no unit-grade evidence can
    # never enter the merge zone, so a threshold test built on one would measure an empty merge
    # set. The default carries the cheapest arm (rare tokens); a test about the gate overrides it.
    feats.setdefault("rare_token_overlap", 3.0)
    return {
        "lo": lo,
        "hi": hi,
        "zone": zone,
        "score": score,
        "certificate": certificate,
        "block": block,
        "cross_source": cross,
        "source_pair": "bazos|sreality" if cross else "sreality|sreality",
        "families": ["ATTR", "TEXT"],
        "veto": None,
        "probes": ["attr_area"],
        "feats": {name: [value, True] for name, value in feats.items()},
    }


def label(
    lo: int,
    hi: int,
    y: int | None,
    *,
    weight: float = 1.0,
    tier: str = "text",
    stratum: str | None = None,
) -> Label:
    verdict = {1: "same_property", 0: "different_property",
               None: "insufficient_evidence"}[y]
    return Label(lo=lo, hi=hi, y=y, verdict=verdict, tier=tier, confidence=0.8,
                 weight=weight, must_not_link=False, developer_project_suspected=False,
                 stratum=stratum)


# --- intervals -----------------------------------------------------------------------------


def test_wilson_on_a_perfect_sample_is_n_over_n_plus_z_squared() -> None:
    # PROGRAM.md §6 rounds this to "n >= 380"; exactly, k=n gives n/(n+z^2), so 380 lands at
    # 0.98999 and 381 is the first n that truly clears a 0.99 lower bound.
    lower = ev.wilson_lower(380, 380)
    assert lower == pytest.approx(380 / (380 + 1.96 ** 2), abs=1e-12)
    assert round(lower, 3) == 0.99
    assert ev.wilson_lower(381, 381) >= 0.99
    assert ev.wilson_lower(379, 380) < 0.99


def test_wilson_known_values_and_degenerate_cases() -> None:
    low, high = ev.wilson_interval(50, 100)
    assert low == pytest.approx(0.4038, abs=5e-4)
    assert high == pytest.approx(0.5962, abs=5e-4)
    assert ev.wilson_interval(0, 0) == (0.0, 1.0)
    assert ev.wilson_interval(0, 10)[0] == 0.0
    assert ev.wilson_interval(10, 10)[1] == 1.0
    with pytest.raises(ValueError):
        ev.wilson_interval(11, 10)


# --- the cluster split ---------------------------------------------------------------------


def test_split_is_deterministic_and_roughly_sixty_twenty_twenty() -> None:
    assert ev.split_of(17) == ev.split_of(17)
    assert ev.split_of(17, seed=1) == ev.split_of(17, seed=1)
    counts = {"train": 0, "validation": 0, "test": 0}
    for group in range(4000):
        counts[ev.split_of(group)] += 1
    assert 0.55 < counts["train"] / 4000 < 0.65
    assert 0.16 < counts["validation"] / 4000 < 0.24
    assert 0.16 < counts["test"] / 4000 < 0.24


def test_a_listing_never_lands_in_two_splits_even_when_a_negative_straddles(
) -> None:
    # The channel that can actually leak: a judged NEGATIVE joining two merge components. Pinning
    # it to one root would put listing 10's evidence in two splits; `split_groups` unions it.
    rows = [pair_row(1, 2, zone="merge"), pair_row(2, 3, zone="merge"),
            pair_row(1, 3, zone="merge")]
    rows += [pair_row(10, 11, zone="merge"), pair_row(11, 12, zone="merge"),
             pair_row(10, 12, zone="merge")]
    rows += [pair_row(3, 10, zone="reject", score=0.01)]
    labels = {
        (1, 2): label(1, 2, 1), (2, 3): label(2, 3, 1), (1, 3): label(1, 3, 0),
        (10, 11): label(10, 11, 1), (11, 12): label(11, 12, 0), (10, 12): label(10, 12, 1),
        (3, 10): label(3, 10, 0),
    }
    groups = ev.split_groups(rows, labels)
    assert groups[3] == groups[10]
    _, report = ev.fit_model(rows, labels, epochs=20)
    check = report.sections["split"]["leakage_check"]
    assert check["n_listings_in_multiple_splits"] == 0
    assert check["n_groups_spanning_splits"] == 0


def test_the_leakage_check_can_actually_fail() -> None:
    # Fed the pair-root pinning the old split used, the check must SEE the leak rather than
    # report a property that is true by construction.
    buckets = {
        "train": [{"group": 1, "key": (1, 2)}, {"group": 1, "key": (2, 3)}],
        "validation": [],
        "test": [{"group": 10, "key": (3, 10)}],
    }
    check = ev._leakage_check(buckets)
    assert check["n_groups_spanning_splits"] == 0  # the vacuous property still holds
    assert check["n_listings_in_multiple_splits"] == 1
    assert check["listings_in_multiple_splits"] == [3]
    assert check["n_rows_touching_a_leaked_listing"] == 2


def planted_rows(n: int = 300, seed: int = 11) -> tuple[list[dict[str, Any]], dict[Any, Label]]:
    """A signal the hand priors cannot see: `gap_days` carries PRIOR_WEIGHTS 0.0."""
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    labels: dict[Any, Label] = {}
    for index in range(n):
        lo, hi = 2 * index + 1, 2 * index + 2
        signal = index / n
        y = 1 if signal > 0.5 else 0
        rows.append(pair_row(lo, hi, zone="band", score=0.5,
                             gap_days=signal + rng.gauss(0.0, 0.04)))
        labels[(lo, hi)] = label(lo, hi, y)
    return rows, labels


def test_the_fit_beats_the_hand_prior_on_a_planted_signal() -> None:
    rows, labels = planted_rows()
    model, report = ev.fit_model(rows, labels, epochs=600)
    test_metrics = report.sections["test_metrics"]
    assert test_metrics["n"] > 20
    assert test_metrics["auc_prior"] == pytest.approx(0.5, abs=0.05)
    assert test_metrics["auc"] > 0.9
    assert test_metrics["auc"] > test_metrics["auc_prior"] + 0.3
    assert test_metrics["log_loss"] < test_metrics["log_loss_prior"]
    assert model.weights["gap_days"] > 0.5
    assert report.sections["reliability_test"]
    assert report.sections["model_version"].startswith("fit_")


def test_abstentions_and_unlabelled_rows_never_reach_the_fit() -> None:
    rows, labels = planted_rows(n=60)
    keys = sorted(labels)
    labels[keys[0]] = label(keys[0][0], keys[0][1], None)
    del labels[keys[1]]
    _, report = ev.fit_model(rows, labels, epochs=20)
    split = report.sections["split"]
    assert split["n_abstain_skipped"] == 1
    assert split["n_unlabelled_skipped"] == 1
    assert split["train"] + split["validation"] + split["test"] == 58


def test_fit_refuses_a_split_it_does_not_implement_and_an_empty_train_set() -> None:
    rows, labels = planted_rows(n=10)
    with pytest.raises(ValueError):
        ev.fit_model(rows, labels, split="pair")
    with pytest.raises(ValueError):
        ev.fit_model(rows, {}, epochs=5)


# --- reports and the CLI ---------------------------------------------------------------------


def write_run(tmp_path: Path, rows: Sequence[dict[str, Any]]) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(run_dir / harness.PAIRS_FILE, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    (run_dir / harness.RUN_FILE).write_text(
        json.dumps({"settings": Settings().to_dict()}), encoding="utf-8"
    )
    return run_dir


def write_judgements(path: Path, labels: dict[Any, Label], tier: str = "text") -> Path:
    lines = []
    for (lo, hi), item in sorted(labels.items()):
        lines.append(json.dumps({
            "lo": lo, "hi": hi, "tier": tier, "model": "fake", "stratum": "merge|model|praha|cross",
            "cost_usd": 0.001,
            "verdict": {"verdict": item.verdict, "confidence": 0.8,
                        "developer_project_suspected": False},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_write_report_writes_both_files_and_never_doubles_a_suffix(tmp_path: Path) -> None:
    class Stub:
        title = "stub"

        def to_json(self) -> dict[str, Any]:
            return {"counts": {"run_pairs": 0}}

        def to_markdown(self) -> str:
            return "# hi"

    json_path, markdown_path = ev.write_report(Stub(), tmp_path / "out" / "eval.json")
    assert json_path.name == "eval.json" and markdown_path.name == "eval.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["counts"]["run_pairs"] == 0
    assert markdown_path.read_text(encoding="utf-8") == "# hi"


def test_fit_command_writes_an_activatable_model(tmp_path: Path) -> None:
    rows, labels = planted_rows(n=120)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    out = io.StringIO()
    code = harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements),
         "--out", str(tmp_path / "fit")], out=out
    )
    assert code == 0
    model_path = tmp_path / "fit" / harness.MODEL_FILE
    model = LogisticModel.from_json(json.loads(model_path.read_text(encoding="utf-8")))
    assert model.version.startswith("fit_")
    assert model.feature_order
    fit_json = json.loads((tmp_path / "fit" / "fit.json").read_text(encoding="utf-8"))
    assert set(fit_json) >= {"split", "test_metrics", "weights", "calibration", "convergence"}
    assert (tmp_path / "fit" / "fit.md").is_file()


@pytest.mark.skipif(
    not (REAL_ARTIFACTS / "run1" / harness.PAIRS_FILE).is_file()
    or not (REAL_ARTIFACTS / "w3out" / "text200" / "judgements.jsonl").is_file(),
    reason="W3 scratchpad artifacts are not present",
)
def test_smoke_fit_over_the_real_run_and_judgements(tmp_path: Path) -> None:
    out = io.StringIO()
    code = harness.main(
        ["fit", str(REAL_ARTIFACTS / "run1"),
         "--judgements", str(REAL_ARTIFACTS / "w3out" / "text200" / "judgements.jsonl"),
         "--epochs", "50", "--out", str(tmp_path / "fit")], out=out
    )
    assert code == 0
    payload = json.loads((tmp_path / "fit" / "fit.json").read_text(encoding="utf-8"))
    assert payload["split"]["leakage_check"]["n_listings_in_multiple_splits"] == 0
    assert payload["split"]["seal"]["sha256"]
    assert (tmp_path / "fit" / harness.SPLIT_MAP_FILE).is_file()


# --- the design-based interval ----------------------------------------------------------------


def test_design_effect_reports_the_effective_sample_size() -> None:
    body = ev.design_effect([1.0] * 10)
    assert body["n_effective"] == pytest.approx(10.0)
    assert body["design_effect"] == pytest.approx(1.0)
    skewed = ev.design_effect([100.0] + [1.0] * 9)
    assert skewed["n_effective"] < 2.0


# --- the fit: weights, seal, calibration honesty ------------------------------------------------


def test_the_fit_stamps_a_split_seal_a_later_refit_can_reuse() -> None:
    rows, labels = planted_rows(n=60)
    groups = ev.split_groups(rows, labels)
    _, first = ev.fit_model(rows, labels, epochs=20)
    _, second = ev.fit_model(rows, labels, epochs=20, split_map=groups)
    assert first.sections["split"]["seal"] == second.sections["split"]["seal"]
    assert second.sections["split"]["from_split_map"] is True
    assert first.sections["split"]["seal"]["sha256"] != ev.split_seal({1: 1})["sha256"]
    # a model change moves the merge edges, so the seal is what keeps the holdout comparable
    merged = [pair_row(1, 3, zone="merge"), pair_row(5, 7, zone="merge")] + rows
    assert ev.split_seal(ev.split_groups(merged, labels))["sha256"] \
        != first.sections["split"]["seal"]["sha256"]


def test_validation_ece_is_labelled_in_sample_and_the_gate_reads_the_test_split() -> None:
    rows, labels = planted_rows(n=200)
    _, report = ev.fit_model(rows, labels, epochs=200)
    calibration = report.sections["calibration"]
    assert calibration["pre_calibration_ece_validation"] is not None
    assert calibration["test_ece"] == report.sections["test_metrics"]["ece"]
    assert calibration["ece_gate"] == 0.05
    headline = "\n".join(report.headline())
    assert "ECE in-sample" in headline
    assert "calibration E21" in headline


def test_a_fit_that_hits_the_epoch_ceiling_says_so_in_the_headline() -> None:
    rows, labels = planted_rows(n=120)
    _, report = ev.fit_model(rows, labels, method="gd", epochs=2)
    assert report.sections["convergence"]["converged_flag"] is False
    headline = "\n".join(report.headline())
    assert "NOT CONVERGED" in headline
    assert "mean|grad|" in headline and "--epochs" in headline


def test_a_newton_fit_that_hits_its_iteration_ceiling_says_so_in_its_own_terms() -> None:
    rows, labels = planted_rows(n=120)
    _, report = ev.fit_model(rows, labels, max_iter=1)
    convergence = report.sections["convergence"]
    assert convergence["converged_flag"] is False
    assert convergence["method"] == "irls" and convergence["max_iter"] == 1
    headline = "\n".join(report.headline())
    assert "NOT CONVERGED" in headline
    assert "max|delta|" in headline and "--max-iter" in headline


def test_irls_is_the_default_and_converges_where_the_gradient_fit_stalls() -> None:
    rows, labels = planted_rows(n=300)
    newton, newton_report = ev.fit_model(rows, labels)
    descent, descent_report = ev.fit_model(rows, labels, method="gd", epochs=3000)
    convergence = newton_report.sections["convergence"]
    assert convergence["method"] == "irls"
    assert convergence["converged_flag"] is True
    assert convergence["iterations"] < 30
    assert set(convergence) >= {"iterations", "max_abs_delta", "log_loss", "converged"}
    assert "converged  " in "\n".join(newton_report.headline())
    # The whole point of the swap: the same penalised objective, reached rather than approached.
    assert newton.fit_report["log_loss"] <= descent.fit_report["log_loss"] + 1e-9
    assert newton_report.sections["test_metrics"]["auc"] >= (
        descent_report.sections["test_metrics"]["auc"] - 0.02
    )


def test_fit_model_refuses_an_optimiser_it_does_not_implement() -> None:
    rows, labels = planted_rows(n=20)
    with pytest.raises(ValueError, match="newton-raphson"):
        ev.fit_model(rows, labels, method="newton-raphson")


def test_the_fit_cli_exposes_the_knobs_and_can_fail_on_a_short_fit(tmp_path: Path) -> None:
    rows, labels = planted_rows(n=120)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    out = io.StringIO()
    code = harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements),
         "--method", "gd", "--epochs", "2",
         "--l2", "0.01", "--weight-cap", "3.0", "--require-convergence",
         "--out", str(tmp_path / "fit")], out=out
    )
    assert code == 1  # the model is still written; the exit code is the loud part
    fit_json = json.loads((tmp_path / "fit" / "fit.json").read_text(encoding="utf-8"))
    assert fit_json["convergence"]["epochs"] == 2 and fit_json["convergence"]["l2"] == 0.01
    assert fit_json["convergence"]["method"] == "gd"
    assert fit_json["split"]["weight_cap"] == 3.0
    split_map = json.loads(
        (tmp_path / "fit" / harness.SPLIT_MAP_FILE).read_text(encoding="utf-8")
    )
    assert split_map
    out2 = io.StringIO()
    assert harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements), "--method", "gd", "--epochs", "2",
         "--split-map", str(tmp_path / "fit" / harness.SPLIT_MAP_FILE),
         "--out", str(tmp_path / "fit2")], out=out2
    ) == 0
    reused = json.loads((tmp_path / "fit2" / "fit.json").read_text(encoding="utf-8"))
    assert reused["split"]["seal"] == fit_json["split"]["seal"]
    assert reused["split"]["from_split_map"] is True


def test_the_fit_cli_defaults_to_newton_and_can_fail_on_a_short_newton_fit(
    tmp_path: Path,
) -> None:
    rows, labels = planted_rows(n=120)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    out = io.StringIO()
    assert harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements), "--require-convergence",
         "--out", str(tmp_path / "fit")], out=out
    ) == 0
    fit_json = json.loads((tmp_path / "fit" / "fit.json").read_text(encoding="utf-8"))
    assert fit_json["convergence"]["method"] == "irls"
    assert fit_json["convergence"]["converged_flag"] is True
    assert fit_json["convergence"]["max_iter"] == ev.FIT_MAX_ITER
    model = LogisticModel.from_json(
        json.loads((tmp_path / "fit" / harness.MODEL_FILE).read_text(encoding="utf-8")))
    assert model.fit_report["iterations"] >= 1.0
    short = io.StringIO()
    assert harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements), "--method", "irls",
         "--max-iter", "1", "--require-convergence", "--out", str(tmp_path / "fit1")], out=short
    ) == 1


VISION_JUDGEMENTS = (
    REAL_ARTIFACTS / "judge" / "35116313682" / "autodedup-judge-35116313682"
    / "judgements.jsonl"
)
TEXT_JUDGEMENTS = (
    REAL_ARTIFACTS / "judge" / "35115036274" / "autodedup-judge-35115036274"
    / "judgements.jsonl"
)


@pytest.mark.skipif(
    not (REAL_ARTIFACTS / "run1" / harness.PAIRS_FILE).is_file()
    or not VISION_JUDGEMENTS.is_file()
    or not TEXT_JUDGEMENTS.is_file(),
    reason="the W4 run and judge artifacts are not in the scratchpad",
)
def test_smoke_newton_fit_over_the_real_run_and_both_judge_tiers(tmp_path: Path) -> None:
    """The fit the W4b weights actually come from: both tiers, the real sample, Newton."""
    out = io.StringIO()
    code = harness.main(
        ["fit", str(REAL_ARTIFACTS / "run1"),
         "--judgements", str(VISION_JUDGEMENTS),
         "--judgements", str(TEXT_JUDGEMENTS),
         "--sample", str(VISION_JUDGEMENTS.parent / "sample.json"),
         "--method", "irls", "--require-convergence",
         "--out", str(tmp_path / "fit")], out=out
    )
    assert code == 0
    payload = json.loads((tmp_path / "fit" / "fit.json").read_text(encoding="utf-8"))
    convergence = payload["convergence"]
    assert convergence["method"] == "irls"
    assert convergence["converged_flag"] is True
    assert convergence["iterations"] <= ev.FIT_MAX_ITER
    assert payload["split"]["leakage_check"]["n_listings_in_multiple_splits"] == 0
    assert payload["test_metrics"]["auc"] > 0.85
    # The real cohort is where the unidentified columns actually live: always-on presence flags
    # duplicate the intercept, and exactly-collinear presence groups share one effect.
    design = payload["design"]
    assert design["reported"] is True
    assert design["dropped_terms"] and design["n_identified"] < design["n_terms"]
    assert design["pinned_terms"] == []


def test_the_convergence_section_carries_only_the_ceiling_that_applied() -> None:
    """`epochs: 3000` beside a 10-step Newton fit is evidence about a knob nobody consulted."""
    rows, labels = planted_rows(n=120)
    _, newton = ev.fit_model(rows, labels)
    _, descent = ev.fit_model(rows, labels, method="gd", epochs=50)
    assert "max_iter" in newton.sections["convergence"]
    assert "epochs" not in newton.sections["convergence"]
    assert "epochs" in descent.sections["convergence"]
    assert "max_iter" not in descent.sections["convergence"]
    assert newton.sections["convergence"]["tol"] == pytest.approx(1e-8)
    assert descent.sections["convergence"]["tol"] == pytest.approx(1e-6)


def test_the_model_version_names_the_optimiser_that_produced_the_weights() -> None:
    """`model_version` is what reaches the scored rows: two optimisers are two coefficient sets."""
    rows, labels = planted_rows(n=120)
    newton, newton_report = ev.fit_model(rows, labels)
    descent, descent_report = ev.fit_model(rows, labels, method="gd", epochs=20)
    assert newton.version.startswith("fit_irls_")
    assert descent.version.startswith("fit_gd_")
    assert newton.version != descent.version
    assert newton_report.sections["model_version"] == newton.version
    assert descent_report.sections["model_version"] == descent.version
    assert ev.fit_model(rows, labels, version="pinned")[0].version == "pinned"


def test_the_newton_fit_reports_the_terms_it_could_not_identify() -> None:
    """A presence flag that is always 1 IS the intercept; the audit must not read it as evidence."""
    rows, labels = planted_rows(n=150)
    model, report = ev.fit_model(rows, labels)
    design = report.sections["design"]
    assert design["reported"] is True
    assert design["n_terms"] > design["n_identified"]
    assert "gap_days:present" in design["dropped_terms"]  # every planted row carries it
    assert model.presence_weights["gap_days"] == 0.0
    for name in design["dropped_terms"]:
        feature = name.removesuffix(":present")
        if name.endswith(":present"):
            assert model.presence_weights.get(feature, 0.0) == 0.0, name
        elif "*" not in name:
            assert model.weights.get(feature, 0.0) == 0.0, name
    headline = "\n".join(report.headline())
    assert "design  " in headline and "identified" in headline
    _, descent = ev.fit_model(rows, labels, method="gd", epochs=20)
    assert descent.sections["design"]["reported"] is False


def test_the_fit_cli_can_loosen_the_convergence_certificate(tmp_path: Path) -> None:
    rows, labels = planted_rows(n=120)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    out = io.StringIO()
    assert harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements), "--tol", "1e-2",
         "--require-convergence", "--out", str(tmp_path / "loose")], out=out
    ) == 0
    loose = json.loads((tmp_path / "loose" / "fit.json").read_text(encoding="utf-8"))
    assert loose["convergence"]["tol"] == 1e-2
    tight = io.StringIO()
    assert harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements),
         "--out", str(tmp_path / "tight")], out=tight
    ) == 0
    strict = json.loads((tmp_path / "tight" / "fit.json").read_text(encoding="utf-8"))
    assert loose["convergence"]["iterations"] <= strict["convergence"]["iterations"]


# --- W4c: the l2 sweep, the calibration bake-off, and per-stratum thresholds -------------------


def test_the_l2_sweep_reports_every_penalty_and_keeps_the_best_validation_log_loss() -> None:
    rows, labels = planted_rows(n=400)
    model, report = ev.fit_model(rows, labels, l2_grid=(0.001, 0.03, 0.3))
    sweep = report.sections["l2_grid"]
    assert [row["l2"] for row in sweep["rows"]] == [0.001, 0.03, 0.3]
    assert sweep["chosen"] == min(sweep["rows"], key=lambda row: row["selection_log_loss"])["l2"]
    assert sweep["selected_on"] == "validation"
    # the penalty that was actually fitted is the one the convergence section reports
    assert report.sections["convergence"]["l2"] == sweep["chosen"]
    assert f"l2={sweep['chosen']:g}" in "\n".join(report.headline())
    assert model.fit_report["converged"] == 1.0


def test_the_l2_sweep_breaks_a_tie_towards_the_stiffer_penalty() -> None:
    rows, labels = planted_rows(n=120)
    # one penalty, twice: identical fits, so the tie-break is the only thing that can decide
    _, report = ev.fit_model(rows, labels, l2_grid=(0.01, 0.01))
    assert report.sections["l2_grid"]["grid"] == [0.01]
    _, wider = ev.fit_model(rows, labels, l2_grid=(0.01, 0.02))
    rows_out = {row["l2"]: row["selection_log_loss"] for row in wider.sections["l2_grid"]["rows"]}
    if rows_out[0.01] == rows_out[0.02]:
        assert wider.sections["l2_grid"]["chosen"] == 0.02


def test_no_grid_means_no_sweep_and_the_caller_s_penalty_stands() -> None:
    rows, labels = planted_rows(n=120)
    _, report = ev.fit_model(rows, labels, l2=0.05)
    assert report.sections["l2_grid"]["rows"] == []
    assert report.sections["l2_grid"]["chosen"] == 0.05
    assert report.sections["convergence"]["l2"] == 0.05


def test_the_calibration_map_is_chosen_out_of_fold_not_in_sample() -> None:
    rows, labels = planted_rows(n=400)
    _, report = ev.fit_model(rows, labels)
    calibration = report.sections["calibration"]
    assert calibration["method"] in ev.CALIBRATION_METHODS
    assert calibration["selected_by"] == "out-of-fold validation ECE (design-weighted)"
    candidates = calibration["candidates"]
    assert set(candidates) == set(ev.CALIBRATION_METHODS)
    scored = {name: body["ece"] for name, body in candidates.items() if body["ece"] is not None}
    assert calibration["method"] == min(scored, key=lambda name: scored[name])
    # every out-of-fold prediction came from a map fitted without it
    for body in candidates.values():
        assert body["n_out_of_fold"] + body["n_skipped"] == report.sections[
            "validation_metrics"]["n"]
    assert "out-of-fold" in "\n".join(report.headline())


def test_a_named_calibration_map_is_used_without_a_bake_off() -> None:
    rows, labels = planted_rows(n=200)
    model, report = ev.fit_model(rows, labels, calibration="platt")
    assert report.sections["calibration"]["method"] == "platt"
    assert report.sections["calibration"]["selected_by"] == "caller"
    assert model.calibration_kind == "platt"
    with pytest.raises(ValueError):
        ev.fit_model(rows, labels, calibration="beta")


def test_the_sealed_test_ece_is_the_one_the_gate_reads() -> None:
    rows, labels = planted_rows(n=400)
    _, report = ev.fit_model(rows, labels)
    calibration = report.sections["calibration"]
    # the in-sample number is published too, and never as the gate
    assert calibration["test_ece"] == report.sections["test_metrics"]["ece"]
    assert calibration["ece_gate_ok"] == (calibration["test_ece"] <= 0.05)
    assert calibration["post_calibration_ece_validation_in_sample"] == report.sections[
        "validation_metrics"]["ece"]


def test_the_bake_off_reports_the_ceiling_a_threshold_cannot_reach() -> None:
    rows, labels = planted_rows(n=400)
    _, report = ev.fit_model(rows, labels)
    candidates = report.sections["calibration"]["candidates"]
    isotonic, platt = candidates["isotonic"], candidates["platt"]
    # isotonic cannot return more than its top bin's observed rate; Platt has no such cap, and a
    # T_hi above the ceiling would switch the score layer off without saying so
    assert isotonic["ceiling"] is not None and platt["ceiling"] is not None
    assert isotonic["n_merge_end"] == platt["n_merge_end"]
    assert "ceiling" in "\n".join(report.headline())
    # The mechanism the ceiling exists to expose: a top bin that is 90% positive caps isotonic at
    # 0.90, so a T_hi of 0.97 would merge nothing at all, while Platt keeps rising past it.
    probs = [0.05] * 50 + [0.95] * 50
    ys = [0] * 50 + [1] * 45 + [0] * 5
    capped = ev._mapped(probs, ys, [0.999999], "isotonic", 10)
    uncapped = ev._mapped(probs, ys, [0.999999], "platt", 10)
    assert capped[0] < 0.97 < uncapped[0]


# --- SW1: one run against one rulings file (M1-M5, the D83 read lists) --------------------------


def _write_rulings(root: Path, labels: Sequence[dict[str, Any]] = (),
                   merges: Sequence[dict[str, Any]] = (),
                   negatives: Sequence[dict[str, Any]] = ()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, rows in zip(ev.RULINGS_FILES, (labels, merges, negatives)):
        (root / name).write_text("".join(json.dumps(row) + "\n" for row in rows),
                                 encoding="utf-8")
    return root


def _run(rows: Sequence[dict[str, Any]], groups: dict[int, Sequence[int]]) -> ev.RunView:
    return ev.RunView({(row["lo"], row["hi"]): row for row in rows},
                      {key: tuple(sorted(members)) for key, members in groups.items()})


def test_the_newest_ruling_wins_across_the_three_files(tmp_path: Path) -> None:
    rulings = ev.read_rulings(_write_rulings(
        tmp_path,
        labels=[{"listing_lo": 2, "listing_hi": 1, "verdict": "same", "decided_at": "2026-09-01"},
                {"listing_lo": 3, "listing_hi": 4, "verdict": "same_building_different_unit",
                 "decided_at": "2026-09-01"},
                {"listing_lo": 5, "listing_hi": 6, "verdict": "unsure", "decided_at": "2026-09-01"}],
        merges=[{"merged_at": "2026-09-02", "members": [{"listing_id": 3}, {"listing_id": 4},
                                                         {"listing_id": 7}]}],
        negatives=[{"listing_lo": 1, "listing_hi": 2, "created_at": "2026-09-03"}]))
    assert rulings == {(1, 2): "different", (3, 4): "same", (3, 7): "same", (4, 7): "same"}


def test_m1_to_m5_read_the_groups_against_the_rulings() -> None:
    run = _run([{"lo": 1, "hi": 2, "zone": "merge", "certificate": "K-C", "families": ["IMG"]},
                {"lo": 2, "hi": 3, "zone": "merge", "families": ["TXT", "IMG"]},
                {"lo": 5, "hi": 6, "zone": "band", "families": []}],
               {1: (1, 2, 3), 7: (7, 8)})
    rulings = {(1, 2): "same", (1, 3): "different", (5, 6): "same", (8, 9): "same",
               (7, 99): "different"}
    explicit = {(8, 9): {"zone": "reject", "veto": "area", "families": []}}
    out = ev.measure(run, rulings, set(range(1, 10)), explicit)
    assert out["M1_precision"]["co_clustered_ruled"] == 2
    assert out["M1_precision"]["ruled_different_together"] == 1
    assert out["M1_precision"]["precision"] == 0.5
    assert out["M1_precision"]["wilson_lower"] is None, "4 rulings are not a measurement"
    assert out["M2_recall"] == {"ruled_same": 3, "together": 1, "recall": 1 / 3}
    assert out["M3_purity"] == {"groups": 2, "with_a_ruled_different_pair": 1, "purity": 0.5}
    assert out["M5_coverage"]["ruled"] == 4 and not out["M5_coverage"]["measurable"]
    table = out["M4_attribution"]
    assert table["co_clustered"]["merge|proof:K-C"] == {"n": 1, "IMG": 1}
    assert table["co_clustered"]["none|not_decided"]["n"] == 2, "1 x 3 and 7 x 8: no stored row"
    assert table["ruled"]["same"]["reject|veto:area"] == {"n": 1, "NONE": 1}
    assert table["ruled"]["same"]["band|score"] == {"n": 1, "NONE": 1}


def test_the_d83_lists_name_every_gain_and_loss_with_its_ruling() -> None:
    base = _run([{"lo": 1, "hi": 2, "zone": "merge"}, {"lo": 3, "hi": 4, "zone": "band"}],
                {1: (1, 2)})
    arm = _run([{"lo": 1, "hi": 2, "zone": "band"}, {"lo": 3, "hi": 4, "zone": "merge"},
                {"lo": 5, "hi": 6, "zone": "reject"}], {3: (3, 4)})
    out = ev.read_lists(base, arm, {(3, 4): "different"}, {(1, 2): "CD"})
    lists, counts = out["lists"], out["counts"]
    assert lists["copairs_gained"] == [[3, 4, "different", None]]
    assert lists["copairs_lost"] == [[1, 2, "unruled", "CD"]]
    assert lists["merge_gained"] == [[3, 4]] and lists["merge_lost"] == [[1, 2]]
    assert lists["pairs_new"] == [[5, 6]] and lists["pairs_lost"] == []
    assert counts["zone_moves"] == {"band->merge": 1, "merge->band": 1}
    assert lists["groups_arm_only"] == [[3, 4]] and lists["groups_base_only"] == [[1, 2]]
    assert counts["copairs_gained_by_ruling"] == {"different": 1}
