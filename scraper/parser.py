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
        "district": _district(raw),
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


def _district(raw: dict[str, Any]) -> str | None:
    """Best-effort district extraction from the human-readable locality."""
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
