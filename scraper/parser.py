"""Map a Sreality detail JSON response to a row dict matching the schema.

Primary source for structured fields is the `recommendations_data` block,
which is cleaner than the free-text `items[]` array. We fall back to
`items[]` for fields that only appear as Czech strings (building type,
condition, floor) and regex `name.value` for disposition.
"""

from __future__ import annotations

import re
from typing import Any
from unicodedata import combining, normalize

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
    # share). Surfaces from the prodej index walk; not its own search slice.
    4: "podil",
}

PRICE_UNIT_BY_CODE: dict[int, str] = {
    1: "celkem",
    2: "měsíc",
}

# Sreality enum codes for the structured fields we promote to typed
# columns in migration 022. Same forgiving pattern as CATEGORY_TYPE:
# unknown codes (including 0, which sreality uses for "not specified")
# return None instead of raising. Czech labels are stored without
# diacritics to match the existing convention for category_main /
# category_type values.
FURNISHED: dict[int, str] = {
    1: "ano",       # vybaveno
    2: "ne",        # nevybaveno
    3: "castecne",  # částečně vybaveno
}

OWNERSHIP: dict[int, str] = {
    1: "osobni",      # osobní
    2: "druzstevni",  # družstevní
    3: "statni",      # státní/obecní
}

# Canonical district labels keyed by sreality's locality_district_id.
# Mirrors the seed in migration 041_district_canonical_label.sql; the
# DB row is the runtime source of truth for the backfilled column,
# this dict feeds the parser on new rows. IDs 1..77 are the 76 Czech
# okresy outside Prague (47 is the city of Prague). 5001..5022 are
# Praha 1..22 — all collapsed to a single "Praha" label per operator
# preference; the locality_district_id column still carries the
# finer value. Unknown IDs (including -1 used for foreign listings)
# return None so the trailing-locality-segment fallback still labels
# them with their country name.
DISTRICTS: dict[int, str] = {
    1:  "okres České Budějovice",
    2:  "okres Český Krumlov",
    3:  "okres Jindřichův Hradec",
    4:  "okres Písek",
    5:  "okres Prachatice",
    6:  "okres Strakonice",
    7:  "okres Tábor",
    8:  "okres Domažlice",
    9:  "okres Cheb",
    10: "okres Karlovy Vary",
    11: "okres Klatovy",
    12: "okres Plzeň-město",
    13: "okres Plzeň-jih",
    14: "okres Plzeň-sever",
    15: "okres Rokycany",
    16: "okres Sokolov",
    17: "okres Tachov",
    18: "okres Česká Lípa",
    19: "okres Děčín",
    20: "okres Chomutov",
    21: "okres Jablonec nad Nisou",
    22: "okres Liberec",
    23: "okres Litoměřice",
    24: "okres Louny",
    25: "okres Most",
    26: "okres Teplice",
    27: "okres Ústí nad Labem",
    28: "okres Hradec Králové",
    29: "okres Chrudim",
    30: "okres Jičín",
    31: "okres Náchod",
    32: "okres Pardubice",
    33: "okres Rychnov nad Kněžnou",
    34: "okres Semily",
    35: "okres Svitavy",
    36: "okres Trutnov",
    37: "okres Ústí nad Orlicí",
    38: "okres Zlín",
    39: "okres Kroměříž",
    40: "okres Prostějov",
    41: "okres Uherské Hradiště",
    42: "okres Olomouc",
    43: "okres Přerov",
    44: "okres Šumperk",
    45: "okres Vsetín",
    46: "okres Jeseník",
    47: "Praha",
    48: "okres Benešov",
    49: "okres Beroun",
    50: "okres Kladno",
    51: "okres Kolín",
    52: "okres Kutná Hora",
    53: "okres Mladá Boleslav",
    54: "okres Mělník",
    55: "okres Nymburk",
    56: "okres Praha-východ",
    57: "okres Praha-západ",
    58: "okres Příbram",
    59: "okres Rakovník",
    60: "okres Bruntál",
    61: "okres Frýdek-Místek",
    62: "okres Karviná",
    63: "okres Nový Jičín",
    64: "okres Opava",
    65: "okres Ostrava-město",
    66: "okres Havlíčkův Brod",
    67: "okres Jihlava",
    68: "okres Pelhřimov",
    69: "okres Třebíč",
    70: "okres Žďár nad Sázavou",
    71: "okres Blansko",
    72: "okres Brno-město",
    73: "okres Brno-venkov",
    74: "okres Břeclav",
    75: "okres Hodonín",
    76: "okres Vyškov",
    77: "okres Znojmo",
    **{i: "Praha" for i in range(5001, 5023)},
}

_DISPOSITION_RE = re.compile(r"\b(\d\+(?:kk|\d))\b", re.IGNORECASE)
_FLOOR_RE = re.compile(r"\s*(-?\d+)\.\s*podla", re.IGNORECASE)
_TOTAL_FLOORS_RE = re.compile(r"z\s*celkem\s*(\d+)", re.IGNORECASE)
_ENERGY_CLASS_RE = re.compile(r"Třída\s+([A-G])", re.IGNORECASE)

