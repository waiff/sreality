"""Intervals, Horvitz-Thompson arithmetic, thresholds, the cluster split, and the two commands."""

from __future__ import annotations

import gzip
import io
import json
import math
import os
import random
from pathlib import Path
from typing import Any, Sequence

import pytest

from autodedup import evaluate as ev
from autodedup import harness
from autodedup.labels import Label, Sample, Stratum
from autodedup.model import LogisticModel, hand_initialised
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


def two_stratum_sample(assignments: dict[tuple[int, int], str],
                       populations: dict[str, tuple[int, int]]) -> Sample:
    return Sample(
        strata={name: Stratum(name, selected, total)
                for name, (selected, total) in populations.items()},
        pair_stratum=dict(assignments),
    )


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


def test_clopper_pearson_matches_the_published_table() -> None:
    for k, n, expected in (
        (0, 10, (0.0, 0.30850)),
        (1, 10, (0.00253, 0.44502)),
        (5, 10, (0.18709, 0.81291)),
        (9, 10, (0.55498, 0.99747)),
        (10, 10, (0.69150, 1.0)),
    ):
        low, high = ev.clopper_pearson(k, n)
        assert low == pytest.approx(expected[0], abs=1e-4)
        assert high == pytest.approx(expected[1], abs=1e-4)


def test_clopper_pearson_is_wider_than_wilson_and_validates_its_inputs() -> None:
    exact = ev.clopper_pearson(95, 100)
    wilson = ev.wilson_interval(95, 100)
    assert exact[0] < wilson[0] and exact[1] > wilson[1]
    assert ev.clopper_pearson(0, 0) == (0.0, 1.0)
    with pytest.raises(ValueError):
        ev.clopper_pearson(-1, 10)


def test_betainc_agrees_with_closed_forms() -> None:
    assert ev._betainc(1.0, 1.0, 0.37) == pytest.approx(0.37, abs=1e-12)
    assert ev._betainc(2.0, 1.0, 0.5) == pytest.approx(0.25, abs=1e-12)
    assert ev._beta_quantile(0.25, 2.0, 1.0) == pytest.approx(0.5, abs=1e-9)


# --- evaluate ------------------------------------------------------------------------------


def test_merge_precision_splits_by_certificate_and_keeps_abstentions_out() -> None:
    rows = [
        pair_row(1, 2, certificate="K-A"),
        pair_row(3, 4, certificate="K-A"),
        pair_row(5, 6, certificate=None),
        pair_row(7, 8, certificate=None),
        pair_row(9, 10, certificate="K-C"),
    ]
    labels = {
        (1, 2): label(1, 2, 1),
        (3, 4): label(3, 4, 0),
        (5, 6): label(5, 6, 1),
        (7, 8): label(7, 8, None),
        (9, 10): label(9, 10, 1),
    }
    report = ev.evaluate(rows, labels, None, Settings())
    merge = report.sections["merge_precision"]
    assert merge["pooled"]["n_judged"] == 4
    assert merge["pooled"]["n_positive"] == 3
    assert merge["pooled"]["n_abstain"] == 1
    assert merge["by_certificate"]["K-A"]["rate"] == pytest.approx(0.5)
    assert merge["by_certificate"]["model"]["n_judged"] == 1
    assert merge["by_certificate"]["K-C"]["rate"] == 1.0


def test_horvitz_thompson_weights_a_two_stratum_draw() -> None:
    rows = [pair_row(i, i + 1) for i in range(1, 41, 2)]
    assignments: dict[tuple[int, int], str] = {}
    labels: dict[tuple[int, int], Label] = {}
    for index, row in enumerate(rows):
        key = (row["lo"], row["hi"])
        stratum = "thin" if index < 10 else "fat"
        assignments[key] = stratum
        positive = index % 10 < (9 if stratum == "thin" else 5)
        labels[key] = label(key[0], key[1], 1 if positive else 0, stratum=stratum)
    sample = two_stratum_sample(assignments, {"thin": (10, 100), "fat": (10, 1000)})

    report = ev.evaluate(rows, labels, sample, Settings())
    pooled = report.sections["merge_precision"]["pooled"]
    assert pooled["n_judged"] == 20 and pooled["n_positive"] == 14
    assert pooled["rate"] == pytest.approx(0.70)
    # (9 x 10) + (5 x 100) over (10 x 10) + (10 x 100)
    assert pooled["weighted_rate"] == pytest.approx(590 / 1100)
    assert report.sections["cohort"]["merge_precision_ht"] == pytest.approx(590 / 1100)
    assert report.sections["cohort"]["weighted"] is True


def test_missed_positives_below_t_lo_are_weighted_by_stratum() -> None:
    rows = [
        pair_row(1, 2, zone="reject", score=0.10),
        pair_row(3, 4, zone="merge", score=0.99),
    ]
    labels = {(1, 2): label(1, 2, 1), (3, 4): label(3, 4, 1)}
    sample = two_stratum_sample(
        {(1, 2): "fat", (3, 4): "thin"}, {"thin": (10, 10), "fat": (1, 1000)}
    )
    cohort = ev.evaluate(rows, labels, sample, Settings()).sections["cohort"]
    assert cohort["weight_positive_total"] == pytest.approx(1001.0)
    assert cohort["weight_positive_below_t_lo"] == pytest.approx(1000.0)
    assert cohort["positives_below_t_lo_share"] == pytest.approx(1000 / 1001)


def test_agreement_is_reported_per_block_and_per_source_pair() -> None:
    rows = [
        pair_row(1, 2, zone="merge", block="praha", cross=True),
        pair_row(3, 4, zone="reject", score=0.01, block="praha", cross=True),
        pair_row(5, 6, zone="merge", block="brno", cross=False),
        pair_row(7, 8, zone="band", score=0.5, block="brno", cross=False),
    ]
    labels = {
        (1, 2): label(1, 2, 1),
        (3, 4): label(3, 4, 1),
        (5, 6): label(5, 6, 1),
        (7, 8): label(7, 8, 0),
    }
    agreement = ev.evaluate(rows, labels, None, Settings()).sections["agreement"]
    assert agreement["by_block"]["praha"] == {"n": 2, "n_agree": 1, "agreement": 0.5,
                                              "wilson_lb": pytest.approx(0.0947, abs=1e-3)}
    assert agreement["by_block"]["brno"]["n"] == 1  # the band pair is undecided, so excluded
    assert agreement["by_side"]["cross"]["n"] == 2
    assert agreement["by_source_pair"]["bazos|sreality"]["n_agree"] == 1


def test_insufficient_evidence_rate_is_reported_per_tier_and_fidelity_over_shared_pairs() -> None:
    rows = [pair_row(1, 2), pair_row(3, 4), pair_row(5, 6)]
    gold = {(1, 2): label(1, 2, 1, tier="gold"), (3, 4): label(3, 4, 0, tier="gold")}
    text = {(1, 2): label(1, 2, 1), (3, 4): label(3, 4, 1), (5, 6): label(5, 6, None)}
    report = ev.evaluate(rows, gold | {}, None, Settings(), by_tier={"gold": gold, "text": text})
    tiers = report.sections["tiers"]
    assert tiers["text"]["insufficient_evidence_rate"] == pytest.approx(1 / 3)
    assert tiers["gold"]["insufficient_evidence_rate"] == 0.0
    fidelity = report.sections["fidelity"]
    assert fidelity["by_cheap_tier"]["text"]["n_both_decided"] == 2
    assert fidelity["by_cheap_tier"]["text"]["agreement"] == pytest.approx(0.5)
    assert fidelity["pooled"]["n"] == 2


def test_labels_with_no_run_row_are_counted_not_silently_dropped() -> None:
    rows = [pair_row(1, 2)]
    labels = {(1, 2): label(1, 2, 1), (99, 100): label(99, 100, 1)}
    counts = ev.evaluate(rows, labels, None, Settings()).sections["counts"]
    assert counts["n_labels_off_run"] == 1
    assert counts["labels_off_run"] == [(99, 100)]


# --- thresholds ----------------------------------------------------------------------------


def attainable_rows() -> list[tuple[float, int, float, str]]:
    rows = [(0.90 + index * 0.00025, 1, 1.0, "merge") for index in range(400)]
    rows += [(index / 200.0, 0, 1.0, "merge") for index in range(100)]
    return rows


