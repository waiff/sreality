"""E137: when a component cannot be one group, keep the maximal consistent sub-groups.

E33's constrained union-find refuses a union and moves on, so which members fall out is decided
by the order the edges happened to arrive in. Measured on the D43 arms that costs 53 labelled
duplicates whose two adverts carry no distinguishing fact at all: the invariant refuses a union
that would have put a fact-carrying pair in one group, and the two clean adverts then land in
different groups depending on which edge was seen first.

E72/E86 say a verdict must be an invariant of the MEMBER SET, not of arrival order. So this
module takes a component of the merge graph and returns a partition that is a pure function of
(members, edge set, invariants): the edges are read in `edge_rank`'s total order — which is a
property of the set, not of the list — and the local search that follows visits members in id
order and only ever accepts a STRICT improvement, so it converges to one partition from one
input whatever order the caller happened to hold the edges in.

The objective is the weight of the merge edges that end up INSIDE a group: every invariant here
is monotone (a subset of a valid set is valid), so nothing is gained by leaving evidence out,
and a certificate outweighs any number of scores because its precision is structural (§6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

# A certificate outranks every learned score, exactly as `cluster.edge_rank` orders them.
CERTIFICATE_WEIGHT: float = 1000.0


@dataclass(frozen=True, slots=True)
class Edge:
    lo: int
    hi: int
    score: float
    certificate: bool

    @property
    def weight(self) -> float:
        return (CERTIFICATE_WEIGHT if self.certificate else 0.0) + 1.0 + self.score

    @property
    def rank(self) -> tuple[int, float, int, int]:
        return (0 if self.certificate else 1, -self.score, self.lo, self.hi)


Invariants = Callable[[Sequence[int]], str | None]


def components(members: Iterable[int], edges: Sequence[Edge]) -> list[list[int]]:
    """The connected components of the merge graph, each sorted, in ascending-first-id order."""
    parent: dict[int, int] = {member: member for member in members}

    def find(item: int) -> int:
        root = item
        while parent[root] != root:
            parent[root] = parent[parent[root]]
            root = parent[root]
        return root

    for edge in edges:
        left, right = find(edge.lo), find(edge.hi)
        if left != right:
            parent[max(left, right)] = min(left, right)
    groups: dict[int, list[int]] = {}
    for member in parent:
        groups.setdefault(find(member), []).append(member)
    return [sorted(group) for _root, group in sorted(groups.items())]


def partition(
    members: Sequence[int],
    edges: Sequence[Edge],
    invariants: Invariants,
    max_rounds: int = 4,
) -> list[list[int]]:
    """Cut one component into consistent groups, maximising the merge evidence kept inside.

    Every returned group satisfies `invariants`; a member no group can hold comes back as a
    singleton, and the caller drops those exactly as the union-find pass does.
    """
    ordered = sorted(edges, key=lambda edge: edge.rank)
    home: dict[int, int] = {member: index for index, member in enumerate(sorted(members))}
    cells: list[list[int]] = [[member] for member in sorted(members)]

    def cell(member: int) -> list[int]:
        return cells[home[member]]

    def move_all(source: int, target: int) -> None:
        for member in cells[source]:
            home[member] = target
        cells[target].extend(cells[source])
        cells[target].sort()
        cells[source] = []

    for edge in ordered:
        left, right = home[edge.lo], home[edge.hi]
        if left == right:
            continue
        merged = sorted(cells[left] + cells[right])
        if invariants(merged) is not None:
            continue
        move_all(max(left, right), min(left, right))

    neighbours: dict[int, list[Edge]] = {member: [] for member in members}
    for edge in ordered:
        neighbours[edge.lo].append(edge)
        neighbours[edge.hi].append(edge)

    def gain(member: int, target: int) -> float:
        out = 0.0
        for edge in neighbours[member]:
            other = edge.hi if edge.lo == member else edge.lo
            if home[other] == home[member] and other != member:
                out -= edge.weight
            elif home[other] == target:
                out += edge.weight
        return out

    for _round in range(max_rounds):
        moved = False
        for member in sorted(members):
            current = home[member]
            best_gain, best_target = 0.0, None
            seen: set[int] = {current}
            for edge in neighbours[member]:
                other = edge.hi if edge.lo == member else edge.lo
                target = home[other]
                if target in seen:
                    continue
                seen.add(target)
                if invariants(sorted(cells[target] + [member])) is not None:
                    continue
                delta = gain(member, target)
                if delta > best_gain:
                    best_gain, best_target = delta, target
            if best_target is None:
                continue
            cells[current].remove(member)
            cells[best_target].append(member)
            cells[best_target].sort()
            home[member] = best_target
            moved = True
        if not moved:
            break

    return [sorted(cell) for cell in cells if cell]
