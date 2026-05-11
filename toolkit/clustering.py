"""cluster_comparables: stdlib k-means on a cohort to surface sub-markets.

Pure function. No DB connection. Works on a list of dicts shaped like
the rows returned by find_comparables. Z-score normalises each axis so
multi-axis runs aren't dominated by the variable with the largest
absolute range, runs Lloyd's algorithm n_restarts times with
deterministic seeds, picks the lowest-inertia result, then de-normalises
centroids back to original units before returning.
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Any, Literal

_AXIS = Literal["price_per_m2", "price_czk", "area_m2", "distance_m"]

_MAX_K = 8


def cluster_comparables(
    listings: list[dict[str, Any]],
    axes: list[_AXIS] | None = None,
    n_clusters: int = 3,
    seed: int = 42,
    max_iterations: int = 100,
    n_restarts: int = 5,
) -> dict[str, Any]:
    from toolkit import _max_last_seen, _now_iso

    if axes is None:
        axes = ["price_per_m2"]
    notes: list[str] = []

    viable: list[tuple[dict[str, Any], list[float]]] = []
    dropped_for_missing_axis = 0
    for l in listings:
        vec: list[float] | None = []
        for a in axes:
            v = l.get(a)
            if v is None:
                vec = None
                break
            vec.append(float(v))
        if vec is None:
            dropped_for_missing_axis += 1
            continue
        viable.append((l, vec))

    if dropped_for_missing_axis:
        notes.append(
            f"dropped {dropped_for_missing_axis} listing(s) missing one of {axes}"
        )

    n_clusters_requested = n_clusters
    k = min(max(n_clusters, 1), len(viable), _MAX_K)
    if k != n_clusters_requested:
        notes.append(
            f"k clamped from {n_clusters_requested} to {k} "
            f"({len(viable)} viable listing(s))"
        )

    clusters_out: list[dict[str, Any]] = []
    inertia_out: float | None = None

    if viable and k > 0:
        means, stdevs = _axis_moments(viable, len(axes))
        normalised = [
            [_z(vec[i], means[i], stdevs[i]) for i in range(len(axes))]
            for _, vec in viable
        ]
        labels, centroids_norm, inertia = _best_kmeans(
            normalised, k, seed, max_iterations, n_restarts,
        )
        clusters_out = _build_clusters(
            viable, labels, centroids_norm, means, stdevs, axes,
        )
        inertia_out = inertia

    return {
        "data": {
            "n_clusters": k if viable else 0,
            "axes": list(axes),
            "inertia": inertia_out,
            "clusters": clusters_out,
        },
        "metadata": {
            "tool": "cluster_comparables",
            "filters_used": {
                "axes": list(axes),
                "n_clusters": n_clusters_requested,
                "seed": seed,
                "n_restarts": n_restarts,
            },
            "result_count": len(clusters_out),
            "queried_at": _now_iso(),
            "data_freshness": _max_last_seen(listings),
            "input_size": len(listings),
            "dropped_for_missing_axis": dropped_for_missing_axis,
            "notes": notes,
        },
    }


def _axis_moments(
    viable: list[tuple[dict[str, Any], list[float]]], n_axes: int,
) -> tuple[list[float], list[float]]:
    means = [0.0] * n_axes
    stdevs = [0.0] * n_axes
    for i in range(n_axes):
        col = [vec[i] for _, vec in viable]
        means[i] = statistics.fmean(col)
        stdevs[i] = statistics.pstdev(col) if len(col) > 1 else 0.0
    return means, stdevs


def _z(x: float, mean: float, stdev: float) -> float:
    if stdev == 0.0:
        return 0.0
    return (x - mean) / stdev


def _best_kmeans(
    points: list[list[float]],
    k: int,
    seed: int,
    max_iterations: int,
    n_restarts: int,
) -> tuple[list[int], list[list[float]], float]:
    best: tuple[list[int], list[list[float]], float] | None = None
    for r in range(n_restarts):
        rng = random.Random(seed + r)
        labels, centroids, inertia = _kmeans_once(
            points, k, rng, max_iterations,
        )
        if best is None or inertia < best[2]:
            best = (labels, centroids, inertia)
    assert best is not None
    return best


def _kmeans_once(
    points: list[list[float]],
    k: int,
    rng: random.Random,
    max_iterations: int,
) -> tuple[list[int], list[list[float]], float]:
    n = len(points)
    d = len(points[0])
    initial_idx = rng.sample(range(n), k)
    centroids = [list(points[i]) for i in initial_idx]
    labels = [0] * n

    for _ in range(max_iterations):
        changed = False
        for i, p in enumerate(points):
            best_c = 0
            best_dist = _sq_dist(p, centroids[0])
            for c in range(1, k):
                dist = _sq_dist(p, centroids[c])
                if dist < best_dist:
                    best_dist = dist
                    best_c = c
            if labels[i] != best_c:
                labels[i] = best_c
                changed = True

        new_centroids: list[list[float]] = []
        for c in range(k):
            members = [points[i] for i in range(n) if labels[i] == c]
            if not members:
                new_centroids.append(list(points[rng.randrange(n)]))
                changed = True
                continue
            new_centroids.append(
                [sum(p[j] for p in members) / len(members) for j in range(d)]
            )
        centroids = new_centroids

        if not changed:
            break

    inertia = sum(
        _sq_dist(points[i], centroids[labels[i]]) for i in range(n)
    )
    return labels, centroids, inertia


def _sq_dist(a: list[float], b: list[float]) -> float:
    return sum((a[i] - b[i]) ** 2 for i in range(len(a)))


def _build_clusters(
    viable: list[tuple[dict[str, Any], list[float]]],
    labels: list[int],
    centroids_norm: list[list[float]],
    means: list[float],
    stdevs: list[float],
    axes: list[str],
) -> list[dict[str, Any]]:
    k = len(centroids_norm)
    clusters: list[dict[str, Any]] = []
    for c in range(k):
        members = [
            (viable[i][0], viable[i][1])
            for i in range(len(viable))
            if labels[i] == c
        ]
        centroid_original = {
            axes[j]: centroids_norm[c][j] * stdevs[j] + means[j]
            for j in range(len(axes))
        }
        stats_by_axis: dict[str, dict[str, float] | None] = {}
        for j, a in enumerate(axes):
            vals = [vec[j] for _, vec in members]
            stats_by_axis[a] = _axis_stats(vals)
        clusters.append({
            "cluster_id": c,
            "size": len(members),
            "centroid": centroid_original,
            "sreality_ids": [
                row.get("sreality_id") for row, _ in members
            ],
            "statistics": stats_by_axis,
        })
    clusters.sort(key=lambda c: -c["size"])
    for new_id, cluster in enumerate(clusters):
        cluster["cluster_id"] = new_id
    return clusters


def _axis_stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    sorted_values = sorted(values)
    return {
        "min": sorted_values[0],
        "median": statistics.median(sorted_values),
        "mean": statistics.fmean(sorted_values),
        "max": sorted_values[-1],
    }
