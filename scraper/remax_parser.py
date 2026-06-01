"""Deterministic HTML parsing for remax-czech.cz (portal framework).

Pure functions, no I/O: `parse_index` turns one search-results page into the
listing ids (+ the per-card price/title/coords the index already carries), and
`parse_detail` turns one listing page into a `ScrapedListing` (the shared
multi-portal contract in `scraper.scraped_listing`).

remax is a STRUCTURED HTML site, so parsing is deterministic (no LLM):
- the search cards are `<div class="pl-items__item" data-url=… data-price=…
  data-gps=… data-title=…>` — price, coordinates and title come straight off the
  card, so the index walk already knows each listing's price (price-change
  detection) and category (the title verb + noun);
- the detail page is a `pd-detail-info__row` → `__label`/`__value` spec block, a
  clean integer `data-advert-price`, per-listing coordinates in `data-gps`
  (DMS, e.g. `50°05'26.1"N,14°29'33.4"E`), and a `mlsf.remax-czech.cz/data//zs/{id}/`
  image gallery (the `_th350` thumbnail strips to the full-resolution original).

Like maxima, remax exposes ONE mixed index (no per-category URL slice) split only
by an offer-type flag (`sale=1` prodej, `sale=2` pronájem); the category is read
from the card title (index) and the detail page's "Typ nemovitosti" row + title
verb (detail), so `parse_detail` derives `category_main`/`category_type` itself.
Typed `<div>` fields are normalised to the same canonical labels the sreality
parser emits (e.g. "Cihlová" -> "cihla", "Osobní" -> "osobni") so cross-portal
filters agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any
from unicodedata import combining, normalize

from selectolax.parser import HTMLParser, Node

from scraper.scraped_listing import ScrapedListing

# Detail "Typ nemovitosti" value (diacritics-stripped, lowercased) -> canonical
# category_main. remax's ~19 fine property types collapse onto the canonical five
# (mirrors the sreality parser.CATEGORY_MAIN labels).
TYP_TO_CATEGORY: tuple[tuple[str, str], ...] = (
    ("byty", "byt"),
    ("apartman", "byt"),
    ("domy a vily", "dum"),
    ("domy", "dum"),
    ("vily", "dum"),
    ("chaty a chalupy", "dum"),
    ("najemni domy", "dum"),
    ("historicke objekty", "dum"),
    ("pozemky", "pozemek"),
    ("kancelare", "komercni"),
    ("obchodni", "komercni"),
    ("restaurace", "komercni"),
    ("ubytovani", "komercni"),
    ("vyroba", "komercni"),
    ("sklady", "komercni"),
    ("vinne sklepy", "komercni"),
    ("zemedelske objekty", "komercni"),
    ("garazova stani", "ostatni"),
    ("garaze", "ostatni"),
    ("male objekty", "ostatni"),
    ("mobilheim", "ostatni"),
    ("houseboat", "ostatni"),
    ("jine", "ostatni"),
)

# Title-noun fallback (diacritics-stripped, lowercased), checked in order so a
# specific category wins before the garage/ostatni catch-all.
CATEGORY_BY_TITLE: tuple[tuple[str, str], ...] = (
    ("bytu", "byt"),
    ("byt ", "byt"),
    ("apartman", "byt"),
    ("rodinneho domu", "dum"),
    ("domu", "dum"),
    ("vily", "dum"),
    ("chaty", "dum"),
    ("chalup", "dum"),
    ("pozemk", "pozemek"),
    ("kancelar", "komercni"),
    ("obchodn", "komercni"),
    ("restaurac", "komercni"),
    ("ubytovac", "komercni"),
    ("vyrob", "komercni"),
    ("skladu", "komercni"),
    ("vinneho sklep", "komercni"),
    ("garaz", "ostatni"),
    ("stani", "ostatni"),
    ("objektu", "ostatni"),
)

# Construction labels -> the canonical codes the sreality parser emits.
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

# Czech-bbox guard: a coordinate outside it (a swapped lat/lon, or a stray pin) is
# dropped rather than stored as geom.
_CZ_LAT_MIN, _CZ_LAT_MAX = 48.0, 51.5
_CZ_LON_MIN, _CZ_LON_MAX = 12.0, 19.0

_ID_RE = re.compile(r"/reality/detail/(\d+)")
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:2|²|\s*2)\b", re.IGNORECASE)
_DISPOSITION_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
_INT_RE = re.compile(r"(-?\d+)")
_PRICE_MAX = 2_147_483_647  # listings.price_czk is a Postgres integer
# data-gps DMS pair: "50°05'26.1"N,14°29'33.4"E" (entities already unescaped).
# Seconds are optional and may read 60 (remax rounds oddly); deg+min/60+sec/3600
# absorbs that without a bogus point.
_DMS_RE = re.compile(
    r"(\d+)\s*°\s*(\d+)\s*'\s*([\d.]+)?\s*\"?\s*([NSEW])", re.IGNORECASE
)
_TOTAL_RE = re.compile(r"z\s*celkem\s*([0-9][0-9\s ]*)", re.IGNORECASE)
# Listing image: mlsf.remax-czech.cz/data//zs/{id}/{photo}_th350.jpg — the
# _th350 thumbnail strips to the full-resolution original (verified).
_IMG_RE = re.compile(r"https://mlsf\.remax-czech\.cz/data/+zs/(\d+)/[^\"'\s]+", re.IGNORECASE)
_THUMB_SUFFIX_RE = re.compile(r"_th\d+(?=\.\w+$)")
# The subject listing's price/coords/address come from page attributes; the FIRST
# occurrence is the subject's (recommended-listing cards follow it lower down).
_ADVERT_PRICE_RE = re.compile(r'data-advert-price="(\d+)"')
_GPS_ATTR_RE = re.compile(r'data-gps="([^"]*)"')
_ADDRESS_ATTR_RE = re.compile(r'data-address="([^"]*)"')


@dataclass(frozen=True)
class IndexItem:
    source_id_native: str
    detail_path: str
    title: str | None = None
    price_text: str | None = None
    gps: str | None = None
    address: str | None = None


@dataclass(frozen=True)
class IndexPage:
    total: int | None
    items: list[IndexItem] = field(default_factory=list)


def _strip_diacritics(text: str) -> str:
    return "".join(c for c in normalize("NFD", text) if not combining(c))


def _norm_key(text: str | None) -> str:
    return _strip_diacritics(text or "").lower().strip()


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


def category_from_typ(typ: str | None) -> str | None:
    """category_main from a detail page's "Typ nemovitosti" value."""
    key = _norm_key(typ)
    if not key:
        return None
    for needle, canon in TYP_TO_CATEGORY:
        if needle in key:
            return canon
    return None


