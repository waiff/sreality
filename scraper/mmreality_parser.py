"""Deterministic HTML parsing for mmreality.cz (portal framework).

Pure functions, no I/O: `parse_index` turns one `/nemovitosti/` search-results
page into the listing ids + the next page, and `parse_detail` turns one listing
page into a `ScrapedListing` (the shared multi-portal contract).

M&M Reality is server-rendered, but — unlike the free-text classifieds (bazos)
or the `<dl>`-table portal (idnes) — every listing detail page embeds a COMPLETE
structured estate object as a Vue prop (`:property="{…}"`, HTML-entity-encoded).
So `parse_detail` decodes that JSON rather than scraping markup: precise
per-listing coordinates, typed condition/construction/ownership, area, floors,
images — all from one object. Typed fields are normalised to the SAME canonical
labels sreality/idnes emit (`smíšená`→`smisena`, `velmi dobrý`→`velmi_dobry`,
`Družstevní`→`druzstevni`, `2+1`) so cross-portal filters / dedup agree.

The single `/nemovitosti/` index is mixed-category; each listing's category
(`byt`/`dum`/…, `prodej`/`pronajem`/…) is read from its own detail JSON, so one
config walks every category (no per-category index slice).
"""

from __future__ import annotations

import html as ihtml
import json
import re
from dataclasses import dataclass, field
from typing import Any
from unicodedata import combining, normalize

from selectolax.parser import HTMLParser, Node

from scraper.scraped_listing import ScrapedListing
from scraper.street import clean_street

SOURCE = "mmreality"

# mmreality category.name / group.name -> our canonical labels. category.name is
# the transaction (Prodej/Pronájem/Dražba); group.name is the property kind.
CATEGORY_TYPE: dict[str, str] = {
    "prodej": "prodej",
    "pronajem": "pronajem",
    "drazba": "drazba",
}
CATEGORY_MAIN: dict[str, str] = {
    "byt": "byt",
    "dum": "dum",
    "pozemek": "pozemek",
    "komercni": "komercni",
    "ostatni": "ostatni",
}

# Construction labels -> the canonical codes parser._BUILDING_TYPE_TEXT emits,
# so a cross-portal "panel" filter matches sreality / idnes / mmreality alike.
BUILDING_TYPE: dict[str, str] = {
    "panelova": "panel",
    "cihlova": "cihla",
    "smisena": "smisena",
    "skeletova": "skelet",
    "drevena": "drevo",
    "kamenna": "kamen",
    "montovana": "montovana",
    "nizkoenergeticka": "nizkoenergeticka",
}
OWNERSHIP: dict[str, str] = {
    "osobni": "osobni",
    "druzstevni": "druzstevni",
    "statni": "statni",
    "obecni": "statni",
}

# Canonical typed-enum vocabularies — the value space the Browse filters, dedup,
# condition scoring and the MF-yield SQL key on. mmreality's free-text enums are
# mapped INTO these and anything outside is dropped to None — the same
# canonical-or-None discipline as _ownership. The lever: mmreality uses the
# placeholder "neuvedeno" ("not specified") as its sentinel (seen live on
# `construction`, near-certain on `condition`); the old free-text passthrough
# leaked it into the typed columns, where it matches no filter option.
_BUILDING_TYPE_CANON: frozenset[str] = frozenset(BUILDING_TYPE.values())
# The sreality "Stav objektu" value space (the cross-portal condition vocabulary,
# verified against the live distribution of listings.condition).
_CONDITION_CANON: frozenset[str] = frozenset({
    "novostavba", "velmi_dobry", "dobry", "po_rekonstrukci", "pred_rekonstrukci",
    "ve_vystavbe", "projekt", "v_rekonstrukci", "spatny", "k_demolici",
})

# Czech-bbox guard: a coordinate outside it (a swapped lat/lon or a foreign
# point) is dropped rather than stored as geom.
_CZ_LAT_MIN, _CZ_LAT_MAX = 48.0, 51.5
_CZ_LON_MIN, _CZ_LON_MAX = 12.0, 19.0

_ID_RE = re.compile(r"/nemovitosti/(\d+)/?")
_DISPOSITION_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
_PAGE_RE = re.compile(r"[?&]page=(\d+)")
# The Vue prop is HTML-entity-encoded (&quot;), so the only literal double quotes
# in the attribute are its delimiters — `[^"]*` captures the whole blob cleanly.
_PROPERTY_ATTR_RE = re.compile(r':property="([^"]*)"')
# Preview sizes, largest first — we store the biggest the CDN offers.
_IMAGE_SIZES: tuple[str, ...] = ("xlarge2", "xlarge", "medium2", "medium", "xsmall")


@dataclass(frozen=True)
class IndexItem:
    source_id_native: str
    detail_path: str
    title: str | None = None
    price_text: str | None = None


