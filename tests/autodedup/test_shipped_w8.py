"""What generation g6 ships (D22): w8 settings, and the negatives E63 may never promote.

The model is unchanged. The W8 clock correction (E62) would have demanded a refit — `w6_gold`
was fitted on the detection clock — so it ships OFF and g6 keeps g5's weights; the refit debt
is E62's, not this generation's.
"""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.decide import context_rule_warrant
from autodedup.features import ABSENT, FEATURE_ORDER
from autodedup.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
W8 = Settings.from_json(ROOT / "settings/w8.json")
W6 = Settings.from_json(ROOT / "settings/w6.json")
NEGATIVES = json.loads(
    (Path(__file__).parent / "data_w8_band_negatives.json").read_text(encoding="utf-8")
)


def test_w8_settings_are_w6_plus_the_three_w8_switches() -> None:
    assert (W8.certificate_kr_enabled, W8.unit_designator_veto, W8.context_rule_enabled) == (
        True, True, True
    )
    # The clock stays on the detection stamp: the truer clock is the looser one for every
    # co-live test it feeds, and flipping it owes a refit (E62).
    assert W8.live_window_from_sighting is False
    # Everything the score reads is g5's, so a zone difference is a rule difference.
    for name in ("t_hi", "t_lo", "store_floor", "bridge_apply", "bridge_min_score"):
        assert getattr(W8, name) == getattr(W6, name), name
    assert W8.t_hi_by_stratum == W6.t_hi_by_stratum
    # D21: the one cell that merges on a cut keeps g5's cut.
    assert W8.t_hi_by_stratum["model|cross"] == 0.9788


def test_e63_ships_with_the_conditions_the_verification_named() -> None:
    assert W8.context_rule_min_score == 0.9999
    assert W8.context_rule_containment_min == 0.90
    assert W8.context_rule_price_ratio_min == 0.995
    assert W8.context_rule_area_max == 0.01          # C1
    assert W8.context_rule_interior_min is None      # C2: no interior arm to need an image floor
    assert W8.context_rule_from_price_veto is True   # C3, the free limb
    assert W8.context_rule_image_population_min == 10
    # C3's block limb withholds 66 labelled DUPLICATES on g5, so it is recorded, not paid.
    assert W8.context_rule_block_min is None


def _feats(row: list) -> dict[str, tuple[float, bool]]:
    _, _, _, _, _, containment, price, area = row
    feats = {name: ABSENT for name in FEATURE_ORDER}
    feats["containment_max"] = (float(containment), True)
    feats["price_last_ratio"] = (float(price), True)
    feats["area_rel_diff"] = (float(area), True)
    return feats


def test_e63_promotes_no_operator_or_gold_band_negative_and_no_disputed_pair() -> None:
    """The whole point of the rule, pinned to the real rows rather than to a remembered claim.

    83 pairs: every g5 band negative the operator (17) or the gold tier (63) names, and all
    three stored pairs touching the disputed operator card 18705144 — which W8 reports both
    ways precisely because no result may depend on how it is ruled."""
    assert len(NEGATIVES["pairs"]) == 83
    promoted = [
        row for row in NEGATIVES["pairs"]
        if context_rule_warrant(_feats(row), float(row[4]), W8) is not None
    ]
    assert promoted == []


def test_the_disputed_card_cannot_reach_the_rule_whichever_way_it_is_ruled() -> None:
    disputed = [row for row in NEGATIVES["pairs"] if row[2] == "disputed"]
    assert len(disputed) == 3
    # Two fail the score clause outright (0.9545 is the isotonic plateau) and one is rejected
    # before any of this; none carries the warrant.
    assert all(context_rule_warrant(_feats(row), float(row[4]), W8) is None for row in disputed)
