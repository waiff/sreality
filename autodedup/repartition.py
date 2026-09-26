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
from typing import Callable, Iterable, Mapping, Sequence

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
# The pairs of a member set a stated fact separates — the pairwise part of `Invariants`,
# which is the only part a cell can shed its way out of.
Blockers = Callable[[Sequence[int]], Sequence[tuple[int, int]]]


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
    keep_factless: bool = False,
    rejoin_cells: bool = False,
    rejoin_invariants: Invariants | None = None,
    shed_blockers: Blockers | None = None,
    shed_max: int = 1,
    shed_max_union: int = 64,
    outer_rounds: int = 1,
    shed_factless_guard: str = "off",
    reconcile_factless_first: bool = False,
) -> list[list[int]]:
    """Cut one component into consistent groups, maximising the merge evidence kept inside.

    Every returned group satisfies `invariants`; a member no group can hold comes back as a
    singleton, and the caller drops those exactly as the union-find pass does.

    E156: `keep_factless` adds the reconciliation the local search cannot reach on its own.
    The search moves ONE member at a time and only on a strict gain, so two adverts joined by
    a merge edge that carries no fact at all can end in two cells because a conflict elsewhere
    in the component broke their cell up first — Penzion Horálka, same 374 m², same price,
    same body, sreality against mmreality, cut to two singletons by a conflict neither of them
    was party to. A member separated from a group it has an edge to must carry a fact AGAINST
    that group; where it does not, and the union holds, it goes back.
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

    def search() -> bool:
        """One sweep of the single-member local search. True when something moved."""
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
        return moved

    def repair() -> bool:
        """Search, then every repair pass in turn. True when any of them moved something."""
        touched = False
        for _round in range(max_rounds):
            if not search():
                break
            touched = True
        # E253: where a cell may shed, the reconciliation has to weigh its own move — the
        # member it drags out of a cell to honour one factless edge can be the member three
        # other edges are holding, and it would undo the shed on the next pass.
        weigh = neighbours if shed_blockers is not None else None
        # E263: the weighing may decide between two factless separations; it may not keep one
        # when the move it refuses would sever only edges a fact already carries.
        blocked_for = shed_blockers if reconcile_factless_first else None
        if keep_factless:
            for _round in range(max_rounds):
                if not _reconcile(ordered, home, cells, invariants, weigh, blocked_for):
                    break
                touched = True
        if rejoin_cells:
            strict = rejoin_invariants or invariants
            for _round in range(max_rounds):
                if not _rejoin(ordered, home, cells, strict):
                    break
                touched = True
                if keep_factless:
                    _reconcile(ordered, home, cells, invariants, weigh, blocked_for)
        if shed_blockers is not None:
            for _round in range(max_rounds):
                if not _shed(ordered, neighbours, home, cells, invariants, shed_blockers,
                             shed_max, shed_max_union, shed_factless_guard):
                    break
                touched = True
                if keep_factless:
                    _reconcile(ordered, home, cells, invariants, weigh, blocked_for)
        return touched

    # E253: the repairs feed each other — a cell a shed has just made smaller is a cell the
    # local search can now move into — so the whole sequence is run to a FIXED POINT rather
    # than once. `outer_rounds` of 1 is the pass every generation up to S9 took.
    for _outer in range(max(1, outer_rounds)):
        if not repair():
            break

    return [sorted(cell) for cell in cells if cell]


def _rejoin(
    ordered: Sequence[Edge],
    home: dict[int, int],
    cells: list[list[int]],
    invariants: Invariants,
) -> bool:
    """E193: offer every cut merge edge its WHOLE-CELL join once more. True when one held.

    The greedy pass at the top of `partition` reads the cells as they stand before the local
    search, so a join it refuses is refused against members the search is about to move away.
    `_reconcile` cannot repair that: it moves one member at a time, and two cells of three
    each never meet a member-sized move. Here the union of the two cells is offered whole, and
    only accepted when the invariants hold on it — the same test the greedy pass applies, on
    the partition the search actually produced.

    The edges are read in `edge.rank` order and each cell is joined at most once per round, so
    the result is a function of the edge SET; the caller bounds the rounds. This is the pass
    W13 measured and refused in its unordered form, where the Prostějov component lost its
    certified member to a cell of three: reading the certificate-first order fixes WHICH join
    is offered first, which is the whole of that complaint.
    """
    moved = False
    touched: set[int] = set()
    for edge in ordered:
        left, right = home[edge.lo], home[edge.hi]
        if left == right or left in touched or right in touched:
            continue
        merged = sorted(cells[left] + cells[right])
        if invariants(merged) is not None:
            continue
        keeper, loser = (left, right) if left < right else (right, left)
        for member in cells[loser]:
            home[member] = keeper
        cells[keeper] = merged
        cells[loser] = []
        touched.add(keeper)
        touched.add(loser)
        moved = True
    return moved


def _reconcile(
    ordered: Sequence[Edge],
    home: dict[int, int],
    cells: list[list[int]],
    invariants: Invariants,
    neighbours: Mapping[int, Sequence[Edge]] | None = None,
    blockers: Blockers | None = None,
) -> bool:
    """E156: put back every separation no fact justifies. True when something moved.

    A REPAIR, not a second optimisation. The local search has already chosen a partition; this
    pass only asks, of each merge edge still cut, whether the invariants can hold the two ends
    together — and if they can, puts the one end back. It never moves a whole cell: re-joining
    cells wholesale re-opens the search's own choices, and measured on the trial cohort that
    cost more duplicates than it recovered (one Prostějov component came out with its certified
    member in a cell of three instead of the cell of fourteen it has certificates into).

    Edges are read in `edge.rank` order and each member moves at most once per round, so the
    result is a function of the edge SET and the rounds terminate. A member the invariants
    refuse everywhere stays where the search left it — the rule is "no fact, no separation",
    not "no separation"."""
    moved = False
    touched: set[int] = set()
    for edge in ordered:
        left, right = home[edge.lo], home[edge.hi]
        if left == right:
            continue
        for member, target in ((edge.lo, right), (edge.hi, left)):
            if member in touched:
                continue
            current = home[member]
            if current == target:
                continue
            if invariants(sorted(cells[target] + [member])) is not None:
                continue
            if (neighbours is not None
                    and _move_gain(neighbours, home, member, target) < 0.0
                    and not _only_facts_severed(neighbours, blockers, home, cells, member)):
                continue
            cells[current].remove(member)
            cells[target].append(member)
            cells[target].sort()
            home[member] = target
            touched.add(member)
            moved = True
            break
    return moved


def _only_facts_severed(
    neighbours: Mapping[int, Sequence[Edge]], blockers: Blockers | None,
    home: Mapping[int, int], cells: Sequence[Sequence[int]], member: int,
) -> bool:
    """E263: is every merge edge this member has where it SITS one a fact already carries?

    Then the cell is holding it on weight alone, and the move it is refusing restores an edge
    no fact refuses. Where even one of the edges it sits on is factless the move is a trade of
    factless separations for factless separations, and E253's weighing decides it as before."""
    if blockers is None:
        return False
    here = [other for other in cells[home[member]] if other != member]
    if not here:
        return False
    blocked = {(lo, hi) for lo, hi in blockers(sorted(here) + [member])}
    for edge in neighbours[member]:
        other = edge.hi if edge.lo == member else edge.lo
        if other == member or home[other] != home[member]:
            continue
        if (min(member, other), max(member, other)) not in blocked:
            return False
    return True


