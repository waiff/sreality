"""Map a bezrealitky GraphQL advert object onto the shared ScrapedListing.

Pure functions, no I/O. Bezrealitky's API returns fully structured enums, so
unlike the bazos free-text crawler there is no regex mining — the work is
translating bezrealitky's enum vocabulary into the SAME canonical label strings
sreality stores (verified against the live `listings` table), so cross-source
filtering, dedup, and condition scoring see one vocabulary. Coordinates come
straight from `gps` (precise per-listing), so no geocoding step is needed.
"""

from __future__ import annotations

import re
from functools import partial
from typing import Any

from scraper import vocabulary
from scraper.area import derive_headline_area
from scraper.attribute_contract import source_value, source_values
from scraper.bezrealitky_client import detail_url
from scraper.published import iso_datetime
from scraper.scraped_listing import ScrapedListing
from scraper.street import clean_street

SOURCE = "bezrealitky"

# bezrealitky enum -> our canonical labels (matching scraper.parser conventions
# and the actual values stored for sreality rows).
OFFER_TYPE: dict[str, str] = {"PRODEJ": "prodej", "PRONAJEM": "pronajem"}
ESTATE_TYPE: dict[str, str] = {
    "BYT": "byt",
    "DUM": "dum",
    "POZEMEK": "pozemek",
    "KANCELAR": "komercni",
    "NEBYTOVY_PROSTOR": "komercni",
    "GARAZ": "ostatni",
    "REKREACNI_OBJEKT": "ostatni",
}
# Portal-agnostic subtype (migration 152) where the estateType is specific
# enough. Only KANCELAR carries a clean sub-type; the other commercial value
# (NEBYTOVY_PROSTOR) is generic and DUM has no house granularity, so they stay
# None.
ESTATE_SUBTYPE: dict[str, str] = {
    "KANCELAR": "kancelar",
}



def _num(value: Any) -> float | None:
    """A number from the GraphQL advert, or None.

    `bool` is refused BEFORE `float`: `float(True)` is 1.0, and bezrealitky's advert
    object carries boolean flags beside its measures, so one renamed key ("frontGarden"
    the size becoming "frontGarden" the flag) would have written a 1.0 m² garden into
    `garden_area` — a plausible-looking measurement, which is the worst kind of wrong.
    A 0 is bezrealitky's empty sentinel, never a measurement.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f or None


def _int(value: Any) -> int | None:
    f = _num(value)
    return int(f) if f is not None else None


def _surface_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return _num(value) is not None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _locality(advert: dict[str, Any]) -> str | None:
    city = advert.get("city")
    quarter = advert.get("cityDistrict")
    parts = [p for p in (city, quarter) if isinstance(p, str) and p.strip()]
    if not parts:
        return None
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    return " - ".join(parts)


def _image_urls(advert: dict[str, Any]) -> list[str]:
    images = advert.get("publicImages") or []
    ordered = sorted(
        (img for img in images if img.get("url")),
        key=lambda img: img.get("order") or 0,
    )
    urls = [img["url"] for img in ordered]
    if urls:
        return urls
    main = (advert.get("mainImage") or {}).get("url")
    return [main] if main else []


def parse_advert(advert: dict[str, Any]) -> ScrapedListing:
    native_id = str(advert["id"])
    uri = advert.get("uri") or native_id
    category_type = OFFER_TYPE.get(advert.get("offerType"))
    category_main = ESTATE_TYPE.get(advert.get("estateType"))
    subtype = ESTATE_SUBTYPE.get(advert.get("estateType"))

    gps = advert.get("gps") or {}
    lat = _num(gps.get("lat"))
    lon = _num(gps.get("lng"))

    read = partial(source_value, SOURCE, params=advert)
    balcony_surfaces = source_values(SOURCE, "has_balcony", advert)
    has_balcony = (
        None
        if all(v is None for v in balcony_surfaces)
        else any(_num(v) is not None for v in balcony_surfaces)
    )
    garage = bool(read("garage")) if read("garage") is not None else None
    # The one cell anywhere whose ABSENCE has always meant false, which is why the
    # contract declares it (`absence="false"`) instead of a helper guessing.
    has_parking = bool(any(source_values(SOURCE, "has_parking", advert)))

    raw = dict(advert)
    raw["image_urls"] = _image_urls(advert)

    # `surface` is bezrealitky's interior measure (it also feeds usable_area);
    # `surfaceLand` is the parcel (it also feeds estate_area). Both go to the one
    # resolver — before W17 only `surface` did, and 2,654 of 2,667 land rows had a
    # parcel in `estate_area` and nothing in `area_m2`.
    surface_land = _num(advert.get("surfaceLand"))
    area_m2, area_basis = derive_headline_area(
        category_main=category_main, usable=_num(advert.get("surface")),
        plot=surface_land,
    )

    return ScrapedListing(
        source=SOURCE,
        source_id_native=native_id,
        source_url=detail_url(uri),
        category_main=category_main,
        category_type=category_type,
        subtype=subtype,
        price_czk=_int(advert.get("price")),
        price_unit="měsíc" if category_type == "pronajem" else "celkem",
        area_m2=area_m2,
        area_basis=area_basis,
        disposition=vocabulary.disposition_code(read("disposition")),
        locality=_locality(advert),
        district=None,
        # bezrealitky's GraphQL advert carries structured street/houseNumber/zip
        # (the only portal that gives the full triple deterministically).
        street=clean_street(_str_or_none(advert.get("street"))),
        house_number=_str_or_none(advert.get("houseNumber")),
        zip=_str_or_none(advert.get("zip")),
        lat=lat,
        lon=lon,
        floor=_int(advert.get("etage")),
        total_floors=_int(advert.get("totalFloors")),
        has_balcony=has_balcony,
        has_parking=has_parking,
        has_lift=bool(advert["lift"]) if advert.get("lift") is not None else None,
        building_type=vocabulary.canonical("building_type", SOURCE, read("building_type")),
        condition=vocabulary.canonical("condition", SOURCE, read("condition")),
        energy_rating=vocabulary.energy_rating(read("energy_rating")),
        estate_area=surface_land,
        usable_area=_num(advert.get("surface")),
        # `frontGarden` is bezrealitky's FRONT YARD ("předzahrádka"), the strip in
        # front of a ground-floor flat — not a house's garden. It is the only
        # garden-shaped measure the advert publishes, so it is what garden_area
        # carries here; read it as that, not as a parcel (rule 23's side columns).
        garden_area=_num(advert.get("frontGarden")),
        category_sub_cb=None,
        furnished=vocabulary.canonical("furnished", SOURCE, read("furnished")),
        terrace=_surface_bool(advert.get("terraceSurface")),
        cellar=_surface_bool(advert.get("cellarSurface")),
        garage=garage,
        parking_lots=None,
        ownership=vocabulary.canonical("ownership", SOURCE, read("ownership")),
        description=(advert.get("description") or "").strip() or None,
        # The detail query requests timeActivated but the anon API returns it
        # NULL today — the mapping is free if access ever appears (the one
        # portal that would carry a REAL timestamp, not a day).
        published_at=iso_datetime(advert.get("timeActivated")),
        raw=raw,
    )
