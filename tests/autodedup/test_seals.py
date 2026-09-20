"""Split seals must be reproducible from the REPOSITORY, not from a scratch directory.

A model's `provenance.seal` names the holdout it was measured on, and `evaluate` refuses to
call a split "sealed" when the map it was given hashes to something else. That guarantee is
only as durable as the file: W6 opened with the W4/W5 maps gone (tmpfs), so neither shipped
model can be re-evaluated on the split it was fitted on. From here a seal is committed under
`autodedup/splits/<sha256>.json`, and a model may name a seal only when the map is committed
or the loss is recorded with its reason.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.autodedup.test_evaluate import planted_rows, write_judgements, write_run

from autodedup import seals
from autodedup.evaluate import split_seal
from autodedup.score_lane import MODELS_DIR

W6_SEAL = "37c8771fda6b06db2ead790fcf7728e0ccb0e80c60cad5358c2905ed39be52cc"


def _models() -> list[Path]:
    return sorted(MODELS_DIR.glob("*.json"))


def test_every_shipped_model_s_seal_is_committed_or_recorded_as_lost() -> None:
    unresolved: list[str] = []
    for path in _models():
        provenance = json.loads(path.read_text(encoding="utf-8")).get("provenance") or {}
        seal = (provenance.get("seal") or {}).get("sha256")
        if not seal:
            continue
        if not seals.known(seal):
            unresolved.append(f"{path.name} -> {seal[:12]}")
    assert not unresolved, (
        "a shipped model names a split seal with neither a committed map nor an entry in "
        f"seals.LOST_SEALS: {', '.join(unresolved)}"
    )


def test_the_lost_seals_are_the_two_the_tmpfs_wipe_took_and_say_why() -> None:
    """A registry that grows silently is a registry nobody notices growing. These two are the
    legacy losses; a NEW model whose map is not committed is a bug, not a third entry."""
    assert len(seals.LOST_SEALS) == 2
    for seal, reason in seals.LOST_SEALS.items():
        assert seals.is_seal(seal)
        assert not seals.committed(seal), "a lost seal that IS committed is no longer lost"
        assert len(reason) > 40 and ("lost" in reason or "wipe" in reason)
    stamped = {
        (json.loads(path.read_text(encoding="utf-8")).get("provenance") or {})
        .get("seal", {}).get("sha256")
        for path in _models()
    }
    assert set(seals.LOST_SEALS) <= stamped, "a lost seal no shipped model names is dead weight"


def test_a_committed_map_hashes_to_the_name_it_is_filed_under() -> None:
    committed = seals.committed_seals()
    assert W6_SEAL in committed, "the W6 seal must be committed, not left in scratch"
    for seal in committed:
        groups = seals.load(seal)
        assert split_seal(groups)["sha256"] == seal, f"{seal[:12]} is filed under the wrong name"


def test_the_w6_seal_is_the_split_the_refit_measured_on() -> None:
    groups = seals.load(W6_SEAL)
    assert len(groups) == 4569
    assert len(set(groups.values())) == 691


def test_resolve_takes_a_path_or_a_seal_and_refuses_anything_else(tmp_path: Path) -> None:
    assert seals.resolve(W6_SEAL) == seals.path_for(W6_SEAL)
    local = tmp_path / "split_map.json"
    seals.write_map(local, {7: 1, 9: 2})
    assert seals.resolve(str(local)) == local
    with pytest.raises(FileNotFoundError):
        seals.resolve("not-a-seal")
    with pytest.raises(FileNotFoundError) as exc:
        seals.resolve(next(iter(seals.LOST_SEALS)))
    # The refusal says WHY the map cannot be produced instead of reading as a typo.
    assert "lost" in str(exc.value)


def test_write_map_round_trips_and_is_byte_stable(tmp_path: Path) -> None:
    path = tmp_path / "split_map.json"
    seals.write_map(path, {3: 1, 1: 2, 2: 1})
    first = path.read_bytes()
    seals.write_map(path, {2: 1, 3: 1, 1: 2})
    assert path.read_bytes() == first
    assert seals.read_map(path) == {1: 2, 2: 1, 3: 1}


# --- the harness side ------------------------------------------------------------------------


def _fit(tmp_path: Path, out_name: str, *extra: str) -> tuple[str, dict]:
    from autodedup import harness

    rows, labels = planted_rows(n=120)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    out = io.StringIO()
    code = harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements), "--method", "gd", "--epochs", "2",
         "--out", str(tmp_path / out_name), *extra], out=out
    )
    assert code == 0
    report = json.loads((tmp_path / out_name / "fit.json").read_text(encoding="utf-8"))
    return out.getvalue(), report


def test_a_fit_whose_seal_is_not_committed_says_how_to_commit_it(tmp_path: Path) -> None:
    """The one sentence that would have saved the W5d holdout."""
    text, report = _fit(tmp_path, "fit")
    seal = report["split"]["seal"]["sha256"]
    assert not seals.committed(seal)
    assert f"seal {seal[:12]} is NOT committed" in text
    assert str(seals.path_for(seal)) in text
    assert str(tmp_path / "fit" / "split_map.json") in text


def test_fit_takes_a_committed_seal_where_it_takes_a_path(tmp_path: Path) -> None:
    from autodedup import harness

    first_text, first = _fit(tmp_path, "fit")
    # Commit the map the way the hint says to, into a stand-in splits directory.
    seal = first["split"]["seal"]["sha256"]
    splits = tmp_path / "splits"
    splits.mkdir()
    seals.write_map(splits / f"{seal}.json", seals.read_map(tmp_path / "fit" / "split_map.json"))
    original, seals.SPLITS_DIR = seals.SPLITS_DIR, splits
    try:
        text, reused = _fit(tmp_path, "fit2", "--split-map", seal)
        assert reused["split"]["from_split_map"] is True
        assert reused["split"]["seal"] == first["split"]["seal"]
        assert "is NOT committed" not in text
    finally:
        seals.SPLITS_DIR = original
    assert "is NOT committed" in first_text


def test_evaluate_finds_the_committed_map_for_the_model_s_own_seal(tmp_path: Path) -> None:
    """A model whose seal IS committed needs no --split-map: the alternative is the warning
    path, where the holdout is silently re-derived from the run being evaluated."""
    from autodedup import harness

    rows, labels = planted_rows(n=120)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    _fit(tmp_path, "fit")
    model_path = tmp_path / "fit" / harness.MODEL_FILE
    model = json.loads(model_path.read_text(encoding="utf-8"))
    seal = model["provenance"]["seal"]["sha256"]
    splits = tmp_path / "splits"
    splits.mkdir()
    seals.write_map(splits / f"{seal}.json", seals.read_map(tmp_path / "fit" / "split_map.json"))
    original, seals.SPLITS_DIR = seals.SPLITS_DIR, splits
    out = io.StringIO()
    try:
        code = harness.main(
            ["evaluate", str(run_dir), "--judgements", str(judgements),
             "--model", str(model_path), "--out", str(tmp_path / "eval")], out=out
        )
    finally:
        seals.SPLITS_DIR = original
    assert code == 0
    assert f"using the committed split map for seal {seal[:12]}" in out.getvalue()
    report = json.loads((tmp_path / "eval" / "eval.json").read_text(encoding="utf-8"))
    holdout = report["holdout"]
    assert holdout["seal"]["sha256"] == seal == holdout["expect_seal"]
    assert holdout["split_map_source"] == "fit"


def test_an_unusable_split_map_is_a_refusal_not_a_traceback(tmp_path: Path) -> None:
    from autodedup import harness

    rows, labels = planted_rows(n=40)
    run_dir = write_run(tmp_path, rows)
    judgements = write_judgements(tmp_path / "j.jsonl", labels)
    assert harness.main(
        ["fit", str(run_dir), "--judgements", str(judgements), "--split-map", "nope",
         "--out", str(tmp_path / "fit")], out=io.StringIO()
    ) == 1
    assert harness.main(
        ["evaluate", str(run_dir), "--judgements", str(judgements), "--split-map", "nope",
         "--out", str(tmp_path / "eval")], out=io.StringIO()
    ) == 1


# --- W9: spent seals, and the seed a committed map records ------------------------------

W9_SEAL = "fb9df2ea9fd773bf0eda256d00894924ba4b8491cc7559181f2c48da75e59884"


def test_the_w6_seal_is_recorded_as_spent_and_still_committed() -> None:
    """A spent seal keeps its file — the incumbent must stay re-measurable on it — and loses
    only its power to DECIDE. W8a's 1,476-candidate search read its test side (D23 ii)."""
    reason = seals.spent(W6_SEAL)
    assert reason and "1,476" in reason
    assert seals.committed(W6_SEAL), "a spent seal that lost its map is lost, not spent"
    assert W6_SEAL not in seals.LOST_SEALS, "spent and lost are different registers"
    assert seals.spent(W9_SEAL) is None, "the fresh seal has not been spent"


