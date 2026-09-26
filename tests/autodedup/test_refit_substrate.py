"""`autodedup.refit_substrate`: one fit directory from several runs, each pair read at its home."""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from tests.autodedup.test_evaluate import pair_row, planted_rows

from autodedup import harness
from autodedup import refit_substrate as rs
from autodedup.labels import (
    label_pairs,
    load_all_judgements,
    load_all_operator_labels,
    operator_label_pairs,
)
from autodedup.settings import Settings


def _write_run(path: Path, rows: Sequence[dict[str, Any]], **settings: Any) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    with gzip.open(path / rs.PAIRS_FILE, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    cfg = Settings().to_dict()
    cfg.update(settings)
    (path / rs.RUN_FILE).write_text(json.dumps({"settings": cfg}), encoding="utf-8")
    return path


def _judgement(lo: int, hi: int, tier: str, verdict: str, **extra: Any) -> dict[str, Any]:
    return {"lo": lo, "hi": hi, "tier": tier, "model": "fake", "stratum": "s",
            "verdict": {"verdict": verdict, "confidence": 0.9}, **extra}


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _operator(lo: int, hi: int, verdict: str, source: str = "explicit") -> dict[str, Any]:
    return {"listing_lo": lo, "listing_hi": hi, "verdict": verdict, "source": source}


def _rows(run_dir: Path) -> list[dict[str, Any]]:
    with gzip.open(run_dir / rs.PAIRS_FILE, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_the_kept_label_is_exactly_the_one_the_label_store_keeps(tmp_path: Path) -> None:
    old = _write_jsonl(tmp_path / "old.jsonl", [
        _judgement(1, 2, "vision", "same_property"),
        _judgement(3, 4, "gold", "different_property", n_votes=3),
        _judgement(5, 6, "text", "same_property"),
        _judgement(7, 8, "vision", "different_property"),
    ])
    new = _write_jsonl(tmp_path / "new.jsonl", [
        _judgement(1, 2, "vision", "different_property"),
        _judgement(3, 4, "vision", "same_property"),
        _judgement(5, 6, "gold", "insufficient_evidence", n_votes=3),
        _judgement(9, 10, "text", "same_property"),
    ])
    ops = _write_jsonl(tmp_path / "op.jsonl", [
        _operator(7, 8, "same", "browse_merge"), _operator(11, 12, "different")])
    sources = [
        rs.judgement_source("old", old, ["a"]),
        rs.judgement_source("new", new, ["b"]),
        rs.operator_source("op", ops, ["b", "a"]),
    ]
    kept = rs.kept_labels(sources)
    store = label_pairs(
        load_all_judgements([old, new]),
        operator=operator_label_pairs(load_all_operator_labels([ops])),
    )
    assert {key: (label.y, label.tier) for key, (label, _) in kept.items()} == {
        key: (label.y, label.tier) for key, label in store.items()
    }
    # a later file corrects an earlier one INSIDE a tier; a higher tier beats a later file
    assert kept[(1, 2)][1].name == "new" and kept[(1, 2)][0].y == 0
    assert kept[(3, 4)][1].name == "old" and kept[(3, 4)][0].tier == "gold"
    # a higher-tier abstention wins like any label (the harness then drops the pair)
    assert kept[(5, 6)][0].y is None and kept[(5, 6)][1].name == "new"
    assert kept[(7, 8)][1].name == "op" and kept[(7, 8)][0].y == 1


def test_each_pair_is_read_at_its_home_then_in_its_own_fallback_order(tmp_path: Path) -> None:
    run_a = _write_run(tmp_path / "a", [
        pair_row(1, 2, score=0.11), pair_row(3, 4, score=0.12), pair_row(7, 8, score=0.13)])
    run_b = _write_run(tmp_path / "b", [pair_row(1, 2, score=0.21), pair_row(5, 6, score=0.22)])
    judged = _write_jsonl(tmp_path / "j.jsonl", [
        _judgement(1, 2, "vision", "same_property"),
        _judgement(3, 4, "vision", "different_property"),
        _judgement(9, 10, "vision", "same_property"),
        _judgement(7, 8, "vision", "insufficient_evidence"),
    ])
    ops = _write_jsonl(tmp_path / "op.jsonl", [_operator(5, 6, "same", "browse_merge")])
    sources = [
        rs.judgement_source("judge", judged, ["a", "b"]),
        rs.operator_source("op", ops, ["b", "a"]),
    ]
    report = rs.assemble({"a": run_a, "b": run_b}, sources, tmp_path / "out")
    rows = {(row["lo"], row["hi"]): row for row in _rows(tmp_path / "out")}
    assert set(rows) == {(1, 2), (3, 4), (5, 6)}
    assert rows[(1, 2)]["score"] == 0.11 and rows[(1, 2)][rs.SUBSTRATE_KEY] == "a"
    assert rows[(5, 6)]["score"] == 0.22 and rows[(5, 6)][rs.SUBSTRATE_KEY] == "b"
    judge = report["sources"]["judge"]
    assert judge["at_home"] == 2 and judge["missing"] == 1 and judge["kept_abstaining"] == 1
    assert report["missing_pairs"] == [[9, 10, "judge"]]
    assert report["runs"]["a"]["rows_taken"] == 2 and report["runs"]["b"]["rows_taken"] == 1

    only_b = [rs.judgement_source("judge", judged, ["b", "a"])]
    rs.assemble({"a": run_a, "b": run_b}, only_b, tmp_path / "out2")
    rows = {(row["lo"], row["hi"]): row for row in _rows(tmp_path / "out2")}
    assert rows[(1, 2)]["score"] == 0.21 and rows[(3, 4)][rs.SUBSTRATE_KEY] == "a"


def test_runs_scored_under_different_settings_are_refused(tmp_path: Path) -> None:
    run_a = _write_run(tmp_path / "a", [pair_row(1, 2)])
    run_b = _write_run(tmp_path / "b", [pair_row(1, 2)], store_floor=0.0)
    judged = _write_jsonl(tmp_path / "j.jsonl", [_judgement(1, 2, "text", "same_property")])
    with pytest.raises(ValueError, match="different settings"):
        rs.assemble({"a": run_a, "b": run_b},
                    [rs.judgement_source("j", judged, ["a"])], tmp_path / "out")
    with pytest.raises(ValueError, match="homes name runs"):
        rs.assemble({"a": run_a}, [rs.judgement_source("j", judged, ["zz"])], tmp_path / "o2")


def test_the_operator_tier_comes_as_one_source(tmp_path: Path) -> None:
    run_a = _write_run(tmp_path / "a", [pair_row(1, 2)])
    ops = _write_jsonl(tmp_path / "op.jsonl", [_operator(1, 2, "same")])
    two = [rs.operator_source("x", ops, ["a"]), rs.operator_source("y", ops, ["a"])]
    with pytest.raises(ValueError, match="ONE source"):
        rs.assemble({"a": run_a}, two, tmp_path / "out")


def test_the_operator_flags_reach_the_weights(tmp_path: Path) -> None:
    ops = _write_jsonl(tmp_path / "op.jsonl", [
        _operator(1, 2, "same", "browse_merge"), _operator(3, 4, "same", "implied")])
    source = rs.operator_source("op", ops, ["a"], browse_merge_weight=0.5,
                                include_implied=False)
    labels = source.tiers["operator"]
    assert set(labels) == {(1, 2)} and labels[(1, 2)].weight == 0.5


def test_harness_fit_reads_the_composite_like_any_run(tmp_path: Path) -> None:
    rows, labels = planted_rows(n=200)
    half = len(rows) // 2
    run_a = _write_run(tmp_path / "a", rows[:half])
    run_b = _write_run(tmp_path / "b", rows[half:])
    judged = _write_jsonl(tmp_path / "j.jsonl", [
        _judgement(lo, hi, "gold", label.verdict, n_votes=3) for (lo, hi), label in
        sorted(labels.items())
    ])
    spec = {
        "runs": {"a": str(run_a), "b": str(run_b)},
        "sources": [{"name": "gold", "judgements": str(judged), "homes": ["b", "a"]}],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    sink = io.StringIO()
    assert rs.main([str(spec_path), "--out", str(tmp_path / "composite")], out=sink) == 0
    assert "200 rows from 2 runs" in sink.getvalue()
    stamped = json.loads((tmp_path / "composite" / rs.RUN_FILE).read_text(encoding="utf-8"))
    assert stamped["substrate"]["rows"] == 200 and "settings" in stamped
    fit_out = io.StringIO()
    code = harness.main(["fit", str(tmp_path / "composite"), "--judgements", str(judged),
                         "--epochs", "400", "--method", "gd",
                         "--out", str(tmp_path / "fit")], out=fit_out)
    assert code == 0
    report = json.loads((tmp_path / "fit" / "fit.json").read_text(encoding="utf-8"))
    assert report["test_metrics"]["auc"] > 0.9


def test_a_spec_source_without_a_file_or_home_is_refused() -> None:
    with pytest.raises(ValueError, match="no home"):
        rs.sources_from_spec({"sources": [{"name": "x", "judgements": "j"}]})
    with pytest.raises(ValueError, match="no label file"):
        rs.sources_from_spec({"sources": [{"name": "x", "homes": ["a"]}]})
