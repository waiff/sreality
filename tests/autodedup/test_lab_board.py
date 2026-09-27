"""The lab's plug board over a hand-made evidence cache: the reference rungs settle each pair the
way `decide_pair` does, a new rung is one registered function, and a sweep is one arm per value."""

from __future__ import annotations

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
