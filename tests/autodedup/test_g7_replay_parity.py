"""g7 must stay byte-replayable through the shipping package (W14's whole contract with W13).

The pin is the incumbent's own numbers — merge 5,158 / band 4,524 / veto 45, 980 clusters over
2,691 listings, with byte-identical membership — reached by re-deciding g7's stored pairs under
`settings/w13.json` and re-clustering them. A D43 dial that leaked into a default would move
one of these.

The offline data pack (`w14/data`) is an artifact, not a repository file, so this SKIPS where
it is absent — CI pins the same contract as a settings fact in `test_d43_engine.py`, which has
no such dependency.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from autodedup.cluster import cluster_pairs
from autodedup.d43 import relation_for
from autodedup.dataset import load as load_cohort
from autodedup.decide import _decide_layers, apply_context_rule, apply_d43_rule
from autodedup.fingerprint import build_all
from autodedup.harness import load_model
from autodedup.hazard_context import PairContext
from autodedup.indistinguishable import FEATURE_SLOTS
from autodedup.settings import Settings

PACK = Path("/home/hejtm/autodedup-artifacts/w14/data")
PACKAGE = Path(__file__).resolve().parents[2] / "autodedup"

pytestmark = pytest.mark.skipif(
    not (PACK / "g7/run.json").is_file(),
    reason="the W14 offline data pack is not on this machine",
)

# g7's own run.json, restated so the test fails loudly rather than comparing a file with itself.
G7_MERGE, G7_BAND, G7_VETO = 5158, 4524, 45
G7_CLUSTERS, G7_LISTINGS = 980, 2691


@pytest.fixture(scope="module")
def replay() -> dict[str, Any]:
    settings = Settings.from_json(PACKAGE / "settings/w13.json")
    model = load_model(str(PACKAGE / "models/w6_gold.json"))
    dataset = load_cohort(PACK / "cohort.jsonl.gz")
    fps = build_all(dataset, settings)
    decisions = []
    slots: dict[tuple[int, int], dict[str, tuple[float, bool]]] = {}
    with gzip.open(PACK / "g7/pairs.jsonl.gz", "rt") as handle:
        for line in handle:
            raw = json.loads(line)
            lo, hi = int(raw["lo"]), int(raw["hi"])
            feats = {key: (float(value[0]), bool(value[1]))
                     for key, value in raw["feats"].items()}
            slots[(lo, hi)] = {name: feats[name] for name in FEATURE_SLOTS if name in feats}
            la, lb = dataset.listings[lo], dataset.listings[hi]
            decision = _decide_layers(fps[lo], fps[hi], la, lb, feats,
                                      tuple(raw.get("probes") or ()), model, settings)
            decision = apply_context_rule(decision, feats, la, lb, settings,
                                          PairContext.from_json(raw.get("context")))
            decisions.append(apply_d43_rule(decision, la, lb, feats, settings))
    vetoed = frozenset((d.lo, d.hi) for d in decisions
                       if d.reason.startswith("guard:unit_designator"))
    clusters = cluster_pairs(decisions, dataset.listings, fps, settings, vetoed,
                             relation_for(settings, dataset.listings, slots))
    return {"decisions": decisions, "clusters": clusters}


def test_g7_zones_replay_to_the_pair(replay: dict[str, Any]) -> None:
    zones = {"merge": 0, "band": 0, "reject": 0, "veto": 0}
    for decision in replay["decisions"]:
        zones[decision.zone] += 1
    assert (zones["merge"], zones["band"], zones["veto"]) == (G7_MERGE, G7_BAND, G7_VETO)


def test_g7_clusters_replay_with_byte_identical_membership(replay: dict[str, Any]) -> None:
    stats = replay["clusters"].stats
    assert stats["n_clusters"] == G7_CLUSTERS
    assert stats["n_clustered_listings"] == G7_LISTINGS
    assert stats["n_must_not_link"] == G7_VETO
    shipped = json.loads((PACK / "g7/clusters.json").read_text())["clusters"]
    assert {tuple(sorted(members)) for members in shipped.values()} == {
        tuple(sorted(members)) for members in replay["clusters"].clusters.values()
    }
