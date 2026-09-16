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
    developer_colive,
    developer_signature,
    disjoint_windows,
    present_value,
    stratum_key,
    stratum_t_hi,
    unit_evidence,
)
from autodedup.features import ABSENT, FEATURE_ORDER
from autodedup.fingerprint import build_fingerprint
from autodedup.settings import Settings

SETTINGS = Settings()
KA_SETTINGS = Settings(certificate_ka_enabled=True)

# The cheapest unit-specific corroboration E45 accepts, added to any pair that is testing
# something else and still has to reach the merge zone.
UNIT_EVIDENCE: dict[str, float] = {"rare_token_overlap": 2.0}


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
            probability: float = 0.5, settings: Settings = SETTINGS) -> Decision:
    return decide_pair(_fp(la), _fp(lb), la, lb, feats, {"attr_dispo"},
                       FixedModel(probability), settings)


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
    assert certificate_of(every, early, late, KA_SETTINGS) == "K-A"
    assert certificate_of(_feats(), early, late, KA_SETTINGS) is None
    assert set(CERTIFICATES) == {"K-A", "K-B", "K-C"}


def test_k_a_is_demoted_by_default_and_switchable_for_the_evaluation() -> None:
    """W4c: K-A merged at 52.6% HT precision on gold, so by default it certifies nothing —
    `certificate_a` still recognises the shape and `certificate_ka_enabled` restores it."""
    early = _listing(1, inactive_at="2024-05-01T00:00:00+00:00",
                     last_seen_at="2024-05-01T00:00:00+00:00")
    late = _listing(2, first_seen_at="2025-01-01T00:00:00+00:00")
    feats = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0, floor_diff=0.0,
                   **UNIT_EVIDENCE)
    assert certificate_a(feats)
    assert certificate_of(feats, early, late, SETTINGS) is None
    assert certificate_of(feats, early, late) is None  # an omitted row leaves K-A OFF
    assert certificate_of(feats, early, late, KA_SETTINGS) == "K-A"

    demoted = _decide(early, late, feats, probability=0.10)
    assert (demoted.zone, demoted.certificate) == ("reject", None)
    restored = _decide(early, late, feats, probability=0.10, settings=KA_SETTINGS)
    assert (restored.zone, restored.certificate) == ("merge", "K-A")


def test_a_certificate_short_circuits_to_merge_under_a_low_score() -> None:
    feats = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0, floor_diff=0.0,
                   **UNIT_EVIDENCE)
    decision = _decide(_listing(1), _listing(2), feats, probability=0.10,
                       settings=KA_SETTINGS)
    assert decision.zone == "merge"
    assert decision.certificate == "K-A"
    assert decision.families >= {"LOC", "ATTR"}


def test_a_single_evidence_family_can_never_merge() -> None:
    """E11 refuses images-only even when the images ARE unit-grade (E45 satisfied)."""
    images_only = _feats(phash_match_ratio=1.0, phash_tight_matches=8.0,
                         interior_match_ratio=1.0, n_images_min=6.0, clip_max_cos=0.99)
    decision = _decide(_listing(1), _listing(2), images_only, probability=0.999)
    assert decision.families == {"IMG"}
    assert decision.zone == "band"
    assert decision.reason == "evidence_gate"


def test_every_certificate_carries_two_evidence_families_on_its_own() -> None:
    """E11 is defence in depth, not a brake on the certificates: each one clears the gate.

    Each minimal set now also carries the E45 corroboration its certificate implies — K-B's
    disjoint-window re-post shape IS the corroboration, K-A leans on the shared unit number,
    K-C on its matched interiors."""
    early = _listing(1, inactive_at="2024-05-01T00:00:00+00:00",
                     last_seen_at="2024-05-01T00:00:00+00:00")
    late = _listing(2, first_seen_at="2025-01-01T00:00:00+00:00")
    minimal = {
        "K-A": _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0,
                      floor_diff=0.0, unit_number_shared=1.0),
        "K-B": _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                      area_rel_diff=0.0, overlap_days=0.0),
        "K-C": _feats(phash_tight_matches=4.0, seq_monotone_ratio=1.0, area_rel_diff=0.0,
                      dispo_equal=1.0, catalog_ratio_max=0.0, interior_match_ratio=0.8,
                      n_images_min=4.0),
    }
    for name, feats in minimal.items():
        assert certificate_of(feats, early, late, KA_SETTINGS) == name
        decision = decide_pair(_fp(early), _fp(late), early, late, feats, set(),
                               FixedModel(0.05), KA_SETTINGS)
        assert len(decision.families) >= 2
        assert decision.zone == "merge"
        assert decision.certificate == name


