"""Rule 6.1 (a) as `lab keep` / `lab cut`, the M4 / M5 read draw and M6's AUC, on hand-made
leaderboard rows (GLOBAL_SEARCH 2.2, 6.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from autodedup.lab import metrics, page, rules

VERIFIED = {"v-trial", "v-c17", "v-c18"}
FIXTURES = [("trial", 1, 2, "Trial case"), ("c17", 3, 4, "Na Zertvach"), ("c18", 5, 6, "Decin")]
PRESENT = {"trial": ["1x2"], "c17": ["3x4"], "c18": ["5x6"]}


def _row(cohort: str, m1: int, m2: int = 0, together: list[str] | None = None,
         m4: dict[str, Any] | None = None, cache: str | None = None,
         verified: Any = True, export: str | None = None) -> dict[str, Any]:
    return {"experiment": "x", "cohort": cohort, "stamp": cache or f"v-{cohort}",
            "cache": cache or f"v-{cohort}", "export": export or f"e-{cohort}", "engine": "eng",
            "verified": verified,
            "m": {"M1": {"together": m1, "n": 60}, "M2": {"together": m2, "n": 12},
                  "M3": {"together": together or [], "present": PRESENT.get(cohort, [])},
                  "M7": {"band": 1}, **({"M4": m4} if m4 else {})}}


INCUMBENT = {"trial": _row("trial", 470, 2), "c17": _row("c17", 46), "c18": _row("c18", 46)}


def _keep(arm: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return rules.keep(arm, INCUMBENT, VERIFIED, fixtures=FIXTURES, **kw)


def test_keep_needs_c18_m1_m3_and_m2() -> None:
    arm = {"trial": _row("trial", 480, 2), "c17": _row("c17", 47), "c18": _row("c18", 46)}
    out = _keep(arm)
    assert out["verdict"] == "KEEP" and out["owed"] and not out["missing"]
    lower = {**arm, "c18": _row("c18", 45)}
    assert "c18 M1 45 < incumbent 46" in _keep(lower)["reasons"]
    joined = {**arm, "c17": _row("c17", 47, together=["Na Zertvach (624 x 18356370)"])}
    dropped = _keep(joined)
    assert dropped["verdict"] == "DROP" and dropped["checks"]["M3"]["arm_only"] == [
        "c17: Na Zertvach (624 x 18356370)"]
    assert _keep(joined, accepted=["Na Zertvach"])["verdict"] == "KEEP"
    m2 = {**arm, "trial": _row("trial", 480, 3)}
    assert "trial M2 3 != incumbent 2" in _keep(m2)["reasons"]


def test_keep_on_partial_rows_is_incomplete_never_keep() -> None:
    """The review's repro: one c18 row cannot KEEP; every row the rule reads is named."""
    out = _keep({"c18": _row("c18", 47)})
    assert out["verdict"] == "INCOMPLETE" and not out["reasons"]
    assert "no arm row for c17" in out["missing"] and "no arm row for trial" in out["missing"]
    assert any("M3: no arm row reads 1 fixture(s) listed under trial" in m for m in out["missing"])
    partial = {k: v for k, v in INCUMBENT.items() if k != "trial"}
    arm = {"trial": _row("trial", 480, 2), "c17": _row("c17", 47), "c18": _row("c18", 46)}
    out = rules.keep(arm, partial, VERIFIED, fixtures=FIXTURES)
    assert out["verdict"] == "INCOMPLETE" and "no incumbent row for trial" in out["missing"]


def test_every_listed_fixture_must_be_read() -> None:
    arm = {"trial": _row("trial", 480, 2), "c17": _row("c17", 47), "c18": _row("c18", 46)}
    extended = [*FIXTURES, ("c6", 7, 8, "c6 houses")]
    out = rules.keep(arm, INCUMBENT, VERIFIED, fixtures=extended)
    assert out["verdict"] == "INCOMPLETE" and out["checks"]["M3"]["unread"] == {
        "arm c6": 1, "incumbent c6": 1}
    assert rules.keep(arm, INCUMBENT, VERIFIED, fixtures=extended,
                      accepted=["c6 houses"])["verdict"] == "KEEP"


def test_rows_off_different_caches_are_not_compared() -> None:
    arm = {"trial": _row("trial", 480, 2), "c17": _row("c17", 47),
           "c18": _row("c18", 46, cache="v-other")}
    out = rules.keep(arm, INCUMBENT, VERIFIED | {"v-other"}, fixtures=FIXTURES)
    assert out["verdict"] == "DROP" and not out["checks"]["comparable"]
    assert any("cache v-other vs incumbent v-c18" in r for r in out["reasons"])


def test_an_alias_compares_a_sibling_artefact_of_one_export() -> None:
    """`--alias c18=c18_w31r1`: another settings row's artefact of the SAME export and engine."""
    sibling = _row("c18", 47, cache="v-c18r1", export="e-c18")
    arm = {"trial": _row("trial", 480, 2), "c17": _row("c17", 47), "c18": sibling}
    ok = VERIFIED | {"v-c18r1"}
    assert rules.keep(arm, INCUMBENT, ok, fixtures=FIXTURES, aliased={"c18"})["verdict"] == "KEEP"
    assert rules.keep(arm, INCUMBENT, ok, fixtures=FIXTURES)["verdict"] == "DROP"
    other = {**arm, "c18": _row("c18", 47, cache="v-c18r1", export="e-post")}
    assert rules.keep(other, INCUMBENT, ok, fixtures=FIXTURES,
                      aliased={"c18"})["verdict"] == "DROP"


