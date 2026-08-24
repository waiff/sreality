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

from scraper.area import derive_headline_area
from scraper.price_text import is_per_area_price
from scraper.scraped_listing import ScrapedListing
from scraper.street import street_from_locality

# Detail "Typ nemovitosti" value (diacritics-stripped, lowercased) -> canonical
# category_main. The live 2026 vocabulary is SEVEN coarse marketing groups
# (Byty / Domy a vily / Pozemky / Chaty a rekreacni objekty / Najemni domy /
# Komercni prostory / Hotely, penziony a restaurace — verified against the
# stored raw params of the full active walk); the finer needles below it are
# the legacy navigation taxonomy, kept in case remax serves it again.
# "Najemni domy" maps to komercni: every other portal lands cinzovni_dum under
# komercni (sreality category_sub_cb 38), so the category must agree for the
# cross-portal subtype filter to see remax rows.
TYP_TO_CATEGORY: tuple[tuple[str, str], ...] = (
    ("byty", "byt"),
    ("apartman", "byt"),
    ("domy a vily", "dum"),
    ("chaty a rekreacni", "dum"),
    ("najemni domy", "komercni"),
    ("hotely", "komercni"),
    ("domy", "dum"),
    ("vily", "dum"),
    ("chaty a chalupy", "dum"),
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

# Detail-URL slug noun -> canonical portal-agnostic subtype slug (migration
# 152). The 2026 coarse "Typ nemovitosti" groups erase the per-listing type,
# but the detail URL keeps it ("/detail/{id}/prodej-ubytovaciho-zarizeni-…"),
# so the URL is the structured signal now. Only the collision-free nouns
# observed across the full production walk are mapped; "prodej-domu" and
# "prodej-chaty-chalupy" are genuinely ambiguous between two of our slugs and
# stay None. Ordered: the more specific noun first.
URL_TO_SUBTYPE: tuple[tuple[str, str], ...] = (
    ("najemniho-cinzovniho-domu", "cinzovni_dum"),
    ("najemniho-domu", "cinzovni_dum"),
    ("cinzovniho-domu", "cinzovni_dum"),
    ("ubytovaciho-zarizeni", "ubytovani"),
    ("kancelarskych-prostor", "kancelar"),
    ("restaurace", "restaurace"),
)

# Detail "Typ nemovitosti" value -> canonical subtype slug, for the values
# specific enough to map: "Najemni domy" from the live vocabulary plus the
# legacy fine taxonomy. The live combined groups ("Hotely, penziony a
# restaurace", "Chaty a rekreacni objekty", "Domy a vily") are ambiguous and
# must NOT match any needle here — subtype_of guards the hotely group so its
# "restaurace" tail can't mis-fire.
TYP_TO_SUBTYPE: tuple[tuple[str, str], ...] = (
    ("najemni domy", "cinzovni_dum"),
    ("kancelare", "kancelar"),
    ("obchodni", "obchodni_prostor"),
    ("restaurace", "restaurace"),
    ("ubytovani", "ubytovani"),
    ("vyroba", "vyroba"),
    ("sklady", "sklad"),
    ("zemedelske objekty", "zemedelsky"),
    ("historicke objekty", "pamatka_jine"),
)

# Title-noun fallback (diacritics-stripped, lowercased), checked in order so a
# specific category wins before the garage/ostatni catch-all (and the
# nájemní/činžovní dům nouns win before the generic "domu").
CATEGORY_BY_TITLE: tuple[tuple[str, str], ...] = (
    ("bytu", "byt"),
    ("byt ", "byt"),
    ("apartman", "byt"),
    ("najemniho domu", "komercni"),
    ("cinzovniho domu", "komercni"),
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
# The subject listing's price/coords come from page attributes; the FIRST
# occurrence is the subject's (recommended-listing cards follow it lower down).
# That holds for data-gps (the subject's #listingMap precedes the carousel) but
# NOT for data-address: on the real captured page every data-address belongs to
# a "Podobné nemovitosti" carousel card and the subject has none — the first
# match is a DIFFERENT listing in a different town (W0 item 0d; it reached
# listings.street on live rows). The subject's own address line is the
# `.pd-header__address` h2; data-address is kept only as raw evidence.
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


def subtype_of(typ: str | None, url: str | None = None) -> str | None:
    """Portal-agnostic subtype slug. The detail-URL noun is tried first (the
    only per-listing type signal left since remax coarsened "Typ nemovitosti"
    to seven marketing groups), then the typ value for the correspondences
    specific enough to map. The combined "Hotely, penziony a restaurace"
    group is guarded out so its "restaurace" tail can't mis-label a hotel."""
    low_url = (url or "").lower()
    for needle, slug in URL_TO_SUBTYPE:
        if needle in low_url:
            return slug
    key = _norm_key(typ)
    if not key or "hotely" in key or "penzion" in key:
        return None
    for needle, slug in TYP_TO_SUBTYPE:
        if needle in key:
            return slug
    return None


def type_of(title: str | None) -> str | None:
    """category_type from the title/slug verb (Prodej -> prodej, Pronájem ->
    pronajem)."""
    low = _norm_key(title)
    if "pronajem" in low:
        return "pronajem"
    if "prodej" in low:
        return "prodej"
    return None


def parse_dms_pair(text: str | None) -> tuple[float | None, float | None]:
    """A `data-gps` DMS pair as (lat, lon), or (None, None) if it is unusable.

    Public because location-data's archived-HTML re-mine lane reads the same attribute out
    of the same pages (W2-6) and a second implementation of this would drift from the live
    scraper's silently — including the CZ-bbox refusal below, which is a correctness rail
    and not a formatting detail."""
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


# The detail spec-table cell renders "{amount} CZK/ {unit}" with three observed units;
# the third is per-area, and a per-m² cell MUST yield NULL (see scraper.price_text).
_DETAIL_PRICE_UNITS: tuple[tuple[str, str], ...] = (
    ("za mesic", "za mesic"),
    ("za nemovitost", "za nemovitost"),
)
# The per-area marker BEFORE the amount. Anchoring can't see it and the digit
# scrape would fold its "2" into the number, so it gets its own narrow test.
_PER_AREA_PREFIX_RE = re.compile(r"(?:za|/)\s*m\s*2|za\s*metr")


def _detail_price(text: str | None, category_type: str | None) -> tuple[int | None, str | None]:
    """Price from the detail spec table's price cell.

    Separate from `_parse_price` on purpose: that one reads an index card's `data-price`
    (a bare amount) and is shared with `index_price`. This cell carries a trailing unit,
    and a naive digit scrape swallows it — `7 759 CZK/ za m2` becomes 77592, taking the
    "2" from "m2" into the number. So the amount is read ONLY from the part before `CZK`.
    """
    default_unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    if not text:
        return None, default_unit
    low = _norm_key(text)
    if any(k in low for k in ("na vyzadani", "info o cene", "cena v rk", "dohodou", "neuvedena")):
        return None, default_unit
    head, sep, tail = low.partition("czk")
    if not sep:
        return None, default_unit
    # per-area pricing has no representation in price_czk. The unit normally
    # FOLLOWS the amount, so the shared anchored test applies to the tail; the
    # prefix shape (`Cena za m2: 7 759 CZK`) has to be caught separately, because
    # the digit scrape below would otherwise take the "2" out of "m2" into the
    # number and store a FABRICATED 27759 — strictly worse than the NULL the
    # deleted `_PER_AREA_MARKERS` used to produce, and there is no write-boundary
    # backstop behind this.
    if is_per_area_price(low[len(head):]) or _PER_AREA_PREFIX_RE.search(head):
        return None, default_unit
    digits = re.sub(r"\D", "", head)
    if not digits:
        return None, default_unit
    value = int(digits)
    unit = next((mapped for token, mapped in _DETAIL_PRICE_UNITS if token in tail), default_unit)
    return (value if 0 < value <= _PRICE_MAX else None), unit


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
    # Map to the canonical sreality codes; drop anything unmapped (e.g.
    # "ostatni") to NULL rather than leaking a non-canonical label through.
    key = _norm_key(text)
    return OWNERSHIP.get(key) if key else None


def _norm_furnished(text: str | None) -> str | None:
    # Canonical sreality codes (parser.FURNISHED): ano / ne / castecne.
    yn = _yes_no(text)
    if yn is True:
        return "ano"
    if yn is False:
        return "ne"
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


# The selling agent's stable key is the `uzivatele/{id}` DIRECTORY of their photo URL
# (mlsf.remax-czech.cz/data//uzivatele/{id}/{asset}_{asset}_photo_detail_w.jpg) — the two
# FILENAME numbers are per-asset and change when a photo is re-uploaded, so only the
# directory is 1:1 with the human (775 distinct ids = 775 distinct (id, profile-slug)
# pairs over 3,000 stored pages). Same photo-URL-derived shape realitymix already uses.
# Note the double slash after `data`, and that the extension is not always .jpg.
_BROKER_UID_RE = re.compile(r"/uzivatele/(\d+)/")
# The profile link is ABSOLUTE (`https://www.remax-czech.cz/reality/{office}/{agent}/`) —
# a `/reality/`-prefixed relative match finds nothing.
_BROKER_PROFILE_RE = re.compile(
    r"remax-czech\.cz/reality/([a-z0-9-]+)/([a-z0-9-]+)/", re.IGNORECASE
)


def _broker(tree: HTMLParser) -> dict[str, Any] | None:
    """The selling agent as the idnes-shaped `raw["broker"]` block resolve_brokers reads.

    Email is included (unlike ceskereality/realitymix, which have none): remax exposes a
    personal `mailto:` on 3,000/3,000 stored pages, and `broker_identities.email_domain`
    is the ONLY firm key — without it a broker gets no firm, no membership and no
    cross-source bridge. Phone is deliberately NOT collected (operator: `broker_phone` is
    an intentional zero on all nine portals).

    ~0.3% of agents hold two ids after an office move (both carrying the same email) —
    the accepted cost of a rename-proof numeric key over a mutable profile slug. Those
    used to split permanently under the never-merge-within-a-source policy; since
    2026-08-20 the name-gated engine reunites them on that shared e-mail whenever it is
    discriminating (toolkit/broker_resolver.py, path A), so the duplicate ids remain but
    the duplicate BROKERS do not.
    """
    block = tree.css_first("div.pd-sidebar__agent-info")
    if block is None:
        return None
    broker: dict[str, Any] = {}
    html = block.html or ""
    uid = _BROKER_UID_RE.search(html)
    if uid:
        broker["broker_id"] = uid.group(1)
    name = block.css_first("strong")
    if name is not None and (text := name.text(strip=True)):
        broker["name"] = text
    mail = block.css_first('a[href^="mailto:"]')
    if mail is not None:
        address = (mail.attributes.get("href") or "")[len("mailto:") :].strip()
        if "@" in address:
            broker["email"] = address.lower()
    profile = _BROKER_PROFILE_RE.search(html)
    if profile:
        broker["agency_slug"] = profile.group(1).lower()
    return broker or None


def _description(tree: HTMLParser) -> str | None:
    """The listing's free-text body.

    The container is a read-more collapse: the FULL text is server-rendered in the
    first response and Vue only toggles a CSS class over it, so no JS execution is
    needed. `<br>` carries the seller's paragraphing, so separate on it rather than
    running every paragraph together.

    The previous selectors (`.pd-detail-text`, `#popis`) match no state of this page,
    pre- or post-JS — 0/300 stored pages carry either — which is why remax sat at 0.0%
    description for its entire life. Do NOT substitute `og:description`: it is REMAX
    marketing boilerplate, byte-identical on every listing, so it would look like a fix
    while poisoning every row with one constant string.
    """
    node = tree.css_first('div.pd-base-info__content-collapse-inner div[ref="content-inner"]')
    if node is None:
        return None
    return node.text(separator="\n", strip=True) or None


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
        # `data-advert-price` is absent on 132/300 stored pages, and the previous
        # `.pd-price` fallback on ALL 300 — so ~44% of remax listings had no price path at
        # all. The spec-table cell carries it on 300/300 and, unlike `.pd-header__price`,
        # is not polluted by the adjacent energy-rating glyphs. `_detail_price` (not
        # `_parse_price`) because that cell carries a trailing unit.
        price_czk, price_unit = _detail_price(
            _text(tree.css_first(".pd-table__value--price")), category_type
        )

    # Coordinates: the first data-gps on the page is the subject listing's (the
    # rest belong to recommended cards). CZ-bbox-guarded.
    lat = lon = None
    gps_match = _GPS_ATTR_RE.search(html)
    if gps_match is not None:
        lat, lon = parse_dms_pair(gps_match.group(1))

    locality, district = _h1_locality(title)
    # W0 item 0d: the subject's own location line. "ulice <Street>, <Town>" when
    # the portal states a street, else just the town/okres — the header
    # classifier from the location program's per-portal caps table.
    header_addr = _text(tree.css_first(".pd-header__address"))
    if header_addr:
        # The h2 nests a "mapa" anchor whose text rides along in _text().
        header_addr = re.sub(r"\s+mapa$", "", header_addr).strip(" ,") or None
    street = None
    if header_addr:
        parts = [p.strip() for p in header_addr.split(",") if p.strip()]
        first = parts[0] if parts else ""
        rest = parts
        if first.lower().startswith("ulice "):
            street_part = first[len("ulice "):]
            rest = parts[1:]
            # The shared extractor needs a town segment for its cross-check (a
            # single bare segment reads as town-only); the header's own tail or
            # the h1 locality provides it.
            context = rest or ([locality] if locality else [])
            street = street_from_locality(
                ", ".join([street_part, *context]) if context else street_part,
                position="first", geo_names=(locality, district), lat=lat, lon=lon,
            )
        if not locality and rest:
            locality = rest[0]
            if " - " in locality:
                district = locality.split(" - ")[-1].strip() or district

    # Carousel contamination guard (W0 0d): data-address is NOT the subject's —
    # kept verbatim as evidence only, never parsed into locality/street.
    addr_match = _ADDRESS_ATTR_RE.search(html)
    carousel_address = (
        unescape(addr_match.group(1)).strip(" ,") or None if addr_match else None
    )

    usable_text = params.get("uzitna plocha")
    total_text = params.get("celkova plocha") or params.get("plocha")
    area_m2, area_basis = derive_headline_area(
        category_main=category_main,
        usable=_parse_area(usable_text),
        total=_parse_area(total_text),
        fallback=_parse_area(title),
    )

    image_urls = _detail_images(html, source_id)

    raw: dict[str, Any] = {
        "id": source_id,
        "title": title,
        "price_text": price_attr,
        # The subject's own header line; the old top-level "address" key (the
        # first data-address, i.e. usually a CAROUSEL card's address) is renamed
        # so no re-mine can mistake it for the subject's again.
        "display_address": header_addr,
        "carousel_address": carousel_address,
        "remax_ref": params.get("cislo zakazky"),
        "broker": _broker(tree),
        "image_urls": image_urls,
        "params": params,
    }

    return ScrapedListing(
        source="remax",
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        subtype=subtype_of(params.get("typ nemovitosti"), source_url),
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=area_m2,
        area_basis=area_basis,
        usable_area=_parse_area(usable_text),
        disposition=_parse_disposition(params.get("dispozice")) or _parse_disposition(title),
        locality=locality,
        district=district,
        street=street,
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
        description=_description(tree),
        raw=raw,
    )