def test_t_hi_is_the_smallest_cut_that_clears_both_precision_gates() -> None:
    report = ev.thresholds(attainable_rows())
    assert report.t_hi == pytest.approx(0.90)
    assert report.n_at_t_hi == 400
    assert report.precision_at_t_hi == 1.0
    assert report.precision_lb_at_t_hi >= 0.99


def test_t_hi_is_none_when_the_sample_is_too_small_to_clear_it() -> None:
    rows = [(0.99, 1, 1.0, "merge") for _ in range(49)] + [(0.99, 0, 1.0, "merge")]
    report = ev.thresholds(rows)
    assert report.t_hi is None
    assert report.band_width is None
    assert report.n == 50 and report.n_positive == 49


def test_t_lo_spends_exactly_the_missed_positive_budget() -> None:
    rows = [(0.1, 1, 1.0, "a"), (0.2, 1, 1.0, "a"), (0.3, 1, 1.0, "a")]
    rows += [(0.95, 1, 1.0, "a") for _ in range(97)]
    report = ev.thresholds(rows)
    assert report.t_lo == pytest.approx(0.3)
    assert report.missed_share_at_t_lo == pytest.approx(0.03)


def test_t_lo_respects_the_weights_not_the_counts() -> None:
    rows = [(0.1, 1, 5.0, "fat")] + [(0.95, 1, 1.0, "thin") for _ in range(99)]
    report = ev.thresholds(rows)
    assert report.t_lo == 0.0  # the one cheap-looking positive stands for five: unaffordable
    assert report.missed_share_at_t_lo == 0.0


def test_band_width_is_the_share_between_the_two_thresholds() -> None:
    rows = attainable_rows() + [(0.5, 1, 1.0, "merge") for _ in range(2)]
    report = ev.thresholds(rows)
    # the two extra positives at 0.5 leave the tail pure, so the smallest clearing cut MOVES
    assert report.t_hi == pytest.approx(0.50)
    assert report.t_lo <= report.t_hi  # the floor of the band never climbs past its ceiling
    assert 0.0 < report.band_width < 1.0
    manual = sum(1 for score, _, _, _ in rows if report.t_lo < score < report.t_hi) / len(rows)
    assert report.band_width == pytest.approx(manual)  # every weight is 1.0 here
    assert report.band_width_source == "weighted_sample"


def test_thresholds_are_also_reported_per_stratum() -> None:
    rows = [(0.9, 1, 1.0, "clean") for _ in range(400)]
    rows += [(0.9, 0, 1.0, "dirty") for _ in range(20)]
    report = ev.thresholds(rows)
    assert report.per_stratum_t_hi["clean"] == pytest.approx(0.9)
    assert report.per_stratum_t_hi["dirty"] is None


def test_thresholds_accept_bare_score_label_pairs() -> None:
    report = ev.thresholds([(0.9, 1), (0.1, 0)])
    assert report.n == 2 and report.weight_positive == 1.0


# --- the cluster split ---------------------------------------------------------------------


def test_merge_components_carry_the_smallest_id_and_only_merge_edges_join() -> None:
    rows = [
        pair_row(1, 2, zone="merge"),
        pair_row(2, 3, zone="merge"),
        pair_row(3, 40, zone="reject", score=0.01),
        pair_row(10, 11, zone="merge"),
    ]
    groups = ev.merge_groups(rows)
    assert groups[1] == groups[2] == groups[3] == 1
    assert groups[10] == groups[11] == 10
    assert 40 not in groups
    assert ev.group_of((3, 40), groups) == 1
    assert ev.group_of((50, 51), groups) == 50


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
    components = ev.merge_groups(rows)
    assert components[3] != components[10]  # two components, and a labelled pair across them
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


def test_the_fit_weights_a_row_by_label_weight_times_stratum_weight_and_caps_it() -> None:
    rows, labels = planted_rows(n=60)
    keys = list(labels)
    sample = two_stratum_sample(
        {key: "fat" for key in keys}, {"fat": (1, 10_000)}
    )
    _, report = ev.fit_model(rows, labels, sample=sample, epochs=20, weight_cap=5.0)
    split = report.sections["split"]
    assert split["n_weights_capped"] == 60  # 1.0 label weight x 10,000 stratum weight, every row
    assert split["weight_cap"] == 5.0
    assert split["train"] + split["validation"] + split["test"] == 60


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


def test_evaluate_command_writes_the_report_and_prints_the_headline(tmp_path: Path) -> None:
    rows, labels = planted_rows(n=60)
    for row in rows:
        row["zone"] = "merge"
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    out = io.StringIO()
    code = harness.main(
        ["evaluate", str(run_dir), "--judgements", str(judgements),
         "--out", str(tmp_path / "eval")], out=out
    )
    assert code == 0
    payload = json.loads((tmp_path / "eval" / "eval.json").read_text(encoding="utf-8"))
    assert set(payload) >= {
        "title", "counts", "settings", "sample", "zones", "merge_precision",
        "band_composition", "reject_zone", "agreement", "cohort", "tiers", "fidelity",
        "thresholds",
    }
    assert (tmp_path / "eval" / "eval.md").is_file()
    assert "merge-zone precision" in out.getvalue()


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
    model = harness.load_model(str(model_path))
    assert model.version.startswith("fit_")
    assert model.feature_order
    fit_json = json.loads((tmp_path / "fit" / "fit.json").read_text(encoding="utf-8"))
    assert set(fit_json) >= {"split", "test_metrics", "weights", "calibration", "convergence"}
    assert (tmp_path / "fit" / "fit.md").is_file()


def test_the_commands_exit_one_on_a_missing_run_or_missing_judgements(tmp_path: Path) -> None:
    rows, labels = planted_rows(n=10)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    assert harness.main(["evaluate", str(tmp_path / "nope"), "--judgements", str(judgements),
                         "--out", str(tmp_path / "e1")], out=io.StringIO()) == 1
    assert harness.main(["evaluate", str(run_dir), "--judgements", str(tmp_path / "nope.jsonl"),
                         "--out", str(tmp_path / "e2")], out=io.StringIO()) == 1
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert harness.main(["evaluate", str(run_dir), "--judgements", str(empty),
                         "--out", str(tmp_path / "e3")], out=io.StringIO()) == 1
    assert harness.main(["fit", str(run_dir), "--judgements", str(empty),
                         "--out", str(tmp_path / "f1")], out=io.StringIO()) == 1


def test_the_precedence_flag_reaches_the_label_store(tmp_path: Path) -> None:
    rows, labels = planted_rows(n=20)
    run_dir = write_run(tmp_path, rows)
    text = write_judgements(tmp_path / "text.jsonl", labels, tier="text")
    out = io.StringIO()
    code = harness.main(
        ["evaluate", str(run_dir), "--judgements", str(text), "--precedence", "text",
         "--out", str(tmp_path / "eval")], out=out
    )
    assert code == 0


@pytest.mark.skipif(
    not (REAL_ARTIFACTS / "run1" / harness.PAIRS_FILE).is_file()
    or not (REAL_ARTIFACTS / "w3out" / "text200" / "judgements.jsonl").is_file(),
    reason="W3 scratchpad artifacts are not present",
)
def test_smoke_evaluate_over_the_real_run_and_judgements(tmp_path: Path) -> None:
    out = io.StringIO()
    code = harness.main(
        ["evaluate", str(REAL_ARTIFACTS / "run1"),
         "--judgements", str(REAL_ARTIFACTS / "w3out" / "gold30" / "judgements.jsonl"),
         "--judgements", str(REAL_ARTIFACTS / "w3out" / "text200" / "judgements.jsonl"),
         "--out", str(tmp_path / "eval")], out=out
    )
    assert code == 0
    payload = json.loads((tmp_path / "eval" / "eval.json").read_text(encoding="utf-8"))
    assert set(payload) >= {
        "title", "counts", "settings", "sample", "zones", "merge_precision",
        "band_composition", "reject_zone", "agreement", "cohort", "tiers", "fidelity",
        "thresholds", "thresholds_all_judged", "holdout", "precedence",
    }
    assert payload["counts"]["run_pairs"] > 1000
    assert payload["merge_precision"]["pooled"]["n_judged"] > 0
    assert payload["sample"]["n_cohort"] > 0
    assert math.isfinite(float(payload["thresholds"]["t_lo"]))
    # two judgements files are two draws, and each one's own sample.json is pooled into one
    # design (W4f) — a single draw would still read "judge_stratum"
    assert payload["sample"]["stratum_fn"].startswith("pooled(2 draws")
    assert payload["sample"]["is_weighted"] is True and payload["counts"]["weighted"] is True
    assert payload["sample"]["design"]["weight_max"] > 10
    assert payload["cohort"]["merge_precision_bootstrap"]["lb"] is not None
    assert payload["holdout"]["n_dev"] + payload["holdout"]["n_sealed"] > 0
    # one concept, one number: both blocks weight positives by the design weight alone
    assert payload["thresholds_all_judged"]["weight_positive"] == pytest.approx(
        payload["cohort"]["weight_positive_total"]
    )


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