def _category_from_title(title: str | None) -> str | None:
    low = _norm_key(title)
    if not low:
        return None
    for needle, canon in CATEGORY_BY_TITLE:
        if needle in low:
            return canon
    return None


def category_of(typ: str | None, title: str | None) -> str | None:
    """category_main: the authoritative detail "Typ nemovitosti" first, then the
    title noun. The index walk passes typ=None (only the card title is known);
    the detail parser passes both so the two can't disagree (which would fragment
    the Health reconciliation)."""
    return category_from_typ(typ) or _category_from_title(title)


def type_of(title: str | None) -> str | None:
    """category_type from the title/slug verb (Prodej -> prodej, Pronájem ->
    pronajem)."""
    low = _norm_key(title)
    if "pronajem" in low:
        return "pronajem"
    if "prodej" in low:
        return "prodej"
    return None


def _parse_dms_pair(text: str | None) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    matches = _DMS_RE.findall(unescape(text))
    coords: dict[str, float] = {}
    for deg, minutes, seconds, hemi in matches:
        val = int(deg) + int(minutes) / 60.0 + (float(seconds) if seconds else 0.0) / 3600.0
        hemi = hemi.upper()
        if hemi in ("S", "W"):
            val = -val
        axis = "lat" if hemi in ("N", "S") else "lon"
        coords.setdefault(axis, val)
    lat, lon = coords.get("lat"), coords.get("lon")
    if lat is not None and lon is not None and _in_cz_bbox(lat, lon):
        return lat, lon
    return None, None


