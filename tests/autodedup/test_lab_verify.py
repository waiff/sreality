"""`lab verify` in CI (B-a): the lab reads `harness run --evidence` and nothing else, and on a small
synthetic cohort its reference ladder reproduces the run row for row and group for group, and each
reference rung returns what the engine function it restates returns, under w31 and under four
settings rows that between them reach EVERY branch of `decide_pair` the lab restates (the census
below fails on a dark branch). A `decide.py` change that breaks the equivalence fails here, so the
lab cannot drift; a stale or sealed artefact is refused."""

from __future__ import annotations

import ast
import dataclasses
import gzip
import io
import json
import pickle
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from autodedup import dataset as ds
from autodedup import evidence, harness
from autodedup.decide import decide_pair
from autodedup.lab import board, cache, verify
from autodedup.lab.__main__ import main as lab_main
from tests.autodedup import lab_fixture

LAB = Path(__file__).resolve().parents[2] / "autodedup" / "lab"
ROWS = ("w31", *lab_fixture.EXERCISE)


def _registry(tmp: Path, export: Path, run_dir: Path, **extra: Any) -> Path:
    path = tmp / "cohorts.json"
    path.write_text(json.dumps({"cache_root": str(tmp / "cache"),
                                "leaderboard": str(tmp / "board.jsonl"),
                                "cohorts": {"fx": {"export": str(export), "run": str(run_dir),
                                                   **extra}}}),
                    encoding="utf-8")
    return path