def test_zones_follow_t_hi_and_t_lo() -> None:
    feats = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_exact=1.0, area_rel_diff=0.5,
                   **UNIT_EVIDENCE)
    merge = _decide(_listing(1), _listing(2), feats, probability=SETTINGS.t_hi)
    band = _decide(_listing(1), _listing(2), feats, probability=0.5)
    edge = _decide(_listing(1), _listing(2), feats, probability=SETTINGS.t_lo)
    reject = _decide(_listing(1), _listing(2), feats, probability=0.01)
    assert (merge.zone, merge.reason) == ("merge", "model")
    assert (band.zone, band.reason) == ("band", "model")
    assert edge.zone == "reject"
    assert reject.zone == "reject"


def test_decision_serialises_for_the_pairs_file() -> None:
    feats = _feats(phash_tight_matches=5.0, seq_monotone_ratio=1.0, area_rel_diff=0.0,
                   dispo_equal=1.0, catalog_ratio_max=0.0, interior_match_ratio=0.9,
                   n_images_min=5.0, same_ruian_adm_kod=1.0)
    payload = _decide(_listing(4), _listing(7), feats, probability=0.9).to_json()
    assert payload["lo"] == 4 and payload["hi"] == 7
    assert payload["zone"] == "merge"
    assert payload["certificate"] == "K-C"
    assert payload["families"] == sorted(payload["families"])


def test_unit_evidence_gate_keeps_a_building_grade_pair_out_of_the_merge_zone() -> None:
    """E45: one address point, one disposition, one area and a catalogue gallery is BUILDING
    evidence — the developer-unit false merges of the gold run had exactly this and nothing more."""
    building = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0,
                      catalog_ratio_max=1.0, n_images_min=0.0, tfidf_cos=0.9,
                      containment_max=0.95, same_street_key=1.0)
    assert not unit_evidence(building, SETTINGS)
    decision = _decide(_listing(1), _listing(2), building, probability=0.999)
    assert decision.zone == "band"
    assert decision.reason == "unit_evidence_gate"

    for corroboration in (
        {"interior_match_ratio": 0.6, "n_images_min": 3.0},
        {"rare_token_overlap": 2.0},
        {"floor_diff": 0.0, "unit_number_shared": 1.0},
    ):
        feats = _feats(**{**{"same_ruian_adm_kod": 1.0, "dispo_equal": 1.0,
                             "area_rel_diff": 0.0, "same_street_key": 1.0},
                          **corroboration})
        assert unit_evidence(feats, SETTINGS)
        assert _decide(_listing(1), _listing(2), feats, probability=0.999).zone == "merge"


def test_unit_evidence_needs_a_non_catalog_photo_on_BOTH_sides() -> None:
    """`n_images_min` is the min over the two sides after E9 catalogue subtraction: a perfect
    interior ratio over zero non-catalogue photos is a catalogue agreement."""
    catalog_only = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0,
                          interior_match_ratio=1.0, n_images_min=0.0)
    assert not unit_evidence(catalog_only, SETTINGS)


def test_the_k_b_shape_is_unit_evidence_on_its_own() -> None:
    """K-B ran 100% on n=94: one broker, one portal, one text, windows that never overlapped."""
    repost = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                    overlap_days=0.0)
    assert unit_evidence(repost, SETTINGS)
    co_live = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                     overlap_days=40.0)
    assert not unit_evidence(co_live, SETTINGS)


