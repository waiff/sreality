"""E83 — carrier-aware catalogue subtraction: a frame is STOCK only when many PARTIES carry it.

E9 subtracts a frame from the evidence set when `images.phash` appears on at least
`catalog_df` listings corpus-wide. That population test cannot tell a marketing catalogue from
a broker re-posting ONE advert nine times: both put one frame on nine listings, and the second
shape is exactly what K-B exists to certify. M59 measured the cost — of 1,348 image-less K-B
pairs under the honest clock, 961 are carried by ONE broker on ONE portal and carry 133
reliable labels, all positive — so the separator the evidence points at is WHO carries the
frames, not how many carry them.

So a frame is stock when its population is large (E9, unchanged) **and** its carriers are
several parties. Diversity is counted over three dimensions a settings row picks: distinct
broker keys, distinct sources, distinct address blocks.

Two honest rails, because carrier identity is only observable INSIDE the cohort while `pop` is
corpus-wide:

*Coverage.* The exemption applies only when the cohort holds `coverage_min` of the frame's
corpus-wide population. A frame the cohort sees 3 of 50 carriers of cannot be attributed to one
party — the other 47 are unseen — so E9 stands and the frame stays subtracted. Every relaxation
of E9 adds image evidence and therefore adds merges, so the unobservable case resolves the
conservative way.

*Unknown identity is its own party.* A carrier with no broker key counts as a distinct party
rather than collapsing with every other unknown, again because the direction that keeps a frame
subtracted is the safe one.

And one purity rail, E60's, restated for a photograph: a frame whose carriers DISAGREE about the
unit — two dispositions, or stated areas further apart than `catalog_carrier_area_tol` — is the
PROJECT's material, whoever holds it, and stays subtracted. A photograph is of one room; carriers
that print two sizes are two units, so the frame certifies nothing about either. Measured on the
g6 cohort: a one-broker re-post chain's exempted frames carry an area spread of exactly 0.0 over
20-31 carriers, while the ceskereality developer's one exclusive frame spans 26 and 27 m2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from autodedup.text_facts import address_block_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from autodedup.dataset import Dataset, Image, Listing
    from autodedup.settings import Settings

COMBINE_ANY = "any"
COMBINE_ALL = "all"
COMBINE_MODES: tuple[str, ...] = (COMBINE_ANY, COMBINE_ALL)

# A bar of 1 is vacuous — every frame has at least one carrier — so a limb starts at two parties.
MIN_CARRIER_BAR = 2


@dataclass(frozen=True, slots=True)
class CarrierPolicy:
    """The settings rows E83 reads, lifted out so a cache key can name them."""

    enabled: bool = False
    min_brokers: int | None = None
    min_sources: int | None = None
    min_blocks: int | None = None
    combine: str = COMBINE_ANY
    coverage_min: float = 1.0
    area_tol: float = 0.01

    @classmethod
    def of(cls, settings: "Settings") -> "CarrierPolicy":
        return cls(
            enabled=bool(getattr(settings, "catalog_carrier_aware", False)),
            min_brokers=getattr(settings, "catalog_min_broker_carriers", None),
            min_sources=getattr(settings, "catalog_min_source_carriers", None),
            min_blocks=getattr(settings, "catalog_min_block_carriers", None),
            combine=str(getattr(settings, "catalog_carrier_combine", COMBINE_ANY)),
            coverage_min=float(getattr(settings, "catalog_carrier_coverage_min", 1.0)),
            area_tol=float(getattr(settings, "catalog_carrier_area_tol", 0.01)),
        )

    def limbs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (name, int(bar))
            for name, bar in (
                ("brokers", self.min_brokers),
                ("sources", self.min_sources),
                ("blocks", self.min_blocks),
            )
            if bar is not None
        )

    def token(self) -> str:
        """Identity of the policy for a memo key — two policies that differ must not share one."""
        if not self.enabled:
            return "off"
        limbs = ",".join(f"{name}>={bar}" for name, bar in self.limbs())
        return f"{self.combine}:{limbs}:cov{self.coverage_min:g}:area{self.area_tol:g}"


@dataclass(frozen=True, slots=True)
class CarrierCensus:
    """What one frame's carriers are, inside the cohort."""

    carriers: int
    brokers: int
    sources: int
    blocks: int
    pop: int | None
    area_spread: float
    dispositions: int

    def coverage(self) -> float | None:
        if self.pop is None or self.pop <= 0:
            return None
        return min(1.0, self.carriers / self.pop)


