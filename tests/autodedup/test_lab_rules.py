"""Rule 6.1 (a) as `lab keep` / `lab cut`, the M4 / M5 read draw and M6's AUC, on hand-made
leaderboard rows (GLOBAL_SEARCH 2.2, 6.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from autodedup.lab import metrics, page, rules

VERIFIED = {"v-trial", "v-c17", "v-c18"}


def _row(cohort: str, m1: int, m2: int = 0, together: list[str] | None = None,
         m4: dict[str, Any] | None = None, cache: str | None = None) -> dict[str, Any]:
    return {"experiment": "x", "cohort": cohort, "cache": cache or f"v-{cohort}",
            "m": {"M1": {"together": m1, "n": 60}, "M2": {"together": m2, "n": 12},
                  "M3": {"together": together or []}, "M7": {"band": 1},
                  **({"M4": m4} if m4 else {})}}


INCUMBENT = {"trial": _row("trial", 470, 2), "c17": _row("c17", 46), "c18": _row("c18", 46)}


def test_keep_needs_c18_m1_m3_and_m2() -> None:
    arm = {"trial": _row("trial", 480, 2), "c17": _row("c17", 47), "c18": _row("c18", 46)}
    out = rules.keep(arm, INCUMBENT, VERIFIED)
    assert out["verdict"] == "KEEP" and out["owed"]
    lower = {**arm, "c18": _row("c18", 45)}
    assert "c18 M1 45 < incumbent 46" in rules.keep(lower, INCUMBENT, VERIFIED)["reasons"]
    joined = {**arm, "c17": _row("c17", 47, together=["Na Zertvach (624 x 18356370)"])}
    assert rules.keep(joined, INCUMBENT, VERIFIED)["verdict"] == "DROP"
    assert rules.keep(joined, INCUMBENT, VERIFIED, accepted=["Na Zertvach"])["verdict"] == "KEEP"
    m2 = {**arm, "trial": _row("trial", 480, 3)}
    assert "trial M2 3 != incumbent 2" in rules.keep(m2, INCUMBENT, VERIFIED)["reasons"]


def test_no_row_from_an_unverified_cache_counts() -> None:
    arm = {"c18": _row("c18", 50, cache="stale")}
    out = rules.keep(arm, INCUMBENT, VERIFIED)
    assert out["verdict"] == "DROP" and not out["checks"]["verified"]


def test_the_cut_is_the_highest_t_that_holds_c18_m1_and_never_reads_the_trial() -> None:
    sweep = {t: {"trial": _row("trial", 999), "c18": _row("c18", m1),
                 "c17": _row("c17", 47, m4={"sampled": 40, "read": 40, "fused": 0})}
             for t, m1 in zip(rules.CUTS, (48, 47, 46, 44, 40))}
    out = rules.cut(sweep, INCUMBENT, VERIFIED)
    assert out["cut"] == 0.80 and out["check"].startswith("holds")
    sweep[0.80]["c17"] = _row("c17", 47, m4={"sampled": 40, "read": 40, "fused": 2})
    assert rules.cut(sweep, INCUMBENT, VERIFIED)["check"].startswith("FAILS")
    sweep[0.80]["c17"] = _row("c17", 47, m4={"sampled": 40, "read": 7, "fused": 0})
    assert "read owed" in rules.cut(sweep, INCUMBENT, VERIFIED)["check"]
    low = {t: {"c18": _row("c18", 40)} for t in rules.CUTS}
    assert rules.cut(low, INCUMBENT, VERIFIED)["cut"] is None


def test_m4_m5_draw_is_seeded_and_order_free() -> None:
    groups = [frozenset({i, i + 1}) for i in range(0, 200, 2)]
    one = page.draw(groups, 40, page.M45_SEED)
    assert len(one) == 40 and one == page.draw(list(reversed(groups)), 40, page.M45_SEED)
    assert set(page.draw(groups[:3], 40, page.M45_SEED)) == set(groups[:3])
    reads = {one[0]: "different", one[1]: "same"}
    out = metrics._reads(one, reads)
    assert (out["read"], out["fused"], out["one_property"]) == (2, 1, 1)
    none = metrics._reads(one, {g: "same" for g in one})
    assert none["fused"] == 0 and abs(none["fused_upper"] - 0.0876) < 1e-3


def test_m6_is_the_rank_auc() -> None:
    score = np.array([0.1, 0.4, 0.35, 0.8])
    positive = np.array([False, False, True, True])
    assert metrics.auc(score, positive) == 0.75
    assert metrics.auc(np.array([0.5, 0.5]), np.array([True, False])) == 0.5
    assert metrics.auc(score, np.ones(4, dtype=bool)) is None


def test_the_board_is_one_table_with_m1_to_m9(tmp_path: Path) -> None:
    entry = {**_row("c18", 46), "config_id": "0", "zones": {"merge": 1, "band": 2},
             "rulings": {"same_together": 46, "same_n": 54, "diff_together": 0, "diff_n": 12},
             "judges": {}, "timings": {}, "verified": True,
             "fixtures": {"apart": 10, "n": 12, "cases_apart": 6, "cases": 7}}
    entry["m"].update({"M4": {"sampled": 40, "read": 40, "fused": 0, "one_property": 40,
                              "fused_upper": 0.0876},
                       "M7": {"band": 2, "groups": 1, "largest": 3},
                       "M8": {"lost_same": {"merge edge, group refused": 3}, "why": None},
                       "M9": {"decide_s": 0.5, "group_s": 1.0, "harness_run_s": 1295.5}})
    board = tmp_path / "board.jsonl"
    board.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    table = metrics.render(board)
    head, rule, line = table.splitlines()
    for m in ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"):
        assert m in head
    assert "| 46/60 |" in line and "| 10/12 (6/7 cases) |" in line and "≤8.8%" in line
    assert "(1295.5)" in line and "| yes |" in line
