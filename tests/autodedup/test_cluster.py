"""Constrained union-find (PROGRAM.md §8): merged-set validation, identity, conflict rows."""

from __future__ import annotations

from typing import Any

from autodedup.cluster import ClusterResult, cluster_pairs, cluster_rows
from autodedup.dataset import Listing, Location
from autodedup.decide import Decision
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.settings import Settings

SETTINGS = Settings()


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
        location=Location(obec_kod=1),
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


def _world(*listings: Listing) -> tuple[dict[int, Listing], dict[int, Fingerprint]]:
    rows = {listing.id: listing for listing in listings}
    return rows, {
        listing.id: build_fingerprint(listing, [], SETTINGS) for listing in listings
    }


def _edge(lo: int, hi: int, score: float, zone: str = "merge",
          certificate: str | None = None) -> Decision:
    return Decision(lo, hi, zone, score, {"LOC", "ATTR"}, certificate, None, "model")


def test_a_chain_of_edges_becomes_one_cluster_keyed_by_the_smallest_id() -> None:
    listings, fps = _world(_listing(7), _listing(3), _listing(9))
    result = cluster_pairs([_edge(3, 7, 0.99), _edge(7, 9, 0.98)], listings, fps, SETTINGS)
    assert result.clusters == {3: [3, 7, 9]}
    assert result.cluster_of(9) == 3
    assert result.stats["n_edges_applied"] == 2
    assert result.stats["n_clusters"] == 1
    assert result.stats["size_histogram"] == {"3": 1}


def test_only_merge_edges_cluster() -> None:
    listings, fps = _world(_listing(1), _listing(2), _listing(3))
    edges = [_edge(1, 2, 0.99), _edge(2, 3, 0.80, zone="band"),
             _edge(1, 3, 0.10, zone="reject")]
    result = cluster_pairs(edges, listings, fps, SETTINGS)
    assert result.clusters == {1: [1, 2]}
    assert result.stats["n_merge_edges"] == 1


def test_a_union_is_validated_on_the_merged_set_not_the_edge() -> None:
    """Two 4% steps are each inside the guard, but the cluster they build spans 8%+ (E33/E34)."""
    listings, fps = _world(
        _listing(1, area_m2=60.0), _listing(2, area_m2=62.5), _listing(3, area_m2=65.5)
    )
    result = cluster_pairs([_edge(1, 2, 0.99), _edge(2, 3, 0.98)], listings, fps, SETTINGS)
    assert result.clusters == {1: [1, 2]}
    assert [conflict["invariant"] for conflict in result.conflicts] == ["area_spread"]
    conflict = result.conflicts[0]
    assert conflict["lo"] == 2 and conflict["hi"] == 3
    assert conflict["members"] == [1, 2, 3]
    assert result.stats["conflicts_by_invariant"] == {"area_spread": 1}
    assert result.stats["n_edges_refused"] == 1


def test_edges_are_applied_in_descending_score() -> None:
    """The weaker edge is the one refused, whichever order it arrives in."""
    listings, fps = _world(
        _listing(1, area_m2=60.0), _listing(2, area_m2=62.5), _listing(3, area_m2=65.5)
    )
    edges = [_edge(2, 3, 0.91), _edge(1, 2, 0.99)]
    result = cluster_pairs(edges, listings, fps, SETTINGS)
    assert result.clusters == {1: [1, 2]}
    assert (result.conflicts[0]["lo"], result.conflicts[0]["hi"]) == (2, 3)

    stronger = cluster_pairs([_edge(2, 3, 0.99), _edge(1, 2, 0.91)], listings, fps, SETTINGS)
    assert stronger.clusters == {2: [2, 3]}
    assert (stronger.conflicts[0]["lo"], stronger.conflicts[0]["hi"]) == (1, 2)


def test_category_and_disposition_invariants_refuse_a_union() -> None:
    listings, fps = _world(_listing(1), _listing(2), _listing(3, disposition="2+kk"))
    result = cluster_pairs([_edge(1, 2, 0.99), _edge(2, 3, 0.98)], listings, fps, SETTINGS)
    assert result.clusters == {1: [1, 2]}
    assert result.conflicts[0]["invariant"] == "disposition"


def test_max_cluster_size_latches() -> None:
    settings = Settings(max_cluster_size=3)
    listings, fps = _world(*[_listing(index) for index in range(1, 6)])
    edges = [_edge(1, 2, 0.99), _edge(2, 3, 0.98), _edge(3, 4, 0.97), _edge(4, 5, 0.96)]
    result = cluster_pairs(edges, listings, fps, settings)
    assert result.clusters == {1: [1, 2, 3], 4: [4, 5]}
    assert result.stats["conflicts_by_invariant"] == {"size": 1}
    assert result.stats["max_size"] == 3