def test_stratified_bootstrap_brackets_the_hajek_rate_and_beats_a_binomial_interval() -> None:
    # 10 judged pairs standing for 1,000 cohort pairs each, 10 standing for one: the unweighted
    # Wilson interval describes a sample nobody drew, so the gate needs a DESIGN interval.
    observations = [("fat", 1 if index < 5 else 0, 1000.0) for index in range(10)]
    observations += [("thin", 1, 1.0) for _ in range(10)]
    body = ev.stratified_bootstrap_ci(observations, draws=500, seed=3)
    assert body["estimate"] == pytest.approx(ev.hajek_rate(observations))
    assert body["lb"] < body["estimate"] < body["ub"]
    assert body["lb"] <= ev.wilson_lower(15, 20)  # the naive interval is anticonservative here
    assert body["n_strata"] == 2 and body["draws"] == 500
    assert body["design_effect"] > 1.5
    assert ev.stratified_bootstrap_ci([])["estimate"] is None


def test_design_effect_reports_the_effective_sample_size() -> None:
    body = ev.design_effect([1.0] * 10)
    assert body["n_effective"] == pytest.approx(10.0)
    assert body["design_effect"] == pytest.approx(1.0)
    skewed = ev.design_effect([100.0] + [1.0] * 9)
    assert skewed["n_effective"] < 2.0


def test_merge_precision_carries_a_bootstrap_interval_around_the_weighted_rate() -> None:
    rows = [pair_row(2 * i + 1, 2 * i + 2) for i in range(20)]
    labels = {}
    assignments = {}
    for index, row in enumerate(rows):
        key = (row["lo"], row["hi"])
        stratum = "thin" if index < 10 else "fat"
        assignments[key] = stratum
        labels[key] = label(key[0], key[1], 1 if index % 2 else 0, stratum=stratum)
    sample = two_stratum_sample(assignments, {"thin": (10, 20), "fat": (10, 2000)})
    pooled = ev.evaluate(rows, labels, sample, Settings(), draws=200).sections[
        "merge_precision"]["pooled"]
    boot = pooled["bootstrap"]
    assert boot["lb"] <= pooled["weighted_rate"] <= boot["ub"]
    assert boot["n"] == 20


# --- thresholds: the zone rule, the ties, the weights, the holdout ---------------------------


def scored(score: float, y: int, weight: float = 1.0, stratum: str = "s",
           **context: Any) -> ev.ScoredRow:
    return ev.ScoredRow(score=score, y=y, weight=weight, stratum=stratum, **context)


def test_t_lo_consumes_a_tie_group_whole_or_not_at_all() -> None:
    # 20 positives share one score: T_lo at that score rejects all 20 (`score <= t_lo`), which is
    # 20% of the positives, not the 3% the budget allows — so the cut must not be taken.
    rows = [(0.568254, 1, 1.0, "s")] * 20 + [(0.99, 1, 1.0, "s")] * 80
    report = ev.thresholds(rows)
    assert report.t_lo == 0.0
    assert report.missed_share_at_t_lo == 0.0
    affordable = [(0.1, 1, 1.0, "s"), (0.1, 1, 1.0, "s")] + [(0.99, 1, 1.0, "s")] * 98
    assert ev.thresholds(affordable, max_missed=0.05).t_lo == pytest.approx(0.1)


def test_t_hi_simulates_the_zone_rule_instead_of_pretending_it_is_a_score_cut() -> None:
    # A certificate merges at ANY score, an auto-reject never merges, and a single-family pair is
    # demoted to the band however high it scores. A pure score cut sees none of the three.
    rows = [scored(0.99, 1) for _ in range(400)]
    rows += [scored(0.10, 0, certificate="K-B") for _ in range(30)]
    rows += [scored(0.99, 0, blocked=True) for _ in range(30)]
    rows += [scored(0.99, 0, diverse=False) for _ in range(30)]
    report = ev.thresholds(rows)
    assert report.t_hi_score_cut_only == pytest.approx(0.99)  # the model layer alone is clean
    # but the 30 certificate negatives ride in at every cut, so the zone as built cannot clear D3
    assert report.t_hi is None
    without = ev.thresholds([row for row in rows if row.certificate is None])
    assert without.t_hi == pytest.approx(0.99)
    assert without.n_at_t_hi == 400  # blocked and single-family rows are outside the merge set


def test_certificate_pairs_are_counted_in_the_merge_set_they_actually_enter() -> None:
    rows = [scored(0.99, 1) for _ in range(400)]
    rows += [scored(0.05, 1, certificate="K-A") for _ in range(20)]
    report = ev.thresholds(rows)
    assert report.t_hi == pytest.approx(0.99)
    assert report.n_at_t_hi == 420
    assert report.n_certificate_in_merge_set == 20


def test_a_discarded_positive_is_spent_against_the_recall_budget_first() -> None:
    rows = [scored(0.9, 1) for _ in range(96)] + [
        scored(0.01, 1, blocked=True, discarded=True) for _ in range(4)
    ]
    report = ev.thresholds(rows)
    assert report.weight_positive_unavoidable == pytest.approx(4.0)
    assert report.missed_share_at_t_lo == pytest.approx(0.04)  # already over budget at T_lo=0
    assert report.t_lo == 0.0


def test_a_positive_the_merge_zone_gate_demoted_is_banded_not_lost() -> None:
    # E45/E46 and E11 demote to the BAND: the operator still sees the pair, so it is not spent
    # against recall — and T_lo still decides whether it reaches the queue at all.
    banded = [scored(0.9, 1, blocked=True) for _ in range(20)]
    report = ev.thresholds([scored(0.99, 1) for _ in range(80)] + banded)
    assert report.weight_positive_unavoidable == 0.0
    assert report.missed_share_at_t_lo == 0.0
    # drop them below T_lo and they ARE lost, which is the cost the dial is supposed to price
    low = [scored(0.01, 1, blocked=True) for _ in range(20)]
    dropped = ev.thresholds([scored(0.99, 1) for _ in range(80)] + low, store_floor=0.5)
    assert dropped.missed_share_at_t_lo == pytest.approx(0.2)


def test_t_lo_is_clamped_up_to_the_store_floor_and_says_so() -> None:
    rows = [(0.9, 1, 1.0, "s") for _ in range(400)]
    report = ev.thresholds(rows, store_floor=0.02)
    assert report.t_lo_raw == 0.0
    assert report.t_lo == pytest.approx(0.02)
    assert report.t_lo_clamped_to == "store_floor"
    assert report.t_hi is not None
    settings = Settings(t_lo=report.t_lo, t_hi=report.t_hi, store_floor=0.02)
    assert settings.t_lo == pytest.approx(0.02)  # the output loads as settings, both orderings
    assert report.settings_loadable is True
    short = ev.thresholds(rows[:5], store_floor=0.02)
    assert short.settings_loadable is False and short.settings_reason == "t_hi unattainable"


def test_band_width_prefers_the_population_over_the_enriched_sample() -> None:
    # The judged sample is deliberately band-enriched; E25's `b` is a population share.
    rows = [(0.99, 1, 1.0, "s") for _ in range(400)] + [(0.5, 0, 50.0, "fat") for _ in range(10)]
    population = [0.99] * 990 + [0.5] * 10
    report = ev.thresholds(rows, population_scores=population)
    assert report.band_width_source == "population"
    assert report.band_width == pytest.approx(0.01)
    assert report.band_width_weighted_sample > 0.5  # what the sample alone would have claimed
    assert report.band_denominator_n == 1000


