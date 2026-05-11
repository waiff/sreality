"""Pure-function tests for cluster_comparables."""

from __future__ import annotations

from typing import Any

from toolkit.clustering import cluster_comparables


def _listing(sid: int, **fields: Any) -> dict[str, Any]:
    return {"sreality_id": sid, **fields}


def test_empty_input_returns_no_clusters():
    res = cluster_comparables([], n_clusters=3)
    assert res["data"]["clusters"] == []
    assert res["data"]["n_clusters"] == 0
    assert res["data"]["inertia"] is None
    assert res["metadata"]["tool"] == "cluster_comparables"
    assert res["metadata"]["result_count"] == 0
    assert res["metadata"]["input_size"] == 0


def test_envelope_metadata_shape():
    listings = [_listing(i, price_per_m2=100.0 + i) for i in range(5)]
    res = cluster_comparables(listings, n_clusters=2)
    md = res["metadata"]
    assert md["tool"] == "cluster_comparables"
    assert md["filters_used"]["axes"] == ["price_per_m2"]
    assert md["filters_used"]["n_clusters"] == 2
    assert md["filters_used"]["seed"] == 42
    assert md["filters_used"]["n_restarts"] == 5
    assert "queried_at" in md
    assert "data_freshness" in md
    assert md["input_size"] == 5
    assert md["dropped_for_missing_axis"] == 0


def test_two_well_separated_groups_recovered_on_one_axis():
    low = [_listing(i, price_per_m2=100.0 + i * 0.1) for i in range(10)]
    high = [_listing(i + 100, price_per_m2=500.0 + i * 0.1) for i in range(10)]
    res = cluster_comparables(low + high, n_clusters=2, seed=7)
    clusters = res["data"]["clusters"]
    assert len(clusters) == 2
    sizes = sorted(c["size"] for c in clusters)
    assert sizes == [10, 10]
    centroids = sorted(c["centroid"]["price_per_m2"] for c in clusters)
    assert centroids[0] < 200 < centroids[1]


def test_determinism_same_seed_yields_same_assignments():
    listings = [
        _listing(i, price_per_m2=v)
        for i, v in enumerate([100.0, 105.0, 110.0, 500.0, 505.0, 510.0])
    ]
    a = cluster_comparables(listings, n_clusters=2, seed=99)
    b = cluster_comparables(listings, n_clusters=2, seed=99)
    ids_a = [sorted(c["sreality_ids"]) for c in a["data"]["clusters"]]
    ids_b = [sorted(c["sreality_ids"]) for c in b["data"]["clusters"]]
    assert sorted(ids_a) == sorted(ids_b)
    assert a["data"]["inertia"] == b["data"]["inertia"]


def test_missing_axis_listings_dropped_with_note():
    listings = [
        _listing(1, price_per_m2=100.0),
        _listing(2, price_per_m2=None),
        _listing(3, price_per_m2=200.0),
    ]
    res = cluster_comparables(listings, n_clusters=2)
    assert res["metadata"]["dropped_for_missing_axis"] == 1
    assert any(
        "missing" in n for n in res["metadata"]["notes"]
    )
    total_assigned = sum(c["size"] for c in res["data"]["clusters"])
    assert total_assigned == 2


def test_k_clamped_to_viable_count():
    listings = [_listing(i, price_per_m2=100.0 * i) for i in range(1, 3)]
    res = cluster_comparables(listings, n_clusters=5, seed=1)
    assert res["data"]["n_clusters"] == 2
    assert any("clamped" in n for n in res["metadata"]["notes"])


def test_all_identical_values_single_cluster_zero_inertia():
    listings = [_listing(i, price_per_m2=300.0) for i in range(8)]
    res = cluster_comparables(listings, n_clusters=3, seed=1)
    assert res["data"]["inertia"] == 0.0
    non_empty = [c for c in res["data"]["clusters"] if c["size"] > 0]
    assert len(non_empty) >= 1
    for c in non_empty:
        assert c["centroid"]["price_per_m2"] == 300.0


def test_multi_axis_normalisation_prevents_scale_domination():
    listings = []
    for i in range(8):
        listings.append(_listing(
            i,
            price_per_m2=100.0 + i,
            area_m2=30.0 + i * 0.5,
        ))
    for i in range(8):
        listings.append(_listing(
            i + 100,
            price_per_m2=500.0 + i,
            area_m2=70.0 + i * 0.5,
        ))
    res = cluster_comparables(
        listings,
        axes=["price_per_m2", "area_m2"],
        n_clusters=2,
        seed=3,
    )
    clusters = res["data"]["clusters"]
    assert len(clusters) == 2
    assert sorted(c["size"] for c in clusters) == [8, 8]


def test_cluster_statistics_present_for_every_axis():
    listings = [
        _listing(i, price_per_m2=100.0 + i, area_m2=50.0 + i)
        for i in range(6)
    ]
    res = cluster_comparables(
        listings, axes=["price_per_m2", "area_m2"], n_clusters=2, seed=5,
    )
    for c in res["data"]["clusters"]:
        if c["size"] == 0:
            continue
        stats = c["statistics"]
        assert set(stats.keys()) == {"price_per_m2", "area_m2"}
        for axis_stats in stats.values():
            assert set(axis_stats.keys()) == {"min", "median", "mean", "max"}


def test_clusters_sorted_by_size_desc_and_renumbered():
    listings = (
        [_listing(i, price_per_m2=100.0) for i in range(2)]
        + [_listing(i + 100, price_per_m2=500.0) for i in range(7)]
    )
    res = cluster_comparables(listings, n_clusters=2, seed=11)
    clusters = res["data"]["clusters"]
    assert [c["cluster_id"] for c in clusters] == [0, 1]
    assert clusters[0]["size"] >= clusters[1]["size"]


def test_data_freshness_aggregated_from_listings():
    listings = [
        _listing(1, price_per_m2=100.0, last_seen_at="2026-05-01T00:00:00+00:00"),
        _listing(2, price_per_m2=110.0, last_seen_at="2026-05-05T00:00:00+00:00"),
    ]
    res = cluster_comparables(listings, n_clusters=2, seed=1)
    assert res["metadata"]["data_freshness"].startswith("2026-05-05")
