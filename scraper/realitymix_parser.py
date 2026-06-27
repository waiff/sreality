"""Deterministic HTML parsing for realitymix.cz (portal framework).

Pure functions, no I/O: `parse_index` turns one search-results page into the
listing ids (+ the page total, which drives the total-paced walk), and
`parse_detail` turns one listing page into a `ScrapedListing` (the shared
multi-portal contract in `scraper.scraped_listing`).

realitymix.cz is a Centrum.cz agency-feed AGGREGATOR served as STRUCTURED
server-rendered HTML, like idnes/ceskereality: each detail page carries a
schema.org `BreadcrumbList` JSON-LD (the clean category path Byty → Prodej →
2+1 → kraj → okres → obec — the drain's category source, since the detail URL
does NOT encode it), a `<li class="detail-information__data-item">` spec list of
`<span>Label:</span><span>Value</span>` rows, precise per-listing coordinates +
a structured street in `<div id="print-map" data-gps-lat/-lon
data-address="Street, Obec, okres Okres">` (so there is no geocode step), an
`st.realitymix.cz/i/{agency}/{id}/nab_*.jpg` gallery, and a stable broker/agency
identity (`/profil-realitniho-maklere/…-{id}`, `data-fk_rk`). Typed fields are
normalised to the SAME canonical labels the sreality parser emits (cihlová ->
cihla, velmi dobrý -> velmi_dobry, osobní -> osobni) so cross-portal
filters/dedup agree.

Many listings hide the price ("Cena na vyžádání" / "Rezervováno" / "info v RK")
-> price_czk is left None (never fabricated), which keeps the MF-yield price>=100k
floor honest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any
from unicodedata import combining, normalize

from selectolax.parser import HTMLParser, Node

from scraper import street
from scraper.scraped_listing import ScrapedListing

# realitymix family/offer URL segments -> our canonical labels (mirrors
# parser.CATEGORY_*). chaty -> dum (sreality lumps chata/chalupa under "dům",
# same as ceskereality's chaty-chalupy).
SALE_TYPE: dict[str, str] = {
    "prodej": "prodej",
    "pronajem": "pronajem",
}
CATEGORY_MAIN: dict[str, str] = {
    "byty": "byt",
    "domy": "dum",
    "chaty": "dum",
    "pozemky": "pozemek",
    "komerce": "komercni",
    "ostatni": "ostatni",
}

# realitymix "Druh objektu" labels (diacritics-stripped) -> the SAME canonical
# construction codes the sreality parser emits. "zděná" -> sreality's dominant
# solid-masonry "cihla"; unknowns pass through rather than mis-map.
BUILDING_TYPE: dict[str, str] = {
    "panelova": "panel",
    "cihlova": "cihla",
    "zdena": "cihla",
    "smisena": "smisena",
    "skeletova": "skelet",
    "drevena": "drevo",
    "kamenna": "kamen",
    "montovana": "montovana",
    "nizkoenergeticka": "nizkoenergeticka",
}
# realitymix "Stav objektu" labels (diacritics-stripped) -> sreality's canonical
# condition vocabulary. The already-matching values pass through; only the
# divergent ones are mapped.
CONDITION: dict[str, str] = {
    "bezvadny": "velmi_dobry",
    "k_rekonstrukci": "pred_rekonstrukci",
    "rozestaveny": "ve_vystavbe",
    "ve_vystavbe": "ve_vystavbe",
    "v_puvodnim_stavu": "dobry",
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
_PRICE_RUN_RE = re.compile(r"\d[\d\s  ​]*")
_PRICE_MAX = 2_147_483_647  # listings.price_czk is a Postgres integer
# Result total on the search page ("výsledky 1-20 z celkem 8928 nalezených").
_TOTAL_RE = re.compile(r"z\s*celkem\s*([\d\s  ]+?)\s*nalezených", re.IGNORECASE)
# Per-listing coordinates + structured address: the #print-map data attributes.
_GPS_LAT_RE = re.compile(r'data-gps-lat="(-?\d+\.\d+)"')
_GPS_LON_RE = re.compile(r'data-gps-lon="(-?\d+\.\d+)"')
_ADDRESS_RE = re.compile(r'id="print-map"[^>]*\bdata-address="([^"]*)"')
# Stable broker identity: the makléř profile id (/profil-realitniho-maklere/{slug}-{id})
# is the per-broker key; data-fk_rk is the agency (realitní kancelář) id.
_BROKER_ID_RE = re.compile(r"/profil-realitniho-maklere/[^\"'/]*?-(\d{3,})\b")
_MAKLER_IMG_RE = re.compile(r"/makleri/makler_(\d+)\.")
_AGENCY_ID_RE = re.compile(r'data-fk_rk="(\d+)"')
# Full-size listing photos: st.realitymix.cz/i/{agency}/{id}/nab_*.jpg. The
# `_nahled` variant is the thumbnail — excluded (data-src is already full size).
_IMG_RE = re.compile(r'https://st\.realitymix\.cz/i/\d+/\d+/nab_\d+\.(?:jpe?g|png|webp)', re.IGNORECASE)
# A trailing house number on the data-address street segment ("Luční 1793/3").
_HOUSE_NO_RE = re.compile(r"\s(\d{1,4}(?:/\d{1,4})?[a-z]?)$", re.IGNORECASE)


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


def index_price(text: str | None) -> int | None:
    """The Kč amount from an index card's price text, or None ("Cena na
    vyžádání" / "Rezervováno" / "info v RK"). Drives price-change detection."""
    return _parse_price(text, None)[0]