@dataclass(slots=True)
class StockIndex:
    """`is_stock(image, catalog_df)` — E9's population test, narrowed by E83's carrier test."""

    policy: CarrierPolicy
    census: dict[int, CarrierCensus]
    exempt: frozenset[int]

    @classmethod
    def build(
        cls,
        listings: Mapping[int, "Listing"],
        images_of: Callable[[int], "Sequence[Image]"],
        settings: "Settings",
    ) -> "StockIndex":
        policy = CarrierPolicy.of(settings)
        if not policy.enabled:
            return cls(policy=policy, census={}, exempt=frozenset())
        carriers: dict[int, set[int]] = {}
        pop: dict[int, int] = {}
        for listing_id in listings:
            for image in images_of(listing_id):
                if image.phash is None:
                    continue
                phash = int(image.phash)
                carriers.setdefault(phash, set()).add(listing_id)
                if image.pop is not None:
                    pop[phash] = max(pop.get(phash, 0), int(image.pop))
        broker_of: dict[int, str] = {}
        source_of: dict[int, str] = {}
        block_of: dict[int, str] = {}
        area_of: dict[int, float | None] = {}
        dispo_of: dict[int, str | None] = {}
        for listing_id, listing in listings.items():
            # An unknown identity is its own party: `None` may not collapse two brokers into one.
            broker_of[listing_id] = listing.broker_key or f"?listing:{listing_id}"
            source_of[listing_id] = listing.source or f"?listing:{listing_id}"
            block_of[listing_id] = address_block_key(listing)
            area = listing.area_m2
            area_of[listing_id] = float(area) if area and area > 0.0 else None
            dispo_of[listing_id] = listing.disposition
        census: dict[int, CarrierCensus] = {}
        exempt: set[int] = set()
        for phash, members in carriers.items():
            areas = [area_of[m] for m in members if area_of[m] is not None]
            row = CarrierCensus(
                carriers=len(members),
                brokers=len({broker_of[m] for m in members}),
                sources=len({source_of[m] for m in members}),
                blocks=len({block_of[m] for m in members}),
                pop=pop.get(phash),
                area_spread=((max(areas) - min(areas)) / max(areas)) if areas else 0.0,
                dispositions=len({dispo_of[m] for m in members if dispo_of[m] is not None}),
            )
            census[phash] = row
            if _is_exempt(row, policy):
                exempt.add(phash)
        return cls(policy=policy, census=census, exempt=frozenset(exempt))

    @classmethod
    def of_dataset(cls, dataset: "Dataset | None", settings: "Settings") -> "StockIndex":
        """The index a cohort pass uses; a pass with no dataset falls back to plain E9.

        Falling back is the conservative direction — without carrier identity every populous
        frame stays subtracted, which is what the engine does today."""
        policy = CarrierPolicy.of(settings)
        if dataset is None or not policy.enabled:
            return cls(policy=policy, census={}, exempt=frozenset())
        return cls.build(dataset.listings, dataset.images, settings)

    def is_stock(self, image: "Image", catalog_df: int) -> bool:
        if not image.is_catalog_candidate(catalog_df):
            return False
        if not self.policy.enabled or image.phash is None:
            return True
        return int(image.phash) not in self.exempt

    def token(self) -> str:
        return self.policy.token()


def _is_exempt(row: CarrierCensus, policy: CarrierPolicy) -> bool:
    coverage = row.coverage()
    if coverage is None or coverage < policy.coverage_min:
        return False
    # E60's purity rail, for a photograph: carriers that disagree about the unit hold the
    # project's material, not one advert's, whoever they are.
    if row.dispositions > 1 or row.area_spread > policy.area_tol:
        return False
    limbs = policy.limbs()
    if not limbs:
        return False
    counts = {"brokers": row.brokers, "sources": row.sources, "blocks": row.blocks}
    met = [counts[name] >= bar for name, bar in limbs]
    diverse = any(met) if policy.combine == COMBINE_ANY else all(met)
    return not diverse


PLAIN = StockIndex(policy=CarrierPolicy(), census={}, exempt=frozenset())