def test_every_spent_seal_names_what_spent_it_and_where_the_choice_moved() -> None:
    for seal, reason in seals.SPENT_SEALS.items():
        assert seals.is_seal(seal)
        assert seals.known(seal), "a seal nobody can resolve cannot be described as spent"
        assert len(reason) > 80 and "SPENT" in reason


def test_a_committed_map_records_the_seed_that_partitions_it(tmp_path: Path) -> None:
    """`split_of` hashes `<seed>:<group>`: one map under two seeds is two holdouts under one
    name, and `split_seal` hashes the map only — so the seed has to travel with the file."""
    assert seals.seed_for(W9_SEAL) == 20260922
    assert seals.seed_for(W6_SEAL) is None, "the legacy bare map predates the seed field"
    local = tmp_path / "split_map.json"
    seals.write_map(local, {7: 1, 9: 2}, seed=123)
    assert seals.read_map(local) == {7: 1, 9: 2}
    assert seals.read_seed(local) == 123
    bare = tmp_path / "bare.json"
    seals.write_map(bare, {7: 1, 9: 2})
    assert seals.read_map(bare) == {7: 1, 9: 2} and seals.read_seed(bare) is None


def test_the_w9_seal_is_the_split_this_wave_chose_on() -> None:
    groups = seals.load(W9_SEAL)
    assert len(groups) == 4456
    assert len(set(groups.values())) == 663
