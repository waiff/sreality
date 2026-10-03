"""The shared ScrapedListing ingestion contract (the eight crawler portals).

One normalized shape every non-sreality portal scraper emits. It carries the
cross-source identity (`source` + `source_id_native`, the natural key) plus the
subset of `listings` columns analytics read. `listing_write.from_scraped` turns
one of these into a write (a synthetic negative `sreality_id` on first sight
while Gate 2 is off); grouping is out-of-band (rule #15: no insert-time matching).

Sreality keeps its own JSON parse path (`scraper.parser` -> `listing_write.from_sreality`);
this contract is for the HTML/crawler sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from scraper.area import stated_plot

# Fields whose change should append a listing_snapshots row. Mirrors the
# semantics of sreality's content hash: identity (source ids, url) is NOT
# hashed; the displayed/analytical content is. lat/lon are deliberately NOT
# hashed: coords are derived/geocoded data prone to oscillation (the
# geocode-skip cycle), and W4-c dropped geom (a pin rides raw_json as a claim);
# a genuine location change surfaces via locality/description.
# street / house_number / zip are likewise NOT hashed — they are
# extracted/derived (e.g. a comma-split of the locality for idnes/maxima/remax),
# so backfilling or refining them must never churn snapshots. published_at is
# NOT hashed either: it is portal lifecycle metadata, not listing content — bazos
# re-stamps it on every bump / TOP renewal, so hashing it would append a snapshot
# per seller promotion, and backfills from already-stored raw must stay snapshot-free.
_HASH_FIELDS: tuple[str, ...] = (
    "category_main", "category_type", "price_czk", "price_unit", "area_m2",
    "disposition", "locality", "district", "floor",
    "total_floors", "has_balcony", "has_parking", "has_lift", "building_type",
    "condition", "energy_rating", "estate_area", "usable_area", "garden_area",
    "category_sub_cb", "subtype", "furnished", "terrace", "cellar", "garage",
    "parking_lots", "ownership", "description",
)

# The ScrapedListing fields that map 1:1 onto `listings` columns (a subset of
# scraper.db.LISTING_COLUMNS). W4-c removed the place columns from that table, so
# `locality`/`district`/`street`/`house_number`/`zip` are NOT here any more: they
# survive on the contract below as parsed portal evidence (and, for locality and
# district, as content-hash inputs — see _HASH_FIELDS), not as columns. A
# listing's location is `listing_location`, resolved from the portal payload.
_LISTING_FIELDS: tuple[str, ...] = (
    "category_main", "category_type", "price_czk", "price_unit", "area_m2",
    "area_basis",
    "disposition",
    "floor", "total_floors",
    "has_balcony", "has_parking", "has_lift", "building_type", "condition",
    "energy_rating", "estate_area", "usable_area", "garden_area",
    "category_sub_cb", "subtype", "furnished", "terrace", "cellar", "garage",
    "parking_lots", "ownership", "description", "published_at",
    # The listing's page on its portal — identity, not content (never hashed); rides
    # LISTING_COLUMNS like every other column (docs/design/portal-listing-url.md).
    "source_url",
)


@dataclass(frozen=True)
class ScrapedListing:
    source: str
    source_id_native: str
    source_url: str
    category_main: str | None = None
    category_type: str | None = None
    price_czk: int | None = None
    price_unit: str | None = None
    area_m2: float | None = None
    # Which physical area `area_m2` holds ('usable'|'floor'|'total'|'plot'|
    # 'unknown'), stamped by scraper.area.derive_headline_area. A PROVENANCE
    # observation, never a value change — deliberately OUT of _HASH_FIELDS so
    # stamping it churns no snapshot (rule 2).
    area_basis: str | None = None
    disposition: str | None = None
    locality: str | None = None
    district: str | None = None
    # Best-effort street name, extracted (not portal-structured), so it stays out
    # of the content hash. house_number / zip are structured where a portal
    # carries them (bezrealitky today). None of the three is a `listings` column
    # after W4-c: they are the parser's reading of the page, kept on the contract
    # as evidence beside raw_json, and the address a listing HAS is whatever the
    # resolver wrote to listing_location.
    street: str | None = None
    house_number: str | None = None
    zip: str | None = None
    # The portal's own pin, when its page publishes one. Not a column either
    # (W4-c dropped listings.geom); it rides raw_json as the coordinate claim
    # the resolver arbitrates under location_data.claims_common.
    lat: float | None = None
    lon: float | None = None
    floor: int | None = None
    total_floors: int | None = None
    has_balcony: bool | None = None
    has_parking: bool | None = None
    has_lift: bool | None = None
    building_type: str | None = None
    condition: str | None = None
    energy_rating: str | None = None
    estate_area: float | None = None
    usable_area: float | None = None
    garden_area: float | None = None
    category_sub_cb: int | None = None
    # Portal-agnostic normalized property sub-type (migration 152); per-portal
    # parsers derive it from their own structured signal, else leave it None.
    subtype: str | None = None
    furnished: str | None = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    parking_lots: int | None = None
    ownership: str | None = None
    description: str | None = None
    # Portal-declared publication/last-bump timestamp (migration 266). A date
    # for the day-granular portals (bazos, ceskereality — stored as midnight
    # UTC in the timestamptz column), a full datetime where the portal exposes
    # one (bezrealitky's timeActivated). Out of the content hash (see above).
    published_at: datetime | date | None = None
    # The source's own payload, stored verbatim in listings.raw_json.
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # `source_url: str` was never enforced. With the column preserve-if-null at the
        # write, a None from a parser would silently KEEP a stale URL instead of clearing
        # it — so refuse at the contract boundary (fails one drain item, not the lane).
        if not isinstance(self.source_url, str) or not self.source_url.strip():
            raise ValueError(
                f"{self.source}/{self.source_id_native}: source_url is required"
            )
        # The ONE call site of the plot rule for every portal that crosses this contract
        # (rule 21): the last point before `content_hash`, so a declined plot is hashed
        # as absence (rules 2/8) rather than NULLed after the hash at the write boundary.
        object.__setattr__(self, "estate_area", stated_plot(
            self.category_main, self.estate_area, usable=self.usable_area,
            headline=self.area_m2, headline_basis=self.area_basis))

    def hash_doc(self) -> dict[str, Any]:
        """The crawlers' hash document: the 28 _HASH_FIELDS, unhashed (hashing.digest hashes it)."""
        return {k: getattr(self, k) for k in _HASH_FIELDS}

    def listing_columns(self) -> dict[str, Any]:
        """The `listings` column values this contract carries; the rest default to NULL."""
        return {k: getattr(self, k) for k in _LISTING_FIELDS}

