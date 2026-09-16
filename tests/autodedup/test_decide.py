"""The rule floor and the three zones (PROGRAM.md §6): guards, auto-rejects, certificates, E11."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from autodedup.dataset import Listing, Location
from autodedup.decide import (
    CERTIFICATES,
    Decision,
    certificate_a,
    certificate_b,
    certificate_c,
    certificate_of,
    decide_pair,
    disjoint_windows,
    present_value,
)
from autodedup.features import ABSENT, FEATURE_ORDER
from autodedup.fingerprint import build_fingerprint
from autodedup.settings import Settings

SETTINGS = Settings()


@dataclass(slots=True)
class FixedModel:
    """A model double: `decide_pair` must not care how the probability was produced."""

    probability: float

    def predict_proba(self, feats: Any) -> float:
        return self.probability


def _listing(listing_id: int, **over: Any) -> Listing:
    row = Listing(
        id=listing_id,
        block="b",
        source="sreality",
        category_main="byt",
        category_type="prodej",
        disposition="3+kk",
        area_m2=78.0,
        floor=3,
        total_floors=6,
        price=6_000_000.0,
        description="x" * 300,
        first_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_at="2025-06-01T00:00:00+00:00",
        location=Location(obec_kod=1, granularity_rank=60),
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


def _fp(listing: Listing) -> Any:
    return build_fingerprint(listing, [], SETTINGS)


def _feats(**over: float) -> dict[str, tuple[float, bool]]:
    feats = {name: ABSENT for name in FEATURE_ORDER}
    for name, value in over.items():
        feats[name] = (float(value), True)
    return feats


def _decide(la: Listing, lb: Listing, feats: dict[str, tuple[float, bool]],
            probability: float = 0.5) -> Decision:
    return decide_pair(_fp(la), _fp(lb), la, lb, feats, {"attr_dispo"},
                       FixedModel(probability), SETTINGS)


def test_present_value_reads_absence_as_unknown() -> None:
    feats = _feats(area_rel_diff=0.4)
    assert present_value(feats, "area_rel_diff") == 0.4
    assert present_value(feats, "dispo_equal") is None


@pytest.mark.parametrize(
    "over, expected",
    [
        ({"category_type": "pronajem"}, "category_type"),
        ({"category_main": "komercni"}, "category_main"),
        ({"area_m2": 95.0}, "area"),
        ({"disposition": "2+kk"}, "disposition"),
        ({"floor": 6}, "floor"),
    ],
)
def test_guards_veto_before_anything_is_scored(over: dict[str, Any], expected: str) -> None:
    decision = _decide(_listing(1), _listing(2, **over), _feats(), probability=0.999)
    assert decision.zone == "veto"
    assert decision.veto == expected
    assert decision.reason == f"guard:{expected}"
    assert decision.score == 0.0
    assert decision.certificate is None


def test_veto_normalises_pair_order() -> None:
    decision = _decide(_listing(9), _listing(4, category_type="pronajem"), _feats())
    assert (decision.lo, decision.hi) == (4, 9)


def test_missing_data_is_never_a_mismatch() -> None:
    decision = _decide(
        _listing(1), _listing(2, disposition=None, area_m2=None, floor=None),
        _feats(dispo_equal=1.0, same_ruian_adm_kod=1.0), probability=0.5,
    )
    assert decision.zone != "veto"


def test_attr_contradictions_auto_reject() -> None:
    feats = _feats(attr_contradictions=3.0, same_ruian_adm_kod=1.0, dispo_equal=1.0,
                   phash_match_ratio=1.0)
    decision = _decide(_listing(1), _listing(2), feats, probability=0.999)
    assert decision.zone == "reject"
    assert decision.reason == "auto_reject:attr_contradictions"
    assert decision.score == pytest.approx(0.999)


def test_numeral_conflict_auto_rejects_even_a_certificate_pair() -> None:
    feats = _feats(numeral_conflict=1.0, same_ruian_adm_kod=1.0, dispo_equal=1.0,
                   area_rel_diff=0.0, floor_diff=0.0)
    decision = _decide(_listing(1), _listing(2), feats, probability=0.999)
    assert decision.zone == "reject"
    assert decision.reason == "auto_reject:numeral_conflict"
    assert decision.certificate is None


def test_certificate_a_needs_the_address_point_and_agreeing_attributes() -> None:
    base = dict(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.015, floor_diff=0.0)
    assert certificate_a(_feats(**base))
    assert not certificate_a(_feats(**{**base, "area_rel_diff": 0.025}))
    assert not certificate_a(_feats(**{**base, "dispo_equal": 0.0}))
    assert not certificate_a(_feats(**{**base, "floor_diff": 1.0}))
    # An unknown floor is unknown, not a mismatch (E12).
    unknown_floor = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0)
    assert certificate_a(unknown_floor)
    # An unknown area cannot assert the 2% clause.
    assert not certificate_a(_feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, floor_diff=0.0))


def test_certificate_b_requires_disjoint_active_windows() -> None:
    feats = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                   area_rel_diff=0.005)
    early = _listing(1, first_seen_at="2024-01-01T00:00:00+00:00",
                     last_seen_at="2024-05-01T00:00:00+00:00",
                     inactive_at="2024-05-01T00:00:00+00:00", is_active=False)
    late = _listing(2, first_seen_at="2025-01-01T00:00:00+00:00",
                    last_seen_at="2025-06-01T00:00:00+00:00")
    assert disjoint_windows(early, late)
    assert certificate_b(feats, early, late)

    overlapping = _listing(2, first_seen_at="2024-02-01T00:00:00+00:00",
                           last_seen_at="2024-09-01T00:00:00+00:00")
    assert not disjoint_windows(early, overlapping)
    assert not certificate_b(feats, early, overlapping)

    unknown = _listing(2, first_seen_at=None)
    assert not disjoint_windows(early, unknown)


def test_certificate_c_needs_four_ordered_non_catalog_matches() -> None:
    base = dict(phash_tight_matches=4.0, seq_monotone_ratio=0.9, area_rel_diff=0.02,
                dispo_equal=1.0, catalog_ratio_max=0.1)
    assert certificate_c(_feats(**base))
    assert not certificate_c(_feats(**{**base, "phash_tight_matches": 3.0}))
    assert not certificate_c(_feats(**{**base, "seq_monotone_ratio": 0.5}))
    assert not certificate_c(_feats(**{**base, "catalog_ratio_max": 0.6}))
    assert not certificate_c(_feats(**{**base, "dispo_equal": 0.0}))


def test_certificate_order_is_a_then_b_then_c() -> None:
    early = _listing(1, inactive_at="2024-05-01T00:00:00+00:00",
                     last_seen_at="2024-05-01T00:00:00+00:00")
    late = _listing(2, first_seen_at="2025-01-01T00:00:00+00:00")
    every = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0, floor_diff=0.0,
                   same_source=1.0, same_broker_key=1.0, containment_max=1.0,
                   phash_tight_matches=6.0, seq_monotone_ratio=1.0, catalog_ratio_max=0.0)
    assert certificate_of(every, early, late) == "K-A"
    assert certificate_of(_feats(), early, late) is None
    assert set(CERTIFICATES) == {"K-A", "K-B", "K-C"}


def test_a_certificate_short_circuits_to_merge_under_a_low_score() -> None:
    feats = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0, floor_diff=0.0)
    decision = _decide(_listing(1), _listing(2), feats, probability=0.10)
    assert decision.zone == "merge"
    assert decision.certificate == "K-A"
    assert decision.families >= {"LOC", "ATTR"}


def test_a_single_evidence_family_can_never_merge() -> None:
    images_only = _feats(phash_match_ratio=1.0, phash_tight_matches=8.0,
                         interior_match_ratio=1.0, clip_max_cos=0.99)
    decision = _decide(_listing(1), _listing(2), images_only, probability=0.999)
    assert decision.families == {"IMG"}
    assert decision.zone == "band"
    assert decision.reason == "evidence_gate"


def test_every_certificate_carries_two_evidence_families_on_its_own() -> None:
    """E11 is defence in depth, not a brake on the certificates: each one clears the gate."""
    early = _listing(1, inactive_at="2024-05-01T00:00:00+00:00",
                     last_seen_at="2024-05-01T00:00:00+00:00")
    late = _listing(2, first_seen_at="2025-01-01T00:00:00+00:00")
    minimal = {
        "K-A": _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0,
                      floor_diff=0.0),
        "K-B": _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                      area_rel_diff=0.0),
        "K-C": _feats(phash_tight_matches=4.0, seq_monotone_ratio=1.0, area_rel_diff=0.0,
                      dispo_equal=1.0, catalog_ratio_max=0.0),
    }
    for name, feats in minimal.items():
        assert certificate_of(feats, early, late) == name
        decision = decide_pair(_fp(early), _fp(late), early, late, feats, set(),
                               FixedModel(0.05), SETTINGS)
        assert len(decision.families) >= 2
        assert decision.zone == "merge"
        assert decision.certificate == name


def test_zones_follow_t_hi_and_t_lo() -> None:
    feats = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_exact=1.0, area_rel_diff=0.5)
    merge = _decide(_listing(1), _listing(2), feats, probability=SETTINGS.t_hi)
    band = _decide(_listing(1), _listing(2), feats, probability=0.5)
    edge = _decide(_listing(1), _listing(2), feats, probability=SETTINGS.t_lo)
    reject = _decide(_listing(1), _listing(2), feats, probability=0.01)
    assert (merge.zone, merge.reason) == ("merge", "model")
    assert (band.zone, band.reason) == ("band", "model")
    assert edge.zone == "reject"
    assert reject.zone == "reject"


def test_decision_serialises_for_the_pairs_file() -> None:
    feats = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0, floor_diff=0.0)
    payload = _decide(_listing(4), _listing(7), feats, probability=0.9).to_json()
    assert payload["lo"] == 4 and payload["hi"] == 7
    assert payload["zone"] == "merge"
    assert payload["certificate"] == "K-A"
    assert payload["families"] == sorted(payload["families"])
