"""The lab's plug board over a hand-made evidence cache: the reference rungs settle each pair the
way `decide_pair` does, a new rung is one registered function, and a sweep is one arm per value."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from autodedup.features import FEATURE_ORDER
from autodedup.lab import board
from autodedup.lab.cache import FIDX, Cohort
from autodedup.settings import Settings

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup" / "settings" / "w31.json"


def _obj(values: list[Any]) -> np.ndarray:
    out = np.empty(len(values), dtype=object)
    out[:] = values
    return out


def _cohort(tmp_path: Path) -> Cohort:
    keys = [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 12), (13, 14)]
    n = len(keys)
    V = np.zeros((n, len(FEATURE_ORDER)))
    P = np.zeros((n, len(FEATURE_ORDER)), dtype=bool)
    V[:, FIDX["same_source"]], P[:, FIDX["same_source"]] = 1.0, True
    V[6, FIDX["tag_room_clip_min2"]], P[6, FIDX["tag_room_clip_min2"]] = 0.99, True
    V[6, FIDX["tag_rooms_private"]], P[6, FIDX["tag_rooms_private"]] = 3.0, True
    sig = {
        "veto": _obj(["unit_designator", "", "", "", "", "", ""]),
        "auto": _obj(["", "numeral_conflict", "", "", "", "", ""]),
        "cert": _obj(["", "", "K-C", "", "", "", ""]),
        "nfam": np.array([1, 1, 2, 1, 1, 1, 1], dtype=np.int16),
        "block": _obj([""] * n),
        "ctx_arm": _obj([""] * n),
        "ctx_refused": _obj([""] * n),
        "score_ref": np.array([0.9, 0.95, 0.99, 0.99, 0.5, 0.1, 0.5]),
        "gate": _obj([(), (), ("floor",), (), (), (), ()]),
        "warrant": _obj(["", "", "", "", "agree:2", "", ""]),
        "refusal": _obj([""] * n),
        "corro": _obj(["", "", "", "", "photo", "", ""]),
    }
    done = {"gate": np.ones(n, dtype=bool), "promote": np.ones(n, dtype=bool)}
    return Cohort("toy", {}, None, Settings.from_json(SETTINGS), None, {}, None,  # type: ignore[arg-type]
                  keys, [frozenset()] * n, V, P, sig, done, "toy", tmp_path)


def _zones(outcome: board.Outcome) -> list[str]:
    return [board.ZONE_NAMES[z] for z in outcome.decisions.zone]


def test_reference_ladder_settles_each_pair_like_decide_pair(tmp_path: Path) -> None:
    c = _cohort(tmp_path)
    ref = {"ladder": [{"rung": r} for r in board.REFERENCE_LADDER], "group": {"step": "components"}}
    out = board.run(c, ref)
    assert _zones(out) == ["veto", "reject", "band", "merge", "merge", "reject", "band"]
    d = out.decisions
    assert d.reason[0] == "guard:unit_designator"
    assert d.reason[2] == "certificate:K-C:d43_gate:floor"
    assert (d.rung[2], d.name[2], d.carrier[2]) == ("fact", "floor", "ATTR")
    assert d.reason[4] == "d43_promote:agree:2"
    assert (d.rung[4], d.carrier[4]) == ("demonstration", "IMG")
    assert out.groups.clusters == {7: (7, 8), 9: (9, 10)}


def test_a_new_rung_is_one_function_and_one_config_line(tmp_path: Path) -> None:
    c = _cohort(tmp_path)
    ladder = [{"rung": r} for r in board.REFERENCE_LADDER]
    room = {"rung": "column_proof", "name": "room",
            "all": [["tag_room_clip_min2", ">=", 0.97], ["tag_rooms_private", ">=", 2]],
            "veto": [["floorplan_conflict", "==", 1]]}
    arm = {"ladder": ladder + [room], "group": {"step": "components"}}
    assert _zones(board.run(c, arm))[6] == "merge"

    @board.rung("toy_everything_bands")
    def _toy(cohort: Cohort, d: board.Decisions, p: dict[str, Any]) -> board.Decisions:
        d = d.copy()
        d.settle(d.zone == board.MERGE, board.BAND, "toy", "all", "NONE", "toy")
        return d
    try:
        out = board.run(c, {"ladder": ladder + [{"rung": "toy_everything_bands"}],
                            "group": {"step": "components"}})
        assert board.MERGE not in set(out.decisions.zone.tolist())
        assert out.groups.clusters == {}
    finally:
        board.RUNGS.pop("toy_everything_bands")


def test_a_rung_switched_off_is_skipped(tmp_path: Path) -> None:
    c = _cohort(tmp_path)
    ladder = [{"rung": r, **({"on": False} if r == "gate" else {})} for r in board.REFERENCE_LADDER]
    assert _zones(board.run(c, {"ladder": ladder, "group": {"step": "components"}}))[2] == "merge"


def test_the_walls_are_not_a_rung_an_arm_may_remove(tmp_path: Path) -> None:
    c = _cohort(tmp_path)
    ladder = [{"rung": r, **({"on": False} if r == "veto" else {})} for r in board.REFERENCE_LADDER]
    out = board.run(c, {"ladder": ladder, "group": {"step": "components"}})
    assert out.walls_forced and _zones(out)[0] == "veto"
    assert not board.run(c, {"ladder": [{"rung": r} for r in board.REFERENCE_LADDER],
                             "group": {"step": "components"}}).walls_forced


def test_components_never_chain_a_flat_to_a_commercial_unit(tmp_path: Path) -> None:
    from types import SimpleNamespace

    c = _cohort(tmp_path)
    c.keys = [(1, 2), (2, 3), (3, 4)]
    c.ds = SimpleNamespace(listings={  # type: ignore[assignment]
        1: SimpleNamespace(category_type="prodej", category_main="byt"),
        2: SimpleNamespace(category_type="prodej", category_main=None),
        3: SimpleNamespace(category_type="prodej", category_main="komercni"),
        4: SimpleNamespace(category_type="prodej", category_main="dum")})
    d = board.Decisions.blank(3, np.array([0.99, 0.95, 0.9]))
    d.zone[:] = board.MERGE
    assert board.components_group(c, d, {}).clusters == {1: (1, 2), 3: (3, 4)}


def test_sweep_expands_one_arm_per_combination() -> None:
    config = {"name": "t", "ladder": [{"rung": "score", "t_lo": 0.2}],
              "sweep": {"ladder.0.t_lo": [0.1, 0.3], "model": ["ref", "x.json"]}}
    arms = board.expand(config)
    assert len(arms) == 4
    assert {(a["ladder"][0]["t_lo"], a["model"]) for a in arms} == {
        (0.1, "ref"), (0.3, "ref"), (0.1, "x.json"), (0.3, "x.json")}
    assert len({board.config_id(a) for a in arms}) == 4


@pytest.mark.parametrize("t_lo,expected", [(0.05, "band"), (0.2, "reject")])
def test_thresholds_are_config_values(tmp_path: Path, t_lo: float, expected: str) -> None:
    c = _cohort(tmp_path)
    ladder = [{"rung": r} for r in board.REFERENCE_LADDER]
    ladder[3] = {"rung": "score", "t_lo": t_lo}
    assert _zones(board.run(c, {"ladder": ladder, "group": {"step": "components"}}))[5] == expected


def test_group_step_dials_are_config_values_with_their_own_memo(tmp_path: Path) -> None:
    c = _cohort(tmp_path)
    c.relation = {}
    plain = board._RelationMemo(c, {}, None)
    ruled = board._RelationMemo(c, {}, None, '{"d43_cluster_image_facts": false}')
    plain[(1, 2)] = False
    ruled[(1, 2)] = True
    assert plain.get((1, 2)) is False and ruled.get((1, 2)) is True
    blank = board.Decisions.blank(c.n, c.sig["score_ref"])
    with pytest.raises(TypeError):
        board.relation_group(c, blank, {"step": "relation", "settings": {"no_such_dial": 1}})


def test_a_learner_from_anywhere_is_one_config_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                    ) -> None:
    c = _cohort(tmp_path)
    lo = np.array([k[0] for k in c.keys])
    hi = np.array([k[1] for k in c.keys])
    np.savez(tmp_path / "m_toy.npz", lo=lo, hi=hi, p=np.array([0.99, 0.99, 0.0, 0.0, 0.0, 0.99, 0.0]))
    monkeypatch.setenv("LAB_MODELS", str(tmp_path))
    arm = {"model": {"npz": "$LAB_MODELS/m_{cohort}.npz"},
           "ladder": [{"rung": "veto"}, {"rung": "auto_reject"},
                      {"rung": "score", "t_hi_by_stratum": {}, "t_hi": 0.9}, {"rung": "gate"}],
           "group": {"step": "components"}}
    out = board.run(c, arm)
    assert _zones(out) == ["veto", "reject", "reject", "reject", "reject", "merge", "reject"]
    assert out.groups.clusters == {11: (11, 12)}


def _review() -> dict[str, Any]:
    def card(ids: list[int]) -> dict[str, Any]:
        return {"members": [{"id": i, "source": "sreality", "area_m2": 50.0, "price": 1e6,
                             "source_url": f"https://example.test/{i}"} for i in ids],
                "pairs": [{"lo": ids[0], "hi": ids[1], "zone": 4, "rung": "score", "name": "cut",
                           "carrier": "NONE", "ruling": None, "judge": {"vision": "different"}}]}
    return {"experiment": "toy", "base": "ref", "cohort": "trial",
            "groups_gained": [card([1, 2]), card([3, 4, 5])],
            "groups_lost": [card([6, 7]), card([3, 4])]}


def test_one_review_page_per_experiment_with_a_seeded_sample() -> None:
    from autodedup.lab import page

    review = _review()
    assert [c["members"][0]["id"] for c in page.sample(review, 2, 7)] == [
        c["members"][0]["id"] for c in page.sample(review, 2, 7)]
    assert {c["side"] for c in page.sample(review, 0, 1)} == {"gained", "lost"}
    assert [c["members"][0]["id"] for c in page.splits(review)] == [6]
    out = page.render(review, 0, 1)
    assert out.count('class="card"') == 3 and 'data-key="3-4-5"' in out
    assert 'data-key="3-4"' not in out and "1 base groups the arm only absorbed" in out
    assert "One property" in out and "Not one property" in out and "vision different" in out


def test_group_reads_come_back_as_labels(tmp_path: Path) -> None:
    from autodedup.lab import metrics

    path = tmp_path / "reads.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in (
        {"kind": "group_read", "members": [3, 4, 5], "verdict": "same"},
        {"kind": "group_read", "members": [6, 7], "verdict": "different"},
        {"kind": "group_read", "members": [6, 7], "verdict": "same"},
        {"kind": "other", "members": [1, 2], "verdict": "same"})) + "\n")
    assert metrics.read_group_reads([path]) == {frozenset({3, 4, 5}): "same",
                                                frozenset({6, 7}): "same"}
