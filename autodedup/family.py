"""E85: K-B certifies a pair only when its FAMILY is consistent with ONE unit.

K-B's clauses are all pair-local — one portal, one broker, one template body, one stated area,
windows that never touched — and W11's blind adjudication of 77 K-B families showed that the
shape they cannot see is a family-level one. A broker who re-posts ONE advert thirty times and
a developer who posts several units of one project SERIALLY under one broker key produce the
same pair evidence; what separates them is what the whole chain does, not what any two of its
adverts do. 67 families read as one unit re-posted, 9 as several units of one project, 1
undecidable, and five family-level signals separate them (M93).

So the family is the guard's unit of judgement. A **family** is a connected component of the
pairs K-B certified BEFORE the guard ran — never of the pairs it merged, and never of what is
left after it: the guard demotes K-B, so a family read off the surviving certificates or off
the merge edges would move under its own verdict and the batch pass and the incremental pass
would disagree about what a family is. A **cell** is a set of family members that could be one
advert re-posted: members compatible on every clause below. A K-B
pair keeps its certificate only when both sides sit in one cell and that cell is internally
consistent; otherwise the certificate is withdrawn and the pair falls to K-C or to the model
exactly as any uncertified pair does (never to a veto, never to a reject — the guard removes
evidence, it does not manufacture a contradiction).

Four modes, because the readings cost differently and W11 measured them: `off` (the shipped
default), `family` (the whole family must be one consistent cell, else every K-B edge in it is
refused), `cell` (partition, and refuse only the edges that cross a cell boundary or sit in an
inconsistent one) and `pair` (refuse only an edge whose own two adverts are incompatible).
`cell` is the shape the W11 adjudication pointed at; `pair` is the only shape whose verdict is
an INVARIANT of the family (E86) — see `cells` for why the other two are not, and why the
real-time lane may not carry them.

INCREMENTALLY, a family is a property of the COHORT at decision time and it only ever grows:
tomorrow's arrival can make today's pure family impure, never the other way round. That is
E64's shape exactly, so it is E64's rail that carries it — `incremental._run_rail` re-opens a
stamped merge to the BAND when the world it was taken under has moved on, never to an unmerge —
with three differences stated here and implemented on the batch side by `refusals`: the trigger
is a K-B family gaining a member rather than a block census growing; the exempt set is the
other certificates (K-R, K-C, K-A are per-pair facts no family can decay, and E64 already
exempts certificates as a class, so K-B must be named the one exception); and the re-evaluation
is per FAMILY rather than per block, capped the same way so one impure arrival re-opens a few
edges rather than a whole development.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from autodedup.dataset import Listing
from autodedup.decide import Decision
from autodedup.features import parse_ts, window_end_stamp
from autodedup.settings import Settings
from autodedup.text_facts import reference_codes, stated_areas, unit_designators

MODES: tuple[str, ...] = ("off", "family", "cell", "pair")

# The clause names a refusal travels under, so a run summary can price each one separately.
CLAUSES: tuple[str, ...] = (
    "printed_area", "disposition", "unit_designator", "ref_code", "concurrent", "price",
)


@dataclass(slots=True, frozen=True)
class MemberFacts:
    """What the guard reads off one advert — all of it available at decision time."""

    listing_id: int
    headline_area: float | None
    price: float | None
    win_start: float | None
    win_end: float | None
    ref_codes: frozenset[str]
    unit_designators: frozenset[str]
    disposition: str | None


def headline_area(listing: Listing, settings: Settings) -> float | None:
    """The area the BODY prints for this unit: the stated area nearest the stored one.

    `listings.area_m2` is a per-portal parse and K-B already demands the two sides agree on it
    within 1 %; the printed number is the advert's own statement, and it is the only thing that
    separates the ceskereality developer's 27,2 m² chain from the two 28,6 m² adverts posted
    beside it under the same broker key with the same price and the same photographs.

    `family_guard_area_window` is what makes the number a HEADLINE rather than the nearest of
    whatever the body happens to print. `text_facts.stated_areas` already drops menus and
    building totals and bounds the rest at 3x the stored area, and 3x is far too wide to read as
    a statement about this unit: the Kokonín half-house (478609 x 536412, one advert printing
    103 and 206, the other only 206, stored 150) is a true re-post that a nearest-match without
    a window refuses on a 100 % disagreement. Outside the window the advert has not printed this
    unit's size in a form the guard can read, and E12 applies — absent, never a mismatch."""
    stored = listing.area_m2
    if not stored or stored <= 0.0:
        return None
    printed = [
        value for value in stated_areas(listing.description, stored)
        if abs(value - stored) / stored <= settings.family_guard_area_window
    ]
    if not printed:
        return None
    return min(printed, key=lambda value: abs(value - stored))