def _parse_total(html: str) -> int | None:
    m = _TOTAL_RE.search(html)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _parse_price(text: str | None, category_type: str | None) -> tuple[int | None, str | None]:
    unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    if not text:
        return None, unit
    low = _strip_diacritics(text).lower()
    if any(k in low for k in ("vyzadani", "dohodou", "rezerv", "info v rk", "v rk", "poptavce", "neuvedena")):
        return None, unit
    m = _PRICE_RUN_RE.search(text)
    if not m:
        return None, unit
    digits = re.sub(r"\D", "", m.group(0))
    if not digits:
        return None, unit
    value = int(digits)
    return (value if 0 < value <= _PRICE_MAX else None), unit


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
    key = re.sub(r"\s+stav$", "", key)
    key = re.sub(r"[\s,/]+", "_", key).strip("_")
    return CONDITION.get(key, key or None)


def _norm_ownership(text: str | None) -> str | None:
    if not text:
        return None
    key = _strip_diacritics(text).lower().strip()
    if "druzstev" in key:
        return "druzstevni"
    if "osob" in key or "soukrom" in key:
        return "osobni"
    if "statni" in key or "obecni" in key:
        return "statni"
    return key or None


def _norm_furnished(text: str | None) -> str | None:
    if not text:
        return None
    low = _strip_diacritics(text).lower()
    if "castec" in low:
        return "castecne"
    if low.startswith("ne") or "neza" in low or "nevyba" in low:
        return "ne"
    if low.startswith("ano") or "zariz" in low or "vybav" in low:
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


def _field_present(params: dict[str, str], *keys: str) -> bool | None:
    """An amenity realitymix renders as its own labelled spec row ("Balkon: 4 m²")
    -> True on presence (the label IS the amenity, the value is its size); an
    explicit "Ne"/"Bez"/"0" value -> False; the row absent -> None (unknown)."""
    for key in keys:
        value = params.get(key)
        if value is None:
            continue
        low = _strip_diacritics(value).lower().strip()
        if low.startswith("ne") or low.startswith("bez") or low in ("0", "0 m2", "0 m²"):
            return False
        return True
    return None


def category_from_breadcrumb(html: str) -> tuple[str | None, str | None]:
    """(category_main, category_type) from the BreadcrumbList JSON-LD: position 2
    is the family (`…/reality/{family}`), position 3 the offer (`…/{family}/{sale}`).
    The detail URL doesn't encode the category, so the drain reads it from here."""
    family: str | None = None
    sale: str | None = None
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1).strip())
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("@type") != "BreadcrumbList":
            continue
        for el in data.get("itemListElement") or []:
            if not isinstance(el, dict):
                continue
            pos = el.get("position")
            ref = ((el.get("item") or {}).get("@id") or "").rstrip("/")
            seg = ref.rsplit("/", 1)[-1] if ref else ""
            if pos == 2:
                family = seg
            elif pos == 3:
                sale = seg
        break
    return CATEGORY_MAIN.get(family or ""), SALE_TYPE.get(sale or "")