def test_the_threshold_search_uses_design_weights_only_not_tier_credibility() -> None:
    rows = [pair_row(1, 2, zone="reject", score=0.05), pair_row(3, 4, zone="merge", score=0.99)]
    labels = {(1, 2): label(1, 2, 1, weight=0.3, tier="text", stratum="fat"),
              (3, 4): label(3, 4, 1, weight=1.0, tier="gold", stratum="thin")}
    sample = two_stratum_sample({(1, 2): "fat", (3, 4): "thin"},
                                {"fat": (1, 1000), "thin": (10, 10)})
    report = ev.evaluate(rows, labels, sample, Settings(), draws=50)
    cohort = report.sections["cohort"]
    # one concept, one number: the tier weight must not deflate the low-score positive
    assert report.sections["thresholds_all_judged"]["weight_positive"] == pytest.approx(
        cohort["weight_positive_total"]
    )
    assert cohort["weight_positive_total"] == pytest.approx(1001.0)


def test_thresholds_are_chosen_on_dev_and_re_measured_on_the_sealed_split() -> None:
    rows: list[dict[str, Any]] = []
    labels: dict[Any, Label] = {}
    for index in range(600):
        lo, hi = 2 * index + 1, 2 * index + 2
        rows.append(pair_row(lo, hi, zone="merge", score=0.99 if index % 10 else 0.2))
        labels[(lo, hi)] = label(lo, hi, 1 if index % 10 else 0)
    report = ev.evaluate(rows, labels, None, Settings(), draws=50)
    holdout = report.sections["holdout"]
    assert holdout["n_dev"] + holdout["n_sealed"] == 600
    assert holdout["n_sealed"] > 50
    assert holdout["t_hi"] == report.sections["thresholds"]["t_hi"]
    assert holdout["precision"] is not None
    assert holdout["stratum_floor"] == ev.STRATUM_FLOOR
    # the selection-inflated number is still published, clearly labelled as the cross-check
    assert report.sections["thresholds_all_judged"]["n"] == 600
    assert report.sections["thresholds"]["n"] < 600


def test_measure_thresholds_scores_a_fixed_cut() -> None:
    body = ev.measure_thresholds([(0.99, 1, 1.0, "a"), (0.99, 0, 1.0, "b"), (0.1, 1, 1.0, "a")],
                                 0.5, 0.2, draws=50)
    assert body["n_merge"] == 2 and body["n_positive"] == 1
    assert body["precision"] == pytest.approx(0.5)
    # D3's floor is read on the DECIDING CELL; bare tuples carry none, so both merges land in
    # one "(none)" cell. The sampler grain rides alongside as a diagnostic, never as the gate.
    assert body["min_stratum_precision"] == pytest.approx(0.5)
    assert body["stratum_floor_ok"] is False
    assert body["min_sampler_stratum_precision"] == 0.0
    assert set(body["per_sampler_stratum_precision"]) == {"a", "b"}
    assert "deciding cell" in body["per_stratum_precision_basis"]
    assert body["missed_share_at_t_lo"] == pytest.approx(0.5)
    assert ev.measure_thresholds([(0.9, 1)], None, 0.2)["precision"] is None


# --- the honesty flags -------------------------------------------------------------------------


def test_an_unweighted_run_never_claims_horvitz_thompson_weighting(tmp_path: Path) -> None:
    rows, labels = planted_rows(n=40)
    from autodedup.labels import sample_from_judgements  # the no-sample fallback the CLI uses

    degenerate = sample_from_judgements([])
    report = ev.evaluate(rows, labels, degenerate, Settings(), draws=50)
    assert report.sections["counts"]["weighted"] is False
    assert report.sections["cohort"]["weighted"] is False
    assert "UNWEIGHTED" in "\n".join(report.headline())
    assert "off (sample estimates only)" in report.to_markdown()


def test_labelled_pairs_that_fall_back_to_weight_one_are_counted() -> None:
    rows = [pair_row(1, 2), pair_row(3, 4)]
    labels = {(1, 2): label(1, 2, 1, stratum="thin"), (3, 4): label(3, 4, 1, stratum="nowhere")}
    sample = two_stratum_sample({}, {"thin": (1, 100)})
    counts = ev.evaluate(rows, labels, sample, Settings(), draws=50).sections["counts"]
    assert counts["n_labelled_without_sample_weight"] == 1


def test_abstentions_are_reported_as_nonresponse_not_assumed_away() -> None:
    rows = [pair_row(1, 2), pair_row(3, 4)]
    labels = {(1, 2): label(1, 2, 1, stratum="thin"), (3, 4): label(3, 4, None, stratum="thin")}
    sample = two_stratum_sample({}, {"thin": (2, 200)})
    body = ev.evaluate(rows, labels, sample, Settings(), draws=50).sections["sample"]["nonresponse"]
    assert body["weight_labelled"] == pytest.approx(200.0)
    assert body["weight_decided"] == pytest.approx(100.0)
    assert body["weight_lost_share"] == pytest.approx(0.5)
    assert body["worst_strata"][0]["stratum"] == "thin"


# --- the fit: weights, seal, calibration honesty ------------------------------------------------


def test_the_weight_cap_defaults_to_a_multiple_of_the_median_weight() -> None:
    rows, labels = planted_rows(n=60)
    keys = sorted(labels)
    assignments = {key: ("fat" if index == 0 else "thin") for index, key in enumerate(keys)}
    sample = two_stratum_sample(assignments, {"fat": (1, 5000), "thin": (59, 118)})
    _, report = ev.fit_model(rows, labels, sample=sample, epochs=20)
    split = report.sections["split"]
    assert split["weight_cap_rule"] == "10x median weight"
    assert split["weight_cap"] == pytest.approx(2.0 * 10)  # median weight is the thin stratum's
    assert split["n_weights_capped"] == 1
    assert split["weights_after_cap"]["n_effective"] > split["weights_before_cap"]["n_effective"]


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
    model = harness.load_model(str(tmp_path / "fit" / harness.MODEL_FILE))
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


def test_a_mismatched_neighbouring_sample_is_dropped_with_a_warning_not_used(
    tmp_path: Path, capsys: Any
) -> None:
    rows, labels = planted_rows(n=40)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    (tmp_path / harness.SAMPLE_FILE).write_text(
        json.dumps({"strata": {"invented|key": {"population": 400, "selected": 2}},
                    "pairs": [{"lo": 1, "hi": 2, "zone": "merge", "block": "praha",
                               "cross_source": True, "feats": {}}]}),
        encoding="utf-8",
    )
    out = io.StringIO()
    code = harness.main(
        ["evaluate", str(run_dir), "--judgements", str(judgements),
         "--out", str(tmp_path / "eval")], out=out
    )
    assert code == 0
    assert "ignoring" in capsys.readouterr().err
    payload = json.loads((tmp_path / "eval" / "eval.json").read_text(encoding="utf-8"))
    assert payload["counts"]["weighted"] is False


def test_the_weighted_precision_at_the_cut_is_reported_beside_the_counted_one() -> None:
    # D3 counts pairs; the cohort weights them. When the two disagree the report must say so
    # rather than publish the count-based gate as if it were the cohort number.
    rows = [ev.ScoredRow(score=0.99, y=1, weight=1.0, stratum="thin") for _ in range(1499)]
    rows += [ev.ScoredRow(score=0.99, y=0, weight=5000.0, stratum="fat")]
    report = ev.thresholds(rows, draws=100)
    assert report.t_hi == pytest.approx(0.99)
    assert report.precision_at_t_hi > 0.995  # the counted gate passes
    assert report.weighted_precision_at_t_hi < 0.5  # the cohort it stands for does not
    assert report.weighted_precision_gate_ok is False
    assert report.bootstrap_at_t_hi["lb"] is not None


def test_an_explicit_sample_that_does_not_match_its_judgements_is_fatal(tmp_path: Path) -> None:
    rows, labels = planted_rows(n=20)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    bad = tmp_path / "other-sample.json"
    bad.write_text(
        json.dumps({"strata": {"invented|key": {"population": 400, "selected": 2}},
                    "pairs": [{"lo": 1, "hi": 2, "zone": "merge", "block": "praha",
                               "cross_source": True, "feats": {}}]}),
        encoding="utf-8",
    )
    assert harness.main(
        ["evaluate", str(run_dir), "--judgements", str(judgements), "--sample", str(bad),
         "--out", str(tmp_path / "eval")], out=io.StringIO()
    ) == 1
    assert harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements), "--sample", str(bad),
         "--out", str(tmp_path / "fit")], out=io.StringIO()
    ) == 1


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


def test_resimulate_calls_the_live_decide_instead_of_re_implementing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    original = ev.decide.decide_pair

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append("called")
        return original(*args, **kwargs)

    monkeypatch.setattr(ev.decide, "decide_pair", spy)
    row = pair_row(1, 2, zone="merge", score=0.99, area_rel_diff=0.0, tfidf_cos=0.95)
    ev.resimulate(row, hand_initialised(), Settings())
    assert seen == ["called"]


