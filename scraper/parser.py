"""Map a Sreality v1 detail JSON response to a row dict matching the schema.

The detail endpoint (`/api/v1/estates/{id}`) returns a wrapped estate object
(`{result, status_code, status_message}`, unwrapped by the client). The estate
is flat snake_case: typed attributes sit at the top level (`usable_area`,
`floor_number`, `building_condition`, …), `locality` holds geo, and
`advert_images` is the gallery. Enum fields arrive as `{name, value}` objects;
sreality uses `value == 0` for "not specified", treated as None. The id field
is `hash_id`.
"""

from __future__ import annotations

from functools import partial
from typing import Any
from unicodedata import combining, normalize

from scraper import sreality_url, vocabulary
from scraper.area import derive_headline_area
from scraper.attribute_contract import floor_convention, source_label, source_value, source_values
from scraper.floor import floor_from_portal
from scraper.published import iso_date

SOURCE = "sreality"

CATEGORY_MAIN: dict[int, str] = {
    1: "byt",
    2: "dum",
    3: "pozemek",
    4: "komercni",
    5: "ostatni",
}

CATEGORY_TYPE: dict[int, str] = {
    1: "prodej",
    2: "pronajem",
    3: "drazba",
    # cb=4 is "prodej podílu nemovitosti" (sale of a fractional ownership
    # share). It IS its own search slice (category_type_cb=4 is a valid filter),
    # walked as its own pair in main.CATEGORIES so it gets a complete index walk
    # and mark_inactive.
    4: "podil",
}


# Portal-agnostic property sub-type, normalized from sreality's category_sub_cb
# code. House (dum) and commercial (komercni) codes occupy disjoint integer
# ranges, so one flat map needs no category_main disambiguation. Apartment /
# land sub-codes are deliberately omitted — `subtype` is only meaningful for
# dum/komercni; everything else (and unknown codes) returns None. Slugs follow
# the diacritics-free convention of category_main / category_type. Other portals
# populate the same column from their own structured signal (see each parser).
SUBTYPE: dict[int, str] = {
    # dum (houses)
    37: "rodinny_dum",          # Rodinný dům
    33: "chata",                # Chata
    43: "chalupa",              # Chalupa
    54: "vicegeneracni_dum",    # Vícegenerační dům
    39: "vila",                 # Vila
    44: "zemedelska_usedlost",  # Zemědělská usedlost
    40: "na_klic",              # Na klíč
    35: "pamatka_jine",         # Památka/jiné
    # komercni (commercial)
    25: "kancelar",             # Kanceláře
    26: "sklad",                # Sklady
    28: "obchodni_prostor",     # Obchodní prostory
    27: "vyroba",               # Výroba
    29: "ubytovani",            # Ubytování
    32: "ostatni",              # Ostatní
    38: "cinzovni_dum",         # Činžovní dům
    30: "restaurace",           # Restaurace
    57: "apartmany",            # Apartmány
    56: "ordinace",             # Ordinace
    31: "zemedelsky",           # Zemědělský objekt
    49: "virtualni_kancelar",   # Virtuální kancelář
}





