"""reas.cz sold pages -> `sold_transactions` rows. Payload in, rows out; no I/O.

A registered sale is an account-less external FACT, not a listing. This module maps one
`/prodane/...` page's embedded `__NEXT_DATA__` blob onto rows whose keys ARE the
`sold_transactions` columns (migration 542), and refuses anything it cannot honestly call
a cadastre-registered sale.

THE THREE REFUSALS, each a silent-failure class that has already been observed:
  * The ACTIVE catalogue served under a 200. reas's `/_next/data/<buildId>/prodane/...`
    route loses the `/prodane` rewrite and returns the live-listings catalogue with no
    sold price on any record. `adsListParams.linkedToTransfer` is THE discriminator and
    it is checked before a single row is read.
  * A record with no cadastral transfer identity. `mapPointerId` is
    `<transferId>_unit_<buildingId>-<cp>-<unitNo>` or `<transferId>_building_<buildingId>`;
    a transferId this module cannot classify means the feed's identity grammar moved, and
    that must be loud, not stored under a fabricated key.
  * An unknown `type`. The sold catalogue is flats and houses only (proven three ways:
    the sold sitemap is `byty` + `domy` and nothing else; zero parcels in 308 records
    despite the query asking for them; reas's own filter enum is closed at four members).
    A fifth type is a contract change, not a row to guess at.

Broker-reported records are DROPPED AND COUNTED, not refused: ~1% of the feed is
reas's own self-reported sale rather than a cadastre transfer, recognisable because its
transferId is a Mongo ObjectId instead of a number. They are a different kind of claim, so
they do not enter a table of registered transfers — but their presence is normal and must
not fail a page.

NEVER MAPPED, and not in `raw` either: `displayArea` (reas's own headline, computed by a
rule that is not ours — `min(utility, floor)` on a flat against our usable-first
precedence, a 30% area gap on 3% of flats and so a 43% per-m² gap), `histogramPrice`
(`soldPrice` indexed to today: identity within 12 months, ×1.10–1.28 beyond it) and
`originalPrice` (corrupt — one observed record carries 1 Kč against a 1,190,000 Kč
asking price). Seller and broker identity is dropped at parse time for the same reason
a committed fixture is scrubbed: we do not hold it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from scraper.area import derive_headline_area
from toolkit.filter_registry import DISPOSITION_OPTIONS

SOURCE = "reas"

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

# reas `type` -> our `category_main`. Closed on purpose; see the module docstring.
_CATEGORY_MAIN: dict[str, str] = {"flat": "byt", "building": "dum"}

# reas `subType` -> an EXISTING `SUBTYPE_OPTIONS` target. `flat` has no meaningful
# subtype for us and `recreational_building` is ambiguous between chata and chalupa;
# both, and anything new, resolve to NULL. Adding an enum member here would push a
# sold-only value into Browse, the watchdog matcher and the comparables agent.
_SUBTYPE: dict[str, str] = {
    "family_house": "rodinny_dum",
    "villa": "vila",
    "hut": "chata",
    "cottage": "chalupa",
}

# The ten dispositions that are a literal pass-through. reas also emits `larger`
# ("5 and above, room count not stated") and `atypic`, which have no target and become
# NULL — 0.68% of the sold population, against widening the shared enum for all of it.
_DISPOSITIONS: frozenset[str] = frozenset(option.value for option in DISPOSITION_OPTIONS)

_OBJECT_ID_RE = re.compile(r"[0-9a-f]{24}\Z")

# Dropped before the record is read, so they reach neither a column nor `raw`.
_NEVER_STORED: frozenset[str] = frozenset({
    "sellerDetails", "companyDetails",
    "displayArea", "histogramPrice", "originalPrice",
})


class ReasPayloadError(ValueError):
    """The payload is not a page of cadastre-registered sales."""


@dataclass(frozen=True)
class SoldTransaction:
    """One row of `sold_transactions`. Every field is a column of that table.

    `fetched_at` is the one column absent here: the parser did not fetch anything, and
    the column's `default now()` is stamped by the writer.
    """

    source: str
    source_record_id: str
    sold_at: date
    price_czk: int
    asking_last_czk: int | None
    listed_at: datetime | None
    published_at: datetime | None
    category_main: str
    category_type: str
    subtype: str | None
    disposition: str | None
    area_m2: float | None
    area_basis: str | None
    usable_area: float | None
    estate_area: float | None
    geom: str
    address_text: str | None
    obec_kod: int | None
    ku_kod: int | None
    ulice_kod: int | None
    # A list, never a tuple: psycopg adapts a list to an array and a tuple to a record.
    photo_urls: list[str]
    source_url: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class SoldPage:
    """One page of rows plus what the caller needs to paginate and write the ledger."""

    rows: list[SoldTransaction]
    count: int
    next_page: int | None
    dropped_broker_reported: int


def parse_sold_html(html: str) -> SoldPage:
    """Parse a `/prodane/...` page. The SSR HTML is the unit of fetch."""
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        raise ReasPayloadError("page carries no __NEXT_DATA__ script")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ReasPayloadError(f"__NEXT_DATA__ is not JSON: {exc}") from exc
    return parse_sold_payload(payload)


def parse_sold_payload(payload: Mapping[str, Any]) -> SoldPage:
    """Parse an already-decoded `__NEXT_DATA__` blob."""
    props = payload.get("props")
    root = props if isinstance(props, Mapping) else payload
    page_props = root.get("pageProps")
    if not isinstance(page_props, Mapping):
        raise ReasPayloadError("payload carries no pageProps")

    params = page_props.get("adsListParams")
    linked = params.get("linkedToTransfer") if isinstance(params, Mapping) else None
    if linked is not True:
        raise ReasPayloadError(
            f"not the sold catalogue: adsListParams.linkedToTransfer is {linked!r} "
            "(the _next/data route answers 200 with the ACTIVE catalogue)"
        )

    result = page_props.get("adsListResult")
    if not isinstance(result, Mapping):
        raise ReasPayloadError("payload carries no adsListResult")
    count = result.get("count")
    if not isinstance(count, int):
        raise ReasPayloadError(f"adsListResult.count is {count!r}, not a number")
    next_page = result.get("nextPage")
    if next_page is not None and not isinstance(next_page, int):
        raise ReasPayloadError(f"adsListResult.nextPage is {next_page!r}")

    records = result.get("data")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ReasPayloadError("adsListResult.data is not a list")

    rows: list[SoldTransaction] = []
    dropped = 0
    for record in records:
        if not isinstance(record, Mapping):
            raise ReasPayloadError("adsListResult.data holds a non-record")
        row = _row(record)
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    return SoldPage(
        rows=rows, count=count, next_page=next_page, dropped_broker_reported=dropped
    )


def _row(record: Mapping[str, Any]) -> SoldTransaction | None:
    """One record -> one row, or None when it is broker-reported (dropped + counted)."""
    map_pointer_id = record.get("mapPointerId")
    if not isinstance(map_pointer_id, str) or not map_pointer_id:
        raise ReasPayloadError("record carries no mapPointerId")
    transfer_id = map_pointer_id.split("_", 1)[0]
    if not transfer_id.isdigit():
        if _OBJECT_ID_RE.fullmatch(transfer_id):
            return None
        raise ReasPayloadError(
            f"mapPointerId {map_pointer_id!r} carries no cadastral transfer id"
        )

    reas_type = record.get("type")
    category_main = _CATEGORY_MAIN.get(reas_type) if isinstance(reas_type, str) else None
    if category_main is None:
        raise ReasPayloadError(f"unknown reas type {reas_type!r}")

    price_czk = record.get("soldPrice")
    if not isinstance(price_czk, int) or price_czk <= 0:
        raise ReasPayloadError(
            f"{map_pointer_id}: soldPrice is {price_czk!r} on a sold record"
        )

    sold_at = _instant(record.get("soldAt"))
    if sold_at is None:
        raise ReasPayloadError(f"{map_pointer_id}: soldAt is {record.get('soldAt')!r}")

    coordinates = record.get("point")
    coordinates = coordinates.get("coordinates") if isinstance(coordinates, Mapping) else None
    if not isinstance(coordinates, Sequence) or len(coordinates) != 2:
        raise ReasPayloadError(f"{map_pointer_id}: point.coordinates is not a pair")
    lng, lat = float(coordinates[0]), float(coordinates[1])

    usable_area = _number(record.get("utilityArea"))
    estate_area = _number(record.get("landArea"))
    area_m2, area_basis = derive_headline_area(
        category_main=category_main,
        usable=usable_area,
        floor=_number(record.get("floorArea")),
    )

    disposition = record.get("disposition")
    subtype = record.get("subType")

    return SoldTransaction(
        source=SOURCE,
        source_record_id=map_pointer_id,
        sold_at=sold_at.date(),
        price_czk=price_czk,
        asking_last_czk=record.get("price") if isinstance(record.get("price"), int) else None,
        listed_at=_instant(record.get("firstVisibleAt")),
        published_at=_instant(record.get("mapPointerPublishedAt")),
        category_main=category_main,
        category_type="prodej",
        subtype=_SUBTYPE.get(subtype) if isinstance(subtype, str) else None,
        disposition=disposition if disposition in _DISPOSITIONS else None,
        area_m2=area_m2,
        area_basis=area_basis,
        usable_area=usable_area,
        estate_area=estate_area,
        geom=f"SRID=4326;POINT({lng} {lat})",
        address_text=_text(record.get("formattedAddress")),
        obec_kod=_code(record.get("municipalityId")),
        ku_kod=_code(record.get("cadastralAreaId")),
        ulice_kod=_code(record.get("streetId")),
        photo_urls=_photo_urls(record.get("imagesWithMetadata")),
        source_url=_text(record.get("link")),
        raw={k: v for k, v in record.items() if k not in _NEVER_STORED},
    )


def _photo_urls(images: Any) -> list[str]:
    """The source's own public URLs, in the order the source renders them."""
    if not isinstance(images, Sequence) or isinstance(images, (str, bytes)):
        return []
    usable = [
        image for image in images
        if isinstance(image, Mapping) and isinstance(image.get("original"), str)
    ]
    usable.sort(key=lambda image: image.get("order") if isinstance(image.get("order"), int) else 0)
    return [image["original"] for image in usable]


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _code(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
