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
    certificate_r,
    disjoint_windows,
    present_value,
    window_gap_days,
    stratum_key,
    stratum_t_hi,
    unit_evidence,
)
from autodedup.features import ABSENT, FEATURE_ORDER
from autodedup.fingerprint import build_fingerprint
from autodedup.settings import MIN_CERTIFICATE_B_GAP_DAYS, Settings

SETTINGS = Settings()
GAP = MIN_CERTIFICATE_B_GAP_DAYS  # E84: an honest-clock row may not omit it
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


def test_which_end_stamp_k_b_reads_is_a_setting_not_a_second_spelling() -> None:
    """W8: the delisting-DETECTION stamp keeps an advert nominally live for up to 70 days.

    The honest clock (`live_window_from_sighting`) says these two never ran together; the
    default keeps the wider window, under which they did, so K-B does not fire."""
    from dataclasses import replace

    feats = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                   area_rel_diff=0.005)
    gone = _listing(1, first_seen_at="2026-06-01T00:00:00+00:00",
                    last_seen_at="2026-07-18T00:00:00+00:00",
                    inactive_at="2026-09-07T00:00:00+00:00", is_active=False)
    successor = _listing(2, first_seen_at="2026-08-03T00:00:00+00:00",
                         last_seen_at="2026-08-06T00:00:00+00:00",
                         inactive_at="2026-09-08T00:00:00+00:00", is_active=False)
    assert not disjoint_windows(gone, successor, SETTINGS)
    assert not certificate_b(feats, gone, successor, SETTINGS)

    # The honest clock makes them disjoint. E65 is then what decides, and the two listings
    # carry no comparable frame, so the certificate does not fire (see the E65 tests below).
    honest = replace(SETTINGS, live_window_from_sighting=True, certificate_b_min_gap_days=GAP,
                     certificate_b_min_images=1.0, certificate_b_min_matched_images=1.0)
    assert disjoint_windows(gone, successor, honest)
    assert not certificate_b(feats, gone, successor, honest)
    with_frames = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                         area_rel_diff=0.005, n_images_min=6.0, phash_loose_matches=5.0)
    assert certificate_b(with_frames, gone, successor, honest)


def test_e84_two_once_seen_adverts_are_not_a_re_post() -> None:
    """The W10 false merge, in miniature: 412540 x 412544, 2.8 seconds apart.

    Under the honest clock an advert seen exactly once has a live window of zero length, so
    `max(starts) > min(ends)` holds for ANY two once-seen adverts — including two different
    products one index walk picked up in the same second. The rail reads the PAIR's gap."""
    from dataclasses import replace

    feats = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                   area_rel_diff=0.0, n_images_min=2.0, phash_loose_matches=1.0)
    one_walk_a = _listing(1, first_seen_at="2026-06-29T12:31:19.100482+00:00",
                          last_seen_at="2026-06-29T12:31:19.100482+00:00",
                          inactive_at="2026-09-07T10:00:43+00:00", is_active=False)
    one_walk_b = _listing(2, first_seen_at="2026-06-29T12:31:21.900697+00:00",
                          last_seen_at="2026-06-29T12:31:21.900697+00:00",
                          inactive_at="2026-09-07T10:00:45+00:00", is_active=False)
    honest = replace(SETTINGS, live_window_from_sighting=True, certificate_b_min_gap_days=GAP,
                     certificate_b_min_images=1.0, certificate_b_min_matched_images=1.0)
    assert not disjoint_windows(one_walk_a, one_walk_b, honest)
    assert not certificate_b(feats, one_walk_a, one_walk_b, honest)

    # Without the rail the same pair certifies — which is exactly what the W10 candidate did
    # and what the gold labels caught. `validate` refuses that row, so the only way to reach
    # it is to defeat the rail after construction.
    unrailed = replace(honest)
    unrailed.certificate_b_min_gap_days = 0.0
    assert disjoint_windows(one_walk_a, one_walk_b, unrailed)
    assert certificate_b(feats, one_walk_a, one_walk_b, unrailed)


