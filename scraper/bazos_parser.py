"""Deterministic HTML parsing for reality.bazos.cz (multi-portal slice 3b).

Pure functions, no I/O: `parse_index` turns one search-results page into the
listing ids + the next offset, and `parse_detail` turns one listing page into
a `ScrapedListing` (the shared multi-portal contract in
`scraper.scraped_listing`). Bazos is a free-form classifieds site — no JSON
API, attributes buried in free text — so disposition and area come out by
regex over the title + description, and coordinates come from the embedded
Google-Maps link (most listings carry one, so no geocoding is needed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser, Node

from scraper.scraped_listing import ScrapedListing

# Bazos URL segments -> our canonical labels (mirrors parser.CATEGORY_* style).
SALE_TYPE: dict[str, str] = {
    "prodam": "prodej",
    "pronajmu": "pronajem",
}
CATEGORY_MAIN: dict[str, str] = {
    "byt": "byt",
    "dum": "dum",
    "pozemky": "pozemek",
    "pozemek": "pozemek",
    "nebytove": "komercni",
    "komercni": "komercni",
    "ostatni": "ostatni",
}

_ID_RE = re.compile(r"/inzerat/(\d+)/")
_PSC_RE = re.compile(r"\b(\d{3})\s?(\d{2})\b")
_COORD_RE = re.compile(r"(-?\d{1,3}\.\d{3,}),\s*(-?\d{1,3}\.\d{3,})")
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:2|²)\b", re.IGNORECASE)
_DISPOSITION_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
_PRICE_DIGITS_RE = re.compile(r"\d[\d\s ]{3,}")


@dataclass(frozen=True)
class IndexItem:
    source_id_native: str
    detail_path: str
    title: str | None = None
    price_text: str | None = None
    locality_text: str | None = None
    posted_date: str | None = None
    views: str | None = None


@dataclass(frozen=True)
class IndexPage:
    total: int | None
    items: list[IndexItem] = field(default_factory=list)
    next_offset: int | None = None


def _text(node: Node | None) -> str | None:
    if node is None:
        return None
    txt = node.text(strip=True)
    return txt or None


def _page_text(tree: HTMLParser) -> str:
    body = tree.body
    if body is not None:
        return body.text(separator=" ", strip=False)
    root = tree.root
    return root.text(separator=" ", strip=False) if root is not None else ""


def _parse_total(text: str) -> int | None:
    # "Zobrazeno 1-20 inzerátů z 6 990"
    m = re.search(r"inzer\w+\s+z\s+([\d  ]+)", text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _parse_price(text: str | None, category_type: str | None) -> tuple[int | None, str | None]:
    unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    if not text:
        return None, unit
    if "dohodou" in text.lower():
        return None, unit
    m = _PRICE_DIGITS_RE.search(text)
    if not m:
        return None, unit
    digits = re.sub(r"\D", "", m.group(0))
    return (int(digits) if digits else None), unit


def _parse_disposition(text: str) -> str | None:
    m = _DISPOSITION_RE.search(text)
    if not m:
        return None
    return f"{m.group(1)}+{m.group(2).lower()}"


def _parse_area(text: str) -> float | None:
    m = _AREA_RE.search(text)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def _parse_coords(href: str | None) -> tuple[float | None, float | None]:
    if not href:
        return None, None
    m = _COORD_RE.search(href)
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def _id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    m = _ID_RE.search(href)
    return m.group(1) if m else None


def parse_index(html: str) -> IndexPage:
    tree = HTMLParser(html)
    total = _parse_total(_page_text(tree))

    items: list[IndexItem] = []
    for block in tree.css("div.inzeraty.inzeratyflex"):
        link = block.css_first('a[href*="/inzerat/"]')
        href = link.attributes.get("href") if link else None
        source_id = _id_from_href(href)
        if not source_id or not href:
            continue
        items.append(
            IndexItem(
                source_id_native=source_id,
                detail_path=href,
                title=_text(block.css_first("h2.nadpis")) or _text(link),
                price_text=_text(block.css_first("div.inzeratycena")),
                locality_text=_text(block.css_first("div.inzeratylok")),
                posted_date=_text(block.css_first("span.velikost10")),
                views=_text(block.css_first("div.inzeratyview")),
            )
        )

    return IndexPage(total=total, items=items, next_offset=_next_offset(tree))


def _next_offset(tree: HTMLParser) -> int | None:
    pager = tree.css_first("div.strankovani")
    if pager is None:
        return None
    for link in pager.css("a"):
        if (link.text(strip=True) or "").startswith("Další"):
            m = re.search(r"/(\d+)/?$", link.attributes.get("href") or "")
            if m:
                return int(m.group(1))
    return None


def _locality(cell_text: str | None) -> tuple[str | None, str | None]:
    """Return (locality, psc) from a 'Town  PSČ' blob.

    The town is taken as the text BEFORE the PSČ so a trailing map-link label
    in the same cell doesn't leak into the town name.
    """
    if not cell_text:
        return None, None
    psc_match = _PSC_RE.search(cell_text)
    if psc_match:
        psc = f"{psc_match.group(1)} {psc_match.group(2)}"
        town = cell_text[: psc_match.start()].strip(" ,\n\t") or None
    else:
        psc = None
        town = cell_text.strip(" ,\n\t") or None
    return town, psc


def _detail_table(tree: HTMLParser) -> dict[str, Node]:
    """Map the left details-table row labels to their value cells."""
    rows: dict[str, Node] = {}
    for tr in tree.css("table tr"):
        cells = tr.css("td")
        if len(cells) < 2:
            continue
        label = (cells[0].text(strip=True) or "").rstrip(":").lower()
        if label:
            rows[label] = cells[1]
    return rows


def parse_detail(
    html: str,
    *,
    source_url: str,
    category_main: str | None,
    category_type: str | None,
) -> ScrapedListing:
    tree = HTMLParser(html)
    source_id = _id_from_href(source_url) or ""

    title = _text(tree.css_first("h1.nadpisdetail")) or ""
    description = _text(tree.css_first("div.popisdetail"))
    haystack = f"{title}\n{description or ''}"

    table = _detail_table(tree)
    price_cell = table.get("cena")
    price_czk, price_unit = _parse_price(_text(price_cell), category_type)

    lok_cell = table.get("lokalita")
    maps_link = lok_cell.css_first('a[href*="map"]') if lok_cell else None
    lat, lon = _parse_coords(maps_link.attributes.get("href") if maps_link else None)
    locality, psc = _locality(_text(lok_cell))

    image_urls = [
        src
        for img in tree.css("img")
        if (src := img.attributes.get("src")) and "bazos.cz/img/" in src
    ]

    raw = {
        "id": source_id,
        "title": title,
        "price_text": _text(price_cell),
        "locality_text": _text(lok_cell),
        "psc": psc,
        "views": _text(table.get("vidělo")) or _text(table.get("videlo")),
        "posted_date": _text(tree.css_first("span.velikost10")),
        "image_urls": image_urls,
    }

    return ScrapedListing(
        source="bazos",
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=_parse_area(haystack),
        disposition=_parse_disposition(haystack),
        locality=locality,
        district=None,
        lat=lat,
        lon=lon,
        description=description,
        raw=raw,
    )