def test_resimulate_replays_the_two_facts_a_stored_row_cannot_recompute() -> None:
    model, settings = hand_initialised(), Settings()
    vetoed = pair_row(1, 2, zone="reject")
    vetoed["veto"] = "area"
    decision = ev.resimulate(vetoed, model, settings)
    assert decision.zone == "veto" and decision.veto == "area"
    # K-B's disjoint-window clause is the one certificate input the row omits, so it is replayed
    certified = pair_row(3, 4, zone="merge", certificate="K-B", same_source=1.0,
                         same_broker_key=1.0, containment_max=0.99, area_rel_diff=0.0,
                         tfidf_cos=0.98, phash_match_ratio=0.9)
    assert ev.decide.disjoint_windows(
        ev._SimSide(3, ev.OVERLAPPING_WINDOW), ev._SimSide(4, ev.DISJOINT_WINDOW)
    ) is True
    assert ev.resimulate(certified, model, settings).certificate == "K-B"


def test_rescoring_a_run_moves_the_zone_with_the_model() -> None:
    rows = [pair_row(1, 2, zone="merge", score=0.99, area_rel_diff=0.0, tfidf_cos=0.95,
                     containment_max=0.95, phash_match_ratio=0.9)]
    blind = LogisticModel(feature_order=("tfidf_cos",), weights={"tfidf_cos": 0.0},
                          presence_weights={}, intercept=-20.0)
    rescored = ev.rescore_rows(rows, blind, Settings())
    assert rescored[0]["score"] < 0.01
    assert rescored[0]["zone"] in {"reject", "band"}
    assert rescored[0]["lo"] == 1 and rescored[0]["hi"] == 2  # the pair identity is untouched


def test_the_decide_stratum_is_the_layer_crossed_with_the_source_side() -> None:
    assert ev.decide_stratum(pair_row(1, 2, certificate="K-B", cross=False)) == "K-B|same"
    assert ev.decide_stratum(pair_row(1, 2, cross=True)) == "model|cross"


def test_a_perfect_stratum_still_needs_three_hundred_and_eighty_one_judged_merges() -> None:
    assert ev.labels_needed_for_lb(0.99) == 381
    assert ev.wilson_lower(380, 380) < 0.99 <= ev.wilson_lower(381, 381)
    assert ev.labels_needed_for_lb(0.97) < ev.labels_needed_for_lb(0.99)
    with pytest.raises(ValueError):
        ev.labels_needed_for_lb(1.0)


def in_cell(score: float, y: int, cell: str, split: str = "train",
            certificate: str | None = None) -> ev.ScoredRow:
    return ev.ScoredRow(score=score, y=y, weight=1.0, stratum=cell, cell=cell,
                        certificate=certificate, split=split)


def test_a_stratum_that_cannot_prove_the_bar_ships_propose_only_and_says_why() -> None:
    # 500 flawless judged merges: enough labels AND enough precision, so this one ships
    dev = [in_cell(0.99, 1, "model|cross") for _ in range(500)]
    # a certificate cell that merges wrong pairs at every cut: a precision problem, not a budget
    dev += [in_cell(0.1, y, "K-C|cross", certificate="K-C") for y in [1] * 90 + [0] * 10]
    # a flawless but thin cell: the bar is out of reach of the SAMPLE, whatever the model does
    dev += [in_cell(0.99, 1, "model|same") for _ in range(30)]
    sealed = [in_cell(0.99, 1, "model|cross", split="test") for _ in range(20)]
    body = ev.stratum_thresholds(dev, sealed)
    strata = body["strata"]
    assert strata["model|cross"]["t_hi"] == pytest.approx(0.99)
    assert strata["model|cross"]["propose_only"] is False
    assert strata["model|same"]["propose_only"] is True
    assert strata["model|same"]["propose_only_reason"] == "sample"
    assert strata["K-C|cross"]["propose_only_reason"] == "precision"
    assert strata["K-C|cross"]["n_certificate_merges"] == 100
    assert strata["K-C|cross"]["precision_certificate_merges"] == pytest.approx(0.9)
    assert body["t_hi_by_stratum"] == {"model|cross": pytest.approx(0.99),
                                       "model|same": None, "K-C|cross": None}
    assert body["auto_merge_strata"] == ["model|cross"]
    # the sealed rows measure the cut; they never choose it
    assert strata["model|cross"]["sealed_n_merge"] == 20
    assert strata["model|cross"]["sealed_precision"] == 1.0
    assert strata["model|cross"]["measured_at"] == "t_hi"
    assert strata["model|same"]["measured_at"].startswith("relaxed")


def test_effective_t_hi_reads_the_override_and_treats_none_as_propose_only() -> None:
    settings = Settings()
    table = {"model|cross": 0.94, "K-C|same": None}
    assert ev.effective_t_hi(settings, "model|cross", table) == 0.94
    assert ev.effective_t_hi(settings, "K-C|same", table) is None
    assert ev.effective_t_hi(settings, "K-B|same", table) == settings.t_hi
    assert ev.effective_t_hi(settings, "K-B|same") == settings.t_hi


def test_effective_t_hi_prefers_the_settings_column_the_row_now_carries() -> None:
    row = Settings(t_hi_by_stratum={"model|same": None})
    assert ev.effective_t_hi(row, "model|same") is None
    assert ev.effective_t_hi(row, "model|cross") == Settings().t_hi
    # An explicit table still wins over the column: a report may ask what a CANDIDATE table does.
    assert ev.effective_t_hi(row, "model|same", {"model|same": 0.5}) == 0.5


def test_the_evaluation_publishes_a_per_stratum_threshold_table() -> None:
    rows: list[dict[str, Any]] = []
    labels: dict[Any, Label] = {}
    for index in range(400):
        lo, hi = 2 * index + 1, 2 * index + 2
        cross = index % 2 == 0
        rows.append(pair_row(lo, hi, zone="merge", score=0.99 if index % 8 else 0.2, cross=cross))
        labels[(lo, hi)] = label(lo, hi, 1 if index % 8 else 0)
    report = ev.evaluate(rows, labels, None, Settings(), draws=50)
    body = report.sections["thresholds_by_stratum"]
    assert body["key"] == "certificate|side"
    assert set(body["strata"]) == {"model|cross", "model|same"}
    assert body["fitted_on"] == "train+validation"
    assert body["measured_on"] == "test (sealed)"
    assert body["labels_needed_for_lb"] == 381
    assert "Per-stratum thresholds" in report.to_markdown()


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


def test_a_propose_only_holdout_still_measures_the_certificates_and_the_recall() -> None:
    sealed = [
        ev.ScoredRow(score=0.2, y=1, certificate="K-C"),
        ev.ScoredRow(score=0.2, y=0, certificate="K-C"),
        ev.ScoredRow(score=0.01, y=1),
        ev.ScoredRow(score=0.99, y=1),
    ]
    body = ev.measure_thresholds(sealed, None, 0.1, draws=50)
    # no SCORE merges anything, but a certificate still does, and that precision is measurable
    assert body["t_hi"] is None
    assert body["n_merge"] == 2 and body["precision"] == pytest.approx(0.5)
    # one of the three positives is below T_lo: the band never sees it
    assert body["missed_share_at_t_lo"] == pytest.approx(1 / 3)


def test_a_certificate_stratum_is_measured_on_the_sealed_split_even_with_no_cut() -> None:
    dev = [in_cell(0.1, y, "K-C|cross", certificate="K-C") for y in [1] * 90 + [0] * 10]
    sealed = [in_cell(0.1, y, "K-C|cross", split="test", certificate="K-C")
              for y in [1] * 9 + [0]]
    body = ev.stratum_thresholds(dev, sealed)["strata"]["K-C|cross"]
    assert body["t_hi"] is None and body["relaxed_t_hi"] is None
    assert body["measured_at"] == "certificates only (no reachable cut)"
    assert body["sealed_n_merge"] == 10 and body["sealed_precision"] == pytest.approx(0.9)


# --- the reconstruction must agree with the engine ------------------------------------------


def reconstructed(rows: Sequence[dict[str, Any]], settings: Settings) -> list[ev.ScoredRow]:
    """What `evaluate` builds its threshold search on, in the same order as `rows`."""
    out: list[ev.ScoredRow] = []
    for row in rows:
        context = ev.decide_context(row, settings)
        out.append(ev.ScoredRow(
            score=ev._score(row), y=1, certificate=context["certificate"],
            diverse=bool(context["diverse"]), blocked=bool(context["blocked"]),
        ))
    return out