def member_facts(listing: Listing, settings: Settings) -> MemberFacts:
    """One advert's family facts, read on the clock the ENGINE is running (E62)."""
    return MemberFacts(
        listing_id=listing.id,
        headline_area=headline_area(listing, settings),
        price=listing.price,
        win_start=parse_ts(listing.first_seen_at),
        win_end=parse_ts(window_end_stamp(listing, settings)),
        ref_codes=frozenset(reference_codes(listing.description)),
        unit_designators=frozenset(unit_designators(listing.description)),
        disposition=listing.disposition,
    )


def _rel(a: float, b: float) -> float:
    low = min(a, b)
    return abs(a - b) / low if low > 0.0 else 0.0


def _overlap_days(a: MemberFacts, b: MemberFacts) -> float | None:
    if None in (a.win_start, a.win_end, b.win_start, b.win_end):
        return None
    return min(a.win_end, b.win_end) - max(a.win_start, b.win_start)  # type: ignore[operator]


def incompatible(a: MemberFacts, b: MemberFacts, settings: Settings) -> str | None:
    """Why these two adverts cannot be one unit re-posted, or None.

    E12 throughout: an absent fact is never a mismatch. Each clause is a settings row because
    each was priced separately against the 77 adjudicated families (M93)."""
    if (a.headline_area is not None and b.headline_area is not None
            and _rel(a.headline_area, b.headline_area) > settings.family_guard_area_tol):
        return "printed_area"
    if (settings.family_guard_disposition_clause
            and a.disposition and b.disposition and a.disposition != b.disposition):
        return "disposition"
    if (settings.family_guard_unit_designator_clause
            and a.unit_designators and b.unit_designators
            and not (a.unit_designators & b.unit_designators)):
        return "unit_designator"
    if (settings.family_guard_ref_code_clause
            and a.ref_codes and b.ref_codes and not (a.ref_codes & b.ref_codes)):
        return "ref_code"
    overlap = _overlap_days(a, b)
    if (settings.family_guard_concurrency_clause
            and overlap is not None and overlap > settings.family_guard_overlap_days):
        return "concurrent"
    if (settings.family_guard_price_clause
            and a.price and b.price and _rel(a.price, b.price) > settings.family_guard_price_tol):
        # A price that MOVES is the one-unit shape only when it moves forward in time and does
        # not move UP by much: a stepwise cut over a chain of re-posts is one flat that is not
        # selling, while two prices running beside each other — or a later advert asking a third
        # more than the earlier one — are two products. F399658's 19,999,000 -> 17,700,000 ->
        # 18,481,000 dips and recovers by 4.4 % and stays one unit; F186168's 27,490 -> 37,490
        # is a different rent package in one serviced-office building.
        if overlap is None or overlap > settings.family_guard_overlap_days:
            return "price"
        earlier, later = (a, b) if (a.win_start or 0.0) <= (b.win_start or 0.0) else (b, a)
        if later.price > earlier.price * (1.0 + settings.family_guard_price_rise_max):
            return "price"
    return None


class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

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


def kb_families(decisions: Iterable[Decision]) -> dict[int, list[int]]:
    """Connected components of the pairs K-B CERTIFIED, keyed by their smallest member.

    Read off the certificate and not off the zone, so the family a pair is judged in cannot
    move when the guard demotes that pair — the definition has to be a fixpoint of its own
    verdict or the batch pass and the incremental pass disagree about what a family is."""
    union = _Union()
    for decision in decisions:
        if decision.certificate == "K-B":
            union.union(int(decision.lo), int(decision.hi))
    out: dict[int, list[int]] = {}
    for item in sorted(union.parent):
        out.setdefault(union.find(item), []).append(item)
    return out


def cells(members: Sequence[MemberFacts], settings: Settings) -> list[list[int]]:
    """Partition a family into sets of adverts that could be ONE unit re-posted.

    Every cell is a CLIQUE under `incompatible`, not a connected component. The relation is not
    transitive — 27,0 and 27,2 and 27,5 chain across a 1 % tolerance, and a 60 % price cut is
    compatible with an advert that a 150 % rise is not — so components let one lax edge fuse two
    units, and a component that is then rejected for not being a clique takes the whole family
    down with it: the ceskereality 2+kk family (16 adverts, two chains at 8,525,000 and
    3,399,000 running side by side) collapses into one component and loses all 107 of its K-B
    pairs, including the 78 inside the chain the operator labelled positive 28 times.

    The partition is a first-fit over members ordered by when they were first seen: each advert
    joins the earliest cell it is compatible with EVERY member of, or opens a new one. Time
    order is what makes it deterministic and what makes it right — a re-post chain is a
    succession, so the cell an advert belongs to is settled by the adverts that came before it.

    Deterministic is not the same as INVARIANT, and W11's verification measured the difference
    (E86, M101): first-fit over the SAME members in a different order yields a different
    partition on 4 of the 37 families of size >= 3, one of them three different partitions over
    20 orders. This function pins the order to first sighting so a cohort pass and a replay of
    it agree, but the verdict still depends on an order — a backfilled advert whose first
    sighting precedes a member already decided moves the boundary. That is why `cell` and
    `family` are batch-only and why `pair`, whose verdict reads only the two adverts of the edge
    it refuses, is the mode an incremental rail may be designed on."""
    order = sorted(members, key=lambda item: (item.win_start is None, item.win_start or 0.0,
                                              item.listing_id))
    groups: list[list[MemberFacts]] = []
    for facts in order:
        for group in groups:
            if all(incompatible(facts, other, settings) is None for other in group):
                group.append(facts)
                break
        else:
            groups.append([facts])
    return sorted(
        (sorted(item.listing_id for item in group) for group in groups),
        key=lambda group: group[0],
    )


