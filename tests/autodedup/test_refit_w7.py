"""Refit W7: the operator tier and the j2-w30c verdicts, measured and REFUSED by the pre-registered bars.

The two candidates (w7a = w6_gold's recipe + the operator tier incl. the Browse merges; w7b = w7a
+ the j2-w30c judge tiers) lose to w6_gold on the pre-registered common sealed population: AUC
0.9613 / 0.9583 against 0.9766 and F1 0.9430 / 0.9463 against 0.9493 (ECE passes for both). So
nothing ships: no `models/w7_gold.json`, no settings row, `w6_gold` byte-identical. What stays is
the seal the read spent, the pre-registration it was read under, and the substrate tool.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autodedup import seals
from autodedup.evaluate import split_seal

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
DOCS = Path(__file__).resolve().parents[2] / "docs" / "design" / "autodedup"
W7_SEAL = "eecc33b95ad50fd3291ba19172223d7c645abf8f7bbf350ed0bf1a88c49ab139"
W7_MAP_ONLY = "f3b63906ceb08972542e23f4d6611c2f407180cc8682872390b4662d04d69c5a"
W6_GOLD_SHA256 = "555893aef142456189528e9589a1e1fbb65fc163f325abfb140c5216f0721d72"


def test_the_refit_seal_is_committed_named_by_map_and_seed_and_spent() -> None:
    groups = seals.load(W7_SEAL)
    assert seals.seed_for(W7_SEAL) == 20261002
    assert seals.seal_id(groups, 20261002) == W7_SEAL
    assert split_seal(groups)["sha256"] == W7_MAP_ONLY, "the fit reports name the map alone"
    assert (len(groups), len(set(groups.values()))) == (4488, 1831)
    assert seals.spent(W7_SEAL) and "nothing shipped" in seals.spent(W7_SEAL)


def test_nothing_shipped_and_the_incumbent_is_byte_identical() -> None:
    assert not (ROOT / "models/w7_gold.json").exists()
    assert not (ROOT / "settings/w30m.json").exists()
    digest = hashlib.sha256((ROOT / "models/w6_gold.json").read_bytes()).hexdigest()
    assert digest == W6_GOLD_SHA256


def test_the_pre_registration_names_the_bars_the_read_applied() -> None:
    prereg = json.loads((DOCS / "refit_w7_preregistration.json").read_text(encoding="utf-8"))
    bars = prereg["model_level_bars"]
    assert "<= 5.00%" in bars["B1_ece"] and "w6_gold" in bars["B2_auc"] and "0.5" in bars["B3_f1"]
    assert [item["id"] for item in prereg["amendments"]] == ["A1"]
    results = json.loads((DOCS / "refit_w7_results.json").read_text(encoding="utf-8"))
    assert results["decision"]["ships"] is None
    common = results["model_level"]["common_sealed_population"]
    for name in ("w7a", "w7b"):
        assert common[name]["auc"] < common["w6_gold"]["auc"]
        assert common[name]["f1"] < common["w6_gold"]["f1"]
        assert common[name]["ece"] <= 0.05
