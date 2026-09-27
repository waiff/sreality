"""`lab verify` in CI (B-a): the lab reads `harness run --evidence` and nothing else, and on a small
synthetic cohort its reference ladder reproduces the run row for row and group for group, each
reference rung returns what the engine function it restates returns, and a stale artefact is
refused. A `decide.py` change that breaks the equivalence fails here, so the lab cannot drift."""

from __future__ import annotations

import ast
import dataclasses
import io
import json
from pathlib import Path
from typing import Any

import pytest

from autodedup import dataset as ds
from autodedup import evidence, harness
from autodedup.lab import board, cache, verify
from tests.autodedup import lab_fixture

LAB = Path(__file__).resolve().parents[2] / "autodedup" / "lab"
# The context and policy rungs never fire under w31 on this cohort; this row makes them fire so
# the rung-by-rung read covers every reference rung (E63 on any stated body, rentals held).
EXERCISE = {"context_rule_min_score": 0.0, "context_rule_area_max": 1.0,
            "context_rule_price_ratio_min": 0.0, "context_rule_containment_min": 0.0,
            "merge_policy": {"prodej|byt": "propose"}}


def _registry(tmp: Path, export: Path, run_dir: Path) -> Path:
    path = tmp / "cohorts.json"
    path.write_text(json.dumps({"cache_root": str(tmp / "cache"),
                                "leaderboard": str(tmp / "board.jsonl"),
                                "cohorts": {"fx": {"export": str(export), "run": str(run_dir)}}}),
                    encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def export(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return lab_fixture.write(tmp_path_factory.mktemp("lab") / "cohort.jsonl.gz")


@pytest.fixture(scope="module")
def w31_run(export: Path) -> Path:
    out = export.parent / "run_w31"
    code = harness.main(["run", str(export), "--out", str(out), "--settings", "w31",
                         "--model", "w6_gold", "--evidence"], out=io.StringIO())
    assert code == 0
    return out


@pytest.fixture(scope="module")
def cohort(export: Path, w31_run: Path) -> cache.Cohort:
    return cache.open_cohort("fx", _registry(export.parent, export, w31_run), workers=1)


def test_harness_run_writes_the_evidence_beside_its_rows(w31_run: Path, cohort: cache.Cohort
                                                        ) -> None:
    manifest = json.loads((w31_run / evidence.MANIFEST).read_text(encoding="utf-8"))
    summary = json.loads((w31_run / harness.RUN_FILE).read_text(encoding="utf-8"))
    assert manifest["pairs"] == summary["pairs_scored"] == cohort.n
    assert manifest["stored"] == summary["pairs_stored"]
    assert manifest["code_digest"] == evidence.code_digest() == cohort.code_digest
    assert cohort.version == manifest["version"]
    assert summary["evidence"]["version"] == manifest["version"]


def test_the_reference_ladder_reproduces_harness_run_row_for_row(cohort: cache.Cohort) -> None:
    outcome = board.run(cohort, board.load_config(LAB / "experiments" / "w31_reference.json"))
    rows = verify.rows(cohort, outcome)
    assert rows["identical"] == rows["pairs"] == cohort.n, rows["examples"]
    assert rows["stored_rows_identical"] == rows["stored_rows"] == rows["stored_lab"] > 0
    groups = verify.groups(outcome, cohort.run.dir)
    assert groups["identical"] and groups["lab"] > 0
    report = {"rows": rows, "groups": groups}
    assert verify.passed(report)


def _rungs(c: cache.Cohort) -> dict[str, Any]:
    out = verify.rung_equivalence(c)
    assert out["ok"], {k: v["examples"] for k, v in out["rungs"].items() if v["examples"]}
    return out["rungs"]


def test_each_reference_rung_returns_the_engine_value_for_every_pair(
        export: Path, cohort: cache.Cohort) -> None:
    plain = _rungs(cohort)
    settings = dataclasses.replace(harness.named_settings("w31"), **EXERCISE)
    root = export.parent / "exercise"
    root.mkdir(exist_ok=True)
    harness.run(ds.load(export), settings, harness.named_model("w6_gold"), root / "run",
                evidence=evidence.Request(evidence.file_digest(export)))
    exercised = cache.open_cohort("fx", _registry(root, export, root / "run"), workers=1)
    moved = {rung: plain[rung]["moved"] + v["moved"] for rung, v in _rungs(exercised).items()}
    assert set(moved) == set(board.REFERENCE_LADDER)
    assert all(moved[rung] > 0 for rung in board.REFERENCE_LADDER), moved
    rows = verify.rows(exercised, board.run(exercised, {}))
    assert rows["identical"] == rows["pairs"], rows["examples"]


def test_a_rung_that_drifts_from_the_engine_is_named(cohort: cache.Cohort,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    def drifted(c: cache.Cohort, d: board.Decisions, p: dict[str, Any]) -> board.Decisions:
        d = d.copy()
        m = (d.zone == board.U) & (c.sig["auto"] != "")
        d.settle(m, board.REJECT, "fact", "x", "ATTR", "auto_reject:drifted")
        d.final |= m
        return d
    monkeypatch.setitem(board.RUNGS, "auto_reject", drifted)
    out = verify.rung_equivalence(cohort)
    assert not out["ok"]
    assert out["rungs"]["veto"]["identical"] == out["pairs"]
    assert out["rungs"]["auto_reject"]["identical"] < out["pairs"]


def test_a_stale_artefact_is_refused(export: Path, w31_run: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence, "code_digest", lambda *a, **k: "000000000000")
    with pytest.raises(cache.StaleEvidence, match="re-run"):
        cache.open_cohort("fx", _registry(export.parent, export, w31_run), workers=1)


def test_the_code_digest_reads_every_module_decide_pair_reads() -> None:
    modules = evidence.engine_modules()
    assert {"autodedup/decide.py", "autodedup/indistinguishable.py", "autodedup/cluster.py",
            "autodedup/harness.py", "autodedup/incremental.py", "autodedup/labels.py",
            "toolkit/room_taxonomy.py"} <= set(modules)
    assert not any(m.startswith("autodedup/lab/") for m in modules)


RETRIEVAL = {"generate_pairs", "build_all", "pair_features", "FeatureContext", "BlockIndex",
             "whole_cohort", "run_pass", "retrieve"}


def test_the_lab_has_no_second_retrieval_path() -> None:
    """The lab's pairs and features come from the artefact: no lab module imports a retrieval or
    feature builder (SW1 moved the batch path to tests/autodedup/whole_cohort.py)."""
    for path in sorted(LAB.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                names = {alias.name for alias in node.names} | {node.module or ""}
                assert not (names & RETRIEVAL) and "whole_cohort" not in (node.module or ""), \
                    (path.name, node.module, names & RETRIEVAL)
            elif isinstance(node, ast.Import):
                assert not any("whole_cohort" in alias.name for alias in node.names), path.name
