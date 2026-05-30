"""Deterministic HTML parsing for ceskereality.cz (portal framework).

Pure functions, no I/O: `parse_index` turns one search-results page into the
listing ids + the next page, and `parse_detail` turns one listing page into a
`ScrapedListing` (the shared multi-portal contract in `scraper.scraped_listing`).

ceskereality is a STRUCTURED HTML portal, like idnes: each detail page carries a
schema.org `individualProduct` JSON-LD block (the clean price + broker), an
`i-info` spec list (paired title/value spans), precise per-listing coordinates in
`data-coord-lat`/`data-coord-lng` attributes (so no geocode is needed — that is
what lets cross-source dedup match it against sreality), and a full-size image
gallery on `img.ceskereality.cz/foto/`. Typed fields are normalised to the SAME
canonical labels the sreality parser emits (e.g. "soukromé" -> "osobni",
"Dobrý" -> "dobry") so cross-portal filters agree.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Callable
from unicodedata import combining, normalize

from selectolax.parser import HTMLParser, Node

from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.scraped_listing import ScrapedListing

Geocoder = Callable[[str], GeocodeResult]

# ceskereality URL segments -> our canonical labels (mirrors parser.CATEGORY_*).
# Both the search and the detail URL use the SAME plural segment
# (/prodej/byty/…), so one map serves parse + category_from_url.
SALE_TYPE: dict[str, str] = {
    "prodej": "prodej",
    "pronajem": "pronajem",
}
CATEGORY_MAIN: dict[str, str] = {
    "byty": "byt",
    "rodinne-domy": "dum",
    "chaty-chalupy": "dum",          # sreality lumps chata/chalupa under "dům"
    "pozemky": "pozemek",
    "komercni-prostory": "komercni",
    "ostatni": "ostatni",
}

# ceskereality construction labels -> canonical codes parser._BUILDING_TYPE_TEXT
# emits, so a cross-portal "panel" filter matches sreality and ceskereality alike.
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
    "soukrome": "osobni",            # ceskereality "soukromé" == sreality "osobní"
    "osobni": "osobni",
    "druzstevni": "druzstevni",
    "statni": "statni",
    "obecni": "statni",
}

# Czech-bbox guard: a coordinate outside it — a swapped lat/lon or a bad pin — is
# dropped rather than stored as geom.
_CZ_LAT_MIN, _CZ_LAT_MAX = 48.0, 51.5
_CZ_LON_MIN, _CZ_LON_MAX = 12.0, 19.0

# The numeric listing id is the trailing "-1234567.html" of the detail URL.
_ID_RE = re.compile(r"-(\d{4,})\.html\b")
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:2|²|\s*2)\b", re.IGNORECASE)
_DISPOSITION_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
_INT_RE = re.compile(r"(-?\d+)")
_ENERGY_RE = re.compile(r"\b([A-G])\b")
_STRANA_RE = re.compile(r"[?&]strana=(\d+)")
_PATH_RE = re.compile(r"^/([a-z]+)/([a-z0-9-]+)/")
# A Czech-format price run: digits split by ordinary / no-break / thin spaces.
_PRICE_RUN_RE = re.compile(r"\d[\d\s  ​]*")
_PRICE_MAX = 2_147_483_647  # listings.price_czk is a Postgres integer
# Per-listing coordinates: ceskereality renders them as data attributes and in a
# Google-Maps "?q=lat,lng" link. lat first in both; the bbox guard catches a swap.
_COORD_LAT_RE = re.compile(r'data-coord-lat="(-?\d+\.\d+)"')
_COORD_LNG_RE = re.compile(r'data-coord-lng="(-?\d+\.\d+)"')
_MAPS_Q_RE = re.compile(r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)")
# Result total on the category page's meta description ("Máme tady 8221 bytů").
_TOTAL_RE = re.compile(r"Máme tady\s*(\d+)")


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


def _in_cz_bbox(lat: float, lon: float) -> bool:
    return _CZ_LAT_MIN <= lat <= _CZ_LAT_MAX and _CZ_LON_MIN <= lon <= _CZ_LON_MAX


def _id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    m = _ID_RE.search(href)
    return m.group(1) if m else None


def category_from_url(url: str) -> tuple[str | None, str | None]:
    """Derive (category_main, category_type) from a detail URL's path,
    /{sale}/{category}/… — the detail-drain recovers a listing's category from
    its own URL since the shared queue doesn't carry it."""
    path = re.sub(r"^https?://[^/]+", "", url or "")
    m = _PATH_RE.match(path)
    if not m:
        return None, None
    return CATEGORY_MAIN.get(m.group(2)), SALE_TYPE.get(m.group(1))


