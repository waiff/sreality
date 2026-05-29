"""Deterministic HTML parsing for reality.idnes.cz (portal framework).

Pure functions, no I/O of their own: `parse_index` turns one search-results page
into the listing ids + the next page, and `parse_detail` turns one listing page
into a `ScrapedListing` (the shared multi-portal contract in
`scraper.scraped_listing`).

Unlike bazos (free-text classifieds), idnes is a STRUCTURED portal: a `<dl>`
spec table (paired `<dt>`/`<dd>`), a clean price element, and — crucially —
precise per-listing coordinates embedded in the page's map config
(`"center":[lon,lat]`). So coordinates come straight from the page and a locality
geocode is only a fallback when the page omits them. The typed `<dl>` fields are
normalised to the same canonical labels the sreality parser emits (e.g.
"panelová" -> "panel", "osobní" -> "osobni") so cross-portal filters agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from unicodedata import combining, normalize

from selectolax.parser import HTMLParser, Node

from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.scraped_listing import ScrapedListing

Geocoder = Callable[[str], GeocodeResult]

# idnes search-URL segments -> our canonical labels (mirrors parser.CATEGORY_*).
SALE_TYPE: dict[str, str] = {
    "prodej": "prodej",
    "pronajem": "pronajem",
}
CATEGORY_MAIN: dict[str, str] = {
    "byty": "byt",
    "domy": "dum",
    "pozemky": "pozemek",
    "komercni": "komercni",
    "ostatni": "ostatni",
}

# Detail URLs use the SINGULAR category segment (/detail/prodej/byt/...), unlike
# the plural search segment (/s/prodej/byty/). The drain derives each listing's
# category from its own detail URL (the queue is category-agnostic), so one
# config can walk many categories.
DETAIL_CATEGORY: dict[str, str] = {
    "byt": "byt",
    "dum": "dum",
    "pozemek": "pozemek",
    "komercni": "komercni",
    "ostatni": "ostatni",
}

# idnes building-construction labels -> the canonical codes parser._BUILDING_TYPE_TEXT
# emits, so a cross-portal "panel" filter matches sreality and idnes alike.
BUILDING_TYPE: dict[str, str] = {
    "panelová": "panel",
    "cihlová": "cihla",
    "smíšená": "smisena",
    "skeletová": "skelet",
    "dřevěná": "drevo",
    "kamenná": "kamen",
    "montovaná": "montovana",
    "nízkoenergetická": "nizkoenergeticka",
}
OWNERSHIP: dict[str, str] = {
    "osobni": "osobni",
    "druzstevni": "druzstevni",
    "statni": "statni",
    "obecni": "statni",
}

# Czech-bbox guard: a coordinate (embedded pin OR geocode) outside it — a swapped
# lat/lon or a geocode that landed abroad — is dropped rather than stored as geom.
_CZ_LAT_MIN, _CZ_LAT_MAX = 48.0, 51.5
_CZ_LON_MIN, _CZ_LON_MAX = 12.0, 19.0

# The detail-URL hash is the source_id_native (24 hex chars today; >=16 to be safe).
_ID_RE = re.compile(r"/detail/[^?#]*?/([0-9a-f]{16,})/?(?:[?#]|$)")
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:2|²|\s*2)\b", re.IGNORECASE)
_DISPOSITION_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
_INT_RE = re.compile(r"(\d+)")
_ENERGY_RE = re.compile(r"\b([A-G])\b")
_PAGE_RE = re.compile(r"[?&]page=(\d+)")
_DETAIL_PATH_RE = re.compile(r"/detail/([^/?#]+)/([^/?#]+)/")
# Map config: "center":[lon, lat]. CZ lat/lon ranges don't overlap, so a swap is
# caught by the bbox guard rather than producing a bogus point.
_CENTER_RE = re.compile(r'"center"\s*:\s*\[\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\]')
_FLOOR_PATRO_RE = re.compile(r"(-?\d+)\.\s*patro")
_FLOOR_NP_RE = re.compile(r"(\d+)\.\s*np")
_FLOOR_PP_RE = re.compile(r"(\d+)\.\s*pp")


@dataclass(frozen=True)
class IndexItem:
    source_id_native: str
    detail_path: str
    title: str | None = None
    price_text: str | None = None
    locality_text: str | None = None


@dataclass(frozen=True)
class IndexPage:
    total: int | None
    items: list[IndexItem] = field(default_factory=list)
    next_offset: int | None = None


def _strip_diacritics(text: str) -> str:
    return "".join(c for c in normalize("NFD", text) if not combining(c))


def _text(node: Node | None) -> str | None:
    if node is None:
        return None
    txt = re.sub(r"\s+", " ", node.text(separator=" ", strip=False)).strip()
    return txt or None


def _page_text(tree: HTMLParser) -> str:
    body = tree.body
    if body is not None:
        return body.text(separator=" ", strip=False)
    root = tree.root
    return root.text(separator=" ", strip=False) if root is not None else ""


def _in_cz_bbox(lat: float, lon: float) -> bool:
    return _CZ_LAT_MIN <= lat <= _CZ_LAT_MAX and _CZ_LON_MIN <= lon <= _CZ_LON_MAX


def _id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    m = _ID_RE.search(href)
    return m.group(1) if m else None


def category_from_url(url: str) -> tuple[str | None, str | None]:
    """Derive (category_main, category_type) from a detail URL's path,
    /detail/{sale}/{cat}/... — the detail-drain recovers a listing's category
    from its own URL since the shared queue doesn't carry it."""
    m = _DETAIL_PATH_RE.search(url or "")
    if not m:
        return None, None
    return DETAIL_CATEGORY.get(m.group(2)), SALE_TYPE.get(m.group(1))