def test_developer_signature_bands_two_co_live_adverts_of_one_broker() -> None:
    """E46: long overlap, one broker on one portal, catalogue-only agreement — the shape of
    every hand-checked K-A false merge (judge: same_building_different_unit)."""
    signature = _feats(same_source=1.0, same_broker_key=1.0, overlap_days=61.0,
                       catalog_ratio_max=0.9, same_ruian_adm_kod=1.0, dispo_equal=1.0,
                       area_rel_diff=0.0, rare_token_overlap=3.0)
    assert developer_signature(signature, SETTINGS)
    decision = _decide(_listing(1), _listing(2), signature, probability=0.999,
                       settings=KA_SETTINGS)
    assert decision.zone == "band"
    assert decision.reason == "certificate:K-A:developer_signature"

    # Matched interiors break the signature: that is unit evidence, not catalogue material.
    # The pair still has to answer E47's co-live clause, which a shared unit number does.
    with_interiors = _feats(same_source=1.0, same_broker_key=1.0, overlap_days=61.0,
                            catalog_ratio_max=0.9, same_ruian_adm_kod=1.0, dispo_equal=1.0,
                            area_rel_diff=0.0, rare_token_overlap=3.0, floor_diff=0.0,
                            unit_number_shared=1.0,
                            interior_match_ratio=0.8, n_images_min=4.0)
    assert not developer_signature(with_interiors, SETTINGS)
    assert _decide(_listing(1), _listing(2), with_interiors, probability=0.999).zone == "merge"


def test_developer_signature_is_not_scoped_to_one_portal_or_one_broker() -> None:
    """E46 widened (W4c fix): the catalogue-only shape merged ACROSS portals too — 83202 x
    10515714 and 83467 x 10515837, both gold `same_building_different_unit`, rare tokens 5 and 3,
    catalog_ratio_max 1.0, no interior agreement at all. Widening it removed both at no measured
    recall cost (merge-zone HT precision 97.03% -> 97.79%, weighted recall unchanged)."""
    cross_portal = _feats(same_source=0.0, overlap_days=55.9, catalog_ratio_max=1.0,
                          same_ruian_adm_kod=1.0, rare_token_overlap=5.0, tfidf_cos=1.0,
                          containment_max=1.0)
    assert developer_signature(cross_portal, SETTINGS)
    assert _decide(_listing(1), _listing(2), cross_portal, probability=0.999).reason \
        == "developer_signature"
    narrow = Settings(developer_signature_same_broker_only=True)
    assert not developer_signature(cross_portal, narrow)


def test_developer_colive_bands_one_brokers_two_adverts_that_ran_side_by_side() -> None:
    """E47: K-A's demotion re-routed the developer shape instead of removing it — 21000 x 27781
    (gold `same_building_different_unit`, 61 co-live days, one broker, one portal) came back as
    K-C, because the two units were photographed together (interior_match_ratio 1.0). No image
    rule can separate them; the co-live window can."""
    colive = _feats(same_source=1.0, same_broker_key=1.0, overlap_days=61.6,
                    same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0, floor_diff=0.0,
                    interior_match_ratio=1.0, n_images_min=7.0, catalog_ratio_max=0.125,
                    phash_tight_matches=7.0, seq_monotone_ratio=1.0)
    assert developer_colive(colive, SETTINGS)
    decision = _decide(_listing(1), _listing(2), colive, probability=0.999)
    assert (decision.zone, decision.certificate) == ("band", "K-C")
    assert decision.reason == "certificate:K-C:developer_colive"

    # A shared unit number answers the gate, and so does a short window (E6's re-post, not a
    # project): both are the release valves the rule is built around.
    stated = {name: value for name, (value, present) in colive.items() if present}
    assert not developer_colive(_feats(**stated, unit_number_shared=1.0), SETTINGS)
    assert not developer_colive(_feats(**{**stated, "overlap_days": 2.0}), SETTINGS)
    assert not developer_colive(colive, Settings(developer_colive_guard=True,
                                                 colive_overlap_days=90.0))


def test_the_rare_token_arm_is_free_by_default_and_can_be_made_to_need_a_photo() -> None:
    """Measured on the gold labels: with E46 widened, every merge resting on rare tokens alone
    is a judged positive (19/19, HT 100%), and demanding a non-catalogue photo beside them cost
    1.9 points of weighted recall for none of precision — so the clause ships OFF, as a dial."""
    tokens_only = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0,
                         rare_token_overlap=3.0)
    assert unit_evidence(tokens_only, SETTINGS)
    strict = Settings(unit_rare_requires_support=True)
    assert not unit_evidence(tokens_only, strict)
    with_photo = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0,
                        rare_token_overlap=3.0, interior_match_ratio=0.2, n_images_min=2.0)
    assert unit_evidence(with_photo, strict)


