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

from scraper.broker_idnes import parse_idnes_broker
from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.scraped_listing import ScrapedListing
from scraper.street import street_from_locality

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
    "komercni-nemovitosti": "komercni",
    "male-objekty-garaze": "ostatni",
}

# Detail URLs use the SINGULAR category segment (/detail/prodej/byt/...), unlike
# the plural search segment (/s/prodej/byty/). The drain derives each listing's
# category from its own detail URL (the queue is category-agnostic), so one
# config can walk many categories.
DETAIL_CATEGORY: dict[str, str] = {
    "byt": "byt",
    "dum": "dum",
    "pozemek": "pozemek",
    "komercni-nemovitost": "komercni",
    "maly-objekt-nebo-garaz": "ostatni",
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

# Portal-agnostic subtype (migration 152). idnes exposes NO structured subtype
# field and its detail URL is only category-main level; the type appears solely
# in the SEO title ("...rodinný dům 6+kk..."). So this is a BEST-EFFORT keyword
# match over the og:title — diacritics-free substrings, gated by category_main so
# a commercial needle can never fire on a house. A title that doesn't name the
# type yields None (the listing still shows at the category level). Order: more
# specific needles first.
SUBTYPE_BY_TITLE: dict[str, tuple[tuple[str, str], ...]] = {
    "dum": (
        ("vicegenera", "vicegeneracni_dum"),
        ("rodinn", "rodinny_dum"),
        ("chalup", "chalupa"),
        ("chata", "chata"),
        ("chaty", "chata"),
        ("usedlost", "zemedelska_usedlost"),
        ("pamatk", "pamatka_jine"),
        ("na klic", "na_klic"),
        ("vila", "vila"),
        ("vily", "vila"),
    ),
    "komercni": (
        ("najemni dum", "cinzovni_dum"),
        ("cinzov", "cinzovni_dum"),
        ("kancelar", "kancelar"),
        ("skladov", "sklad"),
        ("sklad", "sklad"),
        ("obchod", "obchodni_prostor"),
        ("vyrob", "vyroba"),
        ("restaurac", "restaurace"),
        ("ubytov", "ubytovani"),
        ("ordinac", "ordinace"),
        ("zemedelsk", "zemedelsky"),
    ),
}


def subtype_from_title(text: str | None, category_main: str | None) -> str | None:
    """Best-effort subtype from the listing's SEO title (idnes has no structured
    field). Only houses / commercial; the category gates which needle set runs."""
    needles = SUBTYPE_BY_TITLE.get(category_main or "")
    if not needles or not text:
        return None
    low = _strip_diacritics(text).lower()
    for needle, slug in needles:
        if needle in low:
            return slug
    return None

# Czech-bbox guard: a coordinate (embedded pin OR geocode) outside it — a swapped
# lat/lon or a geocode that landed abroad — is dropped rather than stored as geom.
_CZ_LAT_MIN, _CZ_LAT_MAX = 48.0, 51.5
_CZ_LON_MIN, _CZ_LON_MAX = 12.0, 19.0

# The detail-URL hash is the source_id_native (24 hex chars today; >=16 to be safe).
_ID_RE = re.compile(r"/detail/[^?#]*?/([0-9a-f]{16,})/?(?:[?#]|$)")
# An area token before "m²". The first alternative accepts the Czech spaced
# thousands format idnes titles render ("Prodej pole 2 403 m²") — without it
# the match started INSIDE the number and truncated 2403 -> 403 (8k+ corrupted
# area_m2 rows in production, every Kč/m² figure computed from them wrong).
# The lookbehind keeps the grouped form from swallowing a preceding digit
# ("3+1 174 m²" stays 174, never 1174).
_AREA_SEPS = "\u0020\u00a0\u200b\u200c\u200d\u2060"
_AREA_RE = re.compile(
    rf"(?<![\d+.,])(\d{{1,3}}(?:[{_AREA_SEPS}]\d{{3}})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*m(?:2|²|\s*2)\b",
    re.IGNORECASE,
)
_DISPOSITION_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
_INT_RE = re.compile(r"(\d+)")
_ENERGY_RE = re.compile(r"\b([A-G])\b")
_PAGE_RE = re.compile(r"[?&]page=(\d+)")
_DETAIL_PATH_RE = re.compile(r"/detail/([^/?#]+)/([^/?#]+)/")
# A price token: a leading digit then more digits split by ordinary / no-break /
# zero-width spaces (the Czech "9 790 000" thousands format idnes renders with
# &nbsp;/&zwj; between groups). Stops at the first non-space, non-digit char.
_PRICE_RUN_RE = re.compile(r"\d[\d\s\u00a0\u200b\u200c\u200d\u2060]*")
# A unit-price marker directly after the amount ("18 500 Kč/m²", "Kč za m²",
# "Kč/m²/rok"). A per-m² figure must NEVER be stored as the absolute price —
# today idnes shows per-m² only in a grey note span the parser doesn't read
# (verified on 5,773 staged pozemek pages), so this is a drift rail for the day
# a page's main price element becomes unit-priced. Deliberately does NOT match
# "Kč/měsíc" (the m needs ²/2 right after).
_PRICE_PER_M2_RE = re.compile(r"^\s*Kč\s*(?:/\s*|za\s+)m(?:²|2)(?!\w)", re.IGNORECASE)
_PRICE_MAX = 2_147_483_647  # listings.price_czk is a Postgres integer
# Column maxes for the numeric area fields. A parsed area larger than its column
# can hold (a million-m\u00b2 title-number garble, a developer-project "1234567 m\u00b2"
# rendered without thousand separators) gets dropped to NULL rather than
# crashing the drain. Matches the schema in `listings`.
_AREA_M2_MAX = 999_999.9            # listings.area_m2 is numeric(7,1)
_AREA_LARGE_MAX = 99_999_999.9      # usable_area / estate_area / garden_area are numeric(9,1)
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
    # Take only the FIRST price run (digits split by thin/no-break/zero-width
    # spaces, the Czech thousands format). Stripping the whole string would
    # CONCATENATE a struck original price + the current one (or a price note)
    # into a giant number that overflows the price_czk integer column.
    m = _PRICE_RUN_RE.search(text)
    if not m:
        return None, unit
    if _PRICE_PER_M2_RE.match(text[m.end():]):
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
    token = m.group(1)
    for sep in _AREA_SEPS:
        token = token.replace(sep, "")
    return float(token.replace(",", "."))


def _clamp(value: float | None, ceiling: float) -> float | None:
    """Drop an area that would overflow its numeric column rather than crash the
    drain. A real apartment/house area can never exceed millions of m²; a value
    that does is either a parse artifact or genuinely unstorable in the schema."""
    return None if value is None or value > ceiling else value


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
    # Only the canonical set ({osobni, druzstevni, statni}) the ownership filter
    # offers; idnes free-text like "jiné" / "s.r.o." / "podílové" → None rather
    # than polluting the column with values no filter option can match.
    return OWNERSHIP.get(key)


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
    """For a row whose presence is signalled by a check icon OR by free text.
    idnes renders the SAME amenity row both ways depending on what the lister
    filled in: "Balkon" is a bare check icon on some pages and a size /
    orientation text ("4 m 2", "jih , 4 m 2") on others — both mean the
    amenity exists. Verified against live pages + the stored raw params."""
    if dd is None:
        return None
    checked = _has_check(dd)
    if checked is not None:
        return checked
    return True if _text(dd) else None


def _any_true(*vals: bool | None) -> bool | None:
    """Combine related amenity signals the way parser._has_balcony does for
    sreality: None only when every signal is unknown, else any-True."""
    if all(v is None for v in vals):
        return None
    return any(v is True for v in vals)


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
    # The H1 is generic ("Prodej domu …"); the property type lives in the SEO
    # og:title ("…rodinný dům 6+kk…"), so subtype keys off that.
    og = tree.css_first('meta[property="og:title"]')
    og_title = (og.attributes.get("content") if og else None) or ""
    description = _text(tree.css_first("div.b-desc")) or _text(tree.css_first(".b-detail__text"))
    params = _detail_params(tree)

    # The <strong> holds just the amount; the surrounding .b-detail__price also
    # carries the "Chci spočítat hypotéku" CTA / price note (extra digits).
    price_node = tree.css_first(".b-detail__price strong")
    if price_node is None:
        price_node = tree.css_first(".b-detail__price")
        if price_node is not None:
            # A discounted page leads with the struck original in <del> — the
            # first price run of the flattened element would be the OLD price.
            for struck in price_node.css("del"):
                struck.decompose()
    price_text = _text(price_node) or _text(params.get("cena"))
    price_czk, price_unit = _parse_price(price_text, category_type)

    locality = _text(tree.css_first(".b-detail__info"))
    lat, lon, coord_provenance = _resolve_coords(html, locality, geocoder)

    area_text = (
        _text(params.get("užitná plocha"))
        or _text(params.get("podlahová plocha"))
        or _text(params.get("plocha"))
    )
    area_m2 = _clamp(_parse_area(area_text) or _parse_area(title), _AREA_M2_MAX)

    # Amenities: each row is a check icon OR free text (size / orientation /
    # parking kind), so everything goes through _truthy_field. idnes has no
    # standalone "Garáž" row — the garage signal lives in the "Parkování"
    # value ("garáž , parkování na ulici") and the icon-only "Dvojgaráž" row.
    balcony = _truthy_field(params.get("balkon"))
    loggia = _truthy_field(params.get("lodžie"))
    terrace = _truthy_field(params.get("terasa"))
    parking_field = _truthy_field(params.get("parkování"))
    parking_text = _text(params.get("parkování"))
    parking_lots = _parse_int(_text(params.get("počet parkovacích míst")))
    garage = _any_true(
        _truthy_field(params.get("garáž")),
        _truthy_field(params.get("dvojgaráž")),
        ("garaz" in _strip_diacritics(parking_text).lower()) if parking_text else None,
    )

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
        # Broker/agency block for broker intelligence (resolver reads raw_json.broker).
        # Out of the content hash (_HASH_FIELDS is typed columns only), so it never
        # churns snapshots.
        "broker": parse_idnes_broker(html),
    }

    return ScrapedListing(
        source="idnes",
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        subtype=subtype_from_title(f"{og_title} {title}", category_main),
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=area_m2,
        usable_area=_clamp(_parse_area(area_text), _AREA_LARGE_MAX),
        disposition=_parse_disposition(title) or _parse_disposition(_text(params.get("dispozice"))),
        locality=locality,
        district=None,
        # Street is the FIRST comma-segment of locality ("Bělehradská, Pardubice
        # - Polabiny"); the shared guard rejects foreign localities (idnes carries
        # ~37%), "Town - Quarter" tails, and "Town, okres X" forms.
        street=street_from_locality(locality, position="first", lat=lat, lon=lon),
        lat=lat,
        lon=lon,
        floor=_parse_floor(_text(params.get("podlaží"))),
        total_floors=_parse_int(_text(params.get("počet podlaží budovy"))),
        building_type=_norm_building_type(_text(params.get("konstrukce budovy"))),
        condition=_norm_condition(
            _text(params.get("stav bytu"))
            or _text(params.get("stav domu"))
            or _text(params.get("stav objektu"))
            # houses / commercial label their condition row "Stav budovy"
            or _text(params.get("stav budovy"))
        ),
        ownership=_norm_ownership(_text(params.get("vlastnictví"))),
        furnished=_norm_furnished(
            _text(params.get("vybavení")) or _text(params.get("vybavení domu"))
        ),
        energy_rating=_energy_rating(_text(params.get("penb")) or _text(params.get("energetická náročnost"))),
        # Legacy combined boolean — balcony|terrace|loggia, mirroring
        # parser._has_balcony so the cross-portal filter agrees with sreality.
        has_balcony=_any_true(balcony, terrace, loggia),
        has_lift=_truthy_field(params.get("výtah")),
        cellar=_truthy_field(params.get("sklep")),
        terrace=terrace,
        garage=garage,
        # Legacy combined boolean — parking|garage|lots, mirroring parser._has_parking.
        has_parking=_any_true(
            parking_field,
            garage,
            (parking_lots > 0) if parking_lots is not None else None,
        ),
        parking_lots=parking_lots,
        estate_area=_clamp(_parse_area(_text(params.get("plocha pozemku"))), _AREA_LARGE_MAX),
        garden_area=_clamp(_parse_area(_text(params.get("plocha zahrady"))), _AREA_LARGE_MAX),
        description=description,
        raw=raw,
    )