def test_e84_costs_a_real_re_post_nothing_and_reads_the_pair_not_the_side() -> None:
    """A once-seen advert is not itself disqualified: what has to clear the bar is the gap."""
    from dataclasses import replace

    feats = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                   area_rel_diff=0.0, n_images_min=6.0, phash_loose_matches=5.0)
    honest = replace(SETTINGS, live_window_from_sighting=True, certificate_b_min_gap_days=GAP,
                     certificate_b_min_images=1.0, certificate_b_min_matched_images=1.0)
    gone = _listing(1, first_seen_at="2026-06-01T00:00:00+00:00",
                    last_seen_at="2026-06-01T00:00:00+00:00",
                    inactive_at="2026-09-07T00:00:00+00:00", is_active=False)
    successor = _listing(2, first_seen_at="2026-06-29T00:00:00+00:00",
                         last_seen_at="2026-06-29T00:00:00+00:00",
                         inactive_at="2026-09-08T00:00:00+00:00", is_active=False)
    assert window_gap_days(gone, successor, honest) == 28.0
    assert certificate_b(feats, gone, successor, honest)


def test_window_gap_days_is_signed_and_unknown_stays_unknown() -> None:
    overlapping = _listing(2, first_seen_at="2024-02-01T00:00:00+00:00",
                           last_seen_at="2024-09-01T00:00:00+00:00")
    early = _listing(1, first_seen_at="2024-01-01T00:00:00+00:00",
                     last_seen_at="2024-05-01T00:00:00+00:00",
                     inactive_at="2024-05-01T00:00:00+00:00", is_active=False)
    gap = window_gap_days(early, overlapping, SETTINGS)
    assert gap is not None and gap < 0.0
    assert window_gap_days(early, _listing(2, first_seen_at=None), SETTINGS) is None


def test_the_honest_clock_may_not_be_run_without_the_e84_rail() -> None:
    with pytest.raises(ValueError, match="E84 gap rail"):
        Settings(live_window_from_sighting=True, certificate_b_min_images=1.0,
                 certificate_b_min_gap_days=0.0)
    with pytest.raises(ValueError, match="must not be negative"):
        Settings(certificate_b_min_gap_days=-1.0)


def test_certificate_c_needs_four_ordered_non_catalog_matches() -> None:
    base = dict(phash_tight_matches=4.0, seq_monotone_ratio=0.9, area_rel_diff=0.02,
                dispo_equal=1.0, catalog_ratio_max=0.1)
    assert certificate_c(_feats(**base))
    assert not certificate_c(_feats(**{**base, "phash_tight_matches": 3.0}))
    assert not certificate_c(_feats(**{**base, "seq_monotone_ratio": 0.5}))
    assert not certificate_c(_feats(**{**base, "catalog_ratio_max": 0.6}))
    assert not certificate_c(_feats(**{**base, "dispo_equal": 0.0}))


def test_certificate_order_is_r_then_a_then_b_then_c() -> None:
    early = _listing(1, inactive_at="2024-05-01T00:00:00+00:00",
                     last_seen_at="2024-05-01T00:00:00+00:00")
    late = _listing(2, first_seen_at="2025-01-01T00:00:00+00:00")
    every = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0, floor_diff=0.0,
                   same_source=1.0, same_broker_key=1.0, containment_max=1.0,
                   phash_tight_matches=6.0, seq_monotone_ratio=1.0, catalog_ratio_max=0.0)
    assert certificate_of(every, early, late, KA_SETTINGS) == "K-A"
    assert certificate_of(_feats(), early, late, KA_SETTINGS) is None
    assert set(CERTIFICATES) == {"K-A", "K-B", "K-C", "K-R"}
    # E60 is read before every resemblance: the broker SAID which order this is.
    coded = dict(every)
    coded["ref_code_shared"] = (1.0, True)
    assert certificate_of(coded, early, late, KA_SETTINGS) == "K-R"


def test_a_conflicting_unit_designator_vetoes_whatever_the_pair_scores() -> None:
    """E61 at the rule floor: no certificate and no score may reach a pair whose two bodies
    name a different unit of one address block, and the two strings travel on the decision."""
    everything = _feats(same_ruian_adm_kod=1.0, dispo_equal=1.0, area_rel_diff=0.0,
                        floor_diff=0.0, ref_code_shared=1.0, **UNIT_EVIDENCE)
    a = _listing(1, description="Prodej bytu (č.3) v novostavbě.",
                 location=Location(ruian_adm_kod=7, obec_kod=1, granularity_rank=60))
    b = _listing(2, description="Prodej bytu (č.5) v novostavbě.",
                 location=Location(ruian_adm_kod=7, obec_kod=1, granularity_rank=60))
    decision = _decide(a, b, everything, probability=0.999)
    assert decision.zone == "veto"
    assert decision.veto == "unit_designator_conflict"
    assert decision.evidence == {"unit_lo": "3", "unit_hi": "5"}
    assert decision.to_json()["evidence"] == {"unit_lo": "3", "unit_hi": "5"}