def index_price(text: str | None) -> int | None:
    """The Kč amount from an index card's price text, or None ("Info o ceně" /
    "Dohodou"). Drives price-change detection for the detail-refetch queue."""
    return _parse_price(text, None)[0]


def _parse_total(text: str) -> int | None:
    m = re.search(r"(\d[\d\s ]*\d|\d)\s*(?:nemovitost|nabíd|inzer)", text, re.IGNORECASE)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _parse_price(text: str | None, category_type: str | None) -> tuple[int | None, str | None]:
    unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    if not text:
        return None, unit
    low = _strip_diacritics(text).lower()
    if any(k in low for k in ("dohodou", "vyzadani", "poptavce", "info o cene", "neuvedena")):
        return None, unit
    digits = re.sub(r"\D", "", text)
    return (int(digits) if digits else None), unit


def _parse_disposition(text: str | None) -> str | None:
    if not text:
        return None
    m = _DISPOSITION_RE.search(text)
    if not m:
        return None
    return f"{m.group(1)}+{m.group(2).lower()}"


def _parse_area(text: str | None) -> float | None:
    if not text:
        return None
    m = _AREA_RE.search(text)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    m = _INT_RE.search(text)
    return int(m.group(1)) if m else None


def _parse_floor(text: str | None) -> int | None:
    """idnes shows "2. patro (3. NP)" — prefer the 'patro' count; fall back to
    NP (nadzemní podlaží: 1.NP = ground = 0) / PP (podzemní = below ground)."""
    if not text:
        return None
    low = _strip_diacritics(text).lower()
    if "prizem" in low:
        return 0
    m = _FLOOR_PATRO_RE.search(low)
    if m:
        return int(m.group(1))
    m = _FLOOR_NP_RE.search(low)
    if m:
        return int(m.group(1)) - 1
    m = _FLOOR_PP_RE.search(low)
    if m:
        return -int(m.group(1))
    return None


def _norm_condition(text: str | None) -> str | None:
    if not text:
        return None
    key = _strip_diacritics(text).lower().strip()
    key = re.sub(r"\s+stav$", "", key)        # idnes "velmi dobrý stav" -> "velmi dobry"
    key = re.sub(r"\s+", "_", key)
    return key or None


def _norm_ownership(text: str | None) -> str | None:
    if not text:
        return None
    key = _strip_diacritics(text).lower().strip()
    return OWNERSHIP.get(key, key or None)


def _norm_furnished(text: str | None) -> str | None:
    if not text:
        return None
    low = _strip_diacritics(text).lower()
    if "neza" in low or "nevyba" in low:
        return "ne"
    if "castec" in low:
        return "castecne"
    if "zariz" in low or "vybav" in low:
        return "ano"
    return None


def _norm_building_type(text: str | None) -> str | None:
    if not text:
        return None
    raw = text.strip().lower()
    if raw in BUILDING_TYPE:
        return BUILDING_TYPE[raw]
    return _strip_diacritics(raw) or None


def _energy_rating(text: str | None) -> str | None:
    if not text:
        return None
    m = _ENERGY_RE.search(text)
    return m.group(1).upper() if m else None


def _detail_params(tree: HTMLParser) -> dict[str, Node]:
    """Map the spec `<dl>` row labels (lowercased) to their value `<dd>` cells.

    idnes wraps some labels in an `<a>` filter link, which `text()` flattens, so
    "Konstrukce budovy" and a linked "Sklep" both key cleanly."""
    rows: dict[str, Node] = {}
    for dl in tree.css("dl"):
        dts = dl.css("dt")
        dds = dl.css("dd")
        for dt, dd in zip(dts, dds):
            label = (dt.text(separator=" ", strip=True) or "").rstrip(":").strip().lower()
            label = re.sub(r"\s+", " ", label)
            if label and label not in rows:
                rows[label] = dd
    return rows


def _has_check(dd: Node | None) -> bool | None:
    """An amenity `<dd>` carries a check icon when present; some carry a cross
    when absent. Unknown (no icon) -> None rather than a guessed False."""
    if dd is None:
        return None
    html = dd.html or ""
    if "icon--check" in html:
        return True
    if "icon--cross" in html or "icon--times" in html or "icon--close" in html:
        return False
    return None


def _truthy_field(dd: Node | None) -> bool | None:
    """For a row whose presence is signalled by a check icon OR by free text
    (e.g. "Parkování" -> "parkování na ulici")."""
    if dd is None:
        return None
    checked = _has_check(dd)
    if checked is not None:
        return checked
    return True if _text(dd) else None


