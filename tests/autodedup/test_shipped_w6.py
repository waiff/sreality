"""What generation g5 ships (D18): w6 settings load, and w6_gold is w5_gold's weights.

g5 is a CLUSTERING-only generation — no W6 refit arm was promoted — so the model file exists
only to give the generation its own `model_version` (the labels lane scopes a generation by
`(model_version, feature_version)` and g5 must not collapse into g4). If a future wave changes
the weights it must change this test too, deliberately."""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.features import FEATURE_ORDER
from autodedup.model import LogisticModel
from autodedup.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
W5 = json.loads((ROOT / "models/w5_gold.json").read_text(encoding="utf-8"))
W6 = json.loads((ROOT / "models/w6_gold.json").read_text(encoding="utf-8"))

LEARNED = ("weights", "intercept", "interactions", "calibration", "calibration_kind",
           "feature_order", "means", "scales", "presence_weights")


def test_w6_settings_load_and_turn_the_bridge_rail_on() -> None:
    settings = Settings.from_json(ROOT / "settings/w6.json")
    assert settings.bridge_apply is True
    assert settings.bridge_min_score == 0.999
    w5 = Settings.from_json(ROOT / "settings/w5.json")
    assert w5.bridge_apply is False
    # g5 is w5's decision surface: every threshold is the promoted generation's.
    assert (settings.t_hi, settings.t_lo, settings.store_floor) == (w5.t_hi, w5.t_lo,
                                                                    w5.store_floor)
    assert settings.t_hi_by_stratum == w5.t_hi_by_stratum


def test_w6_gold_is_w5_gold_under_a_new_version() -> None:
    assert W6["version"] == "w6_gold" and W5["version"] == "w5_gold"
    for key in LEARNED:
        assert W6.get(key) == W5.get(key), key
    assert W6["provenance"]["inherits"]["model"] == "w5_gold"
    assert W6["provenance"]["w6"]["generation"] == "g5"


def test_the_shipped_model_loads_and_scores_the_current_feature_version() -> None:
    """W8 appends `ref_code_shared` (E60), which `w6_gold` was not fitted on: the load warns by
    name and the model scores that slot at zero. The certificate reads the fact directly, so the
    debt costs the SCORE a signal, not the rule floor — the refit is owed, not urgent."""
    import warnings

    with warnings.catch_warnings(record=True) as warned:
        warnings.simplefilter("always")
        model = LogisticModel.from_json(json.loads(
            (ROOT / "models/w6_gold.json").read_text(encoding="utf-8")))
    assert model.version == "w6_gold"
    assert len(model.feature_order) == W6["provenance"]["feature_version"]["n_features"]
    assert [name for name in FEATURE_ORDER if name not in set(model.feature_order)] == [
        "ref_code_shared"
    ]
    assert len(warned) == 1 and "ref_code_shared" in str(warned[0].message)


def test_the_w6_evidence_file_records_a_met_promotion_bar() -> None:
    bar = json.loads(
        (ROOT / "settings/w6_strata.json").read_text(encoding="utf-8"))["promotion_bar"]
    assert all(clause["met"] for clause in bar.values())
    assert bar["band_share_not_larger"]["g5"] <= bar["band_share_not_larger"]["g4"]
    assert (bar["merge_zone_precision_vs_operator"]["g5"]
            >= bar["merge_zone_precision_vs_operator"]["g4"])