def _detail_params(tree: HTMLParser) -> dict[str, str]:
    """Map the spec list's labels (lowercased, colon-stripped) to their values.
    Each row is `<li class="detail-information__data-item"><span>Label:</span>
    <span>Value</span></li>`."""
    rows: dict[str, str] = {}
    for li in tree.css("li.detail-information__data-item"):
        spans = li.css("span")
        if len(spans) < 2:
            continue
        label = _text(spans[0])
        value = _text(spans[1])
        if not label or value is None:
            continue
        key = re.sub(r"\s+", " ", label.rstrip(":").strip().lower())
        if key and key not in rows:
            rows[key] = value
    return rows


def _resolve_coords(html: str) -> tuple[float | None, float | None, dict[str, Any]]:
    lat_m, lon_m = _GPS_LAT_RE.search(html), _GPS_LON_RE.search(html)
    if lat_m and lon_m:
        lat, lon = float(lat_m.group(1)), float(lon_m.group(1))
        if _in_cz_bbox(lat, lon):
            return lat, lon, {"source": "page"}
    return None, None, {"source": None}


def _address_parts(html: str) -> tuple[str | None, str | None, str | None, str | None]:
    """(street_raw, obec, okres, full) from #print-map data-address, formatted
    "Street, Obec, okres Okres" (the street segment is absent for many houses/plots)."""
    m = _ADDRESS_RE.search(html)
    if not m:
        return None, None, None, None
    full = unescape(m.group(1)).strip()
    parts = [p.strip() for p in full.split(",") if p.strip()]
    okres = next((re.sub(r"^okres\s+", "", p, flags=re.IGNORECASE) for p in parts
                  if p.lower().startswith("okres")), None)
    body = [p for p in parts if not p.lower().startswith("okres")]
    if len(body) >= 2:
        return body[0], body[-1], okres, full
    if len(body) == 1:
        return None, body[0], okres, full
    return None, None, okres, full


def _street_fields(
    street_raw: str | None, geo_names: list[str | None],
    lat: float | None, lon: float | None,
) -> tuple[str | None, str | None]:
    """(street, house_number) from the data-address first segment, through the
    shared don't-fabricate guard. realitymix's first segment is "{street OR místní
    část}, obec, okres" — for rural houses it is a settlement part (e.g.
    "Jindřichov"), not a street, which reject_as_town can't catch (it isn't the
    obec). So a Czech-street MORPHOLOGY gate is required: a candidate that doesn't
    look like a street (prepositional / keyword / street-suffix) is dropped to NULL
    — a fabricated street poisons the dedup key + Browse worse than a NULL does."""
    if not street_raw:
        return None, None
    raw = street_raw.strip()
    house: str | None = None
    hm = _HOUSE_NO_RE.search(raw)
    if hm:
        house = hm.group(1)
        raw = raw[: hm.start()].strip()
    cleaned = street.clean_street(raw)
    if cleaned is None or street.reject_as_town(cleaned, geo_names=geo_names, lat=lat, lon=lon):
        return None, None
    if not street.looks_like_czech_street(cleaned):
        return None, None
    return cleaned, house


def _broker(html: str) -> dict[str, Any] | None:
    """The selling broker/agency as the idnes/ceskereality-shaped raw["broker"]
    block resolve_brokers consumes: broker_id (the per-broker profile/photo id) is
    the stable key, agency_id the realitní-kancelář id. realitymix hides the phone
    behind a /trackredir click, so there is no phone/email here (identity-only)."""
    broker: dict[str, Any] = {}
    bid = _BROKER_ID_RE.search(html) or _MAKLER_IMG_RE.search(html)
    if bid:
        broker["broker_id"] = bid.group(1)
    agency = _AGENCY_ID_RE.search(html)
    if agency:
        broker["agency_id"] = agency.group(1)
    return broker or None


