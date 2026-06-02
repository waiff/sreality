"""Filter code maps for sreality's `ceny-nemovitosti` statistics API.

The `/api/v1/estate_prices` endpoint takes the same filter taxonomy as the
listing search, but with its OWN code maps for a couple of fields (notably
`building_type`, where the stats page uses Panel=5, not the search facet's
Panel=1). These maps were lifted from the page's JS bundle. Czech labels mirror
the diacritics-free convention used by `scraper.parser` so the operator-facing
dataset config reads the same vocabulary as the rest of the platform.

`category_main_cb` / `category_type_cb` / `usable_area_*` / `entity_id` /
`distance` are integers; `building_condition` / `building_type` / `ownership`
are string codes (the API's zod schema types them as strings, even though the
values are numeric).
"""

from __future__ import annotations

# category_main_cb (int) — same as parser.CATEGORY_MAIN's key space.
CATEGORY_MAIN: dict[int, str] = {
    1: "byty",
    2: "domy",
    3: "pozemky",
    4: "komercni",
    5: "ostatni",
}

# category_type_cb (int). Stats datasets always fetch 1 (prodej) AND 2 (pronajem).
CATEGORY_TYPE: dict[int, str] = {
    1: "prodej",
    2: "pronajem",
}
SALE = 1
LEASE = 2

# building_condition (str code) — sreality's full "Stav objektu" enum. The
# stats UI typically only offers VeryGood/NewBuilding/ForRenovation, but the
# API accepts any code; sreality simply returns whatever data exists.
BUILDING_CONDITION: dict[str, str] = {
    "1": "velmi_dobry",      # VeryGood
    "2": "dobry",            # Good
    "3": "spatny",           # Poor
    "4": "ve_vystavbe",      # UnderConstruction
    "5": "projekt",          # Project
    "6": "novostavba",       # NewBuilding
    "7": "k_demolici",       # ToBeDemolished
    "8": "pred_rekonstrukci",  # ForRenovation
    "9": "po_rekonstrukci",  # Renovated
    "10": "v_rekonstrukci",  # InRenovation
}

# building_type (str code) — STATS-page construction map (Panel=5), NOT the
# search facet map (which uses Panel=1). Verified against the HAR request that
# sent building_type=5 for konstrukce=panel.
BUILDING_TYPE: dict[str, str] = {
    "5": "panel",
    "2": "cihla",
    "10": "ostatni",
}

# ownership (str code).
OWNERSHIP: dict[str, str] = {
    "1": "osobni",       # Personal
    "2": "druzstevni",   # Cooperative
    "3": "statni",       # Municipal/State
}

# Human (Czech) labels for the operator-facing dataset config / UI.
LABELS_CS: dict[str, dict[str, str]] = {
    "category_main_cb": {
        "1": "Byty", "2": "Domy", "3": "Pozemky", "4": "Komerční", "5": "Ostatní",
    },
    "category_type_cb": {"1": "Prodej", "2": "Pronájem"},
    "building_condition": {
        "1": "Velmi dobrý", "2": "Dobrý", "3": "Špatný", "4": "Ve výstavbě",
        "5": "Projekt", "6": "Novostavba", "7": "K demolici",
        "8": "Před rekonstrukcí", "9": "Po rekonstrukci", "10": "V rekonstrukci",
    },
    "building_type": {"5": "Panel", "2": "Cihla", "10": "Ostatní"},
    "ownership": {"1": "Osobní", "2": "Družstevní", "3": "Státní/obecní"},
}

# entity_type values returned as `source` by localities/suggest. Municipality
# grain (the dataset default) is "muni".
ENTITY_MUNICIPALITY = "muni"
ENTITY_DISTRICT = "dist"
ENTITY_REGION = "regi"
ENTITY_QUARTER = "quar"


def label_for(field: str, code: object) -> str | None:
    """Czech label for a filter field's code, or None if unknown."""
    if code is None:
        return None
    return LABELS_CS.get(field, {}).get(str(code))


def category_type_label(category_type_cb: int) -> str:
    return CATEGORY_TYPE.get(category_type_cb, str(category_type_cb))