def first_inconsistency(
    members: Sequence[MemberFacts], settings: Settings
) -> tuple[str, int, int] | None:
    """The clause that refuses this set as one unit, with the two adverts it refused on."""
    for index, left in enumerate(members):
        for right in members[index + 1:]:
            clause = incompatible(left, right, settings)
            if clause is not None:
                return clause, left.listing_id, right.listing_id
    return None


def refusals(
    decisions: Sequence[Decision],
    listings: Mapping[int, Listing],
    settings: Settings,
) -> tuple[dict[tuple[int, int], str], dict[str, Any]]:
    """Every K-B pair whose family refuses it, with the clause, plus the run's family report.

    The report is not decoration: a family-grain guard is only auditable if the run says how
    many families it read, how many it found impure and which clause did it, and the
    incremental rail needs the same counters to say what a pass re-opened."""
    mode = settings.family_guard_mode
    empty_report: dict[str, Any] = {
        "mode": mode, "n_families": 0, "n_kb_pairs": 0, "n_refused": 0,
        "by_clause": {}, "n_families_impure": 0, "families": [],
    }
    if mode == "off":
        return {}, empty_report
    families = kb_families(decisions)
    if not families:
        return {}, empty_report

    kb_pairs: dict[int, list[tuple[int, int]]] = {}
    root_of: dict[int, int] = {}
    for root, members in families.items():
        for item in members:
            root_of[item] = root
    for decision in decisions:
        if decision.certificate == "K-B":
            kb_pairs.setdefault(root_of[int(decision.lo)], []).append(
                (int(decision.lo), int(decision.hi))
            )

    refused: dict[tuple[int, int], str] = {}
    by_clause: dict[str, int] = {}
    reports: list[dict[str, Any]] = []
    impure = 0
    for root, members in sorted(families.items()):
        facts = [member_facts(listings[item], settings) for item in members if item in listings]
        by_id = {item.listing_id: item for item in facts}
        pairs = sorted(kb_pairs.get(root, []))
        groups = cells(facts, settings)
        cell_of = {item: index for index, group in enumerate(groups) for item in group}
        whole = first_inconsistency(facts, settings)
        if whole is not None:
            impure += 1
        entry: dict[str, Any] = {
            "family": f"F{root}", "size": len(members), "n_kb_pairs": len(pairs),
            "cells": groups,
            "inconsistency": (
                {"clause": whole[0], "lo": whole[1], "hi": whole[2]} if whole else None
            ),
            "refused": [],
        }
        for lo, hi in pairs:
            clause: str | None = None
            if mode == "family":
                clause = whole[0] if whole is not None else None
            elif mode == "pair":
                # E86: the edge's own two adverts and nothing else, so no third advert and no
                # arrival order can move the verdict.
                left, right = by_id.get(lo), by_id.get(hi)
                if left is not None and right is not None:
                    clause = incompatible(left, right, settings)
            else:
                if cell_of.get(lo) != cell_of.get(hi):
                    # Cells are cliques, so a within-cell pair is compatible by construction and
                    # only a CROSS-cell edge can be refused. The clause is the pair's own when it
                    # has one, and `cross_cell` when the two were separated by a third advert.
                    clause = incompatible(by_id[lo], by_id[hi], settings) or "cross_cell"
            if clause is not None:
                refused[(lo, hi)] = clause
                by_clause[clause] = by_clause.get(clause, 0) + 1
                entry["refused"].append([lo, hi, clause])
        reports.append(entry)

    report = {
        "mode": mode,
        "n_families": len(families),
        "n_kb_pairs": sum(len(rows) for rows in kb_pairs.values()),
        "n_refused": len(refused),
        "n_families_impure": impure,
        "by_clause": dict(sorted(by_clause.items())),
        "families": [entry for entry in reports if entry["refused"] or entry["inconsistency"]],
    }
    return refused, report
