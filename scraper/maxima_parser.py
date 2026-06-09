"""Deterministic HTML parsing for nemovitosti.maxima.cz (portal framework).

Pure functions, no I/O of their own: `parse_index` turns one search-results page
into the listing ids + the next page, and `parse_detail` turns one listing page
into a `ScrapedListing` (the shared multi-portal contract in
`scraper.scraped_listing`).

Maxima is a single-agency catalogue (~220 listings) on a WordPress site, but a
STRUCTURED one: a `<table>` spec block (paired `th.slider_label` / `td.slider_value`),
a clean `div.price` element, and — when present — precise per-listing coordinates
embedded in the page's OpenLayers map config (`"center":[lon,lat]`). So coordinates
come straight from the page when available (some agency listings omit the map).

Unlike sreality/idnes, maxima exposes ONE mixed index (no per-category URL); the
category is encoded in the native id's leading letter (b=byt, d=dum, f=pozemek,
g=komercni, o=ostatni) and the title verb (Prodej/Pronájem), so `parse_detail`
derives `category_main`/`category_type` itself. Typed `<table>` fields are
normalised to the same canonical labels the sreality parser emits (e.g.
"Cihlová" -> "cihla", "Osobní" -> "osobni") so cross-portal filters agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from unicodedata import combining, normalize

from selectolax.parser import HTMLParser, Node

from scraper.scraped_listing import ScrapedListing

# Native-id leading letter -> our canonical category_main (mirrors the sreality
# parser.CATEGORY_* labels). The letter is the agency's own taxonomy and is the
# primary category signal; the title verb is the fallback for an unseen prefix.
CATEGORY_BY_PREFIX: dict[str, str] = {
    "b": "byt",
    "d": "dum",
    "f": "pozemek",
    "g": "komercni",
    "o": "ostatni",
}
# Title-verb fallback when the id prefix is unknown.
CATEGORY_BY_TITLE: tuple[tuple[str, str], ...] = (
    ("byt", "byt"),
    ("dom", "dum"),         # "rodinného domu", "domu"
    ("chat", "dum"),        # chata / chalupa
    ("pozemk", "pozemek"),
    ("komer", "komercni"),
    ("kancel", "komercni"),
    # Checked last so a specific category wins first ("byt s garáží" -> byt): a
    # garage / catch-all "ostatní" title (e.g. "Pronájem ostatní garáže") maps to
    # ostatni, mirroring maxima's own taxonomy (the sale side uses the 'o' prefix).
    ("garaz", "ostatni"),
    ("ostatn", "ostatni"),
)

# idnes/sreality building-construction labels -> the canonical codes the sreality
# parser emits, so a cross-portal "panel" filter matches every source.
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

# Czech-bbox guard: a coordinate outside it (a swapped lat/lon, or a stray pin) is
# dropped rather than stored as geom.
_CZ_LAT_MIN, _CZ_LAT_MAX = 48.0, 51.5
_CZ_LON_MIN, _CZ_LON_MAX = 12.0, 19.0

# The detail-URL slug is the source_id_native, e.g. /nemovitosti/b50087758/.
_ID_RE = re.compile(r"/nemovitosti/([a-z]\d+)/?(?:[?#]|$)")
_LISTING_HREF_RE = re.compile(r"/nemovitosti/[a-z]\d+/?$")
_PAGE_RE = re.compile(r"/page/(\d+)/?")
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:2|²|\s*2)\b", re.IGNORECASE)
_DISPOSITION_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
_INT_RE = re.compile(r"(\d+)")
# Price runs are Czech "18 878 000" (groups split by ordinary / no-break / thin /
# zero-width spaces). The first run only is taken so a struck original + current
# price never concatenate into an integer-overflowing number.
_PRICE_RUN_RE = re.compile(r"\d[\d\s ​‌‍⁠]*")
_PRICE_MAX = 2_147_483_647  # listings.price_czk is a Postgres integer
# Map config: "center":[lon, lat]. The map JSON is passed to JSON.parse('…') with
# the quotes backslash-escaped in the page source (\"center\":[…]), so the quotes
# are optionally preceded by a backslash. CZ lat/lon ranges don't overlap, so a
# swap is caught by the bbox guard rather than producing a bogus point.
_CENTER_RE = re.compile(
    r'\\?"center\\?"\s*:\s*\[\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*[\]\\]'
)
# "3./6." -> floor 3 of 6.
_FLOOR_RE = re.compile(r"(-?\d+)\s*\.\s*/\s*(\d+)\s*\.")
_PENB_RE = re.compile(r"PENB\s*:?\s*([A-G])\b")
# maxima serves /resize/w-{width}-...; a detail page mixes a w-800 cover with w-300
# thumbnails. Request the largest — the CDN caps at the original (~1799x1200).
_MAXIMA_RESIZE_RE = re.compile(r"/resize/w-\d+-")


def _full_size_image_url(src: str) -> str:
    return _MAXIMA_RESIZE_RE.sub("/resize/w-2400-", src, count=1)


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


def category_from_id(native_id: str | None) -> str | None:
    """category_main from a native id's leading letter (b=byt, d=dum, …)."""
    if not native_id:
        return None
    return CATEGORY_BY_PREFIX.get(native_id[0].lower())