_BUILDING_TYPE_TEXT: dict[str, str] = {
    "cihlova": "cihla",
    "panelova": "panel",
    "smisena": "smisena",
    "skeletova": "skelet",
    "drevena": "drevo",
    "kamenna": "kamen",
    "montovana": "montovana",
    "nizkoenergeticka": "nizkoenergeticka",
}


def parse_listing(raw: dict[str, Any]) -> dict[str, Any]:
    rec = raw.get("recommendations_data") or {}
    items_by_name: dict[str, dict[str, Any]] = {
        item.get("name", ""): item for item in (raw.get("items") or [])
    }

    sreality_id = _coalesce_id(raw, rec)
    if sreality_id is None:
        raise ValueError("could not determine sreality_id from response")

    map_obj = raw.get("map") or {}
    lon = map_obj.get("lon") or rec.get("locality_gps_lon")
    lat = map_obj.get("lat") or rec.get("locality_gps_lat")

    return {
        "sreality_id": sreality_id,
        "category_main": CATEGORY_MAIN.get(rec.get("category_main_cb")),
        "category_type": CATEGORY_TYPE.get(rec.get("category_type_cb")),
        "price_czk": _price_czk(raw, rec),
        "price_unit": _price_unit(raw, rec),
        "area_m2": _area_m2(rec, items_by_name),
        "disposition": _disposition(raw),
        "locality": _locality_value(raw),
        "district": _district(rec, raw),
        "locality_district_id": _int_or_none(rec.get("locality_district_id")),
        "locality_region_id": _int_or_none(rec.get("locality_region_id")),
        "locality_municipality_id": _id_or_none(rec.get("locality_municipality_id")),
        "locality_quarter_id": _id_or_none(rec.get("locality_quarter_id")),
        "locality_ward_id": _id_or_none(rec.get("locality_ward_id")),
        "lon": float(lon) if isinstance(lon, (int, float)) else None,
        "lat": float(lat) if isinstance(lat, (int, float)) else None,
        "floor": _floor(items_by_name),
        "total_floors": _total_floors(items_by_name),
        "has_balcony": _has_balcony(rec, items_by_name),
        "has_parking": _has_parking(rec, items_by_name),
        "has_lift": _has_lift(rec, items_by_name),
        "building_type": _building_type(items_by_name),
        "condition": _condition(items_by_name),
        "energy_rating": _energy_rating(items_by_name),
        "estate_area": _numeric_or_none(rec.get("estate_area")),
        "usable_area": _numeric_or_none(rec.get("usable_area")),
        "garden_area": _numeric_or_none(rec.get("garden_area")),
        "category_sub_cb": _int_or_none(rec.get("category_sub_cb")),
        "furnished": FURNISHED.get(_int_or_none(rec.get("furnished"))),
        "terrace": _bool_or_none(rec.get("terrace")),
        "cellar": _bool_or_none(rec.get("cellar")),
        "garage": _bool_or_none(rec.get("garage")),
        "parking_lots": _int_or_none(rec.get("parking_lots")),
        "ownership": OWNERSHIP.get(_int_or_none(rec.get("ownership"))),
    }


def parse_images(raw: dict[str, Any]) -> list[dict[str, Any]]:
    images = ((raw.get("_embedded") or {}).get("images")) or []
    out: list[dict[str, Any]] = []
    for img in images:
        links = img.get("_links") or {}
        href = (
            (links.get("view") or {}).get("href")
            or (links.get("self") or {}).get("href")
            or (links.get("gallery") or {}).get("href")
        )
        if not href:
            continue
        out.append({"url": href, "sequence": img.get("order")})
    return out


def _coalesce_id(
    raw: dict[str, Any], rec: dict[str, Any]
) -> int | None:
    hid = rec.get("hash_id") or raw.get("hash_id")
    if isinstance(hid, (int, str)) and str(hid).isdigit():
        return int(hid)
    href = ((raw.get("_links") or {}).get("self") or {}).get("href", "")
    match = re.search(r"/estates/(\d+)", href)
    return int(match.group(1)) if match else None


def _price_czk(
    raw: dict[str, Any], rec: dict[str, Any]
) -> int | None:
    p = (raw.get("price_czk") or {}).get("value_raw")
    if isinstance(p, (int, float)):
        return int(p)
    p = rec.get("price_summary_czk")
    return int(p) if isinstance(p, (int, float)) else None


def _price_unit(
    raw: dict[str, Any], rec: dict[str, Any]
) -> str | None:
    text = (raw.get("price_czk") or {}).get("unit")
    if isinstance(text, str):
        ascii_text = _strip_diacritics(text.lower())
        if "mesic" in ascii_text:
            return "měsíc"
        if "celkem" in ascii_text:
            return "celkem"
    return PRICE_UNIT_BY_CODE.get(rec.get("price_summary_unit_cb"))