def test_a_shared_order_code_certifies_and_a_differing_one_says_nothing() -> None:
    """E60. The feature is PRESENT only when the two bodies share a code, so the `absent` case
    below is both `no codes at all` and `two different codes` — W7 refuted reading the second
    as a negative (395722 x 486034: N115815 on ceskereality, N118731 on sreality, one flat)."""
    from dataclasses import replace

    early = _listing(1, inactive_at="2024-05-01T00:00:00+00:00",
                     last_seen_at="2024-05-01T00:00:00+00:00")
    late = _listing(2, first_seen_at="2025-01-01T00:00:00+00:00")
    shared = _feats(ref_code_shared=1.0)
    assert certificate_r(shared)
    assert certificate_of(shared, early, late, SETTINGS) == "K-R"
    assert not certificate_r(_feats())
    assert certificate_of(_feats(), early, late, SETTINGS) is None
    # the order code alone satisfies E45 — no image, no rare token, no unit number needed
    assert unit_evidence(shared, SETTINGS)
    off = replace(SETTINGS, certificate_kr_enabled=False)
    assert certificate_of(shared, early, late, off) is None


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


# --- E63: the band promotion the W8 verification endorsed --------------------------------

E63 = Settings(context_rule_enabled=True)


def _e63_feats(**over: float) -> dict[str, tuple[float, bool]]:
    """The warrant: a near-identical body, an identical current price, an agreeing area."""
    base = {"containment_max": 0.97, "price_last_ratio": 1.0, "area_rel_diff": 0.0}
    base.update(over)
    return _feats(**base)


_NO_CENSUS = Settings(context_rule_enabled=True, context_rule_image_population_min=None,
                      context_rule_from_price_veto=False)


def _e63(la: Listing, lb: Listing, feats: dict[str, tuple[float, bool]],
         probability: float = 1.0, settings: Settings = _NO_CENSUS,
         context: Any = None) -> Decision:
    return decide_pair(_fp(la), _fp(lb), la, lb, feats, {"attr_dispo"},
                       FixedModel(probability), settings, context)


def test_e63_promotes_a_band_pair_the_warrant_carries() -> None:
    a, b = _listing(1), _listing(2)
    banded = _e63(a, b, _e63_feats(), settings=Settings())
    assert banded.zone == "band"
    promoted = _e63(a, b, _e63_feats())
    assert (promoted.zone, promoted.reason) == ("merge", "context_rule:text")


def test_e63_is_off_unless_a_settings_row_asks_for_it() -> None:
    assert _e63(_listing(1), _listing(2), _e63_feats(), settings=Settings()).zone == "band"


def test_e63_with_the_default_census_limbs_needs_the_cohort_index() -> None:
    from autodedup.hazard_context import ContextIndex

    a, b = _listing(1), _listing(2)
    assert _e63(a, b, _e63_feats(), settings=E63).zone == "band"
    index = ContextIndex.build({1: a, 2: b}, {})
    assert _e63(a, b, _e63_feats(), settings=E63, context=index).zone == "merge"


@pytest.mark.parametrize(
    "over, why",
    [
        ({"containment_max": 0.89}, "a body that is not near-identical"),
        ({"price_last_ratio": 0.99}, "a price that moved"),
        ({"area_rel_diff": 0.02}, "C1: areas more than 1% apart"),
    ],
)
def test_e63_refuses_a_pair_missing_one_clause(over: dict[str, float], why: str) -> None:
    assert _e63(_listing(1), _listing(2), _e63_feats(**over)).zone == "band", why


def test_e63_refuses_a_score_below_the_top_of_the_range() -> None:
    # 0.9545 is the isotonic plateau the disputed operator card (18705144) and its partner sit
    # on; W8 measured 41 negatives and 310 positives there, so no clause may promote it.
    assert _e63(_listing(1), _listing(2), _e63_feats(), probability=0.9545).zone == "band"
    assert _e63(_listing(1), _listing(2), _e63_feats(), probability=0.9998).zone == "band"


def test_e63_never_overrules_the_developer_guards() -> None:
    # E46: co-live adverts agreeing only on catalogue material.
    feats = _e63_feats(overlap_days=30.0, catalog_ratio_max=1.0, interior_match_ratio=0.0)
    blocked = _e63(_listing(1), _listing(2), feats)
    assert blocked.zone == "band" and blocked.reason == "developer_signature"
    # E47: one broker, one portal, side by side for weeks.
    colive = _e63_feats(same_source=1.0, same_broker_key=1.0, overlap_days=61.0,
                        rare_token_overlap=2.0)
    refused = _e63(_listing(1), _listing(2), colive)
    assert refused.zone == "band" and refused.reason == "developer_colive"


