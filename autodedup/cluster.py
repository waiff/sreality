"""Constrained union-find over the merge edges (PROGRAM.md §8; E33, E34, E36, E37).

Edges are processed certificate-first and then in descending score, so the clustering is
reproducible from the edge set alone, and **every union is validated on the merged member
set** rather than on the edge: one good-looking edge inside a development must not be able to
pull the whole project together. A certificate outranks a learned probability because its
precision is structural, not learned (§6) — when an invariant binds, the structurally certain
edge is the one that survives.

A refused union is not discarded — it becomes a conflict row naming the invariant that
refused it, which is the highest-value item in the validation UI because the engine found
strong evidence in both directions.

E37: an edge whose two sides are BOTH already multi-member clusters is a bridge, and bridging
is outside the autonomous envelope — one wrong bridge corrupts two properties at once. The
bridge is recorded on `ClusterResult.bridges` and surfaced in the stats; nothing is applied.

Cluster identity is the smallest listing id it ever admitted (E36), which is also the union
representative, so identity never moves as a cluster grows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from autodedup.dataset import Listing
from autodedup.decide import Decision
from autodedup.fingerprint import Fingerprint
from autodedup.guards import cluster_invariants_ok
from autodedup.settings import Settings


@dataclass(slots=True)
class ClusterResult:
    clusters: dict[int, list[int]]
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    bridges: list[dict[str, Any]] = field(default_factory=list)

    def cluster_of(self, listing_id: int) -> int | None:
        for key, members in self.clusters.items():
            if listing_id in members:
                return key
        return None


class _UnionFind:
    """Union by smallest id, so the representative IS the cluster identity (E36).

    Member lists are carried on the roots: validating a union must cost the size of the two
    clusters, not a scan of every node seen so far."""

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}
        self.groups: dict[int, list[int]] = {}

    def add(self, item: int) -> int:
        if item not in self.parent:
            self.parent[item] = item
            self.groups[item] = [item]
        return self.find(item)

    def find(self, item: int) -> int:
        parent = self.parent
        if item not in parent:
            return self.add(item)
        root = parent[item]
        while root != parent[root]:
            parent[root] = parent[parent[root]]
            root = parent[root]
        parent[item] = root
        return root

    def members(self, root: int) -> list[int]:
        return sorted(self.groups.get(root, ()))

    def union(self, left: int, right: int) -> int:
        root_left, root_right = self.find(left), self.find(right)
        if root_left == root_right:
            return root_left
        keeper, loser = (root_left, root_right) if root_left < root_right else (root_right, root_left)
        self.parent[loser] = keeper
        self.groups[keeper].extend(self.groups.pop(loser))
        return keeper


def edge_rank(decision: Decision) -> tuple[int, float, int, int]:
    """Certificates first (structural precision), then descending score, then ids (E33)."""
    return (0 if decision.certificate else 1, -decision.score, decision.lo, decision.hi)


def cluster_pairs(
    decisions: Sequence[Decision],
    listings: Mapping[int, Listing],
    fps: Mapping[int, Fingerprint],
    settings: Settings,
    must_not_link: frozenset[tuple[int, int]] | set[tuple[int, int]] = frozenset(),
) -> ClusterResult:
    """Merge edges -> validated clusters, one conflict row per refused union, bridges recorded."""
    edges = sorted(
        (decision for decision in decisions if decision.zone == "merge"), key=edge_rank
    )
    union_find = _UnionFind()
    grouped: dict[int, list[int]] = {}
    for decision in edges:
        union_find.add(decision.lo)
        union_find.add(decision.hi)

    conflicts: list[dict[str, Any]] = []
    bridges: list[dict[str, Any]] = []
    accepted: list[Decision] = []
    redundant = 0
    for decision in edges:
        root_lo = union_find.find(decision.lo)
        root_hi = union_find.find(decision.hi)
        if root_lo == root_hi:
            redundant += 1
            accepted.append(decision)
            continue
        left = union_find.members(root_lo)
        right = union_find.members(root_hi)
        if len(left) > 1 and len(right) > 1:
            bridges.append({
                "lo": decision.lo,
                "hi": decision.hi,
                "score": decision.score,
                "certificate": decision.certificate,
                "families": sorted(decision.families),
                "left_cluster": root_lo,
                "right_cluster": root_hi,
                "left_members": left,
                "right_members": right,
            })
            continue
        members = left + right
        fingerprints = [fps[listing_id] for listing_id in members if listing_id in fps]
        invariant = cluster_invariants_ok(fingerprints, settings, must_not_link)
        if invariant is not None:
            conflicts.append({
                "lo": decision.lo,
                "hi": decision.hi,
                "score": decision.score,
                "certificate": decision.certificate,
                "invariant": invariant,
                "members": sorted(members),
                "families": sorted(decision.families),
            })
            continue
        union_find.union(root_lo, root_hi)
        accepted.append(decision)

    for root, members in union_find.groups.items():
        grouped[root] = sorted(members)
    clusters = {key: members for key, members in sorted(grouped.items()) if len(members) > 1}

    sizes = [len(members) for members in clusters.values()]
    histogram: dict[str, int] = {}
    for size in sizes:
        histogram[str(size)] = histogram.get(str(size), 0) + 1
    by_invariant: dict[str, int] = {}
    for conflict in conflicts:
        name = str(conflict["invariant"])
        by_invariant[name] = by_invariant.get(name, 0) + 1
    stats: dict[str, Any] = {
        "n_merge_edges": len(edges),
        "n_edges_applied": len(accepted) - redundant,
        "n_edges_redundant": redundant,
        "n_edges_refused": len(conflicts),
        "n_bridges_refused": len(bridges),
        "n_must_not_link": len(must_not_link),
        "n_clusters": len(clusters),
        "n_clustered_listings": sum(sizes),
        "size_histogram": dict(sorted(histogram.items(), key=lambda kv: int(kv[0]))),
        "max_size": max(sizes) if sizes else 0,
        "mean_size": (sum(sizes) / len(sizes)) if sizes else 0.0,
        "conflicts_by_invariant": dict(sorted(by_invariant.items())),
    }
    return ClusterResult(
        clusters=clusters, conflicts=conflicts, stats=stats, bridges=bridges
    )


def cluster_rows(
    result: ClusterResult,
    decisions: Iterable[Decision],
    fps: Mapping[int, Fingerprint],
) -> list[dict[str, Any]]:
    """One reportable row per cluster — the local twin of `autodedup.clusters` (§8).

    `min_edge_score` is the UI's default sort, because the weakest accepted edge is where the
    errors live."""
    membership: dict[int, int] = {}
    for key, members in result.clusters.items():
        for listing_id in members:
            membership[listing_id] = key
    edges: dict[int, list[Decision]] = {}
    for decision in decisions:
        if decision.zone != "merge":
            continue
        key = membership.get(decision.lo)
        if key is not None and key == membership.get(decision.hi):
            edges.setdefault(key, []).append(decision)

    rows: list[dict[str, Any]] = []
    for key, members in sorted(result.clusters.items()):
        fingerprints = [fps[listing_id] for listing_id in members if listing_id in fps]
        areas = [fp.area_m2 for fp in fingerprints if fp.area_m2 is not None]
        scores = [decision.score for decision in edges.get(key, ())]
        families: set[str] = set()
        certificates = 0
        for decision in edges.get(key, ()):
            families |= decision.families
            certificates += 1 if decision.certificate else 0
        rows.append({
            "cluster_key": key,
            "size": len(members),
            "members": members,
            "block_key": sorted({fp.block_key for fp in fingerprints}),
            "cat_group": sorted({fp.cat_group for fp in fingerprints if fp.cat_group}),
            "category_main": sorted({fp.category_main for fp in fingerprints if fp.category_main}),
            "category_type": sorted({fp.category_type for fp in fingerprints if fp.category_type}),
            "sources": sorted({fp.source for fp in fingerprints if fp.source}),
            "area_min": min(areas) if areas else None,
            "area_max": max(areas) if areas else None,
            "n_edges": len(scores),
            "min_edge_score": min(scores) if scores else None,
            "mean_edge_score": (sum(scores) / len(scores)) if scores else None,
            "n_certificate_edges": certificates,
            "evidence_families": sorted(families),
            "shared_photo_warning": any(
                fp.catalog_ratio is not None and fp.catalog_ratio > 0.5 for fp in fingerprints
            ),
        })
    return rows