def _images(html: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for href in _IMG_RE.findall(html):
        if "_nahled" in href or href in seen:
            continue
        seen.add(href)
        urls.append(href)
    return urls


def parse_index(html: str) -> IndexPage:
    tree = HTMLParser(html)
    total = _parse_total(html)

    items: list[IndexItem] = []
    seen: set[str] = set()
    # The card is `<li class="w-full advert-item">` — tag-agnostic so a li<->div
    # markup change doesn't break it (the exact `advert-item` class token, NOT the
    # advert-item__* BEM descendants a substring match would also catch).
    for card in tree.css(".advert-item"):
        link = card.css_first('a[href*="/detail/"]')
        href = link.attributes.get("href") if link else None
        source_id = _id_from_href(href)
        if not source_id or not href or source_id in seen:
            continue
        seen.add(source_id)
        items.append(
            IndexItem(
                source_id_native=source_id,
                detail_path=href,
                title=_text(card.css_first("h2")),
                price_text=_card_price(card),
                locality_text=_text(card.css_first("p.text-body-light")),
            )
        )
    return IndexPage(total=total, items=items)


def _card_price(card: Node) -> str | None:
    """An index card's price text. Tailwind classes aren't stable selectors, so
    pick the first node whose text reads as a price (a Kč run) or an explicit
    no-price phrase; index_price then parses it (None for the no-price cases)."""
    for node in card.css("span, div"):
        txt = _text(node)
        if not txt:
            continue
        low = _strip_diacritics(txt).lower()
        if "kč" in txt.lower() or "kc" in low.replace(" ", ""):
            if _PRICE_RUN_RE.search(txt):
                return txt
        if any(k in low for k in ("vyzadani", "dohodou", "rezerv", "info v rk")):
            return txt
    return None


def parse_detail(html: str, *, source_url: str) -> ScrapedListing:
    tree = HTMLParser(html)
    source_id = _id_from_href(source_url) or ""
    category_main, category_type = category_from_breadcrumb(html)
    params = _detail_params(tree)

    title = _text(tree.css_first("h1")) or ""

    price_czk, price_unit = _parse_price(
        params.get("cena") or _detail_price_text(tree), category_type,
    )

    lat, lon, coord_provenance = _resolve_coords(html)
    street_raw, obec, okres, full_address = _address_parts(html)
    region = None
    street_name, house_number = _street_fields(
        street_raw, geo_names=[obec, okres, region], lat=lat, lon=lon,
    )
    locality = full_address or obec

    area_m2 = _parse_area(
        params.get("celková podlahová plocha")
        or params.get("užitná plocha")
        or params.get("podlahová plocha")
        or params.get("plocha")
        or title
    )
    usable_area = _parse_area(params.get("užitná plocha"))
    estate_area = _parse_area(
        params.get("plocha parcely")
        or params.get("plocha pozemku")
        or params.get("výměra pozemku")
    )
    other = _strip_diacritics(params.get("ostatní", "")).lower()

    description = _text(
        tree.css_first("div.advert-description__text-inner-inner")
        or tree.css_first("div.advert-description__text")
    )

    image_urls = _images(html)

    raw: dict[str, Any] = {
        "id": source_id,
        "title": title,
        "locality_text": locality,
        "broker": _broker(html),
        "image_urls": image_urls,
        "coords": coord_provenance,
        "params": params,
    }

    return ScrapedListing(
        source="realitymix",
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=area_m2,
        usable_area=usable_area,
        disposition=_parse_disposition(params.get("dispozice bytu") or params.get("dispozice"))
        or _parse_disposition(title),
        locality=locality,
        district=okres,
        street=street_name,
        house_number=house_number,
        lat=lat,
        lon=lon,
        floor=_parse_floor(params.get("číslo podlaží v domě") or params.get("podlaží")),
        total_floors=_parse_int(params.get("počet podlaží objektu")),
        has_balcony=_field_present(params, "balkon", "balkón", "lodžie"),
        terrace=_field_present(params, "terasa"),
        garage=("garaz" in other) or _field_present(params, "garáž") or None,
        has_parking=("parkov" in other or "garaz" in other) or None,
        building_type=_norm_building_type(params.get("druh objektu") or params.get("konstrukce")),
        condition=_norm_condition(params.get("stav objektu")),
        ownership=_norm_ownership(params.get("vlastnictví")),
        furnished=_norm_furnished(params.get("vybaveno") or params.get("vybavení")),
        energy_rating=_energy_rating(
            params.get("energetická náročnost budovy") or params.get("energetická náročnost")
        ),
        estate_area=estate_area,
        garden_area=_parse_area(params.get("plocha zahrady")),
        description=description,
        raw=raw,
    )


def _detail_price_text(tree: HTMLParser) -> str | None:
    """The price value cell — `<tr class="advert-description__short-props-price">
    <td>Cena:</td><td>VALUE</td></tr>` (the "Nabídněte cenu" button text is
    stripped by _text taking the cell, then _parse_price ignoring non-digits)."""
    row = tree.css_first("tr.advert-description__short-props-price")
    if row is None:
        return None
    cells = row.css("td")
    return _text(cells[-1]) if cells else None