def test_e63_reads_pairs_the_unit_gate_and_the_diversity_gate_banded() -> None:
    gated = _e63(_listing(1), _listing(2), _e63_feats())
    assert gated.zone == "merge"


def test_e63_never_reaches_a_veto_or_an_auto_reject() -> None:
    vetoed = _e63(_listing(1), _listing(2, category_type="pronajem"), _e63_feats())
    assert vetoed.zone == "veto"
    rejected = _e63(_listing(1), _listing(2), _e63_feats(numeral_conflict=1.0))
    assert rejected.zone == "reject"


def test_e63_stamps_the_census_it_was_taken_under() -> None:
    from autodedup.hazard_context import ContextIndex

    a, b = _listing(1), _listing(2)
    index = ContextIndex.build({1: a, 2: b}, {})
    promoted = _e63(a, b, _e63_feats(), settings=E63, context=index)
    assert promoted.zone == "merge"
    assert promoted.evidence["context_cell_n"] == "2"
    assert promoted.evidence["context_image_pop_min"] == "0"


def test_e63_refuses_a_promotion_a_fungible_limb_names() -> None:
    from autodedup.hazard_context import ContextIndex

    a, b = _listing(1), _listing(2)
    a.description = "Ceny od 4 990 000 Kč. " + "x" * 300
    index = ContextIndex.build({1: a, 2: b}, {})
    refused = _e63(a, b, _e63_feats(), settings=E63, context=index)
    assert refused.zone == "band"
    assert refused.reason == "context_rule:fungible:from_price"


def test_e63_refuses_to_promote_when_a_census_limb_has_no_index_to_read() -> None:
    # An unanswered guard fails towards the stricter side, exactly as E48 does.
    assert _e63(_listing(1), _listing(2), _e63_feats(), settings=E63, context=None).zone == "band"
    assert _e63(_listing(1), _listing(2), _e63_feats(), context=None).zone == "merge"


def test_e63_interior_arm_is_off_and_cannot_be_enabled_below_four_images() -> None:
    # No text warrant, an interior ratio of 1.0 over THREE photos, and a score the model path
    # cannot merge on its own: only an interior arm could promote this, and none may.
    feats = _e63_feats(containment_max=0.1, interior_match_ratio=1.0, n_images_min=3.0)
    assert _e63(_listing(1), _listing(2), feats, probability=0.5).zone == "band"
    interior = Settings(context_rule_enabled=True, context_rule_interior_min=0.5,
                        context_rule_image_population_min=None,
                        context_rule_from_price_veto=False)
    assert _e63(_listing(1), _listing(2), feats, probability=0.5,
                settings=interior).zone == "band"
    with pytest.raises(ValueError, match="at least 4"):
        Settings(context_rule_enabled=True, context_rule_interior_min=0.5,
                 context_rule_min_images=3.0)


# --- E65: the K-B image floor ----------------------------------------------------------


def _kb_pair() -> tuple[dict[str, tuple[float, bool]], Listing, Listing]:
    """The 522698 x 13221982 shape: one ceskereality broker's template, disjoint short windows."""
    gone = _listing(1, first_seen_at="2026-07-11T00:00:00+00:00",
                    last_seen_at="2026-07-18T00:00:00+00:00",
                    inactive_at="2026-09-07T00:00:00+00:00", is_active=False)
    successor = _listing(2, first_seen_at="2026-08-03T00:00:00+00:00",
                         last_seen_at="2026-08-06T00:00:00+00:00",
                         inactive_at="2026-09-08T00:00:00+00:00", is_active=False)
    feats = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.99,
                   area_rel_diff=0.0, n_images_min=0.0, catalog_ratio_max=1.0)
    return feats, gone, successor


def test_e65_an_all_catalogue_gallery_counts_as_zero_frames() -> None:
    """`n_images_min` is E9-subtracted: 11 stock photos on each side are zero comparable frames,
    which is the measured shape of 1,318 of the cohort's 1,348 image-less K-B merges."""
    feats, gone, successor = _kb_pair()
    honest = Settings(live_window_from_sighting=True, certificate_b_min_gap_days=GAP,
                      certificate_b_min_images=1.0,
                      certificate_b_min_matched_images=1.0)
    assert disjoint_windows(gone, successor, honest)
    assert not certificate_b(feats, gone, successor, honest)
    assert certificate_of(feats, gone, successor, honest) is None