def _category_from_title(title: str | None) -> str | None:
    if not title:
        return None
    low = _strip_diacritics(title).lower()
    for needle, canon in CATEGORY_BY_TITLE:
        if needle in low:
            return canon
    return None


def category_of(native_id: str | None, title: str | None) -> str | None:
    """category_main, title-verb first then id-prefix. The title is authoritative
    across BOTH agendas: the rent side (af=2) uses native-id prefixes the sale
    taxonomy (b/d/f/g/o) doesn't cover, so a prefix-first derivation would dump
    every rental into 'ostatni'. The index walk and the detail parser both call
    this so their category assignment can never disagree (which would fragment the
    Health reconciliation)."""
    return _category_from_title(title) or category_from_id(native_id)


def _sale_type_from_title(title: str | None) -> str | None:
    if not title:
        return None
    low = _strip_diacritics(title).lower()
    if "pronajem" in low:
        return "pronajem"
    if "prodej" in low:
        return "prodej"
    return None


def _penb_from_text(text: str) -> str | None:
    m = _PENB_RE.search(text)
    return m.group(1).upper() if m else None


def index_price(text: str | None) -> int | None:
    """The Kč amount from an index card's price text, or None ("Dohodou" /
    "Info o ceně"). Drives price-change detection for the detail-refetch queue."""
    return _parse_price(text, None)[0]


def _parse_total(text: str) -> int | None:
    m = re.search(r"(\d[\d\s ]*\d|\d)\s*nemovitost", text, re.IGNORECASE)
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
    # Take only the FIRST price run, so a struck-through original + current price
    # never concatenate into an integer-overflowing number.
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


def _parse_floors(text: str | None) -> tuple[int | None, int | None]:
    """maxima renders 'podlaží' as '3./6.' (floor 3 of 6)."""
    if not text:
        return None, None
    m = _FLOOR_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return _parse_int(text), None


def _yes_no(text: str | None) -> bool | None:
    if not text:
        return None
    low = _strip_diacritics(text).lower().strip()
    if low.startswith("ano"):
        return True
    if low.startswith("ne"):
        return False
    return None


def _norm_condition(text: str | None) -> str | None:
    if not text:
        return None
    key = _strip_diacritics(text).lower().strip()
    key = re.sub(r"\s+stav$", "", key)
    key = re.sub(r"\s+", "_", key)
    return key or None


def _norm_ownership(text: str | None) -> str | None:
    if not text:
        return None
    # Canonical set only ({osobni, druzstevni, statni}); unmapped labels → None
    # rather than leaking a value no ownership filter option can match.
    key = _strip_diacritics(text).lower().strip()
    return OWNERSHIP.get(key)


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
    m = re.search(r"\b([A-G])\b", text)
    return m.group(1).upper() if m else None


def parse_index(html: str) -> IndexPage:
    tree = HTMLParser(html)
    total = _parse_total(_page_text(tree))

    items: list[IndexItem] = []
    seen: set[str] = set()
    for link in tree.css("a"):
        href = link.attributes.get("href")
        if not href or not _LISTING_HREF_RE.search(href):
            continue
        source_id = _id_from_href(href)
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        items.append(
            IndexItem(
                source_id_native=source_id,
                detail_path=href,
                title=_text(link.css_first(".slider_titulek")),
                price_text=_text(link.css_first(".slider_cena")),
                locality_text=_text(link.css_first(".text-slider")),
            )
        )

    return IndexPage(total=total, items=items, next_offset=_next_page(tree))


def _next_page(tree: HTMLParser) -> int | None:
    """The next page number, or None on the last page. The active pager carries
    `pager-active` (the bare home URL = page 1); a numbered link above the active
    page means there is a next page."""
    active = 1
    numbers: list[int] = []
    for link in tree.css("a.btn-pager"):
        href = link.attributes.get("href") or ""
        m = _PAGE_RE.search(href)
        page_no = int(m.group(1)) if m else 1
        cls = link.attributes.get("class") or ""
        if "pager-active" in cls:
            active = page_no
        elif m:
            numbers.append(page_no)
    if any(n > active for n in numbers):
        return active + 1
    return None