def parse_listing(raw: dict[str, Any]) -> dict[str, Any]:
    sreality_id = _int_or_none(raw.get("hash_id"))
    if sreality_id is None:
        sreality_id = _int_or_none(raw.get("id"))
    if sreality_id is None:
        raise ValueError("could not determine sreality_id from response")

    category_main = CATEGORY_MAIN.get(_cb_value(raw.get("category_main_cb")))
    # sreality's only interior measure is `usable_area`; `estate_area` is the parcel,
    # which it publishes on every land row and this parser used to keep OUT of the
    # headline (44,237 of 44,237 land rows carried area_m2 NULL). Both reach the one
    # resolver; which of them becomes the headline is the resolver's call, not ours.
    read = partial(source_value, SOURCE, params=raw)
    label = partial(source_label, SOURCE, params=raw)
    estate_area = _numeric_or_none(raw.get("estate_area"))
    # `parking_lots` is the BOOLEAN and `parking` the count — the payload's names are the
    # opposite way round from the columns'. All three arms are the property's own (R11).
    lots_flag, garage_flag, parking_count = source_values(SOURCE, "has_parking", raw)
    area_m2, area_basis = derive_headline_area(
        category_main=category_main,
        usable=_numeric_or_none(raw.get("usable_area")),
        plot=estate_area,
    )

    return {
        "sreality_id": sreality_id,
        "category_main": category_main,
        "category_type": CATEGORY_TYPE.get(_cb_value(raw.get("category_type_cb"))),
        "price_czk": _price_czk(raw),
        "price_unit": _price_unit(raw),
        "area_m2": area_m2,
        "area_basis": area_basis,
        "disposition": vocabulary.disposition(
            SOURCE, *(_cb_name(v) for v in source_values(SOURCE, "disposition", raw))
        ),
        "floor": floor_from_portal(floor_convention(SOURCE), read("floor")),
        "total_floors": _int_or_none(read("total_floors")),
        "has_balcony": vocabulary.any_true(
            *(vocabulary.yes_no(v) for v in source_values(SOURCE, "has_balcony", raw))
        ),
        "has_parking": vocabulary.any_true(
            vocabulary.yes_no(lots_flag), vocabulary.yes_no(garage_flag),
            None if (count := _int_or_none(parking_count)) is None else count > 0,
        ),
        "has_lift": vocabulary.yes_no(label("has_lift")),
        "building_type": vocabulary.canonical("building_type", SOURCE, label("building_type")),
        "condition": vocabulary.canonical("condition", SOURCE, label("condition")),
        "energy_rating": vocabulary.energy_rating(label("energy_rating")),
        "estate_area": estate_area,
        "usable_area": _numeric_or_none(read("usable_area")),
        "garden_area": _numeric_or_none(read("garden_area")),
        "category_sub_cb": _cb_value(read("category_sub_cb")),
        "subtype": SUBTYPE.get(_cb_value(read("subtype"))),
        "furnished": vocabulary.canonical("furnished", SOURCE, label("furnished")),
        "terrace": vocabulary.yes_no(read("terrace")),
        "cellar": vocabulary.yes_no(read("cellar")),
        "garage": vocabulary.yes_no(read("garage")),
        "parking_lots": _int_or_none(read("parking_lots")),
        "ownership": vocabulary.canonical("ownership", SOURCE, label("ownership")),
        "description": _description(raw),
        # sreality exposes no publish date — `edited` (day-granular last-edit,
        # present on ~40% of rows) is the weak fallback bound for publish-to-
        # ingest SLO math, not first publication.
        "published_at": iso_date(raw.get("edited")),
        # The listing's page on sreality — a stored FACT every surface reads and none
        # reconstructs (docs/design/portal-listing-url.md). Assembled from sreality's own
        # seo names + its closed sub-category codebook; None (counted upstream) when a
        # part is missing, and preserve-if-null at the write so a None never erases.
        "source_url": sreality_url.from_payload(raw)[0],
    }


def locality_coords(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    """The payload's own pin, as (lat, lon).

    NOT part of the listings row: W4-c dropped `listings.geom`, so a coordinate
    reaches the database only as a claim the resolver arbitrates. This is the
    on-demand URL-parse path (scraper.url_parser), where the estimation spec
    needs the subject's point in the same request that fetched it.
    """
    loc = raw.get("locality") or {}
    def _f(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return _f(loc.get("gps_lat")), _f(loc.get("gps_lon"))


def parse_images(raw: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for img in raw.get("advert_images") or []:
        if not isinstance(img, dict):
            continue
        url = img.get("url")
        if not isinstance(url, str) or not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        out.append({"url": url, "sequence": img.get("order")})
    return out


def _cb_name(obj: Any) -> str | None:
    """A `{name, value}` enum's label, or the string itself (`advert_name`)."""
    if isinstance(obj, dict):
        name = obj.get("name")
        return name if isinstance(name, str) else None
    return obj if isinstance(obj, str) else None


def _cb_value(obj: Any) -> int | None:
    """Integer enum code from a {name, value} object; 0 ('not specified') → None."""
    if isinstance(obj, dict):
        v = obj.get("value")
        if isinstance(v, int) and not isinstance(v, bool) and v != 0:
            return v
    return None


def _price_czk(raw: dict[str, Any]) -> int | None:
    for key in ("price_summary_czk", "price_czk"):
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(v)
    return None


def _price_unit(raw: dict[str, Any]) -> str | None:
    for key in ("price_summary_unit_cb", "price_unit_cb"):
        obj = raw.get(key)
        if isinstance(obj, dict):
            name = obj.get("name")
            if isinstance(name, str):
                ascii_name = _strip_diacritics(name.lower())
                if "mesic" in ascii_name:
                    return "za mesic"
                if "nemovitost" in ascii_name or "celkem" in ascii_name:
                    return "za nemovitost"
    return None


def _description(raw: dict[str, Any]) -> str | None:
    val = raw.get("advert_description")
    if not isinstance(val, str):
        return None
    val = val.strip()
    return val or None


def _strip_diacritics(text: str) -> str:
    return "".join(c for c in normalize("NFD", text) if not combining(c))


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _numeric_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None
    return None


