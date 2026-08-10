"""The collision-epoch producer: clustering, the six-value classification, the mandatory
3×3 neighbourhood expansion, and bucket-change detection (03 §3.8.4, 00 §10).

Reference pathologies from the corpus (db-coverage-stats §4.1): bazos 5.56 listings/point,
51.5 % of rows in clusters ≥20, max cluster 276 at one Olomouc point containing 99 distinct
street names; bezrealitky 1.09 with max cluster 12 and a genuinely 1:many real-world
collapse; the idnes 58-row cluster where every row is the SAME street.
"""

from __future__ import annotations

from location_data.resolver import collision
from tests.location_data import mini_mirror as mm

POLICY = mm.COLLISION_POLICY


def _pin(listing_id, lat, lon, **overrides):
    return collision.PinRow(
        listing_id=listing_id, source=overrides.pop("source", "bazos"), lat=lat, lon=lon,
        **overrides,
    )


def test_a_lone_pin_is_normal_and_never_null():
    clusters = collision.build_clusters([_pin(1, 50.0, 14.0)], POLICY)
    assert [c.classification for c in clusters] == ["normal"]


def test_the_olomouc_town_pin_is_town_centroid_suspect():
    """276 listings at one point containing 99 distinct street names, sitting on the obec's
    own representative point."""
    pins = [
        _pin(i, 49.593577, 17.29866, street_key=f"ulice {i}", obec_kod=500496,
             distance_to_admin_centroid_m=5.0)
        for i in range(1, 30)
    ]
    cluster = collision.build_clusters(pins, POLICY)[0]
    assert cluster.classification == "town_centroid_suspect"
    assert cluster.distinct_streets == 29
    assert not cluster.heterogeneity_ok


def test_a_heterogeneous_collapse_away_from_the_centroid_is_a_parser_collapse():
    pins = [
        _pin(i, 49.60, 17.30, street_key=f"ulice {i}", obec_kod=500496,
             distance_to_admin_centroid_m=4000.0)
        for i in range(1, 20)
    ]
    assert collision.build_clusters(pins, POLICY)[0].classification == "parser_collapse_suspect"


def test_the_idnes_same_street_cluster_stays_blockable_as_building_1_to_many():
    """58 rows that are ALL `Unhošťská, Kladno - Kročehlavy`, with a street: homogeneous,
    and correctly still blockable."""
    pins = [
        _pin(i, 50.12413, 14.12853, source="idnes", street_key="unhostska", obec_kod=532053)
        for i in range(1, 20)
    ]
    cluster = collision.build_clusters(pins, POLICY)[0]
    assert cluster.classification == "building_1_to_many"
    assert cluster.heterogeneity_ok


def test_bezrealitkys_multiunit_collapse_is_legitimate_under_its_own_threshold():
    """Rezidence Veletržní 42 / River Garden / Signature Prague: max cluster 12, and the
    portal's own policy row carries `pin_collision_semantics='legitimate_multiunit'`."""
    pins = [
        _pin(i, 50.1030, 14.4300, source="bezrealitky", street_key=f"ulice {i % 3}",
             obec_kod=554782)
        for i in range(1, 9)
    ]
    cluster = collision.build_clusters(pins, POLICY)[0]
    assert cluster.classification == "legitimate_multiunit"


def test_a_foreign_resort_pin_is_classified_as_such():
    """740 listings on one Spanish point; foreign pins are collapsed by construction."""
    pins = [
        _pin(i, 36.42681, -5.14685, source="idnes", is_cz=False, street_key=f"calle {i}")
        for i in range(1, 12)
    ]
    assert collision.build_clusters(pins, POLICY)[0].classification == "foreign_resort_centroid"


def test_the_neighbourhood_is_the_mandatory_three_by_three():
    cells = collision.neighbourhood(collision.cell_of(50.0, 14.0))
    assert len(cells) == 9
    assert collision.cell_of(50.0, 14.0) in cells


def test_a_jittered_centroid_is_caught_by_the_25_m_radius_not_by_exact_equality():
    """A 6th-decimal-jittered centroid walks straight past an exact-equality test."""
    pins = [
        _pin(1, 50.000000, 14.000000, street_key="a"),
        _pin(2, 50.000090, 14.000090, street_key="b"),  # ~12 m away, a DIFFERENT 4-dp cell
    ]
    clusters = {c.cell_key: c for c in collision.build_clusters(pins, POLICY)}
    assert len(clusters) == 2  # exact equality sees two singletons
    assert all(c.n_25m == 2 for c in clusters.values())  # the expansion sees one pair


def test_only_bucket_changes_enqueue_a_re_resolution():
    before = collision.build_clusters(
        [_pin(i, 50.0, 14.0, street_key="a") for i in range(1, 4)], POLICY
    )
    unchanged = collision.build_clusters(
        [_pin(i, 50.0, 14.0, street_key="a") for i in range(1, 4)], POLICY
    )
    assert collision.changed_listings(before, unchanged, POLICY) == []

    grown = collision.build_clusters(
        [_pin(i, 50.0, 14.0, street_key=f"s{i}") for i in range(1, 9)], POLICY
    )
    assert collision.changed_listings(before, grown, POLICY) == list(range(1, 9))


def test_a_cell_that_disappears_re_resolves_its_old_members():
    before = collision.build_clusters(
        [_pin(i, 50.0, 14.0, street_key="a") for i in (1, 2)], POLICY
    )
    assert collision.changed_listings(before, [], POLICY) == [1, 2]


def test_every_classification_is_in_the_six_value_vocabulary():
    pins = [_pin(i, 50.0, 14.0, street_key=f"s{i}") for i in range(1, 10)]
    for cluster in collision.build_clusters(pins, POLICY):
        assert cluster.classification in collision.CLASSIFICATIONS


def test_clusters_are_per_portal_because_portals_collapse_differently():
    pins = [
        _pin(1, 50.0, 14.0, source="bazos", street_key="a"),
        _pin(2, 50.0, 14.0, source="idnes", street_key="a"),
    ]
    clusters = collision.build_clusters(pins, POLICY)
    assert {c.source for c in clusters} == {"bazos", "idnes"}
    assert all(c.listing_count == 1 for c in clusters)
