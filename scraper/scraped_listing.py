"""The shared ScrapedListing ingestion contract (multi-portal dedup).

One normalized shape every non-sreality portal scraper emits. It carries the
cross-source identity (`source` + `source_id_native`, the Tier-0 idempotency
key) plus the subset of `listings` columns the matcher and analytics read.
`scraper.db.ingest_scraped_listing` turns one of these into a `listings` row
(assigning a synthetic negative PK on first sight) and runs the Tier-1 matcher.

Sreality keeps its own JSON parse path (`scraper.parser` -> `upsert_listing`);
this contract is for the HTML/crawler sources (bazos first).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# Fields whose change should append a listing_snapshots row. Mirrors the
# semantics of sreality's content hash: identity (source ids, url) is NOT
# hashed; the displayed/analytical content is. lat/lon are deliberately NOT
# hashed: coords are derived/geocoded data prone to oscillation (the
# geocode-skip cycle), and listings.geom updates on every upsert regardless of
# snapshots; a genuine location change surfaces via locality/description.
# street / house_number / zip are likewise NOT hashed — they are
# extracted/derived (regex over the title+description for bazos, a comma-split
# of the locality for idnes/maxima/remax), so backfilling or refining them must
# never churn snapshots. published_at is NOT hashed either: it is portal
# lifecycle metadata, not listing content — bazos re-stamps it on every bump /
# TOP renewal, so hashing it would append a snapshot per seller promotion, and
# backfills from already-stored raw must stay snapshot-free.
_HASH_FIELDS: tuple[str, ...] = (
    "category_main", "category_type", "price_czk", "price_unit", "area_m2",
    "disposition", "locality", "district", "floor",
    "total_floors", "has_balcony", "has_parking", "has_lift", "building_type",
    "condition", "energy_rating", "estate_area", "usable_area", "garden_area",
    "category_sub_cb", "subtype", "furnished", "terrace", "cellar", "garage",
    "parking_lots", "ownership", "description",
)

# The ScrapedListing fields that map 1:1 onto `listings` columns (a subset of
# scraper.db.LISTING_COLUMNS — sreality-only locality ids are left NULL).
_LISTING_FIELDS: tuple[str, ...] = (
    "category_main", "category_type", "price_czk", "price_unit", "area_m2",
    "disposition", "locality", "district", "street", "house_number", "zip",
    "floor", "total_floors",
    "has_balcony", "has_parking", "has_lift", "building_type", "condition",
    "energy_rating", "estate_area", "usable_area", "garden_area",
    "category_sub_cb", "subtype", "furnished", "terrace", "cellar", "garage",
    "parking_lots", "ownership", "description", "published_at",
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
    disposition: str | None = None
    locality: str | None = None
    district: str | None = None
    # Best-effort street name (the dedup engine's street_key input); extracted,
    # not portal-structured, so it stays out of the content hash. house_number
    # / zip are structured where a portal carries them (bezrealitky today) — the
    # DB columns + LISTING_COLUMNS already exist; the contract just gains a slot.
    street: str | None = None
    house_number: str | None = None
    zip: str | None = None
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

    def content_hash(self) -> str:
        payload = {k: getattr(self, k) for k in _HASH_FIELDS}
        blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_row(self, sreality_id: int | None) -> dict[str, Any]:
        """The dict scraper.db.upsert_listing consumes. Listing columns this
        contract doesn't carry default to NULL via upsert_listing's row.get.
        `sreality_id` is None once the Gate-2 flip-writer (scraper.db's
        `gate2_null_sreality_id_enabled` app_settings flag) is turned on for a
        first-sight non-sreality row — NULL, not a synthetic negative."""
        row: dict[str, Any] = {k: getattr(self, k) for k in _LISTING_FIELDS}
        row["sreality_id"] = sreality_id
        row["lon"] = self.lon
        row["lat"] = self.lat
        return row