def index_price(text: str | None) -> int | None:
    """The Kč amount from an index card's price text, or None ("Cena dohodou" /
    "Info o ceně"). Drives price-change detection for the detail-refetch queue."""
    return _parse_price(text, None)[0]


def _parse_total(html: str) -> int | None:
    m = _TOTAL_RE.search(html)
    return int(m.group(1)) if m else None


def _parse_price(text: str | None, category_type: str | None) -> tuple[int | None, str | None]:
    unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    if not text:
        return None, unit
    low = _strip_diacritics(text).lower()
    if any(k in low for k in ("dohodou", "vyzadani", "poptavce", "info o cene", "neuvedena")):
        return None, unit
    # Take only the FIRST price run; a struck original + current price would
    # otherwise concatenate into a giant number that overflows price_czk.
    m = _PRICE_RUN_RE.search(text)
    if not m:
        return None, unit
    digits = re.sub(r"\D", "", m.group(0))
    if not digits:
        return None, unit
    value = int(digits)
    return (value if value <= _PRICE_MAX else None), unit


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
    """ceskereality renders the floor as "1." (1st floor) or "přízemí" (ground)."""
    if not text:
        return None
    low = _strip_diacritics(text).lower()
    if "prizem" in low:
        return 0
    m = _INT_RE.search(low)
    return int(m.group(1)) if m else None


def _norm_condition(text: str | None) -> str | None:
    if not text:
        return None
    key = _strip_diacritics(text).lower().strip()
    key = re.sub(r"\s+stav$", "", key)        # "velmi dobrý stav" -> "velmi dobry"
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
    key = _strip_diacritics(text.strip().lower())
    return BUILDING_TYPE.get(key, key or None)


def _energy_rating(text: str | None) -> str | None:
    if not text:
        return None
    m = _ENERGY_RE.search(text)
    return m.group(1).upper() if m else None


def _detail_params(tree: HTMLParser) -> dict[str, str]:
    """Map the spec list's labels (lowercased) to their values. ceskereality
    renders each as `<div class="i-info"><span class="i-info__title">…</span>
    <span class="i-info__value">…</span></div>`."""
    rows: dict[str, str] = {}
    for info in tree.css("div.i-info"):
        title = _text(info.css_first("span.i-info__title"))
        value = _text(info.css_first("span.i-info__value"))
        if title:
            key = re.sub(r"\s+", " ", title.rstrip(":").strip().lower())
            if key and key not in rows and value is not None:
                rows[key] = value
    return rows


def _jsonld_product(html: str) -> dict[str, Any]:
    """The schema.org product JSON-LD block (price + broker + address), or {}.
    Picks the block whose `offers` carries a numeric price."""
    for m in re.finditer(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(m.group(1).strip())
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        offers = data.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict) and offers.get("price") is not None:
            data["offers"] = offers
            return data
    return {}


def _resolve_coords(
    html: str, locality: str | None, geocoder: Geocoder | None
) -> tuple[float | None, float | None, dict[str, Any]]:
    lat_m, lng_m = _COORD_LAT_RE.search(html), _COORD_LNG_RE.search(html)
    if lat_m and lng_m:
        lat, lon = float(lat_m.group(1)), float(lng_m.group(1))
        if _in_cz_bbox(lat, lon):
            return lat, lon, {"source": "page"}
    q = _MAPS_Q_RE.search(html)
    if q:
        lat, lon = float(q.group(1)), float(q.group(2))
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


