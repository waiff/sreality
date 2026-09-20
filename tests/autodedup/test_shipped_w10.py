"""What W10 ships, and what it does NOT (E83).

E83 is DATA and it is OFF: the shipped generation stays g6 = `settings/w8.json` +
`models/w6_gold.json`, and `w10_carrier.json` exists to record that carrier-aware stock is
provably inert there (M86). `w10_honest_carrier.json` is the CANDIDATE — honest clock, E65
floor, carrier-aware E9, every cut the incumbent's — and it is not a promotion: the
multi-carrier cell that decides it holds no reliable label on the dev side of the fresh seal
(M88), which is what the two pair lists are for.
"""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.settings import Settings
from autodedup.stock import CarrierPolicy

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
W8 = Settings.from_json(ROOT / "settings/w8.json")
CARRIER = Settings.from_json(ROOT / "settings/w10_carrier.json")
CANDIDATE = Settings.from_json(ROOT / "settings/w10_honest_carrier.json")


def test_the_shipped_row_is_still_g6_and_carries_no_carrier_rule() -> None:
    assert W8.catalog_carrier_aware is False
    assert W8.live_window_from_sighting is False
    assert Settings().catalog_carrier_aware is False


def test_both_w10_rows_spell_the_same_carrier_definition() -> None:
    """One broker, one source, one block, fully observed, carriers agreeing about the unit."""
    for settings in (CARRIER, CANDIDATE):
        assert settings.catalog_carrier_aware is True
        assert settings.catalog_min_broker_carriers == 2
        assert settings.catalog_min_source_carriers == 2
        assert settings.catalog_min_block_carriers == 2
        assert settings.catalog_carrier_combine == "any"
        assert settings.catalog_carrier_coverage_min == 1.0
        assert settings.catalog_carrier_area_tol == 0.01
    assert CarrierPolicy.of(CARRIER) == CarrierPolicy.of(CANDIDATE)


def test_the_carrier_row_changes_nothing_else_about_g6() -> None:
    """M86: on the detection clock E83 moves not one pair, so it may move no other row either."""
    assert CARRIER.live_window_from_sighting is False
    assert CARRIER.certificate_b_min_images == 0.0
    raw = json.loads((ROOT / "settings/w10_carrier.json").read_text(encoding="utf-8"))
    base = json.loads((ROOT / "settings/w8.json").read_text(encoding="utf-8"))
    added = {key for key in raw if raw[key] != base.get(key)}
    assert added == {"catalog_carrier_aware", "catalog_min_broker_carriers",
                     "catalog_min_source_carriers", "catalog_min_block_carriers",
                     "catalog_carrier_combine", "catalog_carrier_coverage_min",
                     "catalog_carrier_area_tol"}


def test_the_candidate_keeps_the_floor_and_every_cut_of_the_incumbent() -> None:
    assert CANDIDATE.live_window_from_sighting is True
    assert CANDIDATE.certificate_b_min_images == 1.0
    assert CANDIDATE.certificate_b_min_matched_images == 1.0
    assert CANDIDATE.t_hi_by_stratum == W8.t_hi_by_stratum
    for name in ("certificate_kr_enabled", "unit_designator_veto", "context_rule_enabled",
                 "bridge_apply", "bridge_min_score", "store_floor", "t_hi", "t_lo",
                 "developer_signature_guard", "developer_catalog_ratio_min",
                 "developer_colive_guard", "colive_overlap_days"):
        assert getattr(CANDIDATE, name) == getattr(W8, name), name


def test_the_two_label_lists_exist_and_hold_ordered_distinct_pairs() -> None:
    for name, cap in (("w10_multicarrier_dev", 200), ("w10_new_merges_dev", 200)):
        pairs = json.loads((ROOT / f"pairs/{name}.json").read_text(encoding="utf-8"))
        keys = [(int(lo), int(hi)) for lo, hi in pairs]
        assert 0 < len(keys) <= cap
        assert len(set(keys)) == len(keys)
        assert all(lo < hi for lo, hi in keys)
