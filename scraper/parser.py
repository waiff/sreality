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
    # share). It IS its own search slice (category_type_cb=4 is a valid filter),
    # walked as its own pair in main.CATEGORIES so it gets a complete index walk
    # and mark_inactive.
    4: "podil",
}

# Sreality enum codes for the structured fields we promote to typed columns.
# Unknown codes (including 0, which sreality uses for "not specified") return
# None instead of raising. Czech labels stored without diacritics to match
# the convention for category_main / category_type values.
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
# IDs 1..77 are the 76 Czech okresy outside Prague (47 is the city of
# Prague). 5001..5022 are Praha 1..22 — all collapsed to a single "Praha"
# label per operator preference; the locality_district_id column still
# carries the finer value. Unknown IDs return None so the locality.district
# text fallback still labels them.
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
_ENERGY_CLASS_RE = re.compile(r"\s*([A-G])\b")

_BUILDING_TYPE_TEXT: dict[str, str] = {
    "cihlova": "cihla",
    "panelova": "panel",
    "smisena": "smisena",
    "skeletova": "skelet",
    "drevena": "drevo",
    "drevostavba": "drevo",
    "kamenna": "kamen",
    "montovana": "montovana",
    "nizkoenergeticka": "nizkoenergeticka",
}

# For a reserved/sold listing sreality overlays the sale STATUS onto the
# building_condition / building_type param names ("Rezervováno", "Prodáno").
# Reject it (→ None) so a status label never lands in an attribute column,
# where it would corrupt the condition / building_type filters and feed
# garbage into condition scoring. The real value is genuinely absent here.
_STATUS_OVERLAY: frozenset[str] = frozenset({"rezervovano", "prodano"})


def parse_listing(raw: dict[str, Any]) -> dict[str, Any]:
    sreality_id = _int_or_none(raw.get("hash_id"))
    if sreality_id is None:
        sreality_id = _int_or_none(raw.get("id"))
    if sreality_id is None:
        raise ValueError("could not determine sreality_id from response")

    loc = raw.get("locality") or {}

    return {
        "sreality_id": sreality_id,
        "category_main": CATEGORY_MAIN.get(_cb_value(raw.get("category_main_cb"))),
        "category_type": CATEGORY_TYPE.get(_cb_value(raw.get("category_type_cb"))),
        "price_czk": _price_czk(raw),
        "price_unit": _price_unit(raw),
        "area_m2": _numeric_or_none(raw.get("usable_area")),
        "disposition": _disposition(raw),
        "locality": _locality_value(loc),
        "district": _district(loc),
        "locality_district_id": _id_or_none(loc.get("district_id")),
        "locality_region_id": _id_or_none(loc.get("region_id")),
        "locality_municipality_id": _id_or_none(loc.get("municipality_id")),
        "locality_quarter_id": _id_or_none(loc.get("quarter_id")),
        "locality_ward_id": _id_or_none(loc.get("ward_id")),
        "lon": _coord(loc.get("gps_lon")),
        "lat": _coord(loc.get("gps_lat")),
        "floor": _int_or_none(raw.get("floor_number")),
        "total_floors": _int_or_none(raw.get("floors")),
        "has_balcony": _has_balcony(raw),
        "has_parking": _has_parking(raw),
        "has_lift": _elevator(raw.get("elevator")),
        "building_type": _building_type(raw.get("building_type")),
        "condition": _condition(raw.get("building_condition")),
        "energy_rating": _energy_rating(raw.get("energy_efficiency_rating_cb")),
        "estate_area": _numeric_or_none(raw.get("estate_area")),
        "usable_area": _numeric_or_none(raw.get("usable_area")),
        "garden_area": _numeric_or_none(raw.get("garden_area")),
        "category_sub_cb": _cb_value(raw.get("category_sub_cb")),
        "furnished": FURNISHED.get(_cb_value(raw.get("furnished"))),
        "terrace": _bool_or_none(raw.get("terrace")),
        "cellar": _bool_or_none(raw.get("cellar")),
        "garage": _bool_or_none(raw.get("garage")),
        "parking_lots": _int_or_none(raw.get("parking")),
        "ownership": OWNERSHIP.get(_cb_value(raw.get("ownership"))),
        "description": _description(raw),
        "street": _loc_str(loc, "street"),
        "house_number": _loc_str(loc, "housenumber") or _loc_str(loc, "streetnumber"),
        "zip": _loc_str(loc, "zip"),
        "street_id": _id_or_none(loc.get("street_id")),
    }


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