def test_must_not_link_is_honoured_inside_the_merged_set() -> None:
    listings, fps = _world(_listing(1), _listing(2), _listing(3))
    edges = [_edge(1, 2, 0.99), _edge(2, 3, 0.98)]
    result = cluster_pairs(edges, listings, fps, SETTINGS, must_not_link={(1, 3)})
    assert result.clusters == {1: [1, 2]}
    assert result.conflicts[0]["invariant"] == "must_not_link"


def test_a_redundant_edge_is_counted_not_reapplied() -> None:
    listings, fps = _world(_listing(1), _listing(2), _listing(3))
    edges = [_edge(1, 2, 0.99), _edge(2, 3, 0.98), _edge(1, 3, 0.97)]
    result = cluster_pairs(edges, listings, fps, SETTINGS)
    assert result.clusters == {1: [1, 2, 3]}
    assert result.stats["n_edges_applied"] == 2
    assert result.stats["n_edges_redundant"] == 1


def test_singletons_are_not_clusters() -> None:
    listings, fps = _world(_listing(1), _listing(2))
    result = cluster_pairs([_edge(1, 2, 0.5, zone="band")], listings, fps, SETTINGS)
    assert result.clusters == {}
    assert result.stats["n_clustered_listings"] == 0
    assert result.stats["mean_size"] == 0.0


def test_cluster_rows_report_the_weakest_accepted_edge() -> None:
    listings, fps = _world(
        _listing(1, source="sreality", area_m2=60.0),
        _listing(2, source="bazos", area_m2=61.0),
        _listing(3, source="idnes", area_m2=62.0),
    )
    edges = [_edge(1, 2, 0.99, certificate="K-A"), _edge(2, 3, 0.975)]
    result = cluster_pairs(edges, listings, fps, SETTINGS)
    rows = cluster_rows(result, edges, fps)
    assert len(rows) == 1
    row = rows[0]
    assert row["cluster_key"] == 1
    assert row["members"] == [1, 2, 3]
    assert row["sources"] == ["bazos", "idnes", "sreality"]
    assert row["min_edge_score"] == 0.975
    assert row["n_certificate_edges"] == 1
    assert row["area_min"] == 60.0 and row["area_max"] == 62.0
    assert row["evidence_families"] == ["ATTR", "LOC"]
    assert row["shared_photo_warning"] is False


def test_empty_input_is_an_empty_result() -> None:
    listings, fps = _world(_listing(1))
    result = cluster_pairs([], listings, fps, SETTINGS)
    assert isinstance(result, ClusterResult)
    assert result.clusters == {} and result.conflicts == []
    assert result.stats["n_clusters"] == 0
    assert cluster_rows(result, [], fps) == []


def test_a_bridge_between_two_existing_clusters_is_recorded_not_applied() -> None:
    """E37: nothing autonomous may join two multi-member clusters."""
    listings, fps = _world(*[_listing(index) for index in range(1, 5)])
    edges = [_edge(1, 2, 0.99), _edge(3, 4, 0.98), _edge(2, 3, 0.97)]
    result = cluster_pairs(edges, listings, fps, SETTINGS)
    assert result.clusters == {1: [1, 2], 3: [3, 4]}
    assert result.conflicts == []
    assert result.stats["n_edges_applied"] == 2
    assert result.stats["n_bridges_refused"] == 1
    bridge = result.bridges[0]
    assert (bridge["lo"], bridge["hi"]) == (2, 3)
    assert bridge["left_members"] == [1, 2] and bridge["right_members"] == [3, 4]
    assert bridge["left_cluster"] == 1 and bridge["right_cluster"] == 3


def test_growing_one_cluster_by_a_singleton_is_not_a_bridge() -> None:
    listings, fps = _world(*[_listing(index) for index in range(1, 5)])
    edges = [_edge(1, 2, 0.99), _edge(2, 3, 0.98), _edge(3, 4, 0.97)]
    result = cluster_pairs(edges, listings, fps, SETTINGS)
    assert result.clusters == {1: [1, 2, 3, 4]}
    assert result.stats["n_bridges_refused"] == 0


def test_a_certificate_edge_outranks_a_higher_scoring_model_edge() -> None:
    """A structural certificate is not a probability: when an invariant binds, it wins (§6)."""
    settings = Settings(max_cluster_size=2)
    listings, fps = _world(_listing(1), _listing(2), _listing(3))
    edges = [_edge(1, 2, 0.10, certificate="K-A"), _edge(1, 3, 0.95)]
    result = cluster_pairs(edges, listings, fps, settings)
    assert result.clusters == {1: [1, 2]}
    assert [(row["lo"], row["hi"]) for row in result.conflicts] == [(1, 3)]
    assert result.conflicts[0]["invariant"] == "size"


