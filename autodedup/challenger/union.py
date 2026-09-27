"""The challenger's group step (GLOBAL_SEARCH 1.4, rung 3): one constrained union.

Must-links first, then the merge edges in descending p. Two groups join only when NO cross pair
states a fact (complete link, the one fact function at group grain, so a pair nobody scored is read
too), crosses a must-not-link, or is a scored pair below `t_neg` (learned negative evidence). A
must-link joins across any fact but rule 15's: a closure never puts a rental with a sale or a flat
with a commercial unit, and never crosses a must-not-link. No repair, no bridge rule, no size cap."""

from __future__ import annotations

from typing import Callable, Iterable, Mapping

from autodedup.challenger.facts import RULE_15


def constrained_union(edges: Iterable[tuple[int, int, float]],
                      stated: Callable[[int, int], str | None],
                      scored: Mapping[tuple[int, int], float], t_neg: float,
                      must_link: Iterable[tuple[int, int]] = (),
                      must_not_link: Iterable[tuple[int, int]] = ()) -> list[tuple[int, ...]]:
    """Every group of two or more adverts (members ascending, groups by their first member).
    `edges` are (lo, hi, p) merge candidates, taken in descending p with ties in the order given;
    `stated(lo, hi)` is the fact function (lo < hi); `scored` holds every scored pair's p."""
    apart = {(min(a, b), max(a, b)) for a, b in must_not_link}
    parent: dict[int, int] = {}
    members: dict[int, list[int]] = {}

    def find(x: int) -> int:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != root:
            parent[x], x = root, parent[x]
        return root

    def cross(left: list[int], right: list[int]) -> list[tuple[int, int]]:
        return [(min(x, y), max(x, y)) for x in left for y in right]

    def join(ra: int, rb: int) -> None:
        if len(members.get(ra, [ra])) < len(members.get(rb, [rb])):
            ra, rb = rb, ra
        parent[rb] = ra
        members[ra] = members.pop(ra, [ra]) + members.pop(rb, [rb])

    for a, b in must_link:
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        pairs = cross(members.get(ra, [ra]), members.get(rb, [rb]))
        if any(k in apart for k in pairs) or any(stated(*k) in RULE_15 for k in pairs):
            continue
        join(ra, rb)
    ranked = sorted(edges, key=lambda edge: -edge[2])
    for a, b, _ in ranked:
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        pairs = cross(members.get(ra, [ra]), members.get(rb, [rb]))
        if any(k in apart or scored.get(k, 1.0) < t_neg for k in pairs):
            continue
        if any(stated(*k) is not None for k in pairs):
            continue
        join(ra, rb)
    groups = [tuple(sorted(m)) for m in members.values() if len(m) > 1]
    return sorted(groups)