def _run(export: Path, out: Path, row: str) -> Path:
    settings = dataclasses.replace(harness.named_settings("w31"), **lab_fixture.EXERCISE.get(row, {}))
    with pytest.MonkeyPatch.context() as patch:
        if row in lab_fixture.WALLED:
            patch.setattr(evidence, "write", lab_fixture.with_wall_pairs(evidence.write))
        summary = harness.run(ds.load(export), settings, harness.named_model("w6_gold"), out,
                              evidence=evidence.Request(evidence.file_digest(export)))
    (out / harness.RUN_FILE).write_text(json.dumps(summary, sort_keys=True, default=str),
                                        encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def export(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return lab_fixture.write(tmp_path_factory.mktemp("lab") / "cohort.jsonl.gz")


@pytest.fixture(scope="module")
def w31_run(export: Path) -> Path:
    return _run(export, export.parent / "run_w31", "w31")


@pytest.fixture(scope="module")
def cohort(export: Path, w31_run: Path) -> cache.Cohort:
    return cache.open_cohort("fx", _registry(export.parent, export, w31_run), workers=1)


@pytest.fixture(scope="module")
def rows(export: Path, cohort: cache.Cohort) -> dict[str, cache.Cohort]:
    out = {"w31": cohort}
    for row in lab_fixture.EXERCISE:
        root = export.parent / row
        root.mkdir(exist_ok=True)
        out[row] = cache.open_cohort("fx", _registry(root, export, _run(export, root / "run", row)),
                                     workers=1)
    return out


def test_harness_run_writes_the_evidence_beside_its_rows(w31_run: Path, cohort: cache.Cohort
                                                        ) -> None:
    manifest = json.loads((w31_run / evidence.MANIFEST).read_text(encoding="utf-8"))
    summary = json.loads((w31_run / harness.RUN_FILE).read_text(encoding="utf-8"))
    walls = len(lab_fixture.WALL_PAIRS)
    assert manifest["pairs"] == summary["pairs_scored"] + walls == cohort.n
    assert manifest["stored"] == summary["pairs_stored"]
    assert manifest["code_digest"] == evidence.code_digest() == cohort.code_digest
    assert manifest["engine_data"] == list(evidence.DIGEST_DATA)
    assert manifest["rulings_digest"] == evidence.rulings_digest()
    assert cohort.version == manifest["version"]
    assert summary["evidence"]["version"] == manifest["version"]


@pytest.mark.parametrize("row", ROWS)
def test_the_reference_ladder_reproduces_harness_run_row_for_row(
        rows: dict[str, cache.Cohort], row: str) -> None:
    c = rows[row]
    outcome = board.run(c, board.load_config(LAB / "experiments" / "w31_reference.json"))
    got = verify.rows(c, outcome)
    assert got["identical"] == got["pairs"] == c.n, got["examples"]
    assert got["stored_rows_identical"] == got["stored_rows"] == got["stored_lab"] > 0
    groups = verify.groups(outcome, c.run.dir)
    assert groups["identical"] and (groups["lab"] > 0 or row == "context_policy")
    rungs = verify.rung_equivalence(c)
    assert rungs["ok"], {k: v["examples"] for k, v in rungs["rungs"].items() if v["examples"]}
    assert verify.passed({"rows": got, "groups": groups, "rung_equivalence": rungs})


def test_every_branch_the_lab_restates_is_read(rows: dict[str, cache.Cohort]) -> None:
    """The census: each branch of `decide_pair` (a zone and a reason form at some stage) is reached
    by some pair of some row, and every reference rung moves some pair."""
    seen: set[tuple[str, str]] = set()
    moved = dict.fromkeys(board.REFERENCE_LADDER, 0)
    for c in rows.values():
        for i in range(c.n):
            seen |= {(d.zone, d.reason) for d in verify._engine_stages(c, i).values()}
        for rung, v in verify.rung_equivalence(c)["rungs"].items():
            moved[rung] += v["moved"]
    dark = [(zone, pattern) for zone, pattern in lab_fixture.BRANCHES
            if not any(z == zone and re.search(pattern, r) for z, r in seen)]
    assert not dark, dark
    assert all(moved.values()), moved


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


def test_a_stale_artefact_is_refused_before_its_pickle_is_read(
        export: Path, w31_run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / evidence.MANIFEST).write_bytes((w31_run / evidence.MANIFEST).read_bytes())
    (run / evidence.FILE).write_bytes(b"not a pickle")
    monkeypatch.setattr(evidence, "code_digest", lambda *a, **k: "000000000000")
    with pytest.raises(cache.StaleEvidence, match="re-run"):
        cache.open_cohort("fx", _registry(tmp_path, export, run), workers=1)
    monkeypatch.undo()
    with pytest.raises(pickle.UnpicklingError):
        cache.open_cohort("fx", _registry(tmp_path, export, run), workers=1)


def test_the_code_digest_reads_every_module_decide_pair_reads() -> None:
    modules = evidence.engine_modules()
    assert {"autodedup/decide.py", "autodedup/indistinguishable.py", "autodedup/cluster.py",
            "autodedup/harness.py", "autodedup/incremental.py", "autodedup/labels.py",
            "toolkit/room_taxonomy.py"} <= set(modules)
    assert not any(m.startswith("autodedup/lab/") for m in modules)
    assert evidence.engine_files() == modules + ["data/clip_taxonomy.json"]


# Repo paths a digest module anchors on `__file__` that are NOT in DIGEST_DATA, and why each
# cannot stale an artefact without re-versioning it.
READ_ELSEWHERE = {
    "settings": "a settings row is hashed into the version by content (settings_bytes)",
    "models": "a model file is hashed into the version by content (model.to_json)",
    "splits": "the committed split seals are read by `fit` / `evaluate`, never by decide_pair",
    "docs/design/autodedup/preregistrations": "the seals refuse an export; they decide nothing",
}


def _anchored_paths(path: Path) -> set[str]:
    """String parts joined onto a `Path(__file__)` anchor in one module-level assignment."""
    out: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            source = ast.unparse(node.value)
            if "__file__" in source or "REPO" in source:
                parts = sorted((n.lineno, n.col_offset, n.value) for n in ast.walk(node.value)
                               if isinstance(n, ast.Constant) and isinstance(n.value, str))
                if parts:
                    out.add("/".join(value for *_, value in parts))
    return out


def test_every_data_file_a_digest_module_reads_is_in_the_digest() -> None:
    """A census: a new `Path(__file__)`-anchored data file in any engine module fails here until
    it joins DIGEST_DATA (or READ_ELSEWHERE says why it cannot stale an artefact)."""
    repo = evidence.REPO
    unlisted = []
    for rel in evidence.engine_modules():
        for joined in _anchored_paths(repo / rel):
            name = joined.strip("/")
            if (name in evidence.DIGEST_DATA or name.split("/")[0] in READ_ELSEWHERE
                    or any(name.startswith(k) for k in READ_ELSEWHERE)
                    or "." not in Path(name).name and name not in READ_ELSEWHERE):
                continue
            unlisted.append(f"{rel}: {name}")
    assert not unlisted, unlisted
    assert "data/clip_taxonomy.json" in {p.strip("/") for p in _anchored_paths(
        repo / "autodedup" / "fingerprint.py")}


def test_the_version_carries_the_rulings_the_run_bound(export: Path, w31_run: Path,
                                                       tmp_path: Path) -> None:
    ruled = harness.run(ds.load(export), harness.named_settings("w31"),
                        harness.named_model("w6_gold"), tmp_path / "ruled",
                        must_not_link=frozenset({(1401, 1402)}),
                        evidence=evidence.Request(evidence.file_digest(export)))
    plain = json.loads((w31_run / evidence.MANIFEST).read_text(encoding="utf-8"))
    assert ruled["evidence"]["rulings_digest"] == evidence.rulings_digest({(1401, 1402)})
    assert ruled["evidence"]["version"] != plain["version"]
    assert ruled["evidence"]["code_digest"] == plain["code_digest"]


def _seal(tmp: Path, blocks: str) -> Path:
    path = tmp / "preregistration_cohort99.json"
    path.write_text(json.dumps({"blocks": {"spelling": blocks}}), encoding="utf-8")
    return path


def test_a_sealed_export_is_refused_unless_its_freeze_is_named(
        export: Path, w31_run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal = _seal(tmp_path, "town:577626 quarter:1")
    assert "town:577626" in evidence.export_blocks(export)
    reg = json.loads(_registry(tmp_path, export, w31_run).read_text(encoding="utf-8"))
    reg["seals"] = [str(seal)]
    (tmp_path / "cohorts.json").write_text(json.dumps(reg), encoding="utf-8")
    with pytest.raises(evidence.SealedExport, match="town:577626"):
        cache.open_cohort("fx", tmp_path / "cohorts.json", workers=1)
    reg["cohorts"]["fx"]["freeze"] = "the freeze addendum"
    (tmp_path / "cohorts.json").write_text(json.dumps(reg), encoding="utf-8")
    assert cache.open_cohort("fx", tmp_path / "cohorts.json", workers=1).n > 0
    monkeypatch.setenv(evidence.SEALS_ENV, str(seal))
    args = ["run", str(export), "--out", str(tmp_path / "sealed"), "--settings", "w31",
            "--model", "w6_gold", "--evidence"]
    with pytest.raises(evidence.SealedExport):
        harness.main(args, out=io.StringIO())
    assert harness.main(args + ["--freeze"], out=io.StringIO()) == 0
    assert harness.main(args[:-1], out=io.StringIO()) == 0


def test_a_rung_reads_its_lazy_signals_under_an_arms_settings(cohort: cache.Cohort) -> None:
    """The gate and the promotion re-read under a settings override by the same engine functions,
    kept per override in the overlay: the PROMOTE-mode variant (`tags are never facts, in any mode`
    is one such override once the engine carries the switch)."""
    idx = cohort.keys.index((1901, 1902))
    override = {"d43_promote_min_agreeing": 9}
    changed = dataclasses.replace(cohort.settings, **override)
    every = np.arange(cohort.n)
    promote = cohort.lazy("promote", every, override)
    for i, (lo, hi) in enumerate(cohort.keys):
        assert tuple(promote[n][i] for n in ("warrant", "refusal", "corro")) == \
            evidence.promote_signals(cohort.ds.listings[lo], cohort.ds.listings[hi],
                                     cohort.feats(i), changed)
    assert promote["warrant"][idx] != cohort.sig["warrant"][idx] == "agree:2"
    ladder = [{"rung": r} for r in board.REFERENCE_LADDER]
    ladder[6] = {"rung": "demonstrate", "settings": override}
    out = board.run(cohort, {"ladder": ladder, "group": {"step": "relation"}})
    la, lb = cohort.ds.listings[1901], cohort.ds.listings[1902]
    engine = decide_pair(cohort.fps[1901], cohort.fps[1902], la, lb, cohort.feats(idx),
                         cohort.probes[idx], cohort.model, changed, cohort.census)
    assert (board.ZONE_NAMES[out.decisions.zone[idx]], out.decisions.reason[idx]) == \
        (engine.zone, engine.reason) != ("merge", "d43_promote:agree:2")
    gate = cohort.lazy("gate", every, {"d43_gate_image_facts": True})
    assert all(gate["gate"][i] == evidence.gate_signals(
        cohort.ds.listings[k[0]], cohort.ds.listings[k[1]], cohort.feats(i),
        dataclasses.replace(cohort.settings, d43_gate_image_facts=True))[0]
        for i, k in enumerate(cohort.keys))
    cohort.save()
    saved = pickle.loads((cohort.path / "overlay.pkl").read_bytes())
    assert json.dumps(override, sort_keys=True) in saved["tagged"]
    with pytest.raises(TypeError):
        cohort.lazy("promote", np.array([idx]), {"no_such_dial": True})


def test_a_withdrawn_k_b_falls_as_the_engine_re_decides_it(cohort: cache.Cohort) -> None:
    """The proof rung's `allow` without K-B is E85's `kb_refused`, pair for pair (off_proof_KB)."""
    ladder = [{"rung": r} for r in board.REFERENCE_LADDER]
    ladder[2] = {"rung": "proof", "allow": ["K-R", "K-A", "K-C"]}
    out = board.run(cohort, {"ladder": ladder, "group": {"step": "relation"}})
    kb = [i for i in range(cohort.n) if cohort.sig["cert"][i] == "K-B"]
    assert kb
    for i in kb:
        lo, hi = cohort.keys[i]
        engine = decide_pair(cohort.fps[lo], cohort.fps[hi], cohort.ds.listings[lo],
                             cohort.ds.listings[hi], cohort.feats(i), cohort.probes[i],
                             cohort.model, cohort.settings, cohort.census, kb_refused=True)
        assert (board.ZONE_NAMES[out.decisions.zone[i]], out.decisions.reason[i]) == \
            (engine.zone, engine.reason)
        assert engine.certificate != "K-B"


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


# --- the lab CLI end to end on the fixture: run, board, keep, cut ------------------------------

def _cli_registry(tmp: Path, export: Path, run: Path) -> Path:
    labels = tmp / "labels"
    labels.mkdir()
    (labels / "operator_labels.jsonl").write_text("\n".join(json.dumps(
        {"listing_lo": lo, "listing_hi": hi, "verdict": v, "decided_at": "2026-09-01"})
        for lo, hi, v in ((1901, 1902, "same"), (1401, 1402, "same"), (1001, 1002, "same"),
                          (1801, 1802, "different"))) + "\n", encoding="utf-8")
    spec = {"export": str(export), "run": str(run)}
    path = tmp / "cohorts.json"
    path.write_text(json.dumps({"cache_root": str(tmp / "cache"),
                                "leaderboard": str(tmp / "board.jsonl"), "labels": str(labels),
                                "cohorts": {"trial": spec, "c17": spec, "c18": spec}}),
                    encoding="utf-8")
    return path


def _keep_apart(tmp: Path, extra: list[dict[str, Any]] = ()) -> Path:
    path = tmp / "keep_apart.json"
    fixtures = [{"cohort": "c18", "ids": [1801, 1802], "case": "Jiraskova floors",
                 "fact": "floor 2 vs 3", "source": "agent read"}, *extra]
    path.write_text(json.dumps({"fixtures": fixtures}), encoding="utf-8")
    return path


def test_the_lab_cli_runs_boards_keeps_and_cuts_on_the_fixture(
        export: Path, w31_run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    from autodedup.lab import metrics

    reg = _cli_registry(tmp_path, export, w31_run)
    monkeypatch.setattr(metrics, "KEEP_APART", _keep_apart(tmp_path))
    cut = tmp_path / "t_cut.json"
    cut.write_text(json.dumps({"name": "t_cut", "ladder": [
        {"rung": r} for r in board.REFERENCE_LADDER[:3]] + [
        {"rung": "score", "t_hi_by_stratum": {}, "t_hi": 0.9}] + [
        {"rung": r} for r in board.REFERENCE_LADDER[4:]],
        "sweep": {"ladder.3.t_hi": [0.7, 0.75, 0.8, 0.85, 0.9]}}), encoding="utf-8")
    base = ["--cohorts", str(reg), "--workers", "1"]
    for name in ("trial", "c17", "c18"):
        assert lab_main([*base, "verify", name, "--rungs", "-1"]) == 0
    capsys.readouterr()
    assert lab_main([*base, "run", str(LAB / "experiments" / "off_demonstrate.json"), str(cut),
                     "--cohort", "trial", "--cohort", "c17", "--cohort", "c18", "--why"]) == 0
    capsys.readouterr()
    board_file = tmp_path / "BOARD.md"
    assert lab_main([*base, "board", "--out", str(board_file)]) == 0
    table = board_file.read_text(encoding="utf-8").splitlines()
    assert all(m in table[0] for m in ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"))
    ref = [line for line in table if "| c18 | w31_reference |" in line]
    assert len(ref) == 1 and "| yes | 2/3 | 0/1 | 1/1 (1/1 cases) |" in ref[0], ref
    off = [line for line in table if "| c18 | off_demonstrate |" in line]
    assert "| yes | 1/3 |" in off[0], off
    capsys.readouterr()
    assert lab_main([*base, "keep", "off_demonstrate", "--incumbent", "w31_reference"]) == 0
    dropped = json.loads(capsys.readouterr().out)
    assert dropped["verdict"] == "DROP" and "c18 M1 1 < incumbent 2" in dropped["reasons"]
    assert dropped["checks"]["comparable"] and dropped["checks"]["verified"]
    assert lab_main([*base, "keep", "t_cut[t_hi=0.9]", "--incumbent", "w31_reference"]) == 0
    kept = json.loads(capsys.readouterr().out)
    assert kept["verdict"] == "KEEP", kept
    monkeypatch.setattr(metrics, "KEEP_APART", _keep_apart(tmp_path, [
        {"cohort": "c6", "ids": [1, 2], "case": "absent houses", "fact": "price",
         "source": "agent read"}]))
    assert lab_main([*base, "keep", "t_cut[t_hi=0.9]", "--incumbent", "w31_reference"]) == 0
    incomplete = json.loads(capsys.readouterr().out)
    assert incomplete["verdict"] == "INCOMPLETE" and any("c6" in m for m in incomplete["missing"])
    monkeypatch.setattr(metrics, "KEEP_APART", _keep_apart(tmp_path))
    assert lab_main([*base, "cut", "t_cut", "--param", "t_hi", "--incumbent",
                     "w31_reference"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cut"] == 0.9 and out["keep"]["verdict"] == "KEEP", out
    assert all(v != "no row" for v in out["sweep"].values())
