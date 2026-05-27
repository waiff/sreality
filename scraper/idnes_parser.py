"""Deterministic HTML parsing for reality.idnes.cz (multi-portal crawler).

Pure functions, no I/O: `parse_index` turns one search-results page into the
listing ids + the next page, and `parse_detail` turns one listing page into a
`ScrapedListing` (the shared contract in `scraper.scraped_listing`). Unlike the
bazos free-text classifieds, idnes is a structured portal — the detail page
carries a `<dl><dt>/<dd>` spec list (Užitná plocha, Konstrukce, Stav, …), a
`b-detail__price` banner, a `b-detail__info` locality line, and a
`data-maptiler-json` map config whose `center` gives [lon, lat] — so we extract
typed fields directly rather than regexing them out of prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser, Node

from scraper.scraped_listing import ScrapedListing

# idnes URL segments -> our canonical labels (mirrors parser.CATEGORY_* style).
# The search path uses plural categories ("byty"); the detail path uses the
# singular ("byt"). We key on the search segment the crawler walks.
SALE_TYPE: dict[str, str] = {
    "prodej": "prodej",
    "pronajem": "pronajem",
}
CATEGORY_MAIN: dict[str, str] = {
    "byty": "byt",
    "domy": "dum",
    "pozemky": "pozemek",
    "komercni-objekty": "komercni",
    "komercni": "komercni",
    "ostatni": "ostatni",
}

_ID_RE = re.compile(r"/detail/[^\s\"']*?([0-9a-f]{24})")
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m\s*(?:2|²)", re.IGNORECASE)
_DISPOSITION_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
# The map config's "center": [lon, lat] is the definitive subject marker; the
# embedded GeoJSON Point "coordinates": [lon, lat] is the robust fallback (the
# first Point is the subject property).
_CENTER_RE = re.compile(
    r'"center"\s*:\s*\[\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\]'
)
_GEOJSON_RE = re.compile(
    r'"coordinates"\s*:\s*\[\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\]'
)


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
    next_page: int | None = None


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


def _id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _ID_RE.search(url)
    return m.group(1) if m else None


def _parse_total(text: str) -> int | None:
    # "Zobrazujeme 1 - 25 z 24 607 inzerátů"
    m = re.search(r"z\s+(\d[\d\s ]*\d|\d)\s+inzer", text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_index(html: str) -> IndexPage:
    tree = HTMLParser(html)
    items: list[IndexItem] = []
    seen: set[str] = set()
    for card in tree.css(".c-products__item"):
        link = card.css_first("a[href*='/detail/']")
        href = link.attributes.get("href") if link else None
        source_id = _id_from_url(href)
        if not source_id or not href or source_id in seen:
            continue
        seen.add(source_id)
        items.append(
            IndexItem(
                source_id_native=source_id,
                detail_path=href,
                title=_text(card.css_first(".c-products__title")),
                price_text=_text(card.css_first(".c-products__price")),
                locality_text=_text(card.css_first(".c-products__info")),
            )
        )
    return IndexPage(
        total=_parse_total(_page_text(tree)),
        items=items,
        next_page=_next_page(tree),
    )


def _next_page(tree: HTMLParser) -> int | None:
    link = tree.css_first("a.paging__item.next")
    if link is None:
        return None
    m = re.search(r"[?&]page=(\d+)", link.attributes.get("href") or "")
    return int(m.group(1)) if m else None


def _detail_params(tree: HTMLParser) -> dict[str, str | None]:
    """Map the spec definition-list labels (lowercased) to their value text."""
    params: dict[str, str | None] = {}
    for dl in tree.css("dl"):
        dts = dl.css("dt")
        dds = dl.css("dd")
        for dt, dd in zip(dts, dds):
            label = (_text(dt) or "").rstrip(":").lower()
            if label and label not in params:
                params[label] = _text(dd)
    return params


def _first_number(text: str | None) -> float | None:
    if not text:
        return None
    m = _NUM_RE.search(text)
    return float(m.group(0).replace(",", ".")) if m else None


def _parse_area(text: str | None) -> float | None:
    if not text:
        return None
    m = _AREA_RE.search(text)
    return float(m.group(1).replace(",", ".")) if m else None


def _parse_disposition(text: str | None) -> str | None:
    if not text:
        return None
    m = _DISPOSITION_RE.search(text)
    return f"{m.group(1)}+{m.group(2).lower()}" if m else None


def _parse_floor(text: str | None) -> int | None:
    """idnes 'Podlaží': přízemí -> 0, 'N. NP' -> N-1, 'N. PP' -> -N."""
    if not text:
        return None
    t = text.lower()
    if "přízem" in t or "prizem" in t:
        return 0
    m = re.search(r"(\d+)\.\s*np", t)
    if m:
        return int(m.group(1)) - 1
    m = re.search(r"(\d+)\.\s*pp", t)
    if m:
        return -int(m.group(1))
    m = re.search(r"-?\d+", t)
    return int(m.group(0)) if m else None


def _parse_price(text: str | None, category_type: str | None) -> tuple[int | None, str | None]:
    unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    if not text:
        return None, unit
    # idnes joins digit groups with U+200D (zwj) + nbsp; strip everything but digits.
    digits = re.sub(r"\D", "", text)
    return (int(digits) if digits else None), unit


def _parse_energy(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"\b([A-G])\b", text)
    return m.group(1) if m else None


def _parse_ownership(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    if "osobní" in t or "osobni" in t:
        return "osobni"
    if "družst" in t or "druzst" in t:
        return "druzstevni"
    if "státní" in t or "statni" in t or "obecní" in t or "obecni" in t:
        return "statni"
    return None


def _parse_furnished(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    if "částečně" in t or "castecne" in t:
        return "castecne"
    if "nezaříz" in t or "nezariz" in t or "nevybav" in t:
        return "ne"
    if "zaříz" in t or "zariz" in t or "vybav" in t:
        return "ano"
    return None


def _parse_locality(info: str | None) -> tuple[str | None, str | None]:
    """'Street, City - Part, okres X' -> (locality, district).

    District is the okres token; locality is the segment just before it (the
    city), dropping any leading street. Prague lines often lack the okres part.
    """
    if not info:
        return None, None
    district = None
    m = re.search(r"okres\s+([^,]+)", info, re.IGNORECASE)
    if m:
        district = m.group(1).strip() or None
    parts = [p.strip() for p in re.split(r",", info) if p.strip()]
    parts = [p for p in parts if not re.match(r"(?i)okres\s+", p)]
    locality = parts[-1] if parts else None
    return locality, district


def _parse_coords(html: str) -> tuple[float | None, float | None]:
    """Subject coordinates from the map config `center`, else the first
    GeoJSON Point. Both are [lon, lat]; we return (lat, lon)."""
    m = _CENTER_RE.search(html) or _GEOJSON_RE.search(html)
    if not m:
        return None, None
    lon, lat = float(m.group(1)), float(m.group(2))
    return lat, lon


def _gallery_images(tree: HTMLParser) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for img in tree.css(".b-gallery img"):
        src = img.attributes.get("data-src") or img.attributes.get("src")
        if src and "/thumbs/" in src and src not in seen:
            seen.add(src)
            urls.append(src)
    if not urls:
        og = tree.css_first('meta[property="og:image"]')
        src = og.attributes.get("content") if og else None
        if src:
            urls.append(src)
    return urls


def parse_detail(
    html: str,
    *,
    source_url: str,
    category_main: str | None,
    category_type: str | None,
) -> ScrapedListing:
    tree = HTMLParser(html)
    source_id = _id_from_url(source_url) or ""

    title = _text(tree.css_first("h1.b-detail__title")) or ""
    info = _text(tree.css_first("p.b-detail__info"))
    locality, district = _parse_locality(info)

    price_czk, price_unit = _parse_price(
        _text(tree.css_first("p.b-detail__price strong")), category_type
    )

    params = _detail_params(tree)
    usable_area = _first_number(params.get("užitná plocha") or params.get("uzitna plocha"))
    area_m2 = usable_area or _parse_area(title)

    description = _text(tree.css_first("div.b-desc"))
    lat, lon = _parse_coords(html)

    image_urls = _gallery_images(tree)
    raw = {
        "id": source_id,
        "title": title,
        "info": info,
        "price_text": _text(tree.css_first("p.b-detail__price strong")),
        "reference": params.get("číslo zakázky") or params.get("cislo zakazky"),
        "image_urls": image_urls,
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
        usable_area=usable_area,
        disposition=_parse_disposition(title),
        locality=locality,
        district=district,
        lat=lat,
        lon=lon,
        floor=_parse_floor(params.get("podlaží") or params.get("podlazi")),
        total_floors=int(_first_number(params.get("počet podlaží budovy")) or 0) or None,
        building_type=params.get("konstrukce budovy"),
        condition=params.get("stav bytu") or params.get("stav objektu"),
        energy_rating=_parse_energy(params.get("penb") or params.get("energetická náročnost")),
        ownership=_parse_ownership(params.get("vlastnictví") or params.get("vlastnictvi")),
        furnished=_parse_furnished(params.get("vybavení") or params.get("vybaveni")),
        description=description,
        raw=raw,
    )
