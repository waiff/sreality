"""What W12 shipped, and — louder — what it did not.

The wave pre-registered E88's two definitions, measured them on a fresh seal's dev + validation
with `w6_gold` unrefitted, and the pre-stated choice rule named NO candidate: both holds cost
more reliable dev duplicates than the honest clock gains. Nothing is promoted; g6 stays the
shipped generation. These assertions are the rail against a later session reading
`settings/w12_candidate.json` as a promotion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup import development as D
from autodedup import seals
from autodedup.evaluate import split_seal
from autodedup.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
W8 = Settings.from_json(ROOT / "settings/w8.json")
W12_SEAL = "ebc141fa51555bf7e2dd1757d84fb912e907c377e927e1b5c6de6132844d1b24"
W11_SEAL = "00e2cb2fe4fe1e8ee9886729ef3420ebaa6aec059934b20e30630751500032b4"

# dev + validation of the W12 seal, `w6_gold` unrefitted, reliable tier = operator + gold minus
# the DISPUTED and CONTESTED pairs. The sealed side was never opened by the build.
HONEST_GAIN_OVER_G6 = 165
NARROW_COST = 212
WIDE_COST = 297
COST_BUDGET = HONEST_GAIN_OVER_G6 / 3.0


def test_the_shipped_generation_is_still_g6_and_the_hold_is_off() -> None:
    assert W8.development_hold_mode == "off"
    assert W8.live_window_from_sighting is False
    assert Settings().development_hold_mode == "off"
    assert not (ROOT / "settings/w12.json").exists(), "W12 promoted nothing"


def test_the_pre_stated_choice_rule_named_no_candidate() -> None:
    """Both pre-registered holds cost more true duplicates than the honest clock buys."""
    assert NARROW_COST > COST_BUDGET and WIDE_COST > COST_BUDGET
    assert NARROW_COST > HONEST_GAIN_OVER_G6 and WIDE_COST > HONEST_GAIN_OVER_G6, (
        "each hold gives back more than the whole gain it was meant to protect"
    )


def test_the_candidate_row_is_the_narrow_arm_and_is_a_record_not_a_promotion() -> None:
    raw = json.loads((ROOT / "settings/w12_candidate.json").read_text(encoding="utf-8"))
    assert raw["development_hold_mode"] == "narrow"
    assert raw["live_window_from_sighting"] is True
    assert raw["certificate_b_min_gap_days"] > 0.0, "E84 is required under the honest clock"
    assert raw.get("certificate_b_min_images", 0.0) == 0.0, "no E65 floor — the hold is the price"
    assert raw.get("family_guard_mode", "off") == "off", "E85 stays off (D30 ii)"
    assert raw.get("catalog_carrier_aware", False) is False, "E83 stays off (D28 ii)"
    assert Settings.from_json(ROOT / "settings/w12_candidate.json").development_hold_mode \
        == "narrow"


def test_every_row_under_settings_is_still_constructible() -> None:
    for path in sorted((ROOT / "settings").glob("*.json")):
        if path.name.endswith("_strata.json"):
            continue
        Settings.from_json(path)


def test_the_pre_registered_vocabulary_and_caps_are_what_was_measured() -> None:
    """A term list or a cap that moved after the measurement makes the measurement a fit."""
    assert D.PROJECT_TERMS == ("rezidenc", "projekt", "developer", "novostavb", "etap",
                               "cenik", "kolaudac", "dokonceni")
    assert D.COOP_TERMS == ("podil", "anuit", "druzstevn")
    defaults = Settings()
    assert defaults.development_vocab_min_share == 0.50
    assert defaults.development_coop_min_size == 3
    assert defaults.development_size_min_narrow == 3
    assert defaults.development_size_min_wide == 4
    assert defaults.development_block_density_min == 20


def test_the_fresh_seal_is_committed_carries_its_seed_and_is_named_by_both() -> None:
    """W12's map reproduces W11's to the listing, so the fresh holdout is a fresh SEED and the
    name has to carry it — otherwise the only place to commit it is on top of a SPENT seal."""
    groups = seals.load(W12_SEAL)
    assert len(groups) == 4456 and len(set(groups.values())) == 664
    assert seals.seed_for(W12_SEAL) == 20260925
    assert seals.seal_id(groups, 20260925) == W12_SEAL
    assert split_seal(groups)["sha256"] == W11_SEAL, "same map, different seed, different seal"
    assert seals.seed_for(W11_SEAL) == 20260923, "the spent seal's own seed is untouched"
    assert seals.spent(W12_SEAL) is None, "the W12 holdout has not been read"


def test_the_seal_that_decided_W11_stays_registered_spent() -> None:
    reason = seals.spent(W11_SEAL)
    assert reason and "FIXPOINT" in reason and W12_SEAL[:8] in reason


def test_the_honest_clock_still_cannot_run_for_free() -> None:
    with pytest.raises(ValueError, match="needs a price"):
        Settings(live_window_from_sighting=True, certificate_b_min_gap_days=1.0 / 1440.0)
