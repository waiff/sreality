"""toolkit.dedup_audit.build_audit_breakdown — the pure decision→rungs mapping that
makes a dedup decision auditable (measured vs threshold, met/unmet, which Settings knob
governs it). No DB; a function of the stored `detail` dict only."""

from __future__ import annotations

from toolkit.dedup_audit import build_audit_breakdown, referenced_settings_keys
from toolkit.dedup_settings import REGISTRY_BY_KEY


def _by_key(detail: dict) -> dict[str, dict]:
    return {r["key"]: r for r in build_audit_breakdown(detail)}


def test_every_referenced_settings_key_exists_in_registry() -> None:
    # A rung deep-links to a Settings knob; the key MUST exist, or the link 404s.
    missing = referenced_settings_keys() - set(REGISTRY_BY_KEY)
    assert not missing, missing


def test_empty_detail_yields_no_rungs() -> None:
    assert build_audit_breakdown(None) == []
    assert build_audit_breakdown({}) == []


def test_phash_met_when_pairs_reach_threshold() -> None:
    r = _by_key({"stage": "phash", "reason": "image_phash",
                 "phash_pairs": 2, "phash_min_pairs": 2, "phash_threshold": 6})["phash"]
    assert r["status"] == "met"
    assert r["value"] == 2 and r["threshold"] == 2
    assert r["settings_keys"] == []  # fixed code constant, no link


def test_phash_unmet_when_below_threshold_and_not_distinctive() -> None:
    r = _by_key({"stage": "visual", "phash_pairs": 1,
                 "phash_min_pairs": 2, "phash_threshold": 6})["phash"]
    assert r["status"] == "unmet"


def test_phash_distinctive_single_match_is_met() -> None:
    r = _by_key({"stage": "phash", "reason": "image_phash", "phash_pairs": 1,
                 "phash_min_pairs": 2, "phash_threshold": 6,
                 "phash_distinctive": True})["phash"]
    assert r["status"] == "met"


def test_visual_high_is_met_and_links_to_model() -> None:
    r = _by_key({"stage": "visual", "reason": "visual_match",
                 "verdict": "High", "room_type": "kitchen"})["verdict"]
    assert r["status"] == "met"
    assert "llm_visual_match_model" in r["settings_keys"]


def test_visual_low_dismiss_links_to_autodismiss_toggle() -> None:
    r = _by_key({"stage": "visual", "reason": "visual_different",
                 "verdict": "Low", "room_type": "bathroom"})["verdict"]
    assert r["status"] == "unmet"
    assert "dedup_forensics_autodismiss_enabled" in r["settings_keys"]


def test_cosine_is_info_and_links_to_both_bands() -> None:
    r = _by_key({"stage": "visual", "cosine": 0.91, "verdict": "High"})["cosine"]
    assert r["status"] == "info"
    assert set(r["settings_keys"]) == {"dedup_cosine_haiku_min", "dedup_cosine_sonnet_min"}


def test_floor_plan_dismiss_shows_both_phash_and_plan_rungs() -> None:
    # The full chain: pHash matched (met) but the floor plan overrode it (unmet → dismiss).
    rungs = _by_key({"stage": "phash", "reason": "floor_plan_different_layout",
                     "phash_pairs": 4, "phash_min_pairs": 2, "phash_threshold": 6})
    assert rungs["phash"]["status"] == "met"
    assert rungs["floor_plan"]["status"] == "unmet"
    assert "llm_floor_plan_match_model" in rungs["floor_plan"]["settings_keys"]


def test_address_rung_is_met_with_no_threshold() -> None:
    r = _by_key({"stage": "address", "reason": "address_exact",
                 "street_key": "id:1", "house_number": "12", "floor": 3})["address"]
    assert r["status"] == "met"
    assert r["settings_keys"] == []
