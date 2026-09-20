"""E88: the honest clock ships everywhere EXCEPT where the new-development hazard lives.

W11 measured the honest live-window clock (E62) with E84's pair-gap rail and no image floor at
347 of 406 sealed reliable labelled duplicates against g6's 300, gained 47 lost 0 (M98) — and
refused to promote it on ONE sealed reliable false merge, gold `522698 x 13221982`, with its
dev-side twin `555448 x 18626270`. Both sit inside two ceskereality serial-poster families of
one new development (Rezidence K Botiči): identical galleries in identical order, near
byte-identical bodies, one price, one stated area, windows that never touched. A gold re-judge
with operator-labelled controls (run 35527277524) returned `same_building_different_unit` for
both again — but returned it for 5 of 10 pairs in the SAME families that the operator had
confirmed as duplicates. Inside this shape nobody but the operator can say whether the adverts
are one unit re-posted or stacked identical units, and that is exactly the hazard the standing
ruling ("NO FALSE MERGES, above all in new developments") names.

So this rule does not try to decide the shape. It HOLDS it. A K-B certificate — the only merge
path that rests on the honest clock's disjoint windows — inside a new-development serial-poster
family is withheld, and the pair is then decided exactly as it would be with no certificate at
all: K-C, the model, or E63. Never a veto, never a reject, never K-R. The hold removes evidence
the way E85 does; what is new is the PREDICATE, which is about the family's PROVENANCE (is this
a development being sold serially?) rather than about whether its members can be one unit.

THE FAMILY. The connected component of K-B-certifiable pairs under one source, one broker and
one address block. K-B already demands one source and one broker, so the block is the only
restriction added here, and it is what makes the component a "project" rather than a portfolio.
Unlike E85's first-fit cells the partition is a connected component, so it is an INVARIANT of
the member set and not of the order the members arrived in — E86's defect cannot reach it.

THE MARKERS, all of them readable at decision time from the adverts themselves:
  * project vocabulary  — `PROJECT_TERMS` in at least `development_vocab_min_share` of members;
  * co-operative financing vocabulary — `COOP_TERMS`, and only in a family of at least
    `development_coop_min_size` adverts (a single co-op advert is a flat, not a development);
  * family size above a small cap;
  * block density — many OTHER listings of the same category at the family's address block;
  * members printing different unit-level facts (REPORTED, not in either definition below).

TWO DEFINITIONS, pre-registered before anything was measured and reported side by side:
  NARROW = vocabulary AND size >= `development_size_min_narrow`
  WIDE   = vocabulary OR  size >= `development_size_min_wide`
           OR block density >= `development_block_density_min`

INCREMENTALLY the hold is MONOTONE in the safe direction and that is what makes it designable:
family membership only grows, vocabulary is a property of members that only arrive, size only
rises and a block census only fills up — so a family can only ever TURN development-like, and a
hold can only ever be ADDED. The rail owed is therefore E64's, with the trigger changed: a
family gaining a member or a marker re-opens that family's earlier K-B merges to the BAND,
never to an unmerge, capped per family the way E64 caps per block. The batch side is built
here; the lane refuses the setting until it carries a family index (`incremental.run_pass`),
exactly as it refuses E83 and E85.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from autodedup.dataset import Listing
from autodedup.decide import Decision
from autodedup.settings import Settings
from autodedup.text_facts import address_block_key, fold

MODES: tuple[str, ...] = ("off", "narrow", "wide")

# The vocabulary, folded and stemmed, written down before any arm ran. A term is matched as a
# substring of `text_facts.fold`ed description text, which is what every other text rule in the
# engine reads.
PROJECT_TERMS: tuple[str, ...] = (
    "rezidenc",    # rezidence, rezidenční
    "projekt",     # projekt, projektu, projektem
    "developer",   # developer, developerský
    "novostavb",   # novostavba, novostavby
    "etap",        # etapa, etapy, etapě
    "cenik",       # ceník
    "kolaudac",    # ke kolaudaci, kolaudace
    "dokonceni",   # dokončení
)
COOP_TERMS: tuple[str, ...] = (
    "podil",       # podíl, podílu
    "anuit",       # anuita, anuitou
    "druzstevn",   # družstevní, družstevního
)

# The marker names a hold travels under, so a run summary can price each one separately.
MARKERS: tuple[str, ...] = ("vocabulary", "size", "block_density")


@dataclass(slots=True, frozen=True)
class FamilyMarkers:
    """What one K-B family shows at decision time. Every field is readable from the adverts."""

    family_id: str
    members: tuple[int, ...]
    source: str | None
    broker_key: str | None
    block_key: str | None
    category_group: str | None
    size: int
    n_project_members: int
    n_coop_members: int
    project_terms: tuple[str, ...]
    coop_terms: tuple[str, ...]
    block_density: int
    unit_fact_disagreement: tuple[str, ...]
    vocabulary: bool
    narrow: bool
    wide: bool

    def fired(self, mode: str) -> tuple[str, ...]:
        """The markers that make this family development-like under `mode`, in MARKERS order."""
        if mode == "off" or not (self.narrow if mode == "narrow" else self.wide):
            return ()
        out = [name for name, hit in (
            ("vocabulary", self.vocabulary),
            ("size", self.size >= self._size_bar),
            ("block_density", self._density_hit),
        ) if hit]
        return tuple(out)

    # Filled by `markers_of`; kept off the public surface because they are the two bars the
    # mode chose, not facts about the family.
    _size_bar: int = 0
    _density_hit: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "family": self.family_id, "size": self.size, "members": list(self.members),
            "source": self.source, "broker_key": self.broker_key, "block": self.block_key,
            "category_group": self.category_group,
            "n_project_members": self.n_project_members, "project_terms": list(self.project_terms),
            "n_coop_members": self.n_coop_members, "coop_terms": list(self.coop_terms),
            "block_density": self.block_density,
            "unit_fact_disagreement": list(self.unit_fact_disagreement),
            "vocabulary": self.vocabulary, "narrow": self.narrow, "wide": self.wide,
        }


class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, item: int) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: int) -> int:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            self.parent[root] = self.parent[self.parent[root]]
            root = self.parent[root]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def same_project(a: Listing, b: Listing) -> bool:
    """One source, one broker, one address block — the edge a family is built over.

    K-B already carries the first two, so only the block is a real restriction: a broker's two
    re-post chains in two buildings are two families, and a hold on one must not reach the other.
    """
    return (
        a.source == b.source
        and (a.broker_key or "") == (b.broker_key or "")
        and bool(a.broker_key)
        and address_block_key(a) == address_block_key(b)
    )


def kb_project_families(
    decisions: Iterable[Decision], listings: Mapping[int, Listing]
) -> dict[int, list[int]]:
    """Connected components of the K-B pairs that stay inside one source/broker/block.

    Read off the CERTIFICATE and not off the zone, for E85's reason: the hold demotes K-B, so a
    family read off what survives the hold would move under its own verdict. Every endpoint of
    a K-B pair joins the map, so a pair whose two sides sit in different blocks still resolves
    to two (singleton) families rather than to none."""
    union = _Union()
    for decision in decisions:
        if decision.certificate != "K-B":
            continue
        lo, hi = int(decision.lo), int(decision.hi)
        union.add(lo)
        union.add(hi)
        la, lb = listings.get(lo), listings.get(hi)
        if la is not None and lb is not None and same_project(la, lb):
            union.union(lo, hi)
    out: dict[int, list[int]] = {}
    for item in sorted(union.parent):
        out.setdefault(union.find(item), []).append(item)
    return out


def _terms_in(listing: Listing, terms: Sequence[str]) -> tuple[str, ...]:
    text = fold(listing.description or "")
    return tuple(term for term in terms if term in text)


def _category_group(listing: Listing) -> str:
    return f"{listing.category_main or '?'}|{listing.category_type or '?'}"


def block_population(listings: Mapping[int, Listing]) -> dict[tuple[str, str], int]:
    """How many cohort listings sit at each (address block, category group)."""
    out: dict[tuple[str, str], int] = {}
    for listing in listings.values():
        key = (address_block_key(listing), _category_group(listing))
        out[key] = out.get(key, 0) + 1
    return out


def _unit_fact_disagreement(members: Sequence[Listing], settings: Settings) -> tuple[str, ...]:
    """Unit-level facts the family's members do NOT agree on. Reported, never a predicate.

    It is the marker with the best claim to being decisive and the worst provenance: M99 read
    the 1+kk family's 28,6-vs-27,2 as a two-character typo corrected once and never reverted,
    so a rule resting on it would refuse a true chain on a typo. Measured and shown, not used.
    """
    from autodedup.family import headline_area

    out: list[str] = []
    areas = [value for value in (headline_area(item, settings) for item in members)
             if value is not None]
    if areas and max(areas) > 0 and (max(areas) - min(areas)) / min(areas) > 0.01:
        out.append("printed_area")
    if len({item.disposition for item in members if item.disposition}) > 1:
        out.append("disposition")
    if len({item.floor for item in members if item.floor is not None}) > 1:
        out.append("floor")
    if len({item.price for item in members if item.price}) > 1:
        out.append("price")
    return tuple(out)


def markers_of(
    family_id: str,
    members: Sequence[int],
    listings: Mapping[int, Listing],
    population: Mapping[tuple[str, str], int],
    settings: Settings,
) -> FamilyMarkers:
    rows = [listings[item] for item in members if item in listings]
    size = len(members)
    project_hits = [_terms_in(item, PROJECT_TERMS) for item in rows]
    coop_hits = [_terms_in(item, COOP_TERMS) for item in rows]
    n_project = sum(1 for terms in project_hits if terms)
    n_coop = sum(1 for terms in coop_hits if terms)
    share = settings.development_vocab_min_share
    denominator = len(rows) or 1
    project_vocab = (n_project / denominator) >= share
    # A co-operative advert on its own is a flat sold as a share; it is a DEVELOPMENT marker
    # only beside a family of adverts, which is what `development_coop_min_size` says.
    coop_vocab = ((n_coop / denominator) >= share and size >= settings.development_coop_min_size)
    vocabulary = project_vocab or coop_vocab

    blocks = {address_block_key(item) for item in rows}
    groups = {_category_group(item) for item in rows}
    block_key = sorted(blocks)[0] if len(blocks) == 1 else None
    group = sorted(groups)[0] if len(groups) == 1 else None
    density = 0
    if block_key is not None and group is not None:
        density = max(0, population.get((block_key, group), 0) - size)

    size_bar = (settings.development_size_min_narrow if settings.development_hold_mode == "narrow"
                else settings.development_size_min_wide)
    density_hit = density >= settings.development_block_density_min
    return FamilyMarkers(
        family_id=family_id,
        members=tuple(sorted(members)),
        source=(sorted({item.source or "?" for item in rows})[0] if len({item.source for item in rows}) == 1 else None),
        broker_key=(sorted({item.broker_key or "?" for item in rows})[0] if len({item.broker_key for item in rows}) == 1 else None),
        block_key=block_key,
        category_group=group,
        size=size,
        n_project_members=n_project,
        n_coop_members=n_coop,
        project_terms=tuple(sorted({term for terms in project_hits for term in terms})),
        coop_terms=tuple(sorted({term for terms in coop_hits for term in terms})),
        block_density=density,
        unit_fact_disagreement=_unit_fact_disagreement(rows, settings),
        vocabulary=vocabulary,
        narrow=vocabulary and size >= settings.development_size_min_narrow,
        wide=(vocabulary or size >= settings.development_size_min_wide or density_hit),
        _size_bar=size_bar,
        _density_hit=density_hit,
    )


def holds(
    decisions: Sequence[Decision],
    listings: Mapping[int, Listing],
    settings: Settings,
) -> tuple[dict[tuple[int, int], str], dict[str, Any]]:
    """Every K-B pair a development family holds, with the markers, plus the run's report.

    A pair is held when EITHER endpoint's family is development-like: a K-B edge whose two
    sides sit in different blocks belongs to two families, and a hold that reached only one of
    them would depend on which side the pair is keyed by."""
    mode = settings.development_hold_mode
    empty: dict[str, Any] = {
        "mode": mode, "n_families": 0, "n_kb_pairs": 0, "n_held": 0, "n_families_held": 0,
        "by_marker": {}, "families": [],
    }
    if mode == "off":
        return {}, empty
    families = kb_project_families(decisions, listings)
    if not families:
        return {}, empty
    population = block_population(listings)

    root_of: dict[int, int] = {}
    for root, members in families.items():
        for item in members:
            root_of[item] = root
    kb_pairs = sorted(
        (int(d.lo), int(d.hi)) for d in decisions if d.certificate == "K-B"
    )

    marks = {
        root: markers_of(f"F{root}", members, listings, population, settings)
        for root, members in sorted(families.items())
    }
    held: dict[tuple[int, int], str] = {}
    by_marker: dict[str, int] = {}
    per_family: dict[int, int] = {}
    for lo, hi in kb_pairs:
        fired: tuple[str, ...] = ()
        root = None
        for side in (lo, hi):
            side_root = root_of.get(side)
            if side_root is None:
                continue
            side_fired = marks[side_root].fired(mode)
            if side_fired and not fired:
                fired, root = side_fired, side_root
        if fired and root is not None:
            reason = "+".join(fired)
            held[(lo, hi)] = reason
            by_marker[reason] = by_marker.get(reason, 0) + 1
            per_family[root] = per_family.get(root, 0) + 1

    rows = []
    for root, mark in marks.items():
        if not mark.fired(mode):
            continue
        entry = mark.to_json()
        entry["markers_fired"] = list(mark.fired(mode))
        entry["n_kb_pairs_held"] = per_family.get(root, 0)
        rows.append(entry)
    report = {
        "mode": mode,
        "n_families": len(families),
        "n_kb_pairs": len(kb_pairs),
        "n_held": len(held),
        "n_families_held": len(rows),
        "by_marker": dict(sorted(by_marker.items())),
        "families": sorted(rows, key=lambda row: (-row["n_kb_pairs_held"], row["family"])),
    }
    return held, report