def parse_index(html: str) -> IndexPage:
    tree = HTMLParser(html)
    total = _parse_total(_page_text(tree))

    items: list[IndexItem] = []
    seen: set[str] = set()
    for block in tree.css("div.c-products__item"):
        cls = block.attributes.get("class") or ""
        if "advertisment" in cls:
            continue
        link = block.css_first("a.c-products__link")
        href = link.attributes.get("href") if link else None
        source_id = _id_from_href(href)
        if not source_id or not href or source_id in seen:
            continue
        seen.add(source_id)
        items.append(
            IndexItem(
                source_id_native=source_id,
                detail_path=href,
                title=_text(block.css_first("h2.c-products__title")),
                price_text=_text(block.css_first("p.c-products__price")),
                locality_text=_text(block.css_first("p.c-products__info")),
            )
        )

    return IndexPage(total=total, items=items, next_offset=_next_page(tree))


def _next_page(tree: HTMLParser) -> int | None:
    for link in tree.css("a.paging__item"):
        cls = link.attributes.get("class") or ""
        if "next" not in cls:
            continue
        m = _PAGE_RE.search(link.attributes.get("href") or "")
        if m:
            return int(m.group(1))
    return None


def _resolve_coords(
    html: str, locality: str | None, geocoder: Geocoder | None
) -> tuple[float | None, float | None, dict[str, Any]]:
    m = _CENTER_RE.search(html)
    if m:
        lon, lat = float(m.group(1)), float(m.group(2))
        if _in_cz_bbox(lat, lon):
            return lat, lon, {"source": "page"}
    if geocoder is not None and locality:
        try:
            g = geocoder(locality)
        except GeocodingError:
            g = None
        if g is not None and _in_cz_bbox(g.lat, g.lng):
            return g.lat, g.lng, {"source": "geocode", "confidence": g.confidence}
    return None, None, {"source": None}


def parse_detail(
    html: str,
    *,
    source_url: str,
    category_main: str | None,
    category_type: str | None,
    geocoder: Geocoder | None = None,
) -> ScrapedListing:
    tree = HTMLParser(html)
    source_id = _id_from_href(source_url) or ""

    title = _text(tree.css_first("h1")) or ""
    description = _text(tree.css_first("div.b-desc")) or _text(tree.css_first(".b-detail__text"))
    params = _detail_params(tree)

    price_text = _text(tree.css_first(".b-detail__price")) or _text(params.get("cena"))
    price_czk, price_unit = _parse_price(price_text, category_type)

    locality = _text(tree.css_first(".b-detail__info"))
    lat, lon, coord_provenance = _resolve_coords(html, locality, geocoder)

    area_text = (
        _text(params.get("užitná plocha"))
        or _text(params.get("podlahová plocha"))
        or _text(params.get("plocha"))
    )
    area_m2 = _parse_area(area_text) or _parse_area(title)

    image_urls: list[str] = []
    seen_img: set[str] = set()
    for a in tree.css('a[data-fancybox="images"]'):
        href = a.attributes.get("href")
        if not href:
            continue
        href = href.split("?")[0]
        if "1gr.cz" not in href and "sta-reality" not in href:
            continue
        if href not in seen_img:
            seen_img.add(href)
            image_urls.append(href)

    raw: dict[str, Any] = {
        "id": source_id,
        "title": title,
        "price_text": price_text,
        "locality_text": locality,
        "idnes_ref": _text(params.get("číslo zakázky")),
        "image_urls": image_urls,
        "coords": coord_provenance,
        "params": {k: _text(v) for k, v in params.items()},
    }

    return ScrapedListing(
        source="idnes",
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=area_m2,
        usable_area=_parse_area(area_text),
        disposition=_parse_disposition(title) or _parse_disposition(_text(params.get("dispozice"))),
        locality=locality,
        district=None,
        lat=lat,
        lon=lon,
        floor=_parse_floor(_text(params.get("podlaží"))),
        total_floors=_parse_int(_text(params.get("počet podlaží budovy"))),
        building_type=_norm_building_type(_text(params.get("konstrukce budovy"))),
        condition=_norm_condition(
            _text(params.get("stav bytu"))
            or _text(params.get("stav domu"))
            or _text(params.get("stav objektu"))
        ),
        ownership=_norm_ownership(_text(params.get("vlastnictví"))),
        furnished=_norm_furnished(_text(params.get("vybavení"))),
        energy_rating=_energy_rating(_text(params.get("penb")) or _text(params.get("energetická náročnost"))),
        has_balcony=_has_check(params.get("balkon")),
        has_lift=_has_check(params.get("výtah")),
        cellar=_has_check(params.get("sklep")),
        terrace=_has_check(params.get("terasa")),
        garage=_has_check(params.get("garáž")),
        has_parking=_truthy_field(params.get("parkování")),
        estate_area=_parse_area(_text(params.get("plocha pozemku"))),
        garden_area=_parse_area(_text(params.get("plocha zahrady"))),
        description=description,
        raw=raw,
    )
