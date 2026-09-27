"""The keep-apart fixture list the lab reads for M3 (`autodedup/lab/keep_apart.json`, shared with G2)."""

from __future__ import annotations

import json

from autodedup.lab import metrics
from autodedup.lab.board import Groups

SECTIONS = ("fixtures", "convention_pairs", "dropped")
SOURCES = {"operator ruling", "agent read", "C7"}


def _doc() -> dict:
    return json.loads(metrics.KEEP_APART.read_text(encoding="utf-8"))


def test_the_file_loads_with_every_section() -> None:
    doc = _doc()
    assert set(SECTIONS) <= set(doc)
    for section in SECTIONS:
        for row in doc[section]:
            assert row["cohort"] and row["case"] and row["source"] in SOURCES, row
            assert row.get("fact") or row.get("convention") or row.get("why"), row
    loaded = metrics.load_fixtures()
    assert sum(len(v) for v in loaded.values()) == len(doc["fixtures"]) > 0


def test_every_id_pair_is_unique_and_ordered() -> None:
    doc = _doc()
    pairs = [tuple(row["ids"]) for section in SECTIONS for row in doc[section]]
    for lo, hi in pairs:
        assert isinstance(lo, int) and isinstance(hi, int) and lo < hi
    assert len(pairs) == len(set(pairs))


def test_m3_counts_the_pairs_an_arm_joins() -> None:
    fixtures = [(1, 2, "joined"), (3, 4, "apart"), (5, 99, "absent")]
    groups = Groups(clusters={1: (1, 2, 7), 3: (3, 8)})
    out = metrics.fixtures_apart(groups, fixtures, {1, 2, 3, 4, 5, 7, 8})
    assert out == {"n": 2, "apart": 1, "together": ["joined (1 x 2)"]}
