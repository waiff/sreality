"""S8 listing grain — the projection row the serving layer reads (01 §7.1, 00 §7).

Every derived value on the row is builder-written from `resolver.derived`, and two of them
carry rules that are easy to get quietly wrong: `geo_cell_key` is written ONLY when
`geo_blockable` (that is what keeps the granularity rung out of an IMMUTABLE expression),
and `location_disputed` is a builder-derived CACHE of the ledger — the reconciler never
writes the projection.
"""

from __future__ import annotations

from location_data.resolver import core, projection
from location_data.resolver.types import ClusterEvidence, GranularityRank
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

RANK = GranularityRank()


def _resolution(claims, collision=None):
    return core.resolve(
        claims, mm.context(collision=collision), resolver_version=RESOLVER_VERSION,
        registry_version_id=7, policy_version="v1", collision_epoch_id=11,
    )


def _row(claims, *, cluster=None, threshold_n=4, disputed=False):
    # The cluster the BUILDER sees is the same one the RESOLVER saw at its stamped epoch —
    # feeding one without the other would test a state the pipeline cannot produce.
    collision = (
        mm.StaticCollision({(cluster.source, cluster.cell_key): cluster}) if cluster else None
    )
    resolution = _resolution(claims, collision)
    return projection.build_listing_row(
        resolution,
        property_id=None,
        resolution_id=1,
        registry_version_label="ruian:2026-07-31",
        rank=RANK,
        cluster=cluster,
        threshold_n=threshold_n,
        location_disputed=disputed,
    )


def _address_claims():
    return [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "coordinate", lat=50.10102, lon=14.34804, declared_precision_label="gps"),
        mm.claim(4, "psc", value_text="160 00"),
    ]


def test_the_four_axes_travel_with_the_coordinate_and_are_never_null():
    row = _row(_address_claims())
    for column in (
        "granularity", "position_source", "blur_evidence", "match_confidence",
        "uncertainty_radius_m", "radius_semantics", "position_licence_class",
        "country_status", "admin_assignment_method", "admin_position_source",
    ):
        assert row[column] is not None, column


def test_the_blocking_keys_are_written_from_the_named_functions():
    row = _row(_address_claims())
    assert row["addr_block_key"] == "a:21690278"
    assert row["building_block_key"] == "b:555001"
    assert row["street_block_key"] == "554782:nad borislavkou:487"


def test_geo_cell_key_is_written_only_when_geo_blockable():
    blockable = _row(_address_claims())
    assert blockable["geo_blockable"] is True
    assert blockable["geo_cell_key"] == "c:50.1010:14.3480"

    collapsed = _row(
        _address_claims(),
        cluster=ClusterEvidence(
            cluster_id=5, source="sreality", cell_key="c:50.1010:14.3480", listing_count=40,
            distinct_streets=9, distinct_obec_kods=1, classification="town_centroid_suspect",
        ),
    )
    assert collapsed["geo_blockable"] is False
    assert collapsed["geo_cell_key"] is None


def test_a_disputed_row_is_never_renderable_as_a_point():
    row = _row(_address_claims(), disputed=True)
    assert row["location_disputed"] is True
    assert row["renderable_as_point"] is False
    assert row["render_as"] != "point"


def test_pin_collision_class_defaults_to_normal_and_is_never_null():
    row = _row(_address_claims())
    assert row["pin_collision_class"] == "normal"
    assert row["cluster_heterogeneity_ok"] is True
    assert row["pin_shared_by_n"] == 1


def test_the_display_label_never_folds_in_the_postal_town():
    """For bazos neither value is wrong — they answer different questions — and 57.0 % of
    rows disagree between the two."""
    claims = _address_claims() + [
        mm.claim(5, "postal_town", value_text="Hodonín", source="bazos"),
    ]
    row = _row(claims)
    assert row["postal_town"] == "Hodonín"
    assert "Hodonín" not in row["display_label"]


def test_the_row_carries_its_whole_version_tuple():
    row = _row(_address_claims())
    assert row["registry_version"] == "ruian:2026-07-31"
    assert row["registry_version_id"] == 7
    assert row["resolver_version"] == RESOLVER_VERSION
    assert row["policy_version"] == "v1"


def test_field_provenance_names_the_claims_behind_every_winning_field():
    row = _row(_address_claims())
    assert row["field_provenance"]["street_name"]["claim_ids"] == [2]
    assert row["street_claim_id"] == 2


def test_a_declared_gps_pin_with_no_street_is_coarse_but_still_precise_positionally():
    """03 §3.16.2: `granularity='obec'` AND `position_quality_class='precise'` — the axes
    are independent — while `geo_blockable` and `renderable_as_point` stay FALSE because
    the canonical predicates keep their granularity rung."""
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378, declared_precision_label="gps"),
    ]
    row = _row(claims)
    assert row["granularity"] == "obec"
    assert row["position_quality_class"] == "precise"
    assert row["geo_blockable"] is False
    assert row["renderable_as_point"] is False
    assert row["is_low_precision"] is True


def test_a_bazos_town_pin_is_excluded_by_class_at_every_granularity():
    cluster = ClusterEvidence(
        cluster_id=9, source="bazos", cell_key="c:50.1010:14.3480", listing_count=276,
        distinct_streets=99, distinct_obec_kods=1, classification="town_centroid_suspect",
    )
    claims = [
        mm.claim(1, "obec_name", value_text="Praha", source="bazos"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40", source="bazos"),
        mm.claim(3, "coordinate", lat=50.10102, lon=14.34804, source="bazos"),
    ]
    row = _row(claims, cluster=cluster)
    assert row["pin_collision_class"] == "town_centroid_suspect"
    assert row["geo_blockable"] is False
    assert row["renderable_as_point"] is False
    assert row["position_quality_class"] == "area"
