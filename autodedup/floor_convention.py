"""N1: two portals count the ground floor differently, and that is ONE fact, not two.

`sreality` calls the ground floor `1. podlaží`; `idnes` calls the same flat `přízemí` and the
one above it `1. patro`. Read naively, one advert says floor 3 and the other says floor 2, and
`total_floors` can carry the same offset — so a single vocabulary difference is charged twice
against the same pair.

WHAT IS DERIVED AND WHAT IS ASSUMED. Nothing here names a portal. `measure_camps` fits the
levels from ANCHOR observations the caller supplies — pairs so close on the facts that survive
the convention (area, price) that a floor gap can only be vocabulary — and the fit refuses any
source it cannot place: an absent source has no known convention and its floors are read the
permissive way. The shipped table lives in the settings row, with its evidence in the wave's
artifacts, so a re-measurement is a new table rather than an edit here.

THE JOINT READING. A camp offset shifts `floor`. It shifts `total_floors` only when the portal
applies the convention to the building as well as to the flat, and on this corpus half the
portal pairs do and half do not — so `total_floors` may differ by the offset only when `floor`
differs by exactly the same offset. Then the two numbers are one convention; apart, they are
two statements about the building and the gate reads them.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

# A camp is 0 (the ground floor is not counted) or 1 (it is). The absolute level is arbitrary —
# only the DIFFERENCE between two sources is ever read — so the fit pins the best-connected
# source at 1 and solves the rest against it.
CampTable = Mapping[str, int]

MIN_OBSERVATIONS: int = 15
MIN_MAJORITY: float = 0.80


@dataclass(frozen=True, slots=True)
class Anchor:
    """One observation: two adverts alike enough that a floor gap can only be vocabulary."""

    source_a: str
    floor_a: int
    source_b: str
    floor_b: int


def camp_offset(
    camps: CampTable | None, source_a: str | None, source_b: str | None
) -> int | None:
    """`floor(a) - floor(b)` attributable to vocabulary alone; None when a camp is unknown."""
    if not camps or source_a is None or source_b is None:
        return None
    level_a, level_b = camps.get(source_a), camps.get(source_b)
    if level_a is None or level_b is None:
        return None
    return int(level_a) - int(level_b)


def floor_gap(
    camps: CampTable | None,
    source_a: str | None,
    floor_a: int | None,
    source_b: str | None,
    floor_b: int | None,
) -> int | None:
    """The floor difference with the convention taken out, or None when a side is silent."""
    if floor_a is None or floor_b is None:
        return None
    offset = camp_offset(camps, source_a, source_b)
    return (floor_a - floor_b) - (offset or 0)


def convention_known(
    camps: CampTable | None, source_a: str | None, source_b: str | None
) -> bool:
    """True when the offset between the two sources is known, so a residual gap is real.

    Two adverts on ONE portal always share the convention. Two portals share a known offset
    only when the table places both of them; an unplaced source (bazos posts both ways) is
    never known to agree with anyone, and its floors keep the permissive reading E12 asks for.
    """
    if source_a is not None and source_a == source_b:
        return True
    return camp_offset(camps, source_a, source_b) is not None


def joint_convention_shift(
    camps: CampTable | None,
    source_a: str | None,
    floor_a: int | None,
    total_a: int | None,
    source_b: str | None,
    floor_b: int | None,
    total_b: int | None,
) -> bool:
    """True when `floor` and `total_floors` shift TOGETHER by one portal-boundary offset.

    Both numbers must be stated on both sides and both gaps must equal the same non-zero
    offset — the camp table's when it has one, else a shared ±1 across a portal boundary. One
    number moving without the other is two statements about the building, not a convention.
    """
    if None in (floor_a, floor_b, total_a, total_b):
        return False
    if source_a is not None and source_a == source_b:
        return False
    delta_floor = int(floor_a) - int(floor_b)  # type: ignore[arg-type]
    delta_total = int(total_a) - int(total_b)  # type: ignore[arg-type]
    if delta_floor == 0 or delta_floor != delta_total:
        return False
    offset = camp_offset(camps, source_a, source_b)
    if offset is not None:
        return delta_floor == offset
    return abs(delta_floor) == 1


def measure_camps(
    anchors: Iterable[Anchor],
    min_observations: int = MIN_OBSERVATIONS,
    min_majority: float = MIN_MAJORITY,
) -> dict[str, object]:
    """Fit `source -> camp` from anchor observations, and report what carried each source.

    A source pair contributes a constraint only when it has `min_observations` anchors and one
    offset in {-1, 0, +1} holds `min_majority` of them. The constraints form a graph; each
    connected component is solved by breadth-first propagation from its best-attested source,
    and any source whose constraints contradict the propagation is DROPPED rather than forced,
    because a portal that posts both ways (bazos) has no camp to find.
    """
    tallies: dict[tuple[str, str], Counter[int]] = defaultdict(Counter)
    for anchor in anchors:
        if anchor.source_a == anchor.source_b:
            continue
        if anchor.source_a < anchor.source_b:
            tallies[(anchor.source_a, anchor.source_b)][anchor.floor_a - anchor.floor_b] += 1
        else:
            tallies[(anchor.source_b, anchor.source_a)][anchor.floor_b - anchor.floor_a] += 1

    constraints: dict[tuple[str, str], int] = {}
    evidence: list[dict[str, object]] = []
    for pair in sorted(tallies):
        counts = tallies[pair]
        total = sum(counts.values())
        offset, hits = max(sorted(counts.items()), key=lambda item: (item[1], -abs(item[0])))
        share = hits / total if total else 0.0
        row = {
            "sources": list(pair),
            "n": total,
            "offset": offset,
            "share": round(share, 4),
            "histogram": {str(key): value for key, value in sorted(counts.items())},
            "used": bool(total >= min_observations and share >= min_majority
                         and abs(offset) <= 1),
        }
        evidence.append(row)
        if row["used"]:
            constraints[pair] = offset

    adjacency: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (left, right), offset in constraints.items():
        adjacency[left].append((right, offset))
        adjacency[right].append((left, -offset))

    degree = Counter({source: len(edges) for source, edges in adjacency.items()})
    camps: dict[str, int] = {}
    dropped: dict[str, str] = {}
    seen: set[str] = set()
    for root, _ in sorted(degree.most_common(), key=lambda item: (-item[1], item[0])):
        if root in seen:
            continue
        levels: dict[str, int] = {root: 1}
        queue = [root]
        conflicted: set[str] = set()
        while queue:
            source = queue.pop(0)
            for other, offset in sorted(adjacency[source]):
                want = levels[source] - offset
                if other in levels:
                    if levels[other] != want:
                        conflicted.add(other)
                    continue
                levels[other] = want
                queue.append(other)
        seen |= set(levels)
        span = sorted(set(levels.values()) - {levels[source] for source in conflicted})
        if len(span) > 2:
            # Three levels is not a ground-floor convention; refuse the whole component rather
            # than pick two of them.
            for source in levels:
                dropped[source] = "component spans more than two levels"
            continue
        floor_level = min(span) if span else 0
        for source, level in sorted(levels.items()):
            if source in conflicted:
                dropped[source] = "contradicts its own component"
                continue
            camps[source] = level - floor_level
    for source in sorted(set(tallies_sources(tallies)) - set(camps) - set(dropped)):
        dropped[source] = "no source pair cleared the evidence bar"
    return {
        "camps": dict(sorted(camps.items())),
        "dropped": dict(sorted(dropped.items())),
        "pairs": evidence,
        "params": {"min_observations": min_observations, "min_majority": min_majority},
    }


def tallies_sources(tallies: Mapping[tuple[str, str], object]) -> set[str]:
    return {source for pair in tallies for source in pair}


def anchors_from_pairs(
    rows: Sequence[tuple[str, int, str, int]]
) -> list[Anchor]:
    """`(source_a, floor_a, source_b, floor_b)` tuples as anchors — the caller picks the rule."""
    return [Anchor(str(a), int(fa), str(b), int(fb)) for a, fa, b, fb in rows]
