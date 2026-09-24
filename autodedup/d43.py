"""E132: D43 read at CLUSTER grain — a property is a set of adverts no stated fact separates.

The pairwise gate is not enough on its own. A group is built transitively, so A-B and B-C can
each carry no distinguishing fact while A and C differ on the floor; without this limb the
relaxed arms carry real negatives and bad groups, with it they carry none.

E157's price limb is read here too, behind `demonstrate_cluster_price`, for the same reason
the facts are: three houses of one Hlubočky parcelling at 9,650,000 / 9,750,000 / 9,850,000 sit
on six portals, the pairwise filter refuses every ceskereality pair among them, and the group
still closes through a sixth portal whose 5 % cross-portal slack covers the 2 % between them.

The relation is memoised because the clusterer asks the same question many times: every union
re-reads the merged member set, and a 32-member group is 496 pairs. A pair the engine never
scored carries no feature row, so the two image facts simply do not apply to it — the same
reading `distinguishing_facts(..., feats=None)` gives, and the permissive direction, stated
here so nobody reads this class as a strict bound.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from autodedup.dataset import Listing
from autodedup.demonstrate import price_conflict
from autodedup.indistinguishable import (
    CLUSTER,
    PROMOTE,
    distinguishing_facts,
    honest_overlap_days,
    overlap_days,
    price_paths_agree,
)
from autodedup.settings import Settings

Feats = Mapping[str, tuple[float, bool]]


class ClusterRelation:
    """`ok(a, b)` = no stated fact separates the two adverts, memoised per unordered pair."""

    __slots__ = ("_listings", "_feats", "_settings", "_memo", "_mode")

    def __init__(
        self,
        listings: Mapping[int, Listing],
        feats: Mapping[tuple[int, int], Feats] | None = None,
        settings: Settings | None = None,
        mode: str = CLUSTER,
    ) -> None:
        self._listings = listings
        self._feats = feats or {}
        self._settings = settings or Settings()
        self._mode = mode
        self._memo: dict[tuple[int, int], bool] = {}

    def strict(self) -> "ClusterRelation":
        """E193: the same relation read at PROMOTION's bar, with its own memo.

        A cut re-offered its join is a merge made on the ABSENCE of a fact, and E136 says that
        is the one place the engine has no positive evidence to fall back on — so the area is
        read at 3 % rather than the gate's 8 %, and the geocode and storey slacks the gate
        carries for a merge it already certified are not extended to a join nobody certified."""
        return ClusterRelation(self._listings, self._feats, self._settings, PROMOTE)

    def ok(self, left: int, right: int) -> bool:
        key = (left, right) if left < right else (right, left)
        hit = self._memo.get(key)
        if hit is None:
            a, b = self._listings.get(key[0]), self._listings.get(key[1])
            if a is None or b is None:
                # A member the pass cannot read is not a member this rule may refuse.
                return True
            hit = not distinguishing_facts(a, b, self._feats.get(key), self._settings,
                                           self._mode)
            if hit and self._settings.demonstrate_cluster_price:
                # E264: on W8's clock when asked for it — `inactive_at` is when the delisting
                # was DETECTED, and a re-post train's tail is co-live only on that stamp.
                overlap = (honest_overlap_days(a, b)
                           if self._settings.demonstrate_cluster_price_honest_clock
                           else overlap_days(a, b))
                hit = not price_conflict(
                    a, b, self._settings,
                    price_paths_agree(a, b, self._settings.d43_price_path_tol),
                    overlap)
            self._memo[key] = hit
        return hit

    def violating_pair(self, ids: Sequence[int]) -> tuple[int, int] | None:
        """The first pair of the member set a stated fact separates, in id order."""
        members = sorted(ids)
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                if not self.ok(left, right):
                    return (left, right)
        return None

    def consistent(self, ids: Iterable[int]) -> bool:
        return self.violating_pair(list(ids)) is None


def relation_for(
    settings: Settings,
    listings: Mapping[int, Listing],
    feats: Mapping[tuple[int, int], Feats] | None = None,
) -> ClusterRelation | None:
    """The relation when the settings row asks for the limb, else None — one place to ask."""
    if not settings.d43_cluster_invariant:
        return None
    return ClusterRelation(listings, feats, settings)