def _move_gain(
    neighbours: Mapping[int, Sequence[Edge]], home: Mapping[int, int],
    member: int, target: int,
) -> float:
    """What moving one member to another cell does to the objective — `partition`'s `gain`."""
    out = 0.0
    for edge in neighbours[member]:
        other = edge.hi if edge.lo == member else edge.lo
        if other == member:
            continue
        if home[other] == home[member]:
            out -= edge.weight
        elif home[other] == target:
            out += edge.weight
    return out


def _weight_inside(
    neighbours: Mapping[int, Sequence[Edge]], members: Iterable[int]
) -> float:
    """The merge evidence a cell of these members keeps INSIDE it — the module's objective."""
    inside = set(members)
    total = 0.0
    for member in inside:
        for edge in neighbours[member]:
            other = edge.hi if edge.lo == member else edge.lo
            if other in inside and other > member:
                total += edge.weight
    return total


def _shed(
    ordered: Sequence[Edge],
    neighbours: Mapping[int, Sequence[Edge]],
    home: dict[int, int],
    cells: list[list[int]],
    invariants: Invariants,
    blockers: Blockers,
    shed_max: int,
    max_union: int,
    factless_guard: str = "off",
) -> bool:
    """E253: let a cell SHED the members that block a cut merge edge. True when one did.

    `_rejoin` offers two cells their whole union and takes the answer; `_reconcile` moves one
    member. Neither can reach the partition where a cell has to give a member UP to take a
    bigger one in — and that is the shape W25 measured on cohort 12: seven families of adverts
    no fact separates, each cut because the cell one half landed in had absorbed a THIRD advert
    that carries a fact against the other half. The absorbed advert was not party to either
    edge; it is simply what the greedy pass happened to reach first, and once inside it can
    veto every later join. Relaxing a rule makes that strictly more likely, which is why the
    seven appeared when E243 recovered its merges.

    The move is the same objective, not a new one: the union minus a bounded cover of its
    conflicting pairs is accepted only when the evidence it keeps inside BEATS what the two
    cells kept apart, so a cell never sheds a member that is worth more than the join. The
    shed members become their own cells and `_reconcile` may put them back elsewhere. Neither
    end of the cut edge may ever be shed — that would answer a different question than the one
    the edge asked.

    E262: and `factless_guard` says WHICH members the cover may name. The objective is a weight,
    and a weight will happily evict the tail of a long re-post train to admit a stranger: on
    cohort 13 that cut 162 certain pairs out of 56 components, every one of them one object —
    504940 and 540892 left a Průhonice cell of twenty identical bodies at 75,000 carrying
    thirty-one certificate edges each, of which twenty were to members no fact separates them
    from. A member is eligible only when EVERY merge edge it has into what the cell keeps is
    itself blocked; then the shed severs nothing a fact was not already severing, and where no
    such cover exists the cut edge simply stays cut.
    """
    moved = False
    touched: set[int] = set()
    for edge in ordered:
        left, right = home[edge.lo], home[edge.hi]
        if left == right or left in touched or right in touched:
            continue
        union = sorted(cells[left] + cells[right])
        if len(union) > max_union or invariants(union) is None:
            continue
        keep = {edge.lo, edge.hi}
        conflicts = [(lo, hi) for lo, hi in blockers(union)]
        if not conflicts or any(lo in keep and hi in keep for lo, hi in conflicts):
            continue
        # E262: where the guard is on, the cover is chosen in the currency the guard reads —
        # a candidate that severs no factless merge edge first — because the greedy-by-degree
        # cover names the train's tail exactly when the tail is what two conflicts run through.
        dirty = (_dirty_members(neighbours, blocked_pairs(conflicts), union, factless_guard)
                 if factless_guard != "off" else frozenset())
        cover = _cover(conflicts, keep, shed_max, dirty)
        if cover is None:
            continue
        kept = [member for member in union if member not in cover]
        if invariants(kept) is not None:
            continue
        if factless_guard != "off" and _severs_factless(
                neighbours, conflicts, cover, kept, factless_guard):
            continue
        before = _weight_inside(neighbours, cells[left]) + _weight_inside(
            neighbours, cells[right])
        if _weight_inside(neighbours, kept) <= before:
            continue
        for member in cover:
            cells.append([member])
            home[member] = len(cells) - 1
        keeper, loser = (left, right) if left < right else (right, left)
        for member in kept:
            home[member] = keeper
        cells[keeper] = kept
        cells[loser] = []
        touched.add(keeper)
        touched.add(loser)
        moved = True
    return moved