def parse_index(html: str) -> IndexPage:
    tree = HTMLParser(html)
    total = _parse_total(html)

    items: list[IndexItem] = []
    seen: set[str] = set()
    for card in tree.css("article.i-estate"):
        link = card.css_first("a.i-estate__image-link") or card.css_first(
            "a.i-estate__title-link"
        )
        href = link.attributes.get("href") if link else None
        source_id = _id_from_href(href)
        if not source_id or not href or source_id in seen:
            continue
        seen.add(source_id)
        items.append(
            IndexItem(
                source_id_native=source_id,
                detail_path=href,
                title=_text(card.css_first(".i-estate__title-link"))
                or _text(card.css_first(".i-estate__header-title")),
                price_text=_text(card.css_first(".i-estate__footer-price-value")),
                locality_text=_text(card.css_first(".i-estate__header-locality")),
            )
        )

    return IndexPage(total=total, items=items, next_offset=_next_page(tree))


def _next_page(tree: HTMLParser) -> int | None:
    """The "next" pagination arrow's ?strana=N, or None on the last page (the
    arrow carries `--disabled` there)."""
    for arrow in tree.css("a.pagination-arrow"):
        cls = arrow.attributes.get("class") or ""
        if "--next" not in cls or "--disabled" in cls:
            continue
        m = _STRANA_RE.search(arrow.attributes.get("href") or "")
        if m:
            return int(m.group(1))
    return None


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
    ld = _jsonld_product(html)
    offers = ld.get("offers") if isinstance(ld.get("offers"), dict) else {}
    params = _detail_params(tree)

    title = (
        ld.get("name")
        or _text(tree.css_first("h1"))
        or ""
    )

    # Price: the JSON-LD offer is the clean source; fall back to the "Cena" spec.
    price_czk: int | None = None
    if isinstance(offers.get("price"), (int, float)):
        value = int(offers["price"])
        price_czk = value if 0 < value <= _PRICE_MAX else None
    price_unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    if price_czk is None:
        price_czk, price_unit = _parse_price(params.get("cena"), category_type)

    address = (offers.get("areaServed") or {}).get("address") or {}
    locality = ", ".join(
        p for p in (address.get("streetAddress"), address.get("addressLocality")) if p
    ) or _text(tree.css_first(".i-estate-detail__locality"))
    lat, lon, coord_provenance = _resolve_coords(html, locality, geocoder)

    area_text = params.get("plocha užitná") or params.get("užitná plocha") or params.get("plocha")
    usable_area = _parse_area(area_text)
    area_m2 = usable_area or _parse_area(title)

    description = unescape(ld.get("description") or "") or _text(
        tree.css_first("div.popisdetail")
    )

    image_urls: list[str] = []
    seen_img: set[str] = set()
    for img in tree.css("img"):
        for attr in ("src", "data-src", "data-lazy"):
            href = img.attributes.get(attr)
            if not href or "/foto/" not in href or "ceskereality.cz" not in href:
                continue
            href = href.split("?")[0]
            if href not in seen_img:
                seen_img.add(href)
                image_urls.append(href)
            break

    broker = offers.get("offeredby") or offers.get("offeredBy") or {}
    raw: dict[str, Any] = {
        "id": source_id,
        "title": title,
        "locality_text": locality,
        "broker_name": (broker.get("name") or "").strip() or None,
        "broker_phone": broker.get("telephone"),
        "image_urls": image_urls,
        "coords": coord_provenance,
        "params": params,
    }

    return ScrapedListing(
        source="ceskereality",
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=area_m2,
        usable_area=usable_area,
        disposition=_parse_disposition(title) or _parse_disposition(params.get("dispozice")),
        locality=locality,
        district=None,
        lat=lat,
        lon=lon,
        floor=_parse_floor(params.get("patro") or params.get("podlaží")),
        total_floors=_parse_int(params.get("počet podlaží") or params.get("podlaží v domě")),
        building_type=_norm_building_type(
            params.get("konstrukce") or params.get("typ stavby") or params.get("stavba")
        ),
        condition=_norm_condition(params.get("stav nemovitosti") or params.get("stav objektu")),
        ownership=_norm_ownership(params.get("vlastnictví")),
        furnished=_norm_furnished(params.get("vybavení")),
        energy_rating=_energy_rating(
            params.get("energetická náročnost") or params.get("penb")
        ),
        estate_area=_parse_area(params.get("plocha pozemku")),
        garden_area=_parse_area(params.get("plocha zahrady")),
        description=description,
        raw=raw,
    )