def test_no_row_from_an_unverified_cache_or_an_external_scorer_counts() -> None:
    arm = {"trial": _row("trial", 480, 2), "c17": _row("c17", 47),
           "c18": _row("c18", 50, cache="stale")}
    out = _keep(arm)
    assert out["verdict"] == "DROP" and not out["checks"]["verified"]
    external = {**arm, "c18": _row("c18", 50, verified="cache verified, scorer external")}
    out = _keep(external)
    assert out["verdict"] == "DROP" and "scorer external" in out["reasons"][0]


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


def test_the_cut_reads_no_row_off_another_cache() -> None:
    sweep = {t: {"c18": _row("c18", 48, cache="v-other" if t == 0.90 else None)}
             for t in rules.CUTS}
    out = rules.cut(sweep, INCUMBENT, VERIFIED | {"v-other"})
    assert out["cut"] == 0.85 and "cache v-other" in out["sweep"]["0.90"]["counts"]


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


def test_a_verify_certifies_the_engine_artefact_and_the_lab_code(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from autodedup.lab import verify

    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "board.py").write_text("RUNG = 1\n")
    (lab / "metrics.py").write_text("M = 1\n")
    monkeypatch.setattr(verify, "LAB_DIR", lab)
    cohort = SimpleNamespace(name="c18", version="v1", code_digest="e1")
    rungs = {"rung_equivalence": {"ok": True, "pairs": 3000}}
    verify.record(tmp_path, cohort, {"rows": {"identical": 1}, "groups": {"harness_run": 1}},
                  True)
    assert verify.verified(tmp_path) == set()
    assert verify.verified(tmp_path, rungs=False) == {verify.stamp("v1")}
    verify.record(tmp_path, cohort, {"rows": {"identical": 1}, "groups": {"harness_run": 1},
                                     **rungs}, True)
    assert verify.verified(tmp_path) == {verify.stamp("v1")}
    old = verify.stamp("v1")
    (lab / "metrics.py").write_text("M = 2\n")
    assert verify.stamp("v1") == old
    (lab / "board.py").write_text("RUNG = 2\n")
    assert verify.stamp("v1") not in verify.verified(tmp_path)
    verify.record(tmp_path, cohort, rungs, False)
    assert verify.verified(tmp_path) == {old}
    verify.record(tmp_path, cohort, rungs, True)
    assert verify.verified(tmp_path) == {old, verify.stamp("v1")}


def test_rule_b_counts_one_reader_per_case_only_the_arm_joins() -> None:
    kept = {"checks": {"M3": {"arm_only": ["trial: Anenske nam. 2+kk (33553 x 519077)",
                                           "c17: Decin gardens (285210 x 18624526)",
                                           "c17: Decin gardens (162157 x 18624521)"]}}}
    assert rules.readers(kept, 0) == {"verdict": "KEEP", "added": 0, "needed": 2, "max": 2,
                                      "cases_needing_a_reader": ["Anenske nam. 2+kk",
                                                                 "Decin gardens"]}
    assert rules.readers(kept, 1)["verdict"] == "DROP"
    assert rules.readers({"checks": {}}, 2)["verdict"] == "KEEP"


def test_stop_rules_combine_a_and_b_as_lab_keep_prints_them() -> None:
    """`lab keep --readers-added`: a DROP by either rule drops the arm; otherwise (a)'s verdict,
    so an INCOMPLETE (a) never becomes a KEEP."""
    table = {(a, b): rules.stop_rules(a, b) for a in ("KEEP", "INCOMPLETE", "DROP")
             for b in ("KEEP", "DROP")}
    assert table == {("KEEP", "KEEP"): "KEEP", ("KEEP", "DROP"): "DROP",
                     ("INCOMPLETE", "KEEP"): "INCOMPLETE", ("INCOMPLETE", "DROP"): "DROP",
                     ("DROP", "KEEP"): "DROP", ("DROP", "DROP"): "DROP"}
    arm = {"trial": _row("trial", 480, 2), "c17": _row("c17", 47), "c18": _row("c18", 46)}
    kept = _keep(arm)
    assert rules.stop_rules(kept["verdict"], rules.readers(kept, 2)["verdict"]) == "KEEP"
    assert rules.stop_rules(kept["verdict"], rules.readers(kept, 3)["verdict"]) == "DROP"
    partial = _keep({"c18": _row("c18", 47)})
    assert rules.readers(partial, 0)["needed"] == 0
    assert rules.stop_rules(partial["verdict"], rules.readers(partial, 0)["verdict"]) == "INCOMPLETE"
    joined = _keep({**arm, "c17": _row("c17", 47, together=["Na Zertvach (624 x 18356370)"])})
    assert rules.stop_rules(joined["verdict"], rules.readers(joined, 0)["verdict"]) == "DROP"