def _severs_factless(
    neighbours: Mapping[int, Sequence[Edge]], conflicts: Sequence[tuple[int, int]],
    cover: Sequence[int], kept: Sequence[int], mode: str,
) -> bool:
    """E262: would this shed cut a merge edge that no stated fact carries?

    `certificate` asks it of the CERTIFIED edges only. `core` asks a weaker question that tells
    the two shapes the census found apart: a genuine intruder conflicts with MANY of the union
    and holds few edges no fact carries, while the tail of a re-post train is the other way
    round — 504940 severs 20 factless edges to clear 11 conflicts. A member whose factless edges
    outnumber the conflicts its eviction clears is the cell's own, not a stranger in it.
    """
    certified_only = mode == "certificate"
    blocked = blocked_pairs(conflicts)
    inside = set(kept)
    for member in cover:
        factless = 0
        for edge in neighbours[member]:
            if certified_only and not edge.certificate:
                continue
            other = edge.hi if edge.lo == member else edge.lo
            if other in inside and (min(member, other), max(member, other)) not in blocked:
                if mode != "core":
                    return True
                factless += 1
        if mode == "core":
            facts = sum(1 for lo, hi in blocked if member in (lo, hi))
            if factless > facts:
                return True
    return False


def blocked_pairs(conflicts: Sequence[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    """The conflict list as a lookup — one spelling of the pair key for the whole module."""
    return frozenset((min(lo, hi), max(lo, hi)) for lo, hi in conflicts)


def _dirty_members(
    neighbours: Mapping[int, Sequence[Edge]], blocked: frozenset[tuple[int, int]],
    union: Sequence[int], mode: str,
) -> frozenset[int]:
    """E262: the union's members whose eviction would sever a merge edge no fact carries."""
    certified_only = mode == "certificate"
    inside = set(union)
    out: set[int] = set()
    for member in union:
        for edge in neighbours[member]:
            if certified_only and not edge.certificate:
                continue
            other = edge.hi if edge.lo == member else edge.lo
            if (other in inside and other != member
                    and (min(member, other), max(member, other)) not in blocked):
                out.add(member)
                break
    return frozenset(out)


def _cover(
    conflicts: Sequence[tuple[int, int]], keep: set[int], limit: int,
    dirty: frozenset[int] = frozenset(),
) -> list[int] | None:
    """The smallest set of members whose removal clears every conflict, greedily, or None.

    Greedy by degree with the smallest id breaking every tie, so the answer is a function of
    the conflict SET. A member the cut edge asked about is never a candidate, so a conflict
    with both ends protected has no cover at all.

    E262: `dirty` names the members whose eviction would sever a factless merge edge, and they
    are chosen LAST. The order is still a total one — (dirty, -degree, id) — so the answer stays
    a function of the set, and the guard still has the last word on the cover that comes out."""
    remaining = [(lo, hi) for lo, hi in conflicts]
    chosen: list[int] = []
    while remaining:
        if len(chosen) >= limit:
            return None
        degree: dict[int, int] = {}
        for lo, hi in remaining:
            for member in (lo, hi):
                if member not in keep:
                    degree[member] = degree.get(member, 0) + 1
        if not degree:
            return None
        pick = min(degree, key=lambda member: (member in dirty, -degree[member], member))
        chosen.append(pick)
        remaining = [(lo, hi) for lo, hi in remaining if lo != pick and hi != pick]
    return sorted(chosen)
