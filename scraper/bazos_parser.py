"""Deterministic HTML parsing for reality.bazos.cz (multi-portal slice 3b).

Pure functions, no I/O of their own: `parse_index` turns one search-results
page into the listing ids + the next offset, and `parse_detail` turns one
listing page into a `ScrapedListing` (the shared multi-portal contract in
`scraper.scraped_listing`). Bazos is a free-form classifieds site — no JSON
API, attributes buried in free text — so disposition and area come out by
regex over the title + description.

Coordinates are resolved TEXT-FIRST and cross-checked (`_resolve_coords`):
a street name mined from the title/description geocodes to a far more precise
point than the embedded maps-link pin, which is frequently a town-centre
approximation. The link is used only as corroboration/fallback when it is
consistent with the text reference; a pin that lands in a different town is
distrusted. Geocoding is injected (a `Geocoder` callable) so these stay
hermetically testable and so a missing `MAPY_CZ_API_KEY` degrades gracefully
to the CZ-guarded link. Precise per-listing coords are what make cross-source
dedup (the 20m / 150m gates) work.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from selectolax.parser import HTMLParser, Node

from scraper.floor import floor_from_text
from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.published import bazos_posted_date
from scraper.scraped_listing import ScrapedListing
from scraper.street import clean_street

Geocoder = Callable[[str], GeocodeResult]

# Bazos URL segments -> our canonical labels (mirrors parser.CATEGORY_* style).
SALE_TYPE: dict[str, str] = {
    "prodam": "prodej",
    "pronajmu": "pronajem",
}
CATEGORY_MAIN: dict[str, str] = {
    "byt": "byt",
    "dum": "dum",
    "chata": "dum",          # bazoš "Chaty, Chalupy" section
    "pozemky": "pozemek",
    "pozemek": "pozemek",
    "zahrada": "pozemek",
    "nebytove": "komercni",
    "komercni": "komercni",
    "kancelar": "komercni",
    "prostory": "komercni",  # "Obchodní prostory"
    "sklad": "komercni",
    "restaurace": "komercni",
    "garaz": "ostatni",
    "ostatni": "ostatni",
}

# Portal-agnostic subtype (migration 152), keyed on the bazoš section slug that
# appears in the detail breadcrumb / index URL. Only the sections that map onto
# a single canonical dum/komercni subtype are listed; "dum" (generic houses,
# bazoš doesn't split rodinný/vila) and land/garage sections carry no subtype.
SUBTYPE: dict[str, str] = {
    "chata": "chata",              # bazoš lumps chaty+chalupy; chata is the slug
    "restaurace": "restaurace",
    "kancelar": "kancelar",
    "prostory": "obchodni_prostor",
    "sklad": "sklad",
}

_ID_RE = re.compile(r"/inzerat/(\d+)/")
_PSC_RE = re.compile(r"\b(\d{3})\s?(\d{2})\b")
_MAP_LABEL_RE = re.compile(r"(?i)\bzobrazit na map[ěe]\b")
_COORD_RE = re.compile(r"(-?\d{1,3}\.\d{3,}),\s*(-?\d{1,3}\.\d{3,})")
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:2|²)\b", re.IGNORECASE)
_DISPOSITION_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
# bazos serves /img/N/ (full ~1200px) and /img/Nt/ (thumbnail ~340px) for the same
# photo; a detail page shows the full cover plus the whole thumbnail strip.
_BAZOS_THUMB_RE = re.compile(r"/img/(\d+)t/")


def _full_size_image_url(src: str) -> str:
    return _BAZOS_THUMB_RE.sub(r"/img/\1/", src, count=1)

# Czech-bbox guard applied to every coordinate candidate (link OR geocode) so a
# stray decimal pair, a swapped lat/lon, or a geocode that landed abroad can
# never become a bogus geom.
_CZ_LAT_MIN, _CZ_LAT_MAX = 48.0, 51.5
_CZ_LON_MIN, _CZ_LON_MAX = 12.0, 19.0

# Cross-check thresholds (km), tunable. A maps-link pin within TRUST of the text
# reference is a precise pin at the right place — preferred over a coarse
# locality-only geocode. Beyond DISTRUST it contradicts the stated location and
# is dropped in favour of the text geocode.
LINK_TRUST_RADIUS_KM = 2.0
LINK_DISTRUST_RADIUS_KM = 5.0

# Street extraction over title + description. Keyword-anchored forms are the
# reliable signal; the bare "<Name> <house-no>" form is gated by a street-like
# suffix + a stopword list because a wrong street is worse than none.
_CZ_UPPER = "A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"
# Inter-word whitespace is horizontal only ([^\S\r\n]): the street name must not
# span the title->description line break, or a description's opening word leaks in
# ("ul. Koterovská" + "\nNabízíme k pronájmu…" -> "ul. Koterovská Nabízíme").
_STREET_NAME = rf"[{_CZ_UPPER}]\w+(?:[^\S\r\n]+[{_CZ_UPPER}]\w+){{0,2}}"
# Optional trailing house number — a street + number geocodes to a precise
# address (high confidence); the lookahead rejects a PSČ ("679 61").
_HOUSE_NO = r"(?:\s+\d{1,4}(?:/\d{1,4})?(?!\s*\d))?"
# Dotted abbreviations (ul./tř./nám./nábř.) may be glued to the name with no
# space ("ul.Výstavní"); the spelled-out keywords still require a space.
_STREET_PREFIX_RE = re.compile(
    rf"(?:(?i:\bul\.|\btř\.|\bnám\.|\bnábř\.)\s*"
    rf"|(?i:\bulic[ei]|\btříd[aěu]|\bnáměstí|\bnábřeží|\bsídlišt[ěi])\s+)"
    rf"{_STREET_NAME}{_HOUSE_NO}"
)
_STREET_SUFFIX_RE = re.compile(
    rf"\b{_STREET_NAME}\s+(?i:ulic[ei]|tříd[aěy]|náměstí|nábřeží){_HOUSE_NO}"
)
_STREET_HOUSENO_RE = re.compile(
    rf"\b([{_CZ_UPPER}]\w+)\s+\d{{1,4}}(?:/\d{{1,4}})?(?!\s*\d)"
)
_STREET_WORD_ENDINGS: tuple[str, ...] = (
    "ova", "ová", "ská", "cká", "ená", "ní", "ého", "ích", "á", "é", "í", "ý",
)
_HOUSENO_STOPWORDS: frozenset[str] = frozenset({
    "byt", "dům", "dum", "garáž", "garaz", "pozemek", "chata", "chalupa",
    "prodej", "pronájem", "pronajem", "patro", "cena", "sleva", "novostavba",
    "vila", "zahrada", "balkon", "balkón", "sklep", "parkování", "podlaží",
})


def _in_cz_bbox(lat: float, lon: float) -> bool:
    return _CZ_LAT_MIN <= lat <= _CZ_LAT_MAX and _CZ_LON_MIN <= lon <= _CZ_LON_MAX
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
    txt = re.sub(r"\s+", " ", node.text(separator=" ", strip=False)).strip()
    return txt or None


def _page_text(tree: HTMLParser) -> str:
    body = tree.body
    if body is not None:
        return body.text(separator=" ", strip=False)
    root = tree.root
    return root.text(separator=" ", strip=False) if root is not None else ""


def _parse_total(text: str) -> int | None:
    # "Zobrazeno 1-20 inzerátů z 6 990"
    m = re.search(r"inzer\w+\s+z\s+(\d[\d\s]*\d|\d)", text)
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
    lat, lon = float(m.group(1)), float(m.group(2))
    if not _in_cz_bbox(lat, lon):
        return None, None
    return lat, lon


def _clean_street(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" ,.;:")


def _looks_like_street_word(word: str) -> bool:
    w = word.lower()
    if w in _HOUSENO_STOPWORDS:
        return False
    return w.endswith(_STREET_WORD_ENDINGS)


def extract_street(haystack: str | None) -> str | None:
    """Best-effort Czech street name from the title + description, or None.

    Conservative on purpose: the keyword-anchored forms ("ulice Dlouhá",
    "náměstí Míru", "Vinohradská třída") are reliable; the bare "<Name>
    <house-no>" form only fires for a street-like word so a listing noun
    ("Byt 3", "Garáž 20", "Letovice 679 61") never masquerades as a street.
    """
    if not haystack:
        return None
    for rx in (_STREET_PREFIX_RE, _STREET_SUFFIX_RE):
        m = rx.search(haystack)
        if m:
            return _clean_street(m.group(0))
    m = _STREET_HOUSENO_RE.search(haystack)
    if m and _looks_like_street_word(m.group(1)):
        return _clean_street(m.group(0))
    return None


def _street_query(street: str, locality: str, psc: str | None) -> str:
    q = f"{street}, {locality}"
    return f"{q} {psc}" if psc else q


def _locality_query(locality: str, psc: str | None) -> str:
    return f"{locality} {psc}" if psc else locality


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    radius_km = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(h))


def _safe_geocode(geocoder: Geocoder | None, query: str) -> GeocodeResult | None:
    if geocoder is None or not query.strip():
        return None
    try:
        return geocoder(query)
    except GeocodingError:
        return None


def _resolve_coords(
    *,
    link_lat: float | None,
    link_lon: float | None,
    street: str | None,
    locality: str | None,
    psc: str | None,
    geocoder: Geocoder | None,
) -> tuple[float | None, float | None, dict[str, Any]]:
    """Single text-first, cross-checked coordinate-resolution step.

    Priority: (1) a high/medium street geocode; (2) the maps-link pin when it
    is consistent with the text reference; (3) a coarse locality geocode.
    Every candidate passes the CZ-bbox guard. The returned provenance dict is
    stored in raw_json so accuracy is auditable.
    """
    link = (
        (link_lat, link_lon)
        if link_lat is not None and link_lon is not None
        else None
    )
    prov: dict[str, Any] = {
        "source": None,
        "street": street,
        "link_present": link is not None,
        "street_confidence": None,
        "locality_confidence": None,
        "text_reference": None,
        "link_text_distance_km": None,
        "notes": [],
    }

    # No geocoder (MAPY_CZ_API_KEY unset): trust only the CZ-guarded link.
    if geocoder is None:
        if link is not None:
            prov["source"] = "link"
            prov["notes"].append("no geocoder; used CZ-guarded maps link")
        return link_lat, link_lon, prov

    # Street geocode (primary candidate) — needs a street AND a locality to
    # disambiguate a bare street name nationwide.
    street_geo = None
    if street and locality:
        street_geo = _safe_geocode(geocoder, _street_query(street, locality, psc))
        if street_geo is not None:
            prov["street_confidence"] = street_geo.confidence

    # Text reference for cross-checking the link: the street geocode if usable,
    # otherwise the locality geocode (the most-specific text available).
    text_ref: tuple[float, float] | None = None
    locality_geo = None
    if street_geo is not None and _in_cz_bbox(street_geo.lat, street_geo.lng):
        text_ref = (street_geo.lat, street_geo.lng)
        prov["text_reference"] = "street"
    elif locality:
        locality_geo = _safe_geocode(geocoder, _locality_query(locality, psc))
        if locality_geo is not None:
            prov["locality_confidence"] = locality_geo.confidence
            if _in_cz_bbox(locality_geo.lat, locality_geo.lng):
                text_ref = (locality_geo.lat, locality_geo.lng)
                prov["text_reference"] = "locality"

    if link is not None and text_ref is not None:
        prov["link_text_distance_km"] = round(_haversine_km(link, text_ref), 3)

    # Priority 1 — street geocode is precise and inherently matches the text.
    if (
        street_geo is not None
        and street_geo.confidence in ("high", "medium")
        and _in_cz_bbox(street_geo.lat, street_geo.lng)
    ):
        prov["source"] = "street"
        return street_geo.lat, street_geo.lng, prov

    # Priority 2 — the maps link, cross-checked against the text reference.
    if link is not None:
        dist = prov["link_text_distance_km"]
        if text_ref is None:
            prov["source"] = "link"
            prov["notes"].append("no text reference; used CZ-guarded maps link")
            return link_lat, link_lon, prov
        if dist is not None and dist > LINK_DISTRUST_RADIUS_KM:
            prov["notes"].append(
                f"maps link {dist:.1f} km from text "
                f"({prov['text_reference']}); distrusted"
            )
        else:
            prov["source"] = "link"
            if dist is not None and dist > LINK_TRUST_RADIUS_KM:
                prov["notes"].append(
                    f"maps link {dist:.1f} km from text; accepted (loose)"
                )
            return link_lat, link_lon, prov

    # Priority 3 — coarse text fallback (locality geocode, or a low-confidence
    # street geocode that in practice resolved to the municipality).
    fallback = locality_geo if locality_geo is not None else street_geo
    if fallback is not None and _in_cz_bbox(fallback.lat, fallback.lng):
        prov["source"] = "locality"
        return fallback.lat, fallback.lng, prov

    if link is not None:
        prov["notes"].append("no trustworthy coordinate; dropped contradicting link")
    return None, None, prov


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
            # The offset is a path segment; a locality-filtered URL appends a
            # query (?hlokalita=…&humkreis=…), so allow a trailing ?/# after it
            # — anchoring on $ alone misses every page past the first when a
            # filter is active.
            m = re.search(r"/(\d+)/?(?:[?#]|$)", link.attributes.get("href") or "")
            if m:
                return int(m.group(1))
    return None


def _locality(cell_text: str | None) -> tuple[str | None, str | None]:
    """Return (locality, psc) from the Lokalita cell text.

    bazos renders the cell two ways: "Town PSČ" (older fixtures) and "PSČ Town"
    (live — the PSČ is the maps-link anchor, the town a separate town-listings
    anchor). Prefer the text before the PSČ; fall back to the text after it when
    nothing precedes it. A trailing "Zobrazit na mapě" map label is stripped first
    so it never leaks into the town name.
    """
    if not cell_text:
        return None, None
    text = _MAP_LABEL_RE.sub(" ", cell_text)
    psc_match = _PSC_RE.search(text)
    if not psc_match:
        return text.strip(" ,\n\t") or None, None
    psc = f"{psc_match.group(1)} {psc_match.group(2)}"
    before = text[: psc_match.start()].strip(" ,\n\t")
    after = text[psc_match.end():].strip(" ,\n\t")
    return (before or after or None), psc


def _detail_table(tree: HTMLParser) -> dict[str, Node]:
    """Map the left details-table row labels to their value cells.

    The value is the LAST cell, not cells[1]: live bazos renders the Lokalita row
    as three cells — label, a map-icon cell, then the cell holding the PSČ/town
    (and the maps link). Reading cells[1] there grabbed the icon cell (no text),
    which left `locality` NULL for every listing and disabled street geocoding.
    For the ordinary two-cell rows (Cena, Vidělo) cells[-1] is still the value.
    """
    rows: dict[str, Node] = {}
    for tr in tree.css("table tr"):
        cells = tr.css("td")
        if len(cells) < 2:
            continue
        label = (cells[0].text(strip=True) or "").rstrip(":").lower()
        if label:
            rows[label] = cells[-1]
    return rows


def _category_from_breadcrumb(tree: HTMLParser) -> tuple[str | None, str | None]:
    """(category_main, category_type) from the detail breadcrumb, or (None, None).

    Reads the breadcrumb LINKS' canonical URL segments (".../pronajmu/byt/" ->
    ('byt', 'pronajem')), NOT the display text: bazos separates the trail with a
    plain ">" and localised labels, so the href segments are the stable signal.
    Lets the drain tell a sale ad from a rental without the queue carrying the
    category."""
    node = tree.css_first("div.drobky")
    if node is None:
        return (None, None)
    cmain: str | None = None
    ctype: str | None = None
    for a in node.css("a"):
        for seg in (a.attributes.get("href") or "").split("/"):
            if ctype is None and seg in SALE_TYPE:
                ctype = SALE_TYPE[seg]
            if cmain is None and seg in CATEGORY_MAIN:
                cmain = CATEGORY_MAIN[seg]
    return (cmain, ctype)


def _subtype_from_breadcrumb(tree: HTMLParser) -> str | None:
    """Portal-agnostic subtype from the detail breadcrumb's section segment, or
    None. Same div.drobky href-segment signal as `_category_from_breadcrumb` —
    e.g. ".../prodam/kancelar/" -> 'kancelar'."""
    node = tree.css_first("div.drobky")
    if node is None:
        return None
    for a in node.css("a"):
        for seg in (a.attributes.get("href") or "").split("/"):
            if seg in SUBTYPE:
                return SUBTYPE[seg]
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

    # The page's own breadcrumb is authoritative for the category (sale vs rent);
    # the passed-in values are only a fallback when it's missing/unrecognised.
    bc_main, bc_type = _category_from_breadcrumb(tree)
    category_main = bc_main or category_main
    category_type = bc_type or category_type
    subtype = _subtype_from_breadcrumb(tree)

    title = _text(tree.css_first("h1.nadpisdetail")) or ""
    description = _text(tree.css_first("div.popisdetail"))
    haystack = f"{title}\n{description or ''}"
    # Deterministic floor (ground=0, like idnes + the LLM rubric) from the same
    # haystack area/disposition come from; the LLM enrichment fills only what this
    # high-precision pass leaves NULL (ambiguous/word-ordinal/mezonet cases).
    floor, total_floors = floor_from_text(haystack)

    table = _detail_table(tree)
    price_cell = table.get("cena")
    price_czk, price_unit = _parse_price(_text(price_cell), category_type)

    lok_cell = table.get("lokalita")
    # The embedded maps link. Prefer the lokalita cell, but live bazos renders
    # the "show on map" link elsewhere on the detail page, so fall back to any
    # Google-Maps / Mapy.cz link anywhere on the page.
    maps_link = (lok_cell.css_first('a[href*="map"]') if lok_cell else None) or \
        tree.css_first('a[href*="google.com/maps"], a[href*="mapy.cz"]')
    link_lat, link_lon = _parse_coords(
        maps_link.attributes.get("href") if maps_link else None
    )
    locality, psc = _locality(_text(lok_cell))

    # Text-first, cross-checked: street geocode wins, the link only corroborates.
    street = extract_street(haystack)
    lat, lon, coord_provenance = _resolve_coords(
        link_lat=link_lat, link_lon=link_lon,
        street=street, locality=locality, psc=psc, geocoder=geocoder,
    )

    # The listing's own photos carry its ad id in the URL (/img/N/sub/<id>.jpg); the
    # "podobné inzeráty" footer shows OTHER ads' cover thumbnails — scope to this id.
    image_urls: list[str] = []
    seen: set[str] = set()
    for img in tree.css("img"):
        src = img.attributes.get("src")
        if not src or "bazos.cz/img/" not in src:
            continue
        if source_id and source_id not in src:
            continue
        full = _full_size_image_url(src)
        if full not in seen:
            seen.add(full)
            image_urls.append(full)

    posted_text = _text(tree.css_first("span.velikost10"))

    raw = {
        "id": source_id,
        "title": title,
        "price_text": _text(price_cell),
        "locality_text": _text(lok_cell),
        "psc": psc,
        "views": _text(table.get("vidělo")) or _text(table.get("videlo")),
        "posted_date": posted_text,
        "image_urls": image_urls,
        "coords": coord_provenance,
    }

    return ScrapedListing(
        source="bazos",
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        subtype=subtype,
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=_parse_area(haystack),
        disposition=_parse_disposition(haystack),
        floor=floor,
        total_floors=total_floors,
        locality=locality,
        district=None,
        # The raw extract (with its "ul." prefix) is the better geocoder query;
        # the STORED street is cleaned to a bare, uniform name (prefix stripped,
        # trailing description bleed like "ul. Teplého Nabízíme" trimmed).
        street=clean_street(street),
        lat=lat,
        lon=lon,
        description=description,
        # Bazos re-stamps this date on every bump / TOP renewal — a LAST-BUMP
        # date, not first publication — but it is the only publish signal the
        # portal exposes (and it is out of the content hash, so a bump never
        # churns a snapshot).
        published_at=bazos_posted_date(posted_text),
        raw=raw,
    )
