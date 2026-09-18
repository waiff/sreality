"""CANDIDATE GROUPS over the residual pairs — the Groups card UX on the unmerged side (§12).

WHY THIS EXISTS. The residual queue asks one question per PAIR, and the pairs of one
generation are not independent questions: measured on g4, 7,653 residual pairs touch only
4,158 listings, 2,909 of those listings sit in two or more pairs (max 31), and about 3,180
already belong to a merged g4 group. So the pair queue asks "advert X vs each member of group
G" over and over, which is why it feels different from the groups queue and why it is slow.

WHAT THIS MODULE DOES, and nothing else: it lifts those pairs to UNIT level and packs the
units into small groups an operator can rule on with one save.

- A UNIT is what the engine already decided is one property: an existing cluster of this
  generation (ALL of its members, locked together) or a single unclustered listing. A unit is
  never split here — the Groups page owns that ruling.
- An EDGE is a residual pair lifted to its two units, keeping the BEST pair score and every
  underlying pair. Several pairs between one cluster and one advert are ONE question.
- A GROUP is a greedy union of units in descending edge score, capped at `MAX_ADVERTS`
  adverts so a card stays reviewable. An edge that would burst the cap is not dropped: it is
  deferred to the next round, where the union-find starts fresh, and it forms a smaller group
  of its own. TWO LOCKED UNITS that alone exceed the cap are forced into one group rather than
  deferred forever — the question "is cluster G1 the same unit as G2?" is a real one and the
  cap is a display budget, not a rule about the data.

THE GUARANTEE (E56): every residual pair of the input lands in EXACTLY ONE candidate group.
A pair that landed in none would be a question the queue silently stopped asking; a pair in
two would be two rulings about one fact, and the second one would overwrite the first. A unit,
unlike a pair, MAY appear in more than one group — that is the price of the cap, and it is why
the cap is a display budget.

PURE. No database, no I/O, no imports from `api`: the inputs are rows somebody else fetched
(`ui_sql.CANDIDATE_PAIRS_SQL` + `CANDIDATE_CLUSTER_MEMBERS_SQL`) and the output is a value.
The only state is `cached_index`, an in-process memo keyed by a store FINGERPRINT the caller
computes — a new score run or a re-clustering changes it, so the cache cannot serve a page the
engine no longer produces.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

# How many ADVERTS one candidate card may hold. Eight fits two rows of the groups card grid
# (four across at lg) and is about as much as one save can honestly rule on.
MAX_ADVERTS = 8

# The rounds are bounded so a pathological graph cannot spin: whatever is still unassigned
# after this many passes becomes one group per edge, which still satisfies the guarantee.
MAX_ROUNDS = 64

# How much of the member-id digest goes into the key. Ten hex characters over a member set is
# 2^40 — the key has to survive a URL and a test fixture, not an adversary.
_KEY_HASH_CHARS = 10


@dataclass(frozen=True)
class ResidualPair:
    """One row of the residual cohort — the SAME cohort the pair queue reads."""

    listing_lo: int
    listing_hi: int
    score: float
    zone: str | None = None
    families: int = 0
    block_key: int | None = None
    block_grain: str | None = None


@dataclass(frozen=True)
class CandidateUnit:
    """One thing the engine already treats as a single property.

    `cluster_key` is the g4 cluster the adverts were merged into, or None for a lone listing.
    A cluster unit carries ALL of its members, including the ones no residual pair names: a
    card that showed three of a five-advert group would be asking about a group nobody can see.
    """

    key: str
    cluster_key: int | None
    listing_ids: tuple[int, ...]

    @property
    def size(self) -> int:
        return len(self.listing_ids)


@dataclass(frozen=True)
class CandidateEdge:
    """Two units, and every residual pair that argues they are one."""

    unit_a: str
    unit_b: str
    score: float
    pairs: tuple[ResidualPair, ...]


@dataclass(frozen=True)
class CandidateGroup:
    candidate_key: str
    units: tuple[CandidateUnit, ...]
    edges: tuple[CandidateEdge, ...]
    pairs: tuple[ResidualPair, ...]

    @property
    def listing_ids(self) -> tuple[int, ...]:
        return tuple(listing_id for unit in self.units for listing_id in unit.listing_ids)

    @property
    def size(self) -> int:
        return sum(unit.size for unit in self.units)

    @property
    def n_units(self) -> int:
        return len(self.units)

    @property
    def score_min(self) -> float | None:
        return min((edge.score for edge in self.edges), default=None)

    @property
    def score_max(self) -> float | None:
        return max((edge.score for edge in self.edges), default=None)

    @property
    def cluster_keys(self) -> tuple[int, ...]:
        return tuple(u.cluster_key for u in self.units if u.cluster_key is not None)

    def zones(self) -> dict[str, int]:
        """The zone mix of the underlying pairs — a header chip, and the zone filter."""
        out: dict[str, int] = {}
        for pair in self.pairs:
            if pair.zone:
                out[pair.zone] = out.get(pair.zone, 0) + 1
        return out

    def families(self) -> int:
        """The union of the pairs' evidence-family bitmasks."""
        mask = 0
        for pair in self.pairs:
            mask |= int(pair.families or 0)
        return mask

    def blocks(self) -> tuple[tuple[int, str | None], ...]:
        """The (code, grain) blocks its pairs sit in — what the BLOCK filter compares."""
        seen: list[tuple[int, str | None]] = []
        for pair in self.pairs:
            if pair.block_key is None:
                continue
            entry = (int(pair.block_key), pair.block_grain)
            if entry not in seen:
                seen.append(entry)
        return tuple(seen)