def test_must_not_link_count_is_reported() -> None:
    listings, fps = _world(_listing(1), _listing(2))
    result = cluster_pairs([_edge(1, 2, 0.99)], listings, fps, SETTINGS,
                           must_not_link={(1, 2), (1, 3)})
    assert result.clusters == {}
    assert result.stats["n_must_not_link"] == 2


# --- E57: the refused bridge, re-offered once -------------------------------------------


def _bridged_world() -> tuple[dict[int, Listing], dict[int, Fingerprint], list[Decision]]:
    """{1,2} and {3,4} cluster first; the 2x3 edge then arrives as a bridge."""
    listings, fps = _world(_listing(1), _listing(2), _listing(3), _listing(4))
    edges = [_edge(1, 2, 1.0), _edge(3, 4, 0.9999), _edge(2, 3, 0.999)]
    return listings, fps, edges


def test_a_bridge_is_still_refused_by_default() -> None:
    listings, fps, edges = _bridged_world()
    result = cluster_pairs(edges, listings, fps, SETTINGS)
    assert sorted(result.clusters.values()) == [[1, 2], [3, 4]]
    assert result.stats["n_bridges_refused"] == 1
    assert result.stats["n_bridges_applied"] == 0
    assert result.bridges[0]["applied"] is False


def test_a_bridge_is_applied_when_the_merged_set_holds() -> None:
    listings, fps, edges = _bridged_world()
    settings = Settings(bridge_apply=True)
    result = cluster_pairs(edges, listings, fps, settings)
    assert result.clusters == {1: [1, 2, 3, 4]}
    assert result.stats["n_bridges_applied"] == 1
    assert result.stats["n_bridges_refused"] == 0
    assert result.bridges[0]["applied"] is True


def test_a_bridge_whose_merged_set_breaks_an_invariant_is_not_applied() -> None:
    """The four adverts are two floors apart: floor_spread refuses the union, not the edge."""
    listings, fps = _world(
        _listing(1), _listing(2), _listing(3, floor=4), _listing(4, floor=4)
    )
    edges = [_edge(1, 2, 1.0), _edge(3, 4, 0.9999), _edge(2, 3, 0.999)]
    result = cluster_pairs(edges, listings, fps, Settings(bridge_apply=True))
    assert sorted(result.clusters.values()) == [[1, 2], [3, 4]]
    assert result.stats["n_bridges_applied"] == 0
    assert result.bridges[0]["invariant"] == "floor_spread"


def test_a_must_not_link_inside_the_merged_set_refuses_the_bridge() -> None:
    listings, fps, edges = _bridged_world()
    result = cluster_pairs(
        edges, listings, fps, Settings(bridge_apply=True), frozenset({(1, 4)})
    )
    assert sorted(result.clusters.values()) == [[1, 2], [3, 4]]
    assert result.bridges[0]["invariant"] == "must_not_link"


def test_a_weak_bridge_needs_a_certificate() -> None:
    listings, fps = _world(_listing(1), _listing(2), _listing(3), _listing(4))
    weak = [_edge(1, 2, 1.0), _edge(3, 4, 0.9999), _edge(2, 3, 0.98)]
    settings = Settings(bridge_apply=True)
    refused = cluster_pairs(weak, listings, fps, settings)
    assert refused.stats["n_bridges_applied"] == 0
    assert refused.bridges[0]["invariant"] == "bridge_score"

    # Certificates rank first, so the two sides must also be certified for the weak K-B edge
    # to arrive as a bridge at all.
    certified = [_edge(1, 2, 1.0, certificate="K-B"),
                 _edge(3, 4, 0.9999, certificate="K-B"),
                 _edge(2, 3, 0.98, certificate="K-B")]
    result = cluster_pairs(certified, listings, fps, settings)
    assert result.clusters == {1: [1, 2, 3, 4]}
    assert result.stats["n_bridges_applied"] == 1


def test_a_bridge_that_a_previous_bridge_made_redundant_is_recorded_as_such() -> None:
    listings, fps = _world(_listing(1), _listing(2), _listing(3), _listing(4))
    edges = [_edge(1, 2, 1.0), _edge(3, 4, 0.9999),
             _edge(2, 3, 0.9995), _edge(1, 4, 0.999)]
    result = cluster_pairs(edges, listings, fps, Settings(bridge_apply=True))
    assert result.clusters == {1: [1, 2, 3, 4]}
    assert result.stats["n_bridges_applied"] == 1
    assert [bridge["invariant"] for bridge in result.bridges
            if not bridge["applied"]] == ["redundant"]