def _parse_price(text: str | None, category_type: str | None) -> tuple[int | None, str | None]:
    unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    if not text:
        return None, unit
    low = _norm_key(text)
    if any(k in low for k in ("info o cene", "cena v rk", "dohodou", "neuvedena", "poptavce")):
        return None, unit
    digits = re.sub(r"\D", "", re.split(r"<", text)[0])
    if not digits:
        return None, unit
    value = int(digits)
    return (value if value <= _PRICE_MAX else None), unit


def index_price(text: str | None) -> int | None:
    """The Kč amount from an index card's data-price text, or None. Drives
    price-change detection for the detail-refetch queue."""
    return _parse_price(text, None)[0]


def _parse_total(html: str) -> int | None:
    flat = re.sub(r"<[^>]+>", "", html)
    m = _TOTAL_RE.search(flat)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


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
    return float(m.group(1).replace(",", ".")) if m else None


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    m = _INT_RE.search(text)
    return int(m.group(1)) if m else None


def _yes_no(text: str | None) -> bool | None:
    low = _norm_key(text)
    if not low:
        return None
    if low.startswith("ano"):
        return True
    if low.startswith("ne"):
        return False
    return None


def _norm_condition(text: str | None) -> str | None:
    key = _norm_key(text)
    if not key:
        return None
    key = re.sub(r"\s+stav$", "", key)
    key = re.sub(r"\s+", "_", key)
    return key or None


def _norm_ownership(text: str | None) -> str | None:
    key = _norm_key(text)
    return OWNERSHIP.get(key, key or None) if key else None


def _norm_furnished(text: str | None) -> str | None:
    yn = _yes_no(text)
    if yn is True:
        return "vybaveno"
    if yn is False:
        return "nevybaveno"
    if "castec" in _norm_key(text):
        return "castecne"
    return None


def _norm_building_type(text: str | None) -> str | None:
    key = _norm_key(text)
    if not key:
        return None
    return BUILDING_TYPE.get(key, key)


def _energy_rating(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"\b([A-G])\b", text)
    return m.group(1).upper() if m else None


def _full_image(url: str) -> str:
    """Strip the `_th350` thumbnail suffix to the full-resolution original."""
    return _THUMB_SUFFIX_RE.sub("", url)


def parse_index(html: str) -> IndexPage:
    tree = HTMLParser(html)
    total = _parse_total(html)

    items: list[IndexItem] = []
    seen: set[str] = set()
    for card in tree.css("div.pl-items__item"):
        attrs = card.attributes
        url = attrs.get("data-url") or ""
        source_id = _id_from_href(url)
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        items.append(
            IndexItem(
                source_id_native=source_id,
                detail_path=url,
                title=unescape(attrs.get("data-title") or "") or None,
                price_text=unescape(attrs.get("data-price") or "") or None,
                gps=unescape(attrs.get("data-gps") or "") or None,
                address=unescape(attrs.get("data-display-address") or "") or None,
            )
        )

    return IndexPage(total=total, items=items)


def _detail_params(tree: HTMLParser) -> dict[str, str]:
    """Map each spec-row label (lowercased, no trailing colon) to its value text.

    remax renders each row as `<div class="pd-detail-info__row">
    <div class="pd-detail-info__label">Label:</div>
    <div class="pd-detail-info__value">value</div></div>`."""
    rows: dict[str, str] = {}
    for row in tree.css("div.pd-detail-info__row"):
        label_node = row.css_first("div.pd-detail-info__label")
        value_node = row.css_first("div.pd-detail-info__value")
        if label_node is None or value_node is None:
            continue
        label = (label_node.text(separator=" ", strip=True) or "").rstrip(":").strip().lower()
        label = _strip_diacritics(re.sub(r"\s+", " ", label))
        value = _text(value_node)
        if label and label not in rows and value is not None:
            rows[label] = value
    return rows


def _detail_images(html: str, source_id: str) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()
    for m in _IMG_RE.finditer(html):
        if m.group(1) != source_id:  # skip recommended/other-listing thumbnails
            continue
        full = _full_image(m.group(0))
        if full not in seen:
            seen.add(full)
            images.append(full)
    return images


