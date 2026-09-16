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
    # the real lane sample resolves through the sampler's own key and really inflates
    assert payload["sample"]["stratum_fn"] == "judge_stratum"
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


def test_a_blocked_positive_is_spent_against_the_recall_budget_first() -> None:
    rows = [scored(0.9, 1) for _ in range(96)] + [scored(0.01, 1, blocked=True) for _ in range(4)]
    report = ev.thresholds(rows)
    assert report.weight_positive_unavoidable == pytest.approx(4.0)
    assert report.missed_share_at_t_lo == pytest.approx(0.04)  # already over budget at T_lo=0
    assert report.t_lo == 0.0


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
    assert body["min_stratum_precision"] == 0.0 and body["stratum_floor_ok"] is False
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
    _, report = ev.fit_model(rows, labels, epochs=2)
    assert report.sections["convergence"]["converged_flag"] is False
    assert "NOT CONVERGED" in "\n".join(report.headline())


def test_the_fit_cli_exposes_the_knobs_and_can_fail_on_a_short_fit(tmp_path: Path) -> None:
    rows, labels = planted_rows(n=120)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    out = io.StringIO()
    code = harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements), "--epochs", "2",
         "--l2", "0.01", "--weight-cap", "3.0", "--require-convergence",
         "--out", str(tmp_path / "fit")], out=out
    )
    assert code == 1  # the model is still written; the exit code is the loud part
    fit_json = json.loads((tmp_path / "fit" / "fit.json").read_text(encoding="utf-8"))
    assert fit_json["convergence"]["epochs"] == 2 and fit_json["convergence"]["l2"] == 0.01
    assert fit_json["split"]["weight_cap"] == 3.0
    split_map = json.loads(
        (tmp_path / "fit" / harness.SPLIT_MAP_FILE).read_text(encoding="utf-8")
    )
    assert split_map
    out2 = io.StringIO()
    assert harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements), "--epochs", "2",
         "--split-map", str(tmp_path / "fit" / harness.SPLIT_MAP_FILE),
         "--out", str(tmp_path / "fit2")], out=out2
    ) == 0
    reused = json.loads((tmp_path / "fit2" / "fit.json").read_text(encoding="utf-8"))
    assert reused["split"]["seal"] == fit_json["split"]["seal"]
    assert reused["split"]["from_split_map"] is True


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
