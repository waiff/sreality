"""The keep-apart fixture list the lab reads for M3 (`autodedup/lab/keep_apart.json`, shared with G2)."""

from __future__ import annotations

import hashlib
import json

import pytest

from autodedup.lab import metrics
from autodedup.lab.board import Groups

SECTIONS = ("fixtures", "convention_pairs", "dropped")
SOURCES = {"operator ruling", "agent read", "C7"}
# w15/g2-model-first pins the same bytes (tests/autodedup/test_w15_g2_keep_apart.py): edit both branches together.
SHA256 = "e15ffe7d654625c6765348293853b162a8afce4ad7af999ace878123550d9dd6"


def _doc() -> dict:
    return json.loads(metrics.KEEP_APART.read_text(encoding="utf-8"))


def test_the_file_is_the_g2_copy() -> None:
    assert hashlib.sha256(metrics.KEEP_APART.read_bytes()).hexdigest() == SHA256


def test_the_file_loads_with_every_section() -> None:
    doc = _doc()
    assert set(SECTIONS) <= set(doc)
    for section in SECTIONS:
        for row in doc[section]:
            assert row["cohort"] and row["case"] and row["source"] in SOURCES, row
            assert row.get("fact") or row.get("convention") or row.get("why"), row
    assert all(row["rule"] for row in doc["convention_pairs"])
    assert len(metrics.load_fixtures()) == len(doc["fixtures"]) > 0


def test_every_id_pair_is_unique_and_ordered() -> None:
    doc = _doc()
    pairs = [tuple(row["ids"]) for section in SECTIONS for row in doc[section]]
    for lo, hi in pairs:
        assert isinstance(lo, int) and isinstance(hi, int) and lo < hi
    assert len(pairs) == len(set(pairs))


FIXTURES = [("c1", 1, 2, "joined"), ("c1", 1, 7, "joined"), ("c1", 3, 4, "apart"), ("c2", 5, 6, "elsewhere")]
GROUPS = Groups(clusters={1: (1, 2, 7), 3: (3, 8)})


def test_m3_counts_pairs_and_cases_an_arm_joins() -> None:
    out = metrics.fixtures_apart(GROUPS, FIXTURES, "c1", {1, 2, 3, 4, 7, 8})
    assert out == {"n": 3, "apart": 1, "cases": 2, "cases_apart": 1,
                   "together": ["joined (1 x 2)", "joined (1 x 7)"],
                   "present": ["1x2", "1x7", "3x4"]}


def test_m3_reads_the_ids_not_the_cohort_name() -> None:
    whole = metrics.fixtures_apart(GROUPS, FIXTURES, "c1", {1, 2, 3, 4, 7, 8})
    assert metrics.fixtures_apart(GROUPS, FIXTURES, "c1_postheal", {1, 2, 3, 4, 7, 8}) == whole
    assert metrics.fixtures_apart(GROUPS, FIXTURES, "c3", {9})["n"] == 0


def test_m3_fails_on_a_missing_fixture_advert() -> None:
    with pytest.raises(ValueError, match=r"apart \(3 x 4\)"):
        metrics.fixtures_apart(GROUPS, FIXTURES, "c1", {1, 2, 3, 7, 8})
    with pytest.raises(ValueError, match=r"apart \(3 x 4\)"):
        metrics.fixtures_apart(GROUPS, FIXTURES, "c1_postheal", {1, 2, 3, 7, 8})
    with pytest.raises(ValueError, match=r"absent from cohort c2"):
        metrics.fixtures_apart(GROUPS, FIXTURES, "c2", {9})


def test_the_table_shows_cases_next_to_pairs(tmp_path) -> None:
    entry = {"cohort": "c1", "experiment": "x", "config_id": "0", "zones": {"merge": 1, "band": 0},
             "groups": 1, "copairs": 1, "rulings": {"same_together": 0, "same_n": 0, "diff_together": 0,
                                                     "diff_n": 0, "precision_wilson_lower": None},
             "judges": {}, "timings": {},
             "fixtures": metrics.fixtures_apart(GROUPS, FIXTURES, "c1", {1, 2, 3, 4, 7, 8})}
    board = tmp_path / "board.jsonl"
    board.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert "| 1/3 (1/2 cases) |" in metrics.render(board)