@dataclass(frozen=True)
class CandidateIndex:
    """One generation's candidate groups, in a canonical order, plus a key lookup."""

    generation: str
    fingerprint: tuple
    groups: tuple[CandidateGroup, ...]

    def get(self, candidate_key: str) -> CandidateGroup | None:
        for group in self.groups:
            if group.candidate_key == candidate_key:
                return group
        return None


def unit_key(cluster_key: int | None, listing_id: int) -> str:
    """`c<cluster>` for a merged group, `l<listing>` for a lone advert — two namespaces that
    cannot collide, because a cluster key and a listing id are both bigints."""
    return f"c{cluster_key}" if cluster_key is not None else f"l{listing_id}"


def candidate_key(listing_ids: Sequence[int]) -> str:
    """The stable key: the smallest listing id, plus a short digest of the sorted member ids.

    The id makes it readable in a URL and a log line; the digest makes it CHANGE when the
    membership does, so a stale link opens nothing rather than a group that has since been
    repacked under the same name."""
    ordered = sorted(int(i) for i in listing_ids)
    digest = hashlib.md5(",".join(str(i) for i in ordered).encode("utf-8")).hexdigest()
    return f"{ordered[0]}-{digest[:_KEY_HASH_CHARS]}"


def _normalise(pairs: Iterable[ResidualPair]) -> list[ResidualPair]:
    """lo < hi, one row per pair, best score wins, sorted by (-score, lo, hi).

    The store already holds each pair once with `listing_lo < listing_hi`; this is the floor
    under a caller that hands the module a hand-built list."""
    best: dict[tuple[int, int], ResidualPair] = {}
    for pair in pairs:
        lo, hi = int(pair.listing_lo), int(pair.listing_hi)
        if lo == hi:
            continue
        if lo > hi:
            lo, hi = hi, lo
            pair = ResidualPair(
                listing_lo=lo,
                listing_hi=hi,
                score=pair.score,
                zone=pair.zone,
                families=pair.families,
                block_key=pair.block_key,
                block_grain=pair.block_grain,
            )
        current = best.get((lo, hi))
        if current is None or float(pair.score or 0.0) > float(current.score or 0.0):
            best[(lo, hi)] = pair
    return sorted(
        best.values(),
        key=lambda p: (-float(p.score or 0.0), p.listing_lo, p.listing_hi),
    )