def test_e65_an_absent_match_count_fails_the_floor() -> None:
    """Nothing to compare is not evidence of agreement — ABSENT must not read as satisfied."""
    feats, gone, successor = _kb_pair()
    feats["n_images_min"] = (8.0, True)
    feats["phash_loose_matches"] = ABSENT
    feats["phash_tight_matches"] = (0.0, True)  # tight is not the limb; loose is
    honest = Settings(live_window_from_sighting=True, certificate_b_min_gap_days=GAP,
                      certificate_b_min_images=1.0,
                      certificate_b_min_matched_images=1.0)
    assert not certificate_b(feats, gone, successor, honest)
    feats["phash_loose_matches"] = (1.0, True)
    assert certificate_b(feats, gone, successor, honest)


def test_e65_floor_is_read_on_both_sides_not_on_the_larger_gallery() -> None:
    """`n_images_min` is the MIN of the two galleries, so one rich side cannot carry an empty one."""
    feats, gone, successor = _kb_pair()
    feats["n_images_min"] = (0.0, True)
    feats["phash_loose_matches"] = (4.0, True)
    honest = Settings(live_window_from_sighting=True, certificate_b_min_gap_days=GAP,
                      certificate_b_min_images=2.0,
                      certificate_b_min_matched_images=2.0)
    assert not certificate_b(feats, gone, successor, honest)
    feats["n_images_min"] = (2.0, True)
    assert certificate_b(feats, gone, successor, honest)


def test_e65_each_limb_is_its_own_settings_row() -> None:
    feats, gone, successor = _kb_pair()
    feats["n_images_min"] = (6.0, True)
    feats["phash_loose_matches"] = (0.0, True)
    sides_only = Settings(live_window_from_sighting=True, certificate_b_min_gap_days=GAP,
                          certificate_b_min_images=1.0)
    assert certificate_b(feats, gone, successor, sides_only)
    both = Settings(live_window_from_sighting=True, certificate_b_min_gap_days=GAP,
                    certificate_b_min_images=1.0,
                    certificate_b_min_matched_images=1.0)
    assert not certificate_b(feats, gone, successor, both)


def test_e65_defaults_off_leaves_the_detection_clock_untouched() -> None:
    """The floor is the honest clock's price, not a free tightening: on the shipped clock it
    withholds 982 of 1,110 K-B merges carrying 169 reliable duplicates and 0 negatives."""
    assert Settings().certificate_b_min_images == 0.0
    assert Settings().certificate_b_min_matched_images == 0.0
    feats, _, _ = _kb_pair()
    # Disjoint under BOTH clocks, so only the floor can decide.
    gone = _listing(1, first_seen_at="2026-01-05T00:00:00+00:00",
                    last_seen_at="2026-01-20T00:00:00+00:00",
                    inactive_at="2026-01-21T00:00:00+00:00", is_active=False)
    successor = _listing(2, first_seen_at="2026-03-01T00:00:00+00:00",
                         last_seen_at="2026-03-10T00:00:00+00:00",
                         inactive_at="2026-03-11T00:00:00+00:00", is_active=False)
    assert certificate_b(feats, gone, successor, Settings(live_window_from_sighting=False))


def test_e95_the_honest_clock_pays_with_e84_and_the_floor_is_optional() -> None:
    """E110 (W13) replaces E65 as the honest clock's price: the operator ruled the two pairs
    that were the floor's whole case (`522698 x 13221982`, `555448 x 18626270`) duplicates, so
    the sealed read that carries the arm is W11's — 347 of 406 against g6's 300, gained 47,
    lost 0 — and the price it measured is the E84 pair gap. The floor still BUILDS and still
    binds `certificate_b`; what changed is that it is no longer mandatory."""
    with pytest.raises(ValueError, match="E84 gap rail"):
        Settings(live_window_from_sighting=True)
    Settings(live_window_from_sighting=True, certificate_b_min_gap_days=GAP)
    Settings(live_window_from_sighting=True, certificate_b_min_gap_days=GAP,
             certificate_b_min_images=1.0)


def test_e65_a_matched_set_cannot_exceed_the_smaller_gallery() -> None:
    with pytest.raises(ValueError, match="matched set is a subset"):
        Settings(certificate_b_min_images=2.0, certificate_b_min_matched_images=3.0)
    with pytest.raises(ValueError, match="must not be negative"):
        Settings(certificate_b_min_images=-1.0)