def _h1_locality(title: str | None) -> tuple[str | None, str | None]:
    """locality + district from the h1 tail (after the m²): 'Praha 3 - Žižkov'."""
    if not title:
        return None, None
    tail = title.split(",")[-1].strip()
    tail = re.sub(r"\s*\(ID\b.*$", "", tail).strip()
    if not tail or _AREA_RE.search(tail):
        return None, None
    district = tail.split(" - ")[-1].strip() if " - " in tail else None
    return tail, district


def parse_detail(
    html: str,
    *,
    source_url: str,
    category_main: str | None = None,
    category_type: str | None = None,
) -> ScrapedListing:
    tree = HTMLParser(html)
    source_id = _id_from_href(source_url) or ""

    h1 = tree.css_first("h1")
    title = _text(h1) or _text(tree.css_first("title")) or ""
    params = _detail_params(tree)

    category_main = category_main or category_of(params.get("typ nemovitosti"), title)
    category_type = category_type or type_of(title) or type_of(source_url) or "prodej"

    price_match = _ADVERT_PRICE_RE.search(html)
    price_attr = price_match.group(1) if price_match else None
    price_czk: int | None = None
    price_unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    if price_attr:
        value = int(price_attr)
        price_czk = value if 0 < value <= _PRICE_MAX else None
    if price_czk is None:
        price_czk, price_unit = _parse_price(_text(tree.css_first(".pd-price")), category_type)

    # Coordinates: the first data-gps on the page is the subject listing's (the
    # rest belong to recommended cards). CZ-bbox-guarded.
    lat = lon = None
    gps_match = _GPS_ATTR_RE.search(html)
    if gps_match is not None:
        lat, lon = _parse_dms_pair(gps_match.group(1))

    locality, district = _h1_locality(title)
    addr_match = _ADDRESS_ATTR_RE.search(html)
    address = (
        unescape(addr_match.group(1)).strip(" ,") or None if addr_match else None
    )
    if address and not locality:
        # data-address is "street, city - district, region"; the middle segment
        # is the locality.
        parts = [p.strip() for p in address.split(",") if p.strip()]
        if len(parts) >= 2:
            locality = parts[1]
            district = locality.split(" - ")[-1].strip() if " - " in locality else district

    usable_text = params.get("uzitna plocha")
    total_text = params.get("celkova plocha") or params.get("plocha")
    area_m2 = _parse_area(usable_text) or _parse_area(total_text) or _parse_area(title)

    image_urls = _detail_images(html, source_id)

    raw: dict[str, Any] = {
        "id": source_id,
        "title": title,
        "price_text": price_attr,
        "address": address,
        "remax_ref": params.get("cislo zakazky"),
        "image_urls": image_urls,
        "params": params,
    }

    return ScrapedListing(
        source="remax",
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=area_m2,
        usable_area=_parse_area(usable_text),
        disposition=_parse_disposition(params.get("dispozice")) or _parse_disposition(title),
        locality=locality,
        district=district,
        lat=lat,
        lon=lon,
        floor=_parse_int(params.get("cislo podlazi")),
        total_floors=_parse_int(params.get("pocet podlazi v objektu")),
        building_type=_norm_building_type(params.get("druh objektu")),
        condition=_norm_condition(params.get("stav objektu")),
        ownership=_norm_ownership(params.get("vlastnictvi")),
        energy_rating=(
            _energy_rating(params.get("energeticka narocnost budovy"))
            or _energy_rating(params.get("energeticka narocnost"))
        ),
        has_balcony=_yes_no(params.get("balkon")) or _yes_no(params.get("lodzie")),
        has_lift=_yes_no(params.get("vytah")),
        cellar=_yes_no(params.get("sklep")),
        terrace=_yes_no(params.get("terasa")),
        garage=_yes_no(params.get("garaz")),
        has_parking=_yes_no(params.get("parkovani")) or _yes_no(params.get("garaz")),
        furnished=_norm_furnished(params.get("vybaveno")),
        estate_area=_parse_area(params.get("plocha pozemku")),
        garden_area=_parse_area(params.get("plocha zahrady")),
        description=_text(tree.css_first(".pd-detail-text")) or _text(tree.css_first("#popis")),
        raw=raw,
    )