def gated_run(settings: Settings) -> list[dict[str, Any]]:
    """One row per shape the rule floor can take, decided by the LIVE `decide_pair`."""
    unit = dict(area_rel_diff=0.0, tfidf_cos=0.97, containment_max=0.96, jaccard_shingle=0.8)
    raw = [
        pair_row(1, 2, **unit),                                    # unit evidence, two families
        pair_row(3, 4, rare_token_overlap=0.0, **unit),            # E45: no unit-grade evidence
        pair_row(5, 6, attr_contradictions=9.0, **unit),           # auto-reject
        pair_row(7, 8, same_source=1.0, same_broker_key=1.0,       # E46: developer signature
                 overlap_days=400.0, catalog_ratio_max=0.99,
                 interior_match_ratio=0.0, **unit),
        pair_row(9, 10, **unit),                                   # one family only (below)
    ]
    raw[4]["feats"] = {"rare_token_overlap": [3.0, True], "area_rel_diff": [0.0, True]}
    raw[-1]["veto"] = None
    vetoed = pair_row(11, 12, **unit)
    vetoed["veto"] = "area"
    raw.append(vetoed)
    # A model that says "merge" to everything, so what varies between these rows is the rule
    # floor and nothing else.
    shouting = LogisticModel(feature_order=("tfidf_cos",), weights={"tfidf_cos": 0.0},
                             presence_weights={}, intercept=20.0)
    return ev.rescore_rows(raw, shouting, settings)


def test_the_merge_set_evaluate_reconstructs_is_the_one_decide_actually_produced() -> None:
    settings = Settings(t_hi=0.5, t_lo=0.1)
    rows = gated_run(settings)
    engine = {index for index, row in enumerate(rows) if row["zone"] == "merge"}
    scored = reconstructed(rows, settings)
    rebuilt = {
        index for index, row in enumerate(scored)
        if row.merged_at(settings.t_hi, zone_rule=True)
    }
    assert engine, rows
    assert rebuilt == engine
    # and the same at every cut: the gates are functions of the pair, not of the threshold
    for cut in (0.0, 0.25, 0.5, 0.9, 1.0):
        rebuilt_at = {
            index for index, row in enumerate(scored) if row.merged_at(cut, zone_rule=True)
        }
        assert rebuilt_at <= set(range(len(rows)))
        assert all(rows[index]["zone"] != "band" or scored[index].blocked
                   or not scored[index].diverse or scored[index].score < cut
                   for index in range(len(rows)) if index not in rebuilt_at)


def test_a_merge_zone_gate_reads_as_blocked_even_when_the_stored_reason_says_model() -> None:
    # The pair scored below T_hi, so the run wrote it as a plain `model` band row: its reason
    # carries no trace of the gate, and a search that trusted the reason would count it back in.
    row = pair_row(1, 2, zone="band", score=0.2, rare_token_overlap=0.0,
                   area_rel_diff=0.0, tfidf_cos=0.9)
    row["reason"] = "model"
    context = ev.decide_context(row, Settings())
    assert context["blocked"] is True
    assert context["merge_zone_block"] == "unit_evidence_gate"
    assert ev.decide_context(pair_row(3, 4, area_rel_diff=0.0))["blocked"] is False


def test_the_gate_is_asked_of_decide_never_parsed_out_of_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    original = ev.decide.merge_zone_block

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append("called")
        return original(*args, **kwargs)

    monkeypatch.setattr(ev.decide, "merge_zone_block", spy)
    ev.decide_context(pair_row(1, 2, area_rel_diff=0.0), Settings())
    assert seen == ["called"]


def test_band_width_is_the_decided_zone_not_the_score_interval() -> None:
    rows = [pair_row(1, 2, zone="reject", score=0.5), pair_row(3, 4, zone="band", score=0.5)]
    labels = {(1, 2): label(1, 2, 0), (3, 4): label(3, 4, 1)}
    body = ev.evaluate(rows, labels, None, Settings(), draws=20).sections["band_composition"]
    # both scores sit inside (t_lo, t_hi); only one pair is in the band the queue will show
    assert body["score_in_band_share"] == pytest.approx(1.0)
    assert body["band_width"] == pytest.approx(0.5)
    assert body["n_band_zone"] == 1
    assert body["band_width_basis"] == "zone == band / stored pairs"


# --- the sealed split has to be the fit's split ----------------------------------------------


def test_evaluate_measures_the_holdout_on_the_fits_split_map_not_a_re_derived_one() -> None:
    rows: list[dict[str, Any]] = []
    labels: dict[Any, Label] = {}
    for index in range(120):
        lo, hi = 2 * index + 1, 2 * index + 2
        rows.append(pair_row(lo, hi, zone="band", score=0.99))
        labels[(lo, hi)] = label(lo, hi, 1 if index % 4 else 0)
        # an UNJUDGED merge edge chaining this pair to the next: what welds two labelled pairs
        # into one component, and what stops being an edge the moment the model changes
        rows.append(pair_row(hi, hi + 1, zone="merge", score=0.99))
    fit_map = ev.split_groups(rows, labels)
    seal = ev.split_seal(fit_map)["sha256"]
    # the same pairs, decided differently: the components — and so the split — move with the model
    moved = [dict(row, zone="band") for row in rows]
    assert ev.split_seal(ev.split_groups(moved, labels))["sha256"] != seal
    derived = ev.evaluate(moved, labels, None, Settings(), draws=20).sections["holdout"]
    pinned = ev.evaluate(moved, labels, None, Settings(), draws=20,
                         split_map=fit_map, expect_seal=seal[:12]).sections["holdout"]
    assert pinned["split_map_source"] == "fit"
    assert derived["split_map_source"] == "re-derived from this run"
    assert pinned["seal"]["sha256"] == seal
    assert pinned["n_sealed"] != derived["n_sealed"]


def test_a_foreign_seal_fails_loudly_instead_of_scoring_a_different_holdout() -> None:
    rows = [pair_row(1, 2, zone="merge", score=0.99)]
    labels = {(1, 2): label(1, 2, 1)}
    with pytest.raises(ValueError, match="split seal mismatch"):
        ev.evaluate(rows, labels, None, Settings(), draws=20,
                    split_map=ev.split_groups(rows, labels), expect_seal="deadbeefdead")


def test_the_evaluate_command_takes_the_split_map_the_fit_wrote(tmp_path: Path) -> None:
    rows: list[dict[str, Any]] = []
    labels: dict[Any, Label] = {}
    for index in range(60):
        lo, hi = 2 * index + 1, 2 * index + 2
        rows.append(pair_row(lo, hi, zone="merge", score=0.99))
        labels[(lo, hi)] = label(lo, hi, 1 if index % 3 else 0)
    run_dir = write_run(tmp_path, rows)
    judgement_path = write_judgements(tmp_path / "j.jsonl", labels)
    fit_map = ev.split_groups(rows, labels)
    map_path = tmp_path / "split_map.json"
    map_path.write_text(
        json.dumps({str(key): value for key, value in fit_map.items()}), encoding="utf-8"
    )
    buffer = io.StringIO()
    code = harness.main(
        ["evaluate", str(run_dir), "--judgements", str(judgement_path),
         "--split-map", str(map_path), "--out", str(tmp_path / "out")],
        out=buffer,
    )
    assert code == 0
    body = json.loads((tmp_path / "out" / "eval.json").read_text(encoding="utf-8"))
    assert body["holdout"]["split_map_source"] == "fit"


# --- a cut has to be read off enough rows, and never off the rows it is measured on ----------


def test_a_cut_read_off_a_handful_of_rows_is_refused_and_flagged() -> None:
    dev = [in_cell(0.99, 1, "model|cross") for _ in range(5)]
    body = ev.stratum_thresholds(dev, [], precision_lb=0.5, precision_point=0.5)
    cell = body["strata"]["model|cross"]
    assert cell["t_hi"] is None
    assert cell["propose_only_reason"] == "sample"
    assert cell["relaxed_insufficient_n"] is True
    assert body["criteria"]["min_n"] == ev.MIN_STRATUM_N
    wide = ev.stratum_thresholds(
        [in_cell(0.99, 1, "model|cross") for _ in range(40)], [],
        precision_lb=0.5, precision_point=0.5,
    )
    assert wide["strata"]["model|cross"]["t_hi"] == pytest.approx(0.99)
    assert wide["strata"]["model|cross"]["relaxed_insufficient_n"] is False