@dataclass(frozen=True)
class IndexPage:
    total: int | None
    items: list[IndexItem] = field(default_factory=list)
    next_offset: int | None = None


def _strip_diacritics(text: str) -> str:
    return "".join(c for c in normalize("NFD", text) if not combining(c))


def _norm_key(text: str | None) -> str | None:
    if not text:
        return None
    return _strip_diacritics(str(text)).lower().strip() or None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    digits = re.sub(r"\D", "", str(v))
    return int(digits) if digits else None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group(0).replace(",", ".")) if m else None


def _to_bool(v: Any) -> bool | None:
    s = str(v).strip().lower()
    if s in ("true", "1", "ano"):
        return True
    if s in ("false", "0", "ne"):
        return False
    return None


def _in_cz_bbox(lat: float, lon: float) -> bool:
    return _CZ_LAT_MIN <= lat <= _CZ_LAT_MAX and _CZ_LON_MIN <= lon <= _CZ_LON_MAX


def _id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _ID_RE.search(url)
    return m.group(1) if m else None


def _disposition(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        m = _DISPOSITION_RE.search(text)
        if m:
            return f"{m.group(1)}+{m.group(2).lower()}"
    return None


def _category_type(obj: dict[str, Any]) -> str | None:
    cat = obj.get("category") or {}
    return CATEGORY_TYPE.get(_norm_key(cat.get("name")) or "")


def _category_main(obj: dict[str, Any]) -> str | None:
    """Map the property-kind group to a canonical category_main. mmreality's
    group.name is e.g. 'Byt' / 'Dům' / 'Pozemek' / 'Komerční objekt' / 'Ostatní'
    — matched by prefix so plural/variant labels still resolve."""
    key = _norm_key((obj.get("group") or {}).get("name"))
    if not key:
        return None
    if key.startswith("byt"):
        return "byt"
    if key.startswith("dum") or key.startswith("dom"):
        return "dum"
    if key.startswith("pozem"):
        return "pozemek"
    if key.startswith("komerc"):
        return "komercni"
    return "ostatni"


def _building_type(obj: dict[str, Any]) -> str | None:
    key = _norm_key((obj.get("construction") or {}).get("name"))
    if not key:
        return None
    # Real values are nouns (Cihla/Panel/Smíšená) that already equal a canonical
    # code, so .get falls through; the canonical guard then drops "neuvedeno" and
    # any other non-canonical label instead of leaking it.
    cand = BUILDING_TYPE.get(key, key)
    return cand if cand in _BUILDING_TYPE_CANON else None


def _condition(obj: dict[str, Any]) -> str | None:
    key = _norm_key((obj.get("condition") or {}).get("name"))
    if not key:
        return None
    key = re.sub(r"\s+stav$", "", key)   # defensive: match idnes's "… stav" stripping
    cand = re.sub(r"\s+", "_", key)
    return cand if cand in _CONDITION_CANON else None


def _ownership(obj: dict[str, Any]) -> str | None:
    key = _norm_key((obj.get("ownership") or {}).get("name"))
    if not key:
        return None
    # Canonical set only ({osobni, druzstevni, statni}); unmapped labels → None
    # rather than leaking a value no ownership filter option can match.
    return OWNERSHIP.get(key)


def _energy_rating(obj: dict[str, Any]) -> str | None:
    code = (obj.get("energyClassification") or {}).get("code")
    if not code:
        return None
    m = re.search(r"[A-G]", str(code).upper())
    return m.group(0) if m else None


def _coords(obj: dict[str, Any]) -> tuple[float | None, float | None]:
    point = obj.get("point") or {}
    lat = _to_float(point.get("latitude"))
    lon = _to_float(point.get("longitude"))
    if lat is None or lon is None or not _in_cz_bbox(lat, lon):
        return None, None
    return lat, lon


def _locality(obj: dict[str, Any]) -> str | None:
    for key in ("location", "municipalityPart", "municipality"):
        val = obj.get(key)
        if val:
            return str(val)
    return None


def _image_urls(obj: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for img in obj.get("images") or []:
        previews = (img or {}).get("previews") or {}
        url = next((previews[s] for s in _IMAGE_SIZES if previews.get(s)), None)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _accessory_names(obj: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for group in obj.get("accessoryGroups") or []:
        for acc in (group or {}).get("accessories") or []:
            key = _norm_key((acc or {}).get("name"))
            if key:
                names.add(key)
    return names


def _has_any(names: set[str], *needles: str) -> bool | None:
    return True if any(n in name for name in names for n in needles) else None


def extract_property(html: str, listing_id: str | None) -> dict[str, Any]:
    """Return the embedded `:property` estate object for `listing_id`.

    A detail page carries several `:property` Vue props (the main listing plus
    related preview cards), so we pick the blob whose `id` matches the listing,
    falling back to the largest blob. Raises ValueError when none parse."""
    candidates: list[dict[str, Any]] = []
    for raw in _PROPERTY_ATTR_RE.findall(html):
        if "&quot;id&quot;" not in raw and '"id"' not in raw:
            continue
        try:
            obj = json.loads(ihtml.unescape(raw))
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if listing_id is not None and str(obj.get("id")) == str(listing_id):
            return obj
        candidates.append(obj)
    if not candidates:
        raise ValueError("no :property estate object found on page")
    return max(candidates, key=lambda o: len(json.dumps(o, default=str)))


def parse_index(html: str) -> IndexPage:
    tree = HTMLParser(html)
    items: list[IndexItem] = []
    seen: set[str] = set()
    for anchor in tree.css("a[data-card-id]"):
        source_id = anchor.attributes.get("data-card-id") or _id_from_url(
            anchor.attributes.get("href")
        )
        href = anchor.attributes.get("href")
        if not source_id or not href or source_id in seen:
            continue
        seen.add(source_id)
        price_node = anchor.css_first("[data-realty-price]")
        items.append(
            IndexItem(
                source_id_native=source_id,
                detail_path=href,
                title=(
                    (price_node.attributes.get("data-realty-name") if price_node else None)
                    or _text(anchor.css_first("h4"))
                ),
                price_text=(
                    price_node.attributes.get("data-realty-price") if price_node else None
                ),
            )
        )
    return IndexPage(total=None, items=items, next_offset=_next_page(tree))


def _text(node: Node | None) -> str | None:
    if node is None:
        return None
    txt = re.sub(r"\s+", " ", node.text(separator=" ", strip=False)).strip()
    return txt or None


def _next_page(tree: HTMLParser) -> int | None:
    link = tree.css_first('link[rel="next"]')
    href = link.attributes.get("href") if link else None
    m = _PAGE_RE.search(href or "")
    return int(m.group(1)) if m else None


def index_price(text: str | None) -> int | None:
    """The Kč amount from an index card's `data-realty-price`, or None
    ("Info o ceně" / "Cena dohodou"). Drives price-change refetch detection."""
    if not text or "dohod" in _strip_diacritics(text).lower():
        return None
    return _to_int(text)


def parse_detail(html: str, *, source_url: str) -> ScrapedListing:
    """Parse one mmreality listing page into a ScrapedListing.

    Category is derived from the embedded estate object (mixed-category index),
    so — unlike idnes — nothing is passed in from the index/URL."""
    listing_id = _id_from_url(source_url)
    obj = extract_property(html, listing_id)
    source_id = str(obj.get("id") or listing_id or "")

    category_type = _category_type(obj)
    price_unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    price_czk = _to_int(obj.get("price"))
    if price_czk == 0:
        price_czk = None

    lat, lon = _coords(obj)
    accessories = _accessory_names(obj)
    parking_lots = _to_int(obj.get("parkingPlaces"))
    overground = _to_int(obj.get("overgroundFloors"))
    underground = _to_int(obj.get("undergroundFloors"))
    total_floors = (
        (overground or 0) + (underground or 0)
        if overground is not None or underground is not None
        else None
    )

    image_urls = _image_urls(obj)
    raw = dict(obj)
    raw["image_urls"] = image_urls
    raw["source_url"] = source_url

    return ScrapedListing(
        source=SOURCE,
        source_id_native=source_id,
        source_url=source_url,
        category_main=_category_main(obj),
        category_type=category_type,
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=_to_float(obj.get("totalArea")) or _to_float(obj.get("usableArea")),
        usable_area=_to_float(obj.get("usableArea")),
        disposition=_disposition((obj.get("type") or {}).get("name"), obj.get("title")),
        locality=_locality(obj),
        district=obj.get("district") or None,
        # The embedded :property estate object carries a structured `street`.
        street=clean_street(obj.get("street") if isinstance(obj.get("street"), str) else None),
        lat=lat,
        lon=lon,
        floor=_to_int(obj.get("floor")),
        total_floors=total_floors,
        building_type=_building_type(obj),
        condition=_condition(obj),
        ownership=_ownership(obj),
        energy_rating=_energy_rating(obj),
        has_lift=_to_bool(obj.get("lift")),
        cellar=(
            _to_bool(obj.get("cellar"))
            if obj.get("cellar") is not None
            else _has_any(accessories, "sklep")
        ),
        has_balcony=_has_any(accessories, "balkon", "lodzie"),
        terrace=_has_any(accessories, "terasa"),
        garage=_has_any(accessories, "garaz"),
        has_parking=(True if parking_lots else _has_any(accessories, "parkov", "garaz")),
        parking_lots=parking_lots,
        estate_area=_to_float(obj.get("landArea")) or _to_float(obj.get("plotArea")),
        garden_area=_to_float(obj.get("gardenArea")),
        description=obj.get("description") or None,
        raw=raw,
    )