class _Forest:
    """Union-find over UNIT keys, carrying each root's advert count and whether it is still
    exactly one unit — `virgin` is what lets two locked clusters that alone burst the cap be
    forced together rather than deferred for ever."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.size: dict[str, int] = {}
        self.members: dict[str, list[str]] = {}

    def add(self, key: str, adverts: int) -> None:
        if key in self.parent:
            return
        self.parent[key] = key
        self.size[key] = adverts
        self.members[key] = [key]

    def find(self, key: str) -> str:
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:
            self.parent[key], key = root, self.parent[key]
        return root

    def virgin(self, root: str) -> bool:
        return len(self.members[root]) == 1

    def union(self, a: str, b: str) -> str:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        # The smaller-keyed root wins, so the merge order is a fact of the data rather than of
        # the iteration order.
        keep, drop = (ra, rb) if ra <= rb else (rb, ra)
        self.parent[drop] = keep
        self.size[keep] += self.size[drop]
        self.members[keep].extend(self.members[drop])
        return keep


def build_candidates(
    pairs: Iterable[ResidualPair],
    cluster_members: Iterable[tuple[int, int]],
    max_adverts: int = MAX_ADVERTS,
) -> tuple[CandidateGroup, ...]:
    """The residual pairs of one generation, packed into candidate groups (E56).

    `cluster_members` is `(cluster_key, listing_id)` for every member of every cluster of that
    generation — the locks. Deterministic: the same inputs always give the same groups, the
    same keys and the same order, because every choice is made on a total order of the data.
    """
    members_of: dict[int, list[int]] = {}
    cluster_of: dict[int, int] = {}
    for cluster_key, listing_id in cluster_members:
        cluster_key, listing_id = int(cluster_key), int(listing_id)
        # A listing in two clusters of one generation is a store the engine should never write;
        # first-wins keeps the lock single-valued rather than raising on a page read.
        if listing_id in cluster_of:
            continue
        cluster_of[listing_id] = cluster_key
        members_of.setdefault(cluster_key, []).append(listing_id)

    units: dict[str, CandidateUnit] = {}

    def unit_of(listing_id: int) -> CandidateUnit:
        cluster_key = cluster_of.get(listing_id)
        key = unit_key(cluster_key, listing_id)
        unit = units.get(key)
        if unit is None:
            ids = (
                tuple(sorted(members_of[cluster_key]))
                if cluster_key is not None
                else (listing_id,)
            )
            unit = CandidateUnit(key=key, cluster_key=cluster_key, listing_ids=ids)
            units[key] = unit
        return unit

    edge_pairs: dict[tuple[str, str], list[ResidualPair]] = {}
    # A residual pair whose two adverts are in ONE cluster cannot exist in the cohort (that is
    # what makes it residual), but a caller can hand one over: it argues nothing about two
    # units, so it rides along with whichever group its unit first joins.
    inside_unit: dict[str, list[ResidualPair]] = {}
    for pair in _normalise(pairs):
        unit_a = unit_of(pair.listing_lo)
        unit_b = unit_of(pair.listing_hi)
        if unit_a.key == unit_b.key:
            inside_unit.setdefault(unit_a.key, []).append(pair)
            continue
        key = (unit_a.key, unit_b.key) if unit_a.key <= unit_b.key else (unit_b.key, unit_a.key)
        edge_pairs.setdefault(key, []).append(pair)

    edges = [
        CandidateEdge(
            unit_a=a,
            unit_b=b,
            score=max(float(p.score or 0.0) for p in rows),
            pairs=tuple(
                sorted(rows, key=lambda p: (-float(p.score or 0.0), p.listing_lo, p.listing_hi))
            ),
        )
        for (a, b), rows in edge_pairs.items()
    ]
    # Descending score is the greedy order: the strongest claim about two units gets to decide
    # the packing first. The unit keys break the tie, so the order is total.
    edges.sort(key=lambda e: (-e.score, e.unit_a, e.unit_b))

    built: list[tuple[tuple[CandidateUnit, ...], tuple[CandidateEdge, ...]]] = []
    pending = edges
    rounds = 0
    while pending and rounds < MAX_ROUNDS:
        rounds += 1
        forest = _Forest()
        for edge in pending:
            forest.add(edge.unit_a, units[edge.unit_a].size)
            forest.add(edge.unit_b, units[edge.unit_b].size)
        taken: dict[str, list[CandidateEdge]] = {}
        deferred: list[CandidateEdge] = []
        for edge in pending:
            root_a, root_b = forest.find(edge.unit_a), forest.find(edge.unit_b)
            if root_a == root_b:
                taken.setdefault(root_a, []).append(edge)
                continue
            fits = forest.size[root_a] + forest.size[root_b] <= max_adverts
            # Two LOCKED units that alone burst the cap would be deferred for ever; the
            # question between them is real, so the cap yields to the lock and says so.
            forced = forest.virgin(root_a) and forest.virgin(root_b)
            if not fits and not forced:
                deferred.append(edge)
                continue
            root = forest.union(root_a, root_b)
            carried = taken.pop(root_a, []) + taken.pop(root_b, [])
            carried.append(edge)
            taken[root] = carried
        if not taken:
            break
        for root, root_edges in taken.items():
            group_units = tuple(
                sorted(
                    (units[key] for key in forest.members[root]),
                    key=lambda u: (u.listing_ids[0], u.key),
                )
            )
            built.append(
                (
                    group_units,
                    tuple(sorted(root_edges, key=lambda e: (-e.score, e.unit_a, e.unit_b))),
                )
            )
        pending = deferred

    # The floor under `MAX_ROUNDS`: whatever is still unpacked becomes one group per edge, so
    # the exactly-once guarantee never depends on the loop converging.
    for edge in pending:
        group_units = tuple(
            sorted(
                (units[edge.unit_a], units[edge.unit_b]),
                key=lambda u: (u.listing_ids[0], u.key),
            )
        )
        built.append((group_units, (edge,)))

    groups: list[CandidateGroup] = []
    for group_units, group_edges in built:
        ids = [listing_id for unit in group_units for listing_id in unit.listing_ids]
        groups.append(
            CandidateGroup(
                candidate_key=candidate_key(ids),
                units=group_units,
                edges=group_edges,
                pairs=tuple(
                    sorted(
                        (pair for edge in group_edges for pair in edge.pairs),
                        key=lambda p: (-float(p.score or 0.0), p.listing_lo, p.listing_hi),
                    )
                ),
            )
        )
    groups.sort(key=lambda g: g.candidate_key)

    # The same-unit strays, attached to the FIRST group (canonical order) that holds their
    # unit — and to a group of their own when no edge ever named it.
    if inside_unit:
        groups = _attach_inside_unit_pairs(groups, units, inside_unit)
    return tuple(groups)


def _attach_inside_unit_pairs(
    groups: list[CandidateGroup],
    units: dict[str, CandidateUnit],
    inside_unit: dict[str, list[ResidualPair]],
) -> list[CandidateGroup]:
    home: dict[str, int] = {}
    for index, group in enumerate(groups):
        for unit in group.units:
            home.setdefault(unit.key, index)
    extra: list[CandidateGroup] = []
    for key, rows in sorted(inside_unit.items()):
        index = home.get(key)
        if index is None:
            unit = units[key]
            extra.append(
                CandidateGroup(
                    candidate_key=candidate_key(unit.listing_ids),
                    units=(unit,),
                    edges=(),
                    pairs=tuple(rows),
                )
            )
            continue
        group = groups[index]
        groups[index] = CandidateGroup(
            candidate_key=group.candidate_key,
            units=group.units,
            edges=group.edges,
            pairs=tuple(
                sorted(
                    [*group.pairs, *rows],
                    key=lambda p: (-float(p.score or 0.0), p.listing_lo, p.listing_hi),
                )
            ),
        )
    out = groups + extra
    out.sort(key=lambda g: g.candidate_key)
    return out


# ------------------------------------------------------------------- the in-process memo

_CACHE: dict[str, CandidateIndex] = {}
# One entry per generation, and the store holds a handful. Bounded anyway: an unbounded memo
# on a long-lived API process is a leak that only shows up in production.
_CACHE_MAX = 4


def cached_index(
    generation: str,
    fingerprint: tuple,
    build: Callable[[], tuple[CandidateGroup, ...]],
) -> CandidateIndex:
    """The generation's index, built once per FINGERPRINT.

    The fingerprint is the caller's cheap statement about the store — the pair count, the
    newest `decided_at` and the cluster-member count. A new score run or a re-clustering moves
    at least one of the three, so a cached index can never outlive the pass it describes. A
    generation whose fingerprint has not moved is served from memory, which is what keeps
    paging from rebuilding a few thousand pairs on every scroll."""
    hit = _CACHE.get(generation)
    if hit is not None and hit.fingerprint == fingerprint:
        return hit
    index = CandidateIndex(generation=generation, fingerprint=fingerprint, groups=build())
    if len(_CACHE) >= _CACHE_MAX and generation not in _CACHE:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[generation] = index
    return index


def clear_cache() -> None:
    """For tests, and for a process that wants to drop the memo deliberately."""
    _CACHE.clear()