def test_with_no_dev_split_every_threshold_is_refused_and_the_label_says_so() -> None:
    dev = [in_cell(0.99, 1, "model|cross") for _ in range(500)]
    body = ev.stratum_thresholds(
        dev, dev, allow_overrides=False, fitted_on="all judged (NO dev split; not held out)"
    )
    assert body["t_hi_by_stratum"] == {"model|cross": None}
    assert body["strata"]["model|cross"]["propose_only_reason"] == "no dev split"
    assert body["fitted_on"].startswith("all judged")
    assert body["measured_on"] == "test (same rows as the fit)"
    assert body["auto_merge_strata"] == []


def test_a_certificate_cell_that_merges_wrong_pairs_says_precision_not_sample() -> None:
    # 116 flawless certificate merges: thin for a 0.99 lower bound, but nothing is wrong with it
    clean = [in_cell(0.1, 1, "K-C|cross", certificate="K-C") for _ in range(116)]
    dirty = [in_cell(0.1, y, "K-C|same", certificate="K-C") for y in [1] * 18 + [0] * 2]
    body = ev.stratum_thresholds(clean + dirty, [])["strata"]
    assert body["K-C|cross"]["propose_only_reason"] == "sample"
    assert body["K-C|cross"]["precision_certificate_merges"] == 1.0
    assert body["K-C|same"]["propose_only_reason"] == "precision"


def test_a_published_cut_is_a_probability_not_a_rank() -> None:
    """Two score clusters 3e-8 apart: a search over raw scores would stop BETWEEN them and publish
    a cut no operator can read and no rerun can reproduce. The grid `expressible_cuts` returns is
    the coarsest thing a settings row may state, and the markdown prints it re-enterably."""
    rows: list[dict[str, Any]] = []
    labels: dict[Any, Label] = {}
    for index in range(400):
        lo, hi = 2 * index + 1, 2 * index + 2
        score = 0.953125003065951 if index % 2 else 0.953125030633085
        rows.append(pair_row(lo, hi, zone="merge", score=score, cross=bool(index % 2)))
        labels[(lo, hi)] = label(lo, hi, 1)
    report = ev.evaluate(rows, labels, None, Settings(t_hi=0.9, t_lo=0.3), draws=20)
    markdown = report.to_markdown()
    for cut in report.sections["thresholds_by_stratum"]["strata"].values():
        if cut["relaxed_t_hi"] is not None:
            assert f"{cut['relaxed_t_hi']:.12g}" in markdown
            # on the 1e-4 grid the two clusters are one cut, so neither can be singled out
            assert cut["relaxed_t_hi"] in (0.9531, 0.9532)
    assert report.sections["thresholds_by_stratum"]["criteria"]["cut_resolution"] == 1e-4


def test_the_cut_grid_gives_each_score_a_neighbour_on_either_side() -> None:
    cuts = ev.expressible_cuts([0.98625585, 0.9862558542015111])
    assert cuts == [0.9863, 0.9862]
    # 0.9862 admits both pairs, 0.9863 admits neither — and there is nothing in between to fit to
    assert ev.expressible_cuts([0.5], resolution=None) == [0.5]


def test_the_sealed_split_is_also_measured_at_the_settings_actually_loaded() -> None:
    rows: list[dict[str, Any]] = []
    labels: dict[Any, Label] = {}
    for index in range(300):
        lo, hi = 2 * index + 1, 2 * index + 2
        rows.append(pair_row(lo, hi, zone="merge", score=0.99 if index % 5 else 0.4))
        labels[(lo, hi)] = label(lo, hi, 1 if index % 5 else 0)
    loaded = Settings(t_hi=0.3, t_lo=0.1)
    report = ev.evaluate(rows, labels, None, loaded, draws=20)
    searched = report.sections["holdout"]
    in_force = report.sections["holdout_at_settings"]
    assert in_force["t_hi"] == loaded.t_hi and in_force["t_lo"] == loaded.t_lo
    assert in_force["n"] == searched["n"]
    # the loaded row merges the 0.4 negatives too, which is exactly what the search refused to do
    assert in_force["n_merge"] > (searched["n_merge"] or 0)
    assert in_force["precision"] < 1.0


def test_decide_context_blocks_a_propose_only_stratum_without_discarding_it() -> None:
    """E48 removes the pair from the merge set at EVERY cut, but it is still proposed."""
    row = pair_row(1, 2, zone="band", certificate="K-C", cross=False)
    gated = Settings(t_hi_by_stratum={"K-C|same": None})
    context = ev.decide_context(row, gated)
    assert context["stratum_propose_only"] is True
    assert context["blocked"] is True
    assert context["discarded"] is False
    assert context["merge_zone_block"] is None
    open_row = ev.decide_context(row, Settings())
    assert open_row["stratum_propose_only"] is False and open_row["blocked"] is False


def test_the_report_carries_zone_recall_beside_the_score_based_line() -> None:
    """The score line is a function of the cuts alone; the zone line is what the engine did."""
    rows = [
        pair_row(1, 2, zone="merge", score=0.99),
        # A high-scoring positive the RULE FLOOR banded: no cut sees it, the score line cannot.
        pair_row(3, 4, zone="band", score=0.99, certificate="K-C"),
        pair_row(5, 6, zone="reject", score=0.01),
    ]
    labels = {(1, 2): label(1, 2, 1), (3, 4): label(3, 4, 1), (5, 6): label(5, 6, 1)}
    body = ev.evaluate(rows, labels, None, Settings(), draws=20).sections["cohort"]
    zone_recall = body["zone_recall"]
    assert zone_recall["positives_in_merge_zone_share"] == pytest.approx(1 / 3)
    assert zone_recall["positives_in_band_zone_share"] == pytest.approx(1 / 3)
    assert zone_recall["positives_in_reject_zone_share"] == pytest.approx(1 / 3)
    # The score-based line sees only one of the three as missed, which is the understatement.
    assert body["positives_in_band_share"] == pytest.approx(0.0)
    assert body["positives_below_t_lo_share"] == pytest.approx(1 / 3)


def test_a_judged_positive_the_run_never_stored_is_still_a_missed_duplicate() -> None:
    """The recall denominator is built from the run's own rows, so a labelled pair the run did not
    store — below `store_floor`, or never a candidate — silently left the denominator. A
    calibration that pushes pairs under the floor then reads as a recall IMPROVEMENT."""
    rows = [
        pair_row(1, 2, zone="merge", score=0.99),
        pair_row(3, 4, zone="band", score=0.5),
        pair_row(5, 6, zone="reject", score=0.01),
    ]
    labels = {
        (1, 2): label(1, 2, 1), (3, 4): label(3, 4, 1), (5, 6): label(5, 6, 1),
        (7, 8): label(7, 8, 1),  # judged a duplicate; the run never stored it
        (9, 10): label(9, 10, 0),
    }
    report = ev.evaluate(rows, labels, None, Settings(), draws=20)
    counts = report.sections["counts"]
    assert counts["n_labels_off_run"] == 2
    assert counts["n_labels_off_run_judged"] == 2
    assert counts["n_labels_off_run_positive"] == 1
    body = report.sections["cohort"]
    zone_recall = body["zone_recall"]
    assert zone_recall["positives_in_off_run_zone_share"] == pytest.approx(0.25)
    assert zone_recall["positives_in_merge_zone_share"] == pytest.approx(0.25)
    # merge+band is 50%, not the 66.7% the run-only denominator would have printed
    assert (zone_recall["positives_in_merge_zone_share"]
            + zone_recall["positives_in_band_zone_share"]) == pytest.approx(0.5)
    # The SCORE line still divides by the on-run positives: an unstored pair has no score.
    assert body["weight_positive_on_run"] == pytest.approx(3.0)
    assert body["weight_positive_off_run"] == pytest.approx(1.0)
    assert body["positives_below_t_lo_share"] == pytest.approx(1 / 3)
    assert "off-run" in "\n".join(report.headline())