def _resolve_coords(html: str) -> tuple[float | None, float | None, dict[str, Any]]:
    m = _CENTER_RE.search(html)
    if m:
        lon, lat = float(m.group(1)), float(m.group(2))
        if _in_cz_bbox(lat, lon):
            return lat, lon, {"source": "page"}
    return None, None, {"source": None}


def _detail_params(tree: HTMLParser) -> dict[str, str]:
    """Map the spec-table row labels (lowercased) to their value text.

    maxima renders each spec row as `<tr><th class="slider_label">label</th>
    <td class="slider_value">value</td></tr>`."""
    rows: dict[str, str] = {}
    for tr in tree.css("tr"):
        label_node = tr.css_first("th.slider_label")
        value_node = tr.css_first("td.slider_value")
        if label_node is None or value_node is None:
            continue
        label = (label_node.text(separator=" ", strip=True) or "").rstrip(":").strip().lower()
        label = re.sub(r"\s+", " ", label)
        value = _text(value_node)
        if label and label not in rows and value is not None:
            rows[label] = value
    return rows


def parse_detail(
    html: str,
    *,
    source_url: str,
    category_main: str | None = None,
    category_type: str | None = None,
) -> ScrapedListing:
    tree = HTMLParser(html)
    source_id = _id_from_href(source_url) or ""

    title = _text(tree.css_first("h3")) or _text(tree.css_first("title")) or ""
    description = _text(tree.css_first("#collapse-inzerat-text"))
    params = _detail_params(tree)

    # Category is encoded in the title verb (authoritative across both agendas) +
    # the id prefix (sale only); derive it when the caller didn't pass an override.
    # Same `category_of` the index walk uses, so the two never disagree.
    category_main = category_main or category_of(source_id, title)
    category_type = category_type or _sale_type_from_title(title) or "prodej"

    price_text = _text(tree.css_first("div.price")) or params.get("cena")
    price_czk, price_unit = _parse_price(price_text, category_type)

    locality = _text(tree.css_first("div.locality"))
    lat, lon, coord_provenance = _resolve_coords(html)

    usable_text = params.get("plocha užitná") or params.get("užitná plocha")
    floor_text = params.get("plocha podlahová") or params.get("podlahová plocha")
    area_m2 = (
        _parse_area(usable_text)
        or _parse_area(floor_text)
        or _parse_area(title)
    )
    floor, total_floors = _parse_floors(params.get("podlaží"))

    source_id_upper = source_id.upper()
    image_urls: list[str] = []
    seen_img: set[str] = set()
    for img in tree.css("img"):
        src = img.attributes.get("src")
        if not src or "/resize/" not in src or source_id_upper not in src:
            continue
        full = _full_size_image_url(src)
        if full not in seen_img:
            seen_img.add(full)
            image_urls.append(full)

    raw: dict[str, Any] = {
        "id": source_id,
        "title": title,
        "price_text": price_text,
        "locality_text": locality,
        "maxima_ref": params.get("id zakázky"),
        "image_urls": image_urls,
        "coords": coord_provenance,
        "params": params,
    }

    # No `subtype` (migration 152): maxima exposes no property-subtype signal —
    # the "typ domu" spec row is STRUCTURAL (Patrový / Přízemní), the native-id
    # prefix is category-main level, and the operator opted to leave it NULL
    # rather than infer from the free-text title. NULL still shows at the
    # category level; it only drops out under a specific-subtype filter.
    return ScrapedListing(
        source="maxima",
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=area_m2,
        usable_area=_parse_area(usable_text),
        disposition=_parse_disposition(title) or _parse_disposition(params.get("dispozice")),
        locality=locality,
        district=None,
        lat=lat,
        lon=lon,
        floor=floor,
        total_floors=total_floors,
        building_type=_norm_building_type(params.get("budova")),
        condition=_norm_condition(params.get("stav objektu")),
        ownership=_norm_ownership(params.get("vlastnictví")),
        energy_rating=(
            _energy_rating(params.get("energetická náročnost"))
            or _energy_rating(params.get("penb"))
            or _penb_from_text(_page_text(tree))
        ),
        has_balcony=_yes_no(params.get("balkón")),
        has_lift=_yes_no(params.get("výtah")),
        cellar=_yes_no(params.get("sklep")),
        terrace=_yes_no(params.get("terasa")),
        garage=_yes_no(params.get("garáž")),
        has_parking=_yes_no(params.get("parkovací stání")) or _yes_no(params.get("garáž")),
        estate_area=_parse_area(params.get("plocha pozemku")),
        garden_area=_parse_area(params.get("plocha zahrady")),
        description=description,
        raw=raw,
    )
