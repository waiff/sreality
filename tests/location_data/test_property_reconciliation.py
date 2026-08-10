"""S8 property grain — a RECONCILIATION over children, never a single-child lottery
(00 §7.5, 03 §3.9.4).

Today every property location column is `CASE WHEN cnt = 1 THEN l.<col> ELSE p.<col> END`,
with multi-source groups owned by an async recompute that picks a representative — "so a
group containing one precise pin and one town centroid may publish the centroid", and the
disagreement is discarded. Disagreement among members is one of the strongest available
signals that a grouping is wrong, so it is a first-class output here.
"""

from __future__ import annotations

from location_data.resolver import projection
from location_data.resolver.types import GranularityRank

RANK = GranularityRank()


def _child(listing_id, **overrides):
    row = {
        "listing_id": listing_id,
        "source": "sreality",
        "lat": 50.10102,
        "lon": 14.34804,
        "granularity": "address_point",
        "position_source": "registry_point",
        "position_quality_class": "precise",
        "blur_evidence": "none",
        "match_confidence": "exact",
        "uncertainty_radius_m": 10.0,
        "radius_semantics": "geometric_bound",
        "position_licence_class": "cc_by_ruian",
        "ruian_adm_kod": 21690278,
        "stavebni_objekt_kod": 555001,
        "obec_kod": 554782,
        "cast_obce_kod": 490067,
        "okres_kod": 3100,
        "kraj_kod": 19,
        "admin_path": "k19.o3100.b554782",
        "admin_assignment_method": "registry",
        "street_name": "Nad Bořislavkou",
        "psc": "16000",
        "display_label": "Nad Bořislavkou 487/40, Vokovice, Praha",
        "place_search_text": "Nad Bořislavkou Vokovice Praha",
        "country_code": "CZ",
        "country_status": "cz",
        "pin_shared_by_n": 1,
        "geo_blockable": True,
        "render_as": "point",
    }
    row.update(overrides)
    return row


def _centroid_child(listing_id, **overrides):
    return _child(
        listing_id,
        source="bazos",
        lat=50.0755,
        lon=14.4378,
        granularity="obec",
        position_source="admin_centroid",
        position_quality_class="area",
        match_confidence="low",
        uncertainty_radius_m=9000.0,
        ruian_adm_kod=None,
        stavebni_objekt_kod=None,
        street_name=None,
        display_label="Praha",
        geo_blockable=False,
        render_as="area",
        **overrides,
    )


def test_a_precise_child_beats_a_centroid_child_and_the_lottery_cannot_happen():
    row = projection.build_property_row(42, [_centroid_child(2), _child(1)], rank=RANK)
    assert row is not None
    assert row["winner_listing_id"] == 1
    assert row["granularity"] == "address_point"
    assert row["position_source"] == "registry_point"
    assert row["winner_rule"].startswith("highest_precision:")


def test_the_winner_does_not_depend_on_member_order():
    forward = projection.build_property_row(42, [_child(1), _centroid_child(2)], rank=RANK)
    backward = projection.build_property_row(42, [_centroid_child(2), _child(1)], rank=RANK)
    assert forward == backward


def test_the_disagreement_columns_are_populated_not_discarded():
    row = projection.build_property_row(42, [_child(1), _centroid_child(2)], rank=RANK)
    assert row["member_count"] == 2
    assert row["members_with_geom"] == 2
    assert row["distinct_street_names"] == 1  # the centroid child has none
    assert row["distinct_obec_kods"] == 1
    assert row["member_spread_m"] is not None and row["member_spread_m"] > 1000
    assert "precision_mix" in row["disagreement_flags"]
    assert "member_spread_exceeds_uncertainty" not in row["disagreement_flags"]


def test_two_villages_sharing_one_town_pin_raise_the_spread_flag():
    """repo-iss §2: two genuinely different houses in DIFFERENT villages inherited one
    town-level coordinate, landed 0 m apart and were approved as one property."""
    row = projection.build_property_row(
        7,
        [
            _child(1, street_name="Horní Bousov 12", obec_kod=1111, lat=50.4300, lon=15.1200,
                   uncertainty_radius_m=10.0),
            _child(2, street_name="Vlčí Pole 3", obec_kod=2222, lat=50.4600, lon=15.1600,
                   uncertainty_radius_m=10.0),
        ],
        rank=RANK,
    )
    assert set(row["disagreement_flags"]) >= {
        "street_disagreement", "obec_disagreement", "member_spread_exceeds_uncertainty"
    }
    assert row["distinct_obec_kods"] == 2


def test_a_member_without_a_geometry_is_flagged_and_never_wins():
    row = projection.build_property_row(
        9, [_child(1, lat=None, lon=None, geo_blockable=False), _centroid_child(2)], rank=RANK
    )
    assert row["winner_listing_id"] == 2
    assert row["members_with_geom"] == 1
    assert "members_without_geom" in row["disagreement_flags"]


def test_a_single_member_group_states_its_rule_rather_than_implying_a_choice():
    row = projection.build_property_row(3, [_child(1)], rank=RANK)
    assert row["winner_rule"] == "sole_member"
    assert row["disagreement_flags"] == []
    assert row["member_spread_m"] is None


def test_an_empty_group_produces_no_row():
    assert projection.build_property_row(5, [], rank=RANK) is None