def _cb_value(obj: Any) -> int | None:
    """Integer enum code from a {name, value} object; 0 ('not specified') → None."""
    if isinstance(obj, dict):
        v = obj.get("value")
        if isinstance(v, int) and not isinstance(v, bool) and v != 0:
            return v
    return None


def _coord(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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
                    return "měsíc"
                if "nemovitost" in ascii_name or "celkem" in ascii_name:
                    return "celkem"
    return None


def _disposition(raw: dict[str, Any]) -> str | None:
    for source in (
        (raw.get("category_sub_cb") or {}).get("name"),
        raw.get("advert_name"),
    ):
        if isinstance(source, str):
            match = _DISPOSITION_RE.search(source)
            if match:
                return match.group(1).lower()
    return None


def _locality_value(loc: dict[str, Any]) -> str | None:
    parts = [p for p in (loc.get("city"), loc.get("citypart")) if isinstance(p, str) and p]
    if not parts:
        return None
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    return " - ".join(parts)


def _loc_str(loc: dict[str, Any], key: str) -> str | None:
    """A structured-address string field from the rich `locality` shape.

    Only the detail response carries street / housenumber / zip / street_id; the
    index-only `{name, value, accuracy}` shape lacks them, so this returns None
    there. bazos and other crawler sources carry none of these either.
    """
    val = loc.get(key)
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        val = str(val)
    return val.strip() or None if isinstance(val, str) else None


def _district(loc: dict[str, Any]) -> str | None:
    did = _int_or_none(loc.get("district_id"))
    if did is not None:
        label = DISTRICTS.get(did)
        if label:
            return label
    text = loc.get("district")
    return text if isinstance(text, str) and text else None


def _description(raw: dict[str, Any]) -> str | None:
    val = raw.get("advert_description")
    if not isinstance(val, str):
        return None
    val = val.strip()
    return val or None


def _has_balcony(raw: dict[str, Any]) -> bool | None:
    vals = [raw.get(k) for k in ("balcony", "terrace", "loggia")]
    if all(v is None for v in vals):
        return None
    return any(bool(v) for v in vals)


def _has_parking(raw: dict[str, Any]) -> bool | None:
    vals = [raw.get(k) for k in ("parking_lots", "garage", "parking")]
    if all(v is None for v in vals):
        return None
    return any(bool(v) for v in vals)


def _elevator(obj: Any) -> bool | None:
    if not isinstance(obj, dict):
        return None
    if _cb_value(obj) is None:  # value 0 / unspecified
        return None
    name = _strip_diacritics(str(obj.get("name", "")).lower())
    if name.startswith("ano"):
        return True
    if name.startswith("ne"):
        return False
    return None


def _building_type(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    key = _strip_diacritics(name.strip().lower())
    if key.startswith("-") or "vyber" in key or "nezadano" in key or key in _STATUS_OVERLAY:
        return None
    return _BUILDING_TYPE_TEXT.get(key, key)


def _condition(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    key = _strip_diacritics(name.strip().lower())
    if key.startswith("-") or "vyber" in key or key in _STATUS_OVERLAY:
        return None
    # Diacritic-free, underscore-joined to match the schema convention and the
    # existing canonical values (e.g. "velmi_dobry", "po_rekonstrukci"); the
    # legacy condition filter binds against this column.
    return key.replace(" ", "_")


def _energy_rating(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if isinstance(name, str):
        match = _ENERGY_CLASS_RE.match(name)
        if match:
            return match.group(1).upper()
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
    """Sreality returns true/false (or 0/1) for amenity flags; missing → None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None
