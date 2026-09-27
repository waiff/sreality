"""The keep-apart fixture list the G2 prototype reads (`autodedup/lab/keep_apart.json`, shared with the lab)."""

from __future__ import annotations

import hashlib

import pytest

from autodedup_w15_g2 import fixtures

SOURCES = {"operator ruling", "agent read", "C7"}
# global/g3-fast-iteration pins the same bytes (tests/autodedup/test_lab_keep_apart.py): edit both branches together.
SHA256 = "e15ffe7d654625c6765348293853b162a8afce4ad7af999ace878123550d9dd6"


def _ids(cohort: str) -> set[int]:
    return {i for row in fixtures.load()["fixtures"] if row["cohort"] == cohort for i in row["ids"]}


def test_the_file_is_the_lab_copy() -> None:
    assert hashlib.sha256(fixtures.PATH.read_bytes()).hexdigest() == SHA256


def test_the_file_loads_with_every_section() -> None:
    doc = fixtures.load()
    assert set(fixtures.SECTIONS) <= set(doc)
    assert doc["fixtures"], "the hard bar needs pairs"
    for section in fixtures.SECTIONS:
        for row in doc[section]:
            assert row["cohort"] and row["case"] and row["source"] in SOURCES, row
            assert row.get("fact") or row.get("convention") or row.get("why"), row
    assert all(row["rule"] for row in doc["convention_pairs"])


def test_every_id_pair_is_unique_and_ordered() -> None:
    doc = fixtures.load()
    pairs = [tuple(row["ids"]) for section in fixtures.SECTIONS for row in doc[section]]
    for lo, hi in pairs:
        assert isinstance(lo, int) and isinstance(hi, int) and lo < hi
    assert len(pairs) == len(set(pairs))


def test_present_reads_the_ids_not_the_cohort_name() -> None:
    c17 = _ids("c17")
    got = fixtures.present("c17", c17)
    assert len(got) == sum(1 for row in fixtures.load()["fixtures"] if row["cohort"] == "c17")
    assert got["Na Zertvach 783/23 rooms (624 x 18356370)"] == (624, 18356370)
    assert fixtures.present("c17_postheal", c17) == got
    assert fixtures.present("c19", {1, 2}) == {}


def test_present_fails_on_a_missing_fixture_advert() -> None:
    c17 = _ids("c17")
    with pytest.raises(ValueError, match=r"624 x 18356370"):
        fixtures.present("c17", c17 - {624})
    with pytest.raises(ValueError, match=r"624 x 18356370"):
        fixtures.present("c17_postheal", c17 - {624})
    with pytest.raises(ValueError, match=r"absent from cohort c17"):
        fixtures.present("c17", set())


def test_adversary_is_m3_on_the_arm_groups() -> None:
    pytest.importorskip("sklearn")
    from autodedup_w15_g2.sealed import adversary

    c17 = _ids("c17") | {5}
    out = adversary("c17", {7: [18356370, 18733617, 5]}, c17)
    assert len(out) == len(fixtures.present("c17", c17))
    assert [k for k, v in out.items() if v["together"]] == ["Na Zertvach 783/23 rooms (18356370 x 18733617)"]
    assert out["Na Zertvach 783/23 rooms (18356370 x 18733617)"]["group_size"] == 3
    assert out["Na Zertvach 783/23 rooms (624 x 18356370)"] == {
        "together": False, "group_size": None, "a_group": 1, "b_group": 3}
    with pytest.raises(ValueError):
        adversary("c17", {}, c17 - {18733617})
