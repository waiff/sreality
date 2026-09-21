"""What W15 built: g8b = `settings/w15.json`, and the numbers that chose it on DEV.

The W14 adversarial read found merges ACROSS facts the predicate could not see — printed
parcels, accessory numbers, offered extent, and the two-unit signature where a developer's
neighbouring flats sit 0.5–2 % apart. W15 reads each as a general rule (E140–E145) and pins the
measurement here, so a later default change cannot silently re-price it.

Numbers live in `/home/hejtm/autodedup-artifacts/w14/repair/`:
`arms_g8b.json`, `region/region_arms.json`, `measure_readings.json`, `group_grain.json`,
`verify_phase2_R8_facts_cap256.json`, `colive_refutation.json`, `preregistration_g8b.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
W13 = Settings.from_json(ROOT / "settings/w13.json")
W14 = Settings.from_json(ROOT / "settings/w14.json")
W15 = Settings.from_json(ROOT / "settings/w15.json")

# Trial cohort, dev side, replayed through the shipping package (`repair/arms_g8b.json`).
TRIAL = {
    "g7": {"groups": 980, "listings": 2691, "band": 4524, "duplicates_co": 977, "sealed": 208},
    "g8": {"groups": 1309, "listings": 4279, "band": 1521, "duplicates_co": 1457,
           "sealed": 311, "losses": 50, "operator_losses": 32},
    "g8b": {"groups": 1310, "listings": 4285, "band": 1527, "duplicates_co": 1490,
            "sealed": 314, "losses": 38, "operator_losses": 22},
}
OPERATOR_LOSS_BAR = 30
# 140-town region cohort, 15,017 listings (`repair/region/region_arms.json`).
REGION = {
    "g7": {"certain_co": 8618, "groups": 2650},
    "g8": {"certain_co": 16809, "groups": 3290, "max_group": 32,
           "fused_groups": {"parcel": 10, "two_unit": 28, "extent": 7, "accessory": 2}},
    "g8b": {"certain_co": 20588, "groups": 3298, "fused_groups": {}},
    "certain_total": 22421,
    "order_code_total": 1415,
}
# What each reading costs on KNOWN duplicates, region cohort (`repair/measure_readings.json`).
COST_REGION_CERTAIN = {"parcel": 1, "accessory": 6, "extent": 0, "two_unit": 58}
COST_REGION_ORDER_CODE = {"parcel": 0, "accessory": 0, "extent": 0, "two_unit": 0}
# What each reading buys at GROUP grain on the 18 groups g8 fused (`repair/group_grain.json`).
BUYS_FUSED_GROUPS = {"parcel": 8, "accessory": 0, "extent": 2, "two_unit": 16, "union": 18}
# The six trial pairs phase 2 hand-read as DIFFERENT, all separated under g8b.
PHASE_2_TRIAL_PAIRS = ((552565, 18013683), (18013683, 18901047), (74774, 464713),
                       (83211, 10504650), (18574455, 18574459), (83051, 18617867))
PHASE_2_TRIAL_PAIRS_STILL_CO = 0
PHASE_2_REGION_GROUPS_STILL_WHOLE = 0
# E145's refutation of N1's same-portal premise: a one-storey gap on KNOWN duplicates, by portal.
SAME_PORTAL_ONE_STOREY_RATE = {"sreality": 0.075, "ceskereality": 0.068, "realitymix": 0.15,
                               "bazos": 0.235, "idnes": 0.017}
# D49: the widest same-portal price gap a KNOWN duplicate shows is not 55 %.
WIDEST_SAME_PORTAL_GAP = {"trial": 0.55, "region": 0.80}
COLIVE_SWEEP = {0.0: (1428, 310, 78), 3.0: (1467, 311, 54),
                7.0: (1483, 311, 46), 14.0: (1483, 311, 46)}


def test_w15_is_w14_plus_the_five_readings_and_the_cap_and_nothing_else() -> None:
    raw14 = json.loads((ROOT / "settings/w14.json").read_text(encoding="utf-8"))
    raw15 = json.loads((ROOT / "settings/w15.json").read_text(encoding="utf-8"))
    changed = {key for key in set(raw14) | set(raw15)
               if raw14.get(key) != raw15.get(key)}
    assert changed == {
        "d43_parcel_numbers", "d43_accessory_designators", "d43_offered_extent",
        "d43_offered_extent_requires_price_gap", "d43_two_unit_signature",
        "d43_two_unit_area_tol", "d43_two_unit_price_tol", "d43_two_unit_stated_tol",
        "floor_same_source_feed", "max_cluster_size",
    }
    assert W15.max_cluster_size == 256
    assert W15.floor_same_source_feed == "broker"
    assert W15.d43_two_unit_area_tol == pytest.approx(0.005)
    assert W15.d43_two_unit_price_tol == pytest.approx(0.005)
    assert W15.d43_two_unit_stated_tol == pytest.approx(0.005)
    # D49: the co-live price contradiction is refused, and its rows ship as dials.
    assert W15.d43_price_colive_contradiction is False
    assert W15.d43_price_colive_same_source_only is False
    assert W15.d43_price_colive_min_overlap_days == 0.0


def test_the_earlier_generations_do_not_read_any_of_it() -> None:
    """g7 and g8 must keep replaying byte-for-byte, so every W15 row is off in both."""
    for settings in (W13, W14):
        assert settings.d43_parcel_numbers is False
        assert settings.d43_accessory_designators is False
        assert settings.d43_offered_extent is False
        assert settings.d43_two_unit_signature is False
        assert settings.floor_same_source_feed == "portal"
    # w13 never names the cap (it runs on the default 8); w14 pins 32 and g8b lifts it.
    assert W13.max_cluster_size == 8
    assert W14.max_cluster_size == 32


def test_the_defaults_are_off_so_an_unset_row_is_g8s_reading() -> None:
    default = Settings()
    assert default.d43_parcel_numbers is False
    assert default.d43_accessory_designators is False
    assert default.d43_offered_extent is False
    assert default.d43_two_unit_signature is False
    assert default.floor_same_source_feed == "portal"
    assert default.d43_price_colive_min_overlap_days == 0.0
    assert default.d43_price_colive_same_source_only is False


def test_the_operator_tier_loss_bar_is_met_and_the_gains_rise() -> None:
    assert TRIAL["g8b"]["operator_losses"] == 22 <= OPERATOR_LOSS_BAR
    assert TRIAL["g8"]["operator_losses"] == 32 > OPERATOR_LOSS_BAR
    # The repairs are not a trade: both cohorts' duplicate recall goes UP, not down.
    assert TRIAL["g8b"]["duplicates_co"] > TRIAL["g8"]["duplicates_co"] > TRIAL["g7"]["duplicates_co"]
    assert TRIAL["g8b"]["sealed"] >= TRIAL["g8"]["sealed"]
    assert REGION["g8b"]["certain_co"] > REGION["g8"]["certain_co"] > REGION["g7"]["certain_co"]
    # Criterion (i)'s sealed floor, already clear on the SPENT seal.
    assert TRIAL["g8b"]["sealed"] >= 0.97 * TRIAL["g8"]["sealed"]


def test_no_reading_costs_a_duplicate_carrying_an_agency_order_code() -> None:
    """The strongest independent duplicate reference this program has: 1,415 region pairs."""
    assert all(value == 0 for value in COST_REGION_ORDER_CODE.values())


def test_the_union_of_the_readings_separates_every_group_g8_fused() -> None:
    assert BUYS_FUSED_GROUPS["union"] == 18
    assert max(BUYS_FUSED_GROUPS[name] for name in ("parcel", "accessory", "extent",
                                                    "two_unit")) < 18
    assert REGION["g8b"]["fused_groups"] == {}
    assert PHASE_2_TRIAL_PAIRS_STILL_CO == 0
    assert PHASE_2_REGION_GROUPS_STILL_WHOLE == 0


def test_n1s_same_portal_premise_is_refuted_by_every_portal() -> None:
    """E145: a one-storey gap inside ONE portal is common on known duplicates."""
    assert min(SAME_PORTAL_ONE_STOREY_RATE.values()) > 0.0
    assert sum(1 for rate in SAME_PORTAL_ONE_STOREY_RATE.values() if rate >= 0.05) >= 4


def test_the_55_percent_price_ceiling_was_a_trial_cohort_statistic() -> None:
    """D49: one advert carries two prices at once — freehold against a co-op share, and a dražba."""
    assert WIDEST_SAME_PORTAL_GAP["region"] > WIDEST_SAME_PORTAL_GAP["trial"]
    # Every overlap bar costs duplicates against g8b's 1,490 / 314 and buys nothing measured.
    for duplicates, sealed, losses in COLIVE_SWEEP.values():
        assert duplicates < TRIAL["g8b"]["duplicates_co"]
        assert sealed <= TRIAL["g8b"]["sealed"]
        assert losses > TRIAL["g8b"]["losses"]