def test_the_settings_holdout_reads_the_per_stratum_table_it_names() -> None:
    """`holdout_at_settings` says it measures the row in force; E48's table IS that row. Measuring
    it through the global `t_hi` alone reports a certificates-only merge set and calls it the
    shipped table — exactly the cell W4f newly switched on would be missing."""
    rows: list[dict[str, Any]] = []
    labels: dict[Any, Label] = {}
    for index in range(300):
        lo, hi = 2 * index + 1, 2 * index + 2
        cross = index % 2 == 0
        rows.append(pair_row(lo, hi, zone="merge", score=0.99, cross=cross))
        labels[(lo, hi)] = label(lo, hi, 1)
    loaded = Settings(t_hi=1.0, t_hi_by_stratum={"model|cross": 0.96, "model|same": None})
    in_force = ev.evaluate(rows, labels, None, loaded, draws=20
                           ).sections["holdout_at_settings"]
    assert in_force["t_hi_by_stratum"] == {"model|cross": 0.96, "model|same": None}
    merged = in_force["n_merge"]
    assert merged > 0  # the global 1.0 alone would merge nothing
    assert set(in_force["per_stratum_precision"]) == {"model|cross"}  # `model|same` is held back
    assert in_force["stratum_t_hi_applied"] == {"model|cross": 0.96, "model|same": None}


def test_a_propose_only_cell_merges_nothing_even_with_a_certificate() -> None:
    """`decide_pair` consults E48's table BEFORE the certificates, so `None` holds those back too."""
    rows = [
        ev.ScoredRow(score=0.99, y=1, certificate="K-C", cell="K-C|same"),
        ev.ScoredRow(score=0.99, y=1, certificate="K-C", cell="K-C|cross"),
    ]
    body = ev.measure_thresholds(rows, 1.0, 0.1, draws=20,
                                 stratum_t_hi={"K-C|same": None})
    assert body["n_merge"] == 1
    assert set(body["per_stratum_precision"]) == {"K-C|cross"}


# --- several draws, one design (W4f) -----------------------------------------------------------


def draw_of(
    name: str, pairs: Sequence[tuple[int, int]], selected: int, population: int, **kwargs: Any
) -> Sample:
    return Sample(
        strata={name: Stratum(name, selected, population)},
        pair_stratum={ev.pair_key(*pair): name for pair in pairs},
        **kwargs,
    )


def test_pooled_sample_counts_distinct_drawn_pairs_not_the_first_file() -> None:
    """Two draws at different rates: the union's rate is what weights the pooled label set.

    Taking either file's own `n_selected` (2 of 100, or 4 of 100) would inflate every pair the
    other draw contributed — the label store holds one verdict per pair, the design behind it is
    the union of the draws."""
    first = draw_of("cell", [(1, 2), (3, 4)], selected=2, population=100)
    second = draw_of("cell", [(3, 4), (5, 6), (7, 8), (9, 10)], selected=4, population=100)
    pooled = ev.pooled_sample([first, second])
    assert pooled.strata["cell"].n_selected == 5  # (3,4) is drawn twice, counted once
    assert pooled.strata["cell"].n_total == 100
    assert pooled.weight_for_name("cell") == pytest.approx(20.0)
    assert first.weight_for_name("cell") == pytest.approx(50.0)
    assert pooled.is_weighted is True
    assert pooled.stratum_fn.startswith("pooled(2 draws")


def test_pooled_sample_orders_frames_by_draw_id_not_by_argument_order() -> None:
    """The draws are stamped against different engine runs, so their populations differ and the
    newest frame is the one closest to the run being evaluated. Which one that is comes off the
    lane artifact id on the sample path — reversing `--judgements` must not move an HT weight."""
    old = draw_of("cell", [(1, 2)], selected=1, population=50,
                  path="/j/35100000001/autodedup-judge-35100000001/sample.json")
    new = draw_of("cell", [(3, 4)], selected=1, population=80,
                  path="/j/35100000002/autodedup-judge-35100000002/sample.json")
    for order in ([old, new], [new, old]):
        pooled = ev.pooled_sample(order)
        assert pooled.strata["cell"].n_total == 80
        assert "newest frame wins" in pooled.stratum_fn
        assert pooled.order_source.startswith("draw id")


def test_pooled_sample_refuses_to_let_argv_pick_the_frame() -> None:
    """No draw id and populations that disagree is the one case where the order of the arguments
    would decide the estimate. That is a refusal, not a default."""
    old = draw_of("cell", [(1, 2)], selected=1, population=50)
    new = draw_of("cell", [(3, 4)], selected=1, population=80)
    with pytest.raises(ValueError, match="carry no draw id"):
        ev.pooled_sample([old, new])
    # Frames that agree make the order immaterial, so the same call is fine.
    same = draw_of("cell", [(3, 4)], selected=1, population=50)
    assert ev.pooled_sample([old, same]).strata["cell"].n_total == 50


def test_pooled_sample_reports_a_frame_conflict_instead_of_collapsing_the_weight() -> None:
    """A frame that moved can report a population under the pairs actually drawn (3 in the newest
    draw, 21 across all of them). Taking the drawn count as the population would set that cell's
    inflation factor to 1.0 — silently, and in the negative-control merge cells, which are the
    ones that most need it. The widest frame that named the stratum is used and the conflict is
    on the record."""
    old = draw_of("cell", [(1, 2), (3, 4), (5, 6)], selected=3, population=40,
                  path="/j/35100000001/autodedup-judge-35100000001/sample.json")
    new = draw_of("cell", [(7, 8)], selected=1, population=1,
                  path="/j/35100000002/autodedup-judge-35100000002/sample.json")
    pooled = ev.pooled_sample([old, new])
    assert pooled.strata["cell"].n_selected == 4
    assert pooled.strata["cell"].n_total == 40
    assert pooled.weight_for_name("cell") == pytest.approx(10.0)
    conflict = pooled.frame_conflicts[0]
    assert conflict["stratum"] == "cell"
    assert conflict["population_newest_frame"] == 1
    assert conflict["population_used"] == 40
    assert conflict["weight_under_newest_frame"] == pytest.approx(0.25)
    assert "FRAME CONFLICTS" in pooled.stratum_fn


def test_pooled_sample_counts_a_pair_drawn_under_two_names_once() -> None:
    """A pair whose zone moved between the two runs the draws were stamped against carries ONE
    verdict. Putting it in both denominators inflates the cell that has no numerator to match, so
    it is attributed to the newest draw's name and the pooled map — not the judgement stamp — is
    what the weight is read on."""
    first = draw_of("reject|x", [(1, 2)], selected=1, population=10,
                    path="/j/35100000001/autodedup-judge-35100000001/sample.json")
    second = draw_of("band|x", [(1, 2)], selected=1, population=10,
                     path="/j/35100000002/autodedup-judge-35100000002/sample.json")
    pooled = ev.pooled_sample([first, second])
    assert pooled.strata["reject|x"].n_selected == 0
    assert pooled.strata["band|x"].n_selected == 1
    assert pooled.stratum_of((1, 2)) == "band|x"
    # The stamp on the judgement row says `reject|x`; the pooled design overrules it.
    assert pooled.weight_for((1, 2), "reject|x") == pytest.approx(10.0)
    assert ev.stratum_of_pair(pooled, (1, 2), "reject|x") == "band|x"


def test_pooled_sample_of_one_draw_is_that_draw() -> None:
    only = draw_of("cell", [(1, 2)], selected=1, population=10, stratum_fn="judge_stratum")
    assert ev.pooled_sample([only]) is only
    assert ev.pooled_sample([]) is ev.EMPTY_SAMPLE


def test_resolve_sample_pairs_one_draw_with_each_judgements_file(tmp_path: Path) -> None:
    """`--sample` repeated is positional against `--judgements`; one covers them all; a count
    that is neither is a refusal rather than a silent mis-pairing."""
    def write(name: str, selected: int, population: int, pairs: Sequence[tuple[int, int]]) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps({
            "strata": {"cell": {"selected": selected, "population": population}},
            "pairs": [{"lo": lo, "hi": hi, "stratum": "cell"} for lo, hi in pairs],
        }), encoding="utf-8")
        return path

    first, second = write("s1.json", 2, 100, [(1, 2), (3, 4)]), write("s2.json", 3, 100,
                                                                     [(5, 6), (7, 8), (9, 10)])
    judgements = [str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")]
    pooled, note = harness.resolve_sample(judgements, [str(first), str(second)])
    assert pooled is not None and pooled.strata["cell"].n_selected == 5
    assert "2 draws pooled" in note
    one, _ = harness.resolve_sample(judgements, [str(first)])
    assert one.strata["cell"].n_selected == 2
    with pytest.raises(ValueError, match="pass one per"):
        harness.resolve_sample(judgements + [str(tmp_path / "c.jsonl")], [str(first), str(second)])