def test_a_disabled_gate_restores_the_old_behaviour() -> None:
    """Both gates are settings-driven so the evaluation can price each one separately."""
    building = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0,
                      catalog_ratio_max=1.0, n_images_min=0.0, same_street_key=1.0,
                      area_exact=1.0)
    off = Settings(unit_evidence_required=False, developer_signature_guard=False,
                   developer_colive_guard=False)
    assert _decide(_listing(1), _listing(2), building, probability=0.999, settings=off).zone \
        == "merge"


def test_auto_reject_limit_is_settings_driven() -> None:
    feats = _feats(attr_contradictions=2.0, same_ruian_adm_kod=1.0, dispo_equal=1.0,
                   **UNIT_EVIDENCE)
    assert _decide(_listing(1), _listing(2), feats, probability=0.999).zone == "merge"
    strict = Settings(max_attr_contradictions=2.0)
    strict_decision = _decide(_listing(1), _listing(2), feats, probability=0.999,
                              settings=strict)
    assert strict_decision.reason == "auto_reject:attr_contradictions"


def test_the_stratum_key_is_spelled_the_way_the_evaluation_spells_it() -> None:
    """E48: the table an evaluation writes is the table the engine reads, so one spelling."""
    from autodedup.evaluate import decide_stratum

    same = _feats(same_source=1.0)
    cross = _feats(same_source=0.0)
    assert stratum_key(same, "K-C") == "K-C|same"
    assert stratum_key(cross, None) == "model|cross"
    assert stratum_key(same, "K-C") == decide_stratum({"certificate": "K-C", "cross_source": False})
    assert stratum_key(cross, None) == decide_stratum({"certificate": None, "cross_source": True})
    # An unknown side is the stricter cell, never the laxer one.
    assert stratum_key(_feats(), None) == "model|cross"


def test_a_propose_only_stratum_bands_a_certificate_that_would_otherwise_merge() -> None:
    """The switch D3 needs: K-C|same ran 94.7% dev / 85.7% sealed, under the 0.97 floor, and a
    certificate is read BEFORE `t_hi`, so no global cut can hold it back."""
    feats = _feats(phash_tight_matches=5.0, seq_monotone_ratio=1.0, area_rel_diff=0.0,
                   dispo_equal=1.0, catalog_ratio_max=0.0, interior_match_ratio=0.9,
                   n_images_min=5.0, same_source=1.0)
    assert _decide(_listing(1), _listing(2), feats, probability=0.01).zone == "merge"
    gated = Settings(t_hi_by_stratum={"K-C|same": None})
    decision = _decide(_listing(1), _listing(2), feats, probability=0.99, settings=gated)
    assert decision.zone == "band"
    assert decision.reason == "certificate:K-C:stratum_propose_only"
    assert decision.certificate == "K-C"
    # The same certificate on the OTHER side of the table still merges.
    cross = dict(feats)
    cross["same_source"] = (0.0, True)
    assert _decide(_listing(1), _listing(2), cross, probability=0.01,
                   settings=gated).zone == "merge"


def test_a_stratum_cut_overrides_the_global_one_for_the_score_layer() -> None:
    feats = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_exact=1.0, area_rel_diff=0.5,
                   same_source=0.0, **UNIT_EVIDENCE)
    row = Settings(t_hi=0.97, t_hi_by_stratum={"model|cross": 0.99, "model|same": None})
    assert stratum_t_hi(feats, None, row) == 0.99
    assert _decide(_listing(1), _listing(2), feats, probability=0.98, settings=row).zone == "band"
    assert _decide(_listing(1), _listing(2), feats, probability=0.995, settings=row).zone == "merge"
    same = dict(feats)
    same["same_source"] = (1.0, True)
    assert stratum_t_hi(same, None, row) is None
    assert _decide(_listing(1), _listing(2), same, probability=1.0, settings=row).zone == "band"
    # An EMPTY table is the incumbent engine, untouched.
    assert stratum_t_hi(feats, None, Settings()) == Settings().t_hi
