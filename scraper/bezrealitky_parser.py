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
from typing import Any

from scraper.bezrealitky_client import detail_url
from scraper.scraped_listing import ScrapedListing

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
CONSTRUCTION: dict[str, str] = {
    "BRICK": "cihla",
    "PANEL": "panel",
    "MIXED": "smisena",
    "SKELET": "skelet",
    "STONE": "kamen",
    "WOOD": "drevo",
    "PREFAB": "montovana",
}
CONDITION: dict[str, str] = {
    "VERY_GOOD": "velmi_dobry",
    "GOOD": "dobry",
    "BAD": "spatny",
    "CONSTRUCTION": "ve_vystavbe",
    "PROJECT": "projekt",
    "NEW": "novostavba",
    "DEMOLITION": "k_demolici",
    "BEFORE_RECONSTRUCTION": "pred_rekonstrukci",
    "AFTER_RECONSTRUCTION": "po_rekonstrukci",
    "AFTER_PARTIAL_RECONSTRUCTION": "po_rekonstrukci",
    "IN_RECONSTRUCTION": "v_rekonstrukci",
}
OWNERSHIP: dict[str, str] = {
    "OSOBNI": "osobni",
    "DRUZSTEVNI": "druzstevni",
    "OBECNI": "statni",
}
FURNISHED: dict[str, str] = {
    "VYBAVENY": "ano",
    "NEVYBAVENY": "ne",
    "CASTECNE": "castecne",
}

_DISP_RE = re.compile(r"DISP_(\d)_(KK|1|IZB)")


def _disposition(value: str | None) -> str | None:
    if not value:
        return None
    if value == "GARSONIERA":
        return "1+kk"
    m = _DISP_RE.fullmatch(value)
    if not m:
        return None
    n, kind = m.group(1), m.group(2)
    return f"{n}+kk" if kind == "KK" else f"{n}+1"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f or None  # treat 0 as "not specified" (bezrealitky's empty sentinel)


def _int(value: Any) -> int | None:
    f = _num(value)
    return int(f) if f is not None else None


def _surface_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return _num(value) is not None


def _energy(value: str | None) -> str | None:
    return value if value in ("A", "B", "C", "D", "E", "F", "G") else None


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

    gps = advert.get("gps") or {}
    lat = _num(gps.get("lat"))
    lon = _num(gps.get("lng"))

    balcony_surfaces = [
        advert.get("balconySurface"),
        advert.get("terraceSurface"),
        advert.get("loggiaSurface"),
    ]
    has_balcony = (
        None
        if all(v is None for v in balcony_surfaces)
        else any(_num(v) is not None for v in balcony_surfaces)
    )
    garage = bool(advert.get("garage")) if advert.get("garage") is not None else None
    has_parking = bool(advert.get("parking") or advert.get("garage"))

    raw = dict(advert)
    raw["image_urls"] = _image_urls(advert)

    return ScrapedListing(
        source=SOURCE,
        source_id_native=native_id,
        source_url=detail_url(uri),
        category_main=category_main,
        category_type=category_type,
        price_czk=_int(advert.get("price")),
        price_unit="měsíc" if category_type == "pronajem" else "celkem",
        area_m2=_num(advert.get("surface")),
        disposition=_disposition(advert.get("disposition")),
        locality=_locality(advert),
        district=None,
        lat=lat,
        lon=lon,
        floor=_int(advert.get("etage")),
        total_floors=_int(advert.get("totalFloors")),
        has_balcony=has_balcony,
        has_parking=has_parking,
        has_lift=bool(advert["lift"]) if advert.get("lift") is not None else None,
        building_type=CONSTRUCTION.get(advert.get("construction")),
        condition=CONDITION.get(advert.get("condition")),
        energy_rating=_energy(advert.get("penb")),
        estate_area=_num(advert.get("surfaceLand")),
        usable_area=_num(advert.get("surface")),
        garden_area=_num(advert.get("frontGarden")),
        category_sub_cb=None,
        furnished=FURNISHED.get(advert.get("equipped")),
        terrace=_surface_bool(advert.get("terraceSurface")),
        cellar=_surface_bool(advert.get("cellarSurface")),
        garage=garage,
        parking_lots=None,
        ownership=OWNERSHIP.get(advert.get("ownership")),
        description=(advert.get("description") or "").strip() or None,
        raw=raw,
    )