def _area_m2(
    rec: dict[str, Any], items_by_name: dict[str, dict[str, Any]]
) -> float | None:
    ua = rec.get("usable_area")
    if isinstance(ua, (int, float)) and ua > 0:
        return float(ua)
    item = _find_item(
        items_by_name,
        "Užitná ploch",  # API typo, omits final "a"
        "Užitná plocha",
        "Plocha podlahová",
        "Plocha užitná",
    )
    if item is None:
        return None
    try:
        return float(str(item.get("value", "")).replace(",", "."))
    except (ValueError, TypeError):
        return None


def _disposition(raw: dict[str, Any]) -> str | None:
    for source in (
        (raw.get("name") or {}).get("value"),
        raw.get("meta_description"),
    ):
        if not isinstance(source, str):
            continue
        match = _DISPOSITION_RE.search(source)
        if match:
            return match.group(1).lower()
    return None


def _locality_value(raw: dict[str, Any]) -> str | None:
    val = (raw.get("locality") or {}).get("value")
    return val if isinstance(val, str) and val else None


def _district(rec: dict[str, Any], raw: dict[str, Any]) -> str | None:
    """Canonical district label derived from locality_district_id, with
    a trailing-locality-segment fallback for IDs we don't map (today
    only `-1`, used by sreality for foreign listings — preserves the
    country name in `district`)."""
    label = DISTRICTS.get(_int_or_none(rec.get("locality_district_id")) or 0)
    if label is not None:
        return label
    text = _locality_value(raw)
    if not text:
        return None
    parts = [p.strip() for p in text.split(",")]
    return parts[-1] if len(parts) >= 2 else None


def _floor(items_by_name: dict[str, dict[str, Any]]) -> int | None:
    item = _find_item(items_by_name, "Podlaží")
    if item is None:
        return None
    match = _FLOOR_RE.match(str(item.get("value", "")))
    return int(match.group(1)) if match else None


def _total_floors(items_by_name: dict[str, dict[str, Any]]) -> int | None:
    item = _find_item(items_by_name, "Podlaží")
    if item is None:
        return None
    match = _TOTAL_FLOORS_RE.search(str(item.get("value", "")))
    return int(match.group(1)) if match else None


def _has_balcony(
    rec: dict[str, Any], items_by_name: dict[str, dict[str, Any]]
) -> bool | None:
    for key in ("balcony", "terrace", "loggia"):
        if rec.get(key):
            return True
    if rec:
        return False
    for needle in ("Balkón", "Lodžie", "Terasa"):
        if needle in items_by_name:
            if items_by_name[needle].get("value"):
                return True
    return None


def _has_parking(
    rec: dict[str, Any], items_by_name: dict[str, dict[str, Any]]
) -> bool | None:
    for key in ("parking_lots", "garage"):
        if rec.get(key):
            return True
    if rec:
        return False
    item = _find_item(items_by_name, "Parkování", "Garáž")
    return bool(item.get("value")) if item else None


def _has_lift(
    rec: dict[str, Any], items_by_name: dict[str, dict[str, Any]]
) -> bool | None:
    if rec.get("elevator") is not None:
        return bool(rec.get("elevator"))
    item = _find_item(items_by_name, "Výtah")
    return bool(item.get("value")) if item else None


def _building_type(
    items_by_name: dict[str, dict[str, Any]]
) -> str | None:
    item = _find_item(items_by_name, "Stavba", "Konstrukce budovy")
    if item is None:
        return None
    raw_value = str(item.get("value", "")).strip()
    if not raw_value:
        return None
    key = _strip_diacritics(raw_value.lower())
    return _BUILDING_TYPE_TEXT.get(key, raw_value.lower())


def _condition(
    items_by_name: dict[str, dict[str, Any]]
) -> str | None:
    item = _find_item(items_by_name, "Stav objektu")
    if item is None:
        return None
    val = str(item.get("value", "")).strip()
    return val.lower() if val else None


def _energy_rating(
    items_by_name: dict[str, dict[str, Any]]
) -> str | None:
    item = _find_item(items_by_name, "Energetická náročnost")
    if item is None:
        return None
    code = item.get("value_type")
    if isinstance(code, str) and 1 <= len(code) <= 2:
        return code.upper()
    match = _ENERGY_CLASS_RE.search(str(item.get("value", "")))
    return match.group(1).upper() if match else None


def _find_item(
    items_by_name: dict[str, dict[str, Any]],
    *needles: str,
) -> dict[str, Any] | None:
    """Return the first item whose name starts with any of the needles.

    Case-insensitive and tolerates the API's occasional typos
    (e.g. "Užitná ploch" matches a "Užitná plocha" needle if we add it).
    """
    lowered = {name.lower(): item for name, item in items_by_name.items()}
    for needle in needles:
        nl = needle.lower()
        for name, item in lowered.items():
            if name.startswith(nl) or nl.startswith(name):
                return item
    return None


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


def _id_or_none(value: Any) -> int | None:
    """Like _int_or_none but maps sreality's -1 sentinel ("unknown") to None."""
    out = _int_or_none(value)
    return None if out is None or out < 0 else out


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


def _bool_or_none(value: Any) -> bool | None:
    """Sreality returns 0/1 for amenity flags; missing key → None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None
