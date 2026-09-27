"""The keep-apart fixture list the G2 prototype reads (`autodedup/lab/keep_apart.json`, shared with the lab)."""

from __future__ import annotations

from autodedup_w15_g2 import fixtures

SOURCES = {"operator ruling", "agent read", "C7"}


def test_the_file_loads_with_every_section() -> None:
    doc = fixtures.load()
    assert set(fixtures.SECTIONS) <= set(doc)
    assert doc["fixtures"], "the hard bar needs pairs"
    for section in fixtures.SECTIONS:
        for row in doc[section]:
            assert row["cohort"] and row["case"] and row["source"] in SOURCES, row
            assert row.get("fact") or row.get("convention") or row.get("why"), row


def test_every_id_pair_is_unique_and_ordered() -> None:
    doc = fixtures.load()
    pairs = [tuple(row["ids"]) for section in fixtures.SECTIONS for row in doc[section]]
    for lo, hi in pairs:
        assert isinstance(lo, int) and isinstance(hi, int) and lo < hi
    assert len(pairs) == len(set(pairs))


def test_by_cohort_keeps_every_fixture() -> None:
    per = fixtures.by_cohort()
    assert sum(len(v) for v in per.values()) == len(fixtures.load()["fixtures"])
    assert per["c17"]["Na Zertvach 783/23 rooms (624 x 18356370)"] == (624, 18356370)
