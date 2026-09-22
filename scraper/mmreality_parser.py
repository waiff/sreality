"""Deterministic HTML parsing for mmreality.cz (portal framework).

Pure functions, no I/O: `parse_index` turns one `/nemovitosti/` search-results
page into the listing ids + the next page, and `parse_detail` turns one listing
page into a `ScrapedListing` (the shared multi-portal contract).

M&M Reality is server-rendered, but — unlike the free-text classifieds (bazos)
or the `<dl>`-table portal (idnes) — every listing detail page embeds a COMPLETE
structured estate object as a Vue prop (`:property="{…}"`, HTML-entity-encoded).
So `parse_detail` decodes that JSON rather than scraping markup: precise
per-listing coordinates, typed condition/construction/ownership, area, floors,
images — all from one object. Typed fields are normalised to the SAME canonical
labels sreality/idnes emit (`smíšená`→`smisena`, `velmi dobrý`→`velmi_dobry`,
`Družstevní`→`druzstevni`, `2+1`) so cross-portal filters / dedup agree.

The single `/nemovitosti/` index is mixed-category; each listing's category
(`byt`/`dum`/…, `prodej`/`pronajem`/…) is read from its own detail JSON, so one
config walks every category (no per-category index slice).
"""

from __future__ import annotations

import html as ihtml
import json
import re
from dataclasses import dataclass, field
from functools import partial
from collections.abc import Mapping
from typing import Any
from unicodedata import combining, normalize

from selectolax.parser import HTMLParser, Node

from scraper import vocabulary
from scraper.area import PortalAreas, derive_headline_area
from scraper.attribute_contract import source_label, source_value, source_values
from scraper.scraped_listing import ScrapedListing
from scraper.street import clean_street, street_from_locality

SOURCE = "mmreality"

# mmreality category.name / group.name -> our canonical labels. category.name is
# the transaction (Prodej/Pronájem/Dražba); group.name is the property kind.
CATEGORY_TYPE: dict[str, str] = {
    "prodej": "prodej",
    "pronajem": "pronajem",
    "drazba": "drazba",
}
CATEGORY_MAIN: dict[str, str] = {
    "byt": "byt",
    "dum": "dum",
    "pozemek": "pozemek",
    "komercni": "komercni",
    "ostatni": "ostatni",
}



# Czech-bbox guard: a coordinate outside it (a swapped lat/lon or a foreign
# point) is dropped rather than stored as geom.
_CZ_LAT_MIN, _CZ_LAT_MAX = 48.0, 51.5
_CZ_LON_MIN, _CZ_LON_MAX = 12.0, 19.0

_ID_RE = re.compile(r"/nemovitosti/(\d+)/?")
_PAGE_RE = re.compile(r"[?&]page=(\d+)")
# The Vue prop is HTML-entity-encoded (&quot;), so the only literal double quotes
# in the attribute are its delimiters — `[^"]*` captures the whole blob cleanly.
_PROPERTY_ATTR_RE = re.compile(r':property="([^"]*)"')
# Preview sizes, largest first — we store the biggest the CDN offers.
_IMAGE_SIZES: tuple[str, ...] = ("xlarge2", "xlarge", "medium2", "medium", "xsmall")


@dataclass(frozen=True)
class IndexItem:
    source_id_native: str
    detail_path: str
    title: str | None = None
    price_text: str | None = None


@dataclass(frozen=True)
class IndexPage:
    total: int | None
    items: list[IndexItem] = field(default_factory=list)
    next_offset: int | None = None


def _strip_diacritics(text: str) -> str:
    return "".join(c for c in normalize("NFD", text) if not combining(c))


def _norm_key(text: str | None) -> str | None:
    if not text:
        return None
    return _strip_diacritics(str(text)).lower().strip() or None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    digits = re.sub(r"\D", "", str(v))
    return int(digits) if digits else None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group(0).replace(",", ".")) if m else None



def _in_cz_bbox(lat: float, lon: float) -> bool:
    return _CZ_LAT_MIN <= lat <= _CZ_LAT_MAX and _CZ_LON_MIN <= lon <= _CZ_LON_MAX


def _id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _ID_RE.search(url)
    return m.group(1) if m else None



def _name_of(obj: Any) -> str | None:
    """A `{id, name}` enum's label, or the string itself (`title`)."""
    if isinstance(obj, dict):
        name = obj.get("name")
        return name if isinstance(name, str) else None
    return obj if isinstance(obj, str) else None


def _code_of(obj: Any) -> str | None:
    """mmreality states the PENB class in `energyClassification.code`, not `.name`."""
    return str((obj or {}).get("code") or "") or None if isinstance(obj, dict) else None


def _category_type(obj: dict[str, Any]) -> str | None:
    cat = obj.get("category") or {}
    return CATEGORY_TYPE.get(_norm_key(cat.get("name")) or "")


def _category_main(obj: dict[str, Any]) -> str | None:
    """Map the property-kind group to a canonical category_main. mmreality's
    group.name is e.g. 'Byt' / 'Dům' / 'Pozemek' / 'Komerční objekt' / 'Ostatní'
    — matched by prefix so plural/variant labels still resolve."""
    key = _norm_key((obj.get("group") or {}).get("name"))
    if not key:
        return None
    if key.startswith("byt"):
        return "byt"
    if key.startswith("dum") or key.startswith("dom"):
        return "dum"
    if key.startswith("pozem"):
        return "pozemek"
    if key.startswith("komerc"):
        return "komercni"
    return "ostatni"



def _coords(obj: dict[str, Any]) -> tuple[float | None, float | None]:
    point = obj.get("point") or {}
    lat = _to_float(point.get("latitude"))
    lon = _to_float(point.get("longitude"))
    if lat is None or lon is None or not _in_cz_bbox(lat, lon):
        return None, None
    return lat, lon


def _locality(obj: dict[str, Any]) -> str | None:
    for key in ("location", "municipalityPart", "municipality"):
        val = obj.get(key)
        if val:
            return str(val)
    return None


# W0 item 0g (location-data program): the estate's originalTitle carries an
# explicit street marker ("Prodej restaurace, ..., Bratronice, ul. Hlavní")
# that the structured `street` field sometimes lacks — measured 5/12 on the
# mining corpus. First occurrence only; up to the next comma.
# Token-capped (<=3 words) and anchored to a capitalized/numeral start —
# review finding: an uncapped [^,\n]{2,60} capture would ride trailing
# prose into the street when the marker is not comma-terminated.
_TITLE_STREET_RE = re.compile(
    r"\bul\.\s+((?:\d{1,2}\.\s*)?[^\s,]+(?:\s+[^\s,]+){0,2})"
)


# Lowercase words that legitimately continue a Czech street name.
_STREET_PARTICLES: frozenset[str] = frozenset({
    "nad", "pod", "u", "na", "v", "ve", "z", "ze", "k", "ke", "mezi", "za",
})


def _title_street(
    obj: dict[str, Any], locality: str | None, district: str | None,
    lat: float | None, lon: float | None,
) -> str | None:
    title = obj.get("originalTitle")
    if not isinstance(title, str):
        return None
    m = _TITLE_STREET_RE.search(title)
    if m is None:
        return None
    cand = m.group(1).strip(" .")
    if not cand or not (cand[0].isupper() or cand[0].isdigit()):
        return None
    # Truncate a prose tail: a street phrase continues only with capitalized
    # words, digits (house number), name particles, or the genitive word after
    # a numeral ("28. října"). "Dlouhá 15 volejte kdykoliv" -> "Dlouhá 15".
    words = cand.split()
    kept = [words[0]]
    for prev, w in zip(words, words[1:]):
        if (
            w[0].isupper() or w[0].isdigit()
            or w.lower() in _STREET_PARTICLES or prev.endswith(".")
        ):
            kept.append(w)
        else:
            break
    cand = " ".join(kept).strip(" .")
    if not cand:
        return None
    ctx = locality or district
    # The shared extractor supplies the don't-fabricate town cross-check.
    return street_from_locality(
        f"{cand}, {ctx}" if ctx else cand, position="first",
        geo_names=(locality, district), lat=lat, lon=lon,
    )


def _image_urls(obj: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for img in obj.get("images") or []:
        previews = (img or {}).get("previews") or {}
        url = next((previews[s] for s in _IMAGE_SIZES if previews.get(s)), None)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls



class NoPropertyObject(ValueError):
    """The page parsed but carries no `:property` object at all. On mmreality
    a removed listing's URL can answer 200 with the old title and NOTHING
    else (no similar cards either) -- the removed-plot shape seen 2026-09-07.
    The portal decides whether that is gone (the site's own page) or an error
    (some other 200 body)."""


class PropertyMismatch(ValueError):
    """The page parsed, but none of its `:property` objects IS the requested
    listing. mmreality keeps a removed listing's URL alive (HTTP 200, the old
    title) and fills the page with "similar" preview cards -- so this is the
    portal's gone signal in disguise, not a parse failure."""


def extract_property(html: str, listing_id: str | None) -> dict[str, Any]:
    """Return the embedded `:property` estate object for `listing_id`.

    A detail page carries several `:property` Vue props (the main listing plus
    related preview cards), so we pick the blob whose `id` matches the listing.
    When `listing_id` is known and NO blob matches, raise PropertyMismatch: the
    2026-09-07 presence checks found that a removed listing's URL still serves
    200 with the old title and a page of substitute cards, and the old
    largest-blob fallback ingested those substitutes as if they were the
    requested listing (their rows overwritten with preview-card data). The
    fallback survives only for callers with no id to match. Raises ValueError
    when nothing parses at all."""
    candidates: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    for raw in _PROPERTY_ATTR_RE.findall(html):
        if "&quot;id&quot;" not in raw and '"id"' not in raw:
            continue
        try:
            obj = json.loads(ihtml.unescape(raw))
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if listing_id is not None and str(obj.get("id")) == str(listing_id):
            matches.append(obj)
        candidates.append(obj)
    if matches:
        # The page can carry the listing twice: the full estate object and a
        # preview card of itself. The fullest one is the listing.
        return max(matches, key=lambda o: len(json.dumps(o, default=str)))
    if not candidates:
        raise NoPropertyObject("no :property estate object found on page")
    if listing_id is not None:
        raise PropertyMismatch(
            f"page carries no :property object for listing {listing_id} "
            f"(found {[str(o.get('id')) for o in candidates][:5]})"
        )
    return max(candidates, key=lambda o: len(json.dumps(o, default=str)))


def parse_index(html: str) -> IndexPage:
    tree = HTMLParser(html)
    items: list[IndexItem] = []
    seen: set[str] = set()
    for anchor in tree.css("a[data-card-id]"):
        source_id = anchor.attributes.get("data-card-id") or _id_from_url(
            anchor.attributes.get("href")
        )
        href = anchor.attributes.get("href")
        if not source_id or not href or source_id in seen:
            continue
        seen.add(source_id)
        price_node = anchor.css_first("[data-realty-price]")
        items.append(
            IndexItem(
                source_id_native=source_id,
                detail_path=href,
                title=(
                    (price_node.attributes.get("data-realty-name") if price_node else None)
                    or _text(anchor.css_first("h4"))
                ),
                price_text=(
                    price_node.attributes.get("data-realty-price") if price_node else None
                ),
            )
        )
    return IndexPage(total=declared_total(html), items=items, next_offset=_next_page(tree))


def _text(node: Node | None) -> str | None:
    if node is None:
        return None
    txt = re.sub(r"\s+", " ", node.text(separator=" ", strip=False)).strip()
    return txt or None


# The page's Vue SSR state (`<vue-property-list-grid :ssr="{...}">`, HTML-entity
# encoded JSON) carries `metadata.count`: the portal's own result total for this
# filter. Read it off the raw text in either encoding rather than decoding the
# ~400 KB attribute — the number is the only thing the walk needs from it.
_DECLARED_RE = re.compile(
    r'metadata(?:&quot;|"):\{(?:&quot;|")count(?:&quot;|"):(\d+)'
)


def declared_total(html: str) -> int | None:
    """The portal-declared result count for this index page, or None when the
    SSR state is absent (a throttled or error page) — never 0 by default, so an
    unmeasurable page reads as `unknown`, not `complete` (rule #3)."""
    m = _DECLARED_RE.search(html)
    return int(m.group(1)) if m else None


# The portal's own property-type groups, as the filter UI and the SSR
# `metadata.groups` list them. A group id the walk does not know shows up as
# `group-<id>` in the drift check, which is the point: an unknown type is a
# coverage gap until someone maps it.
GROUP_SLUGS: dict[int, str] = {
    11: "byty", 10: "domy", 4: "pozemky", 2: "komercni-objekty", 210: "ostatni",
}
_GROUPS_RE = re.compile(r'metadata(?:&quot;|"):\{.*?groups(?:&quot;|"):\[(.*?)\]', re.S)
_GROUP_ID_RE = re.compile(r'group(?:&quot;|"):(\d+)')


def live_groups(html: str) -> set[int] | None:
    """Property-type group ids the page's SSR state declares for this listing
    tree, or None when the state is absent (never an empty set for a broken
    page -- absence must not read as 'the portal has no categories')."""
    m = _GROUPS_RE.search(html)
    if not m:
        return None
    groups = {int(g) for g in _GROUP_ID_RE.findall(m.group(1))}
    # An empty or differently-keyed array is unreadable, not "no categories":
    # returning set() would make the drift alarm report every configured type
    # as dead on every walk.
    return groups or None


def _next_page(tree: HTMLParser) -> int | None:
    link = tree.css_first('link[rel="next"]')
    href = link.attributes.get("href") if link else None
    m = _PAGE_RE.search(href or "")
    return int(m.group(1)) if m else None


def index_price(text: str | None) -> int | None:
    """The Kč amount from an index card's `data-realty-price`, or None
    ("Info o ceně" / "Cena dohodou"). Drives price-change refetch detection."""
    if not text or "dohod" in _strip_diacritics(text).lower():
        return None
    return _to_int(text)


def areas_from_params(
    obj: Mapping[str, Any],
    *,
    category_main: str | None,
) -> PortalAreas:
    """mmreality's area keys, in ITS precedence — spelled here once and nowhere else.

    `obj` is the embedded `:property` estate object, which `parse_detail` stores whole
    under `listings.raw_json` (`raw = dict(obj)`), so these are TOP-LEVEL raw_json keys
    rather than the `params` spec-cell map the HTML portals carry. `parse_detail` reads it
    off a live page and `scripts/reparse.py` replays `parse_detail` over the stored page —
    one key order, read twice (rule 21).

    THE PARCEL IS `parcelArea` ("Plocha parcely"), and that is the whole of W21 here. The
    chain this replaced read `landArea or plotArea or totalArea`: on 14,417 stored rows
    `landArea` and `plotArea` carry a value on EXACTLY ZERO of them — they are keys the
    page declares and never fills — so `estate_area` was always `totalArea`, and
    `totalArea` is not a measure at all. It is a figure the page DERIVES, and what it sums
    depends on the category: `parcelArea + usableArea` on a dum (verified on three captures
    and on 4,133 stored rows carrying all three, just as `parcelArea = builtUpArea +
    gardenArea`), `== usableArea` on komerční / ostatní, which state no parcel, and
    `== parcelArea` on a pozemek, which has no interior to add. Writing it into
    `estate_area` inflated 1,178 active houses' plots by 44-50 %, and before W1 the same
    number was the HEADLINE on 1,515 more. `parcelArea` is stated on every one of the
    3,653 active land rows and 2,693 active houses, and on land it equals `totalArea`
    exactly (0 rows disagree), so dropping the derived figure costs nothing there and stops
    the parser inventing a measure the portal never published.

    `totalArea` is therefore not offered to the resolver in ANY slot: a derived figure
    stamped 'total' would be a confident wrong label, which is worse than the missing value
    it replaces. That costs a headline on 19 rows corpus-wide whose page states neither
    input (5 byt, 2 komerční, 11 dum, 1 inactive pozemek); their parcel, when they have
    one, is in `estate_area`.

    `estate_area` IS filled here, on land too — from `parcelArea`, a cell the page itself
    labels "Plocha parcely". That is the rule for every writer: fill `estate_area` only
    from a LABELLED parcel, never by synthesising one from `area_m2`. What no writer may
    do is manufacture the column for a portal whose land pages carry no parcel label —
    `public.plot_area_m2` (migration 534) is what makes those rows reachable.
    """
    plot = _to_float(obj.get("parcelArea"))
    area_m2, area_basis = derive_headline_area(
        category_main=category_main,
        usable=_to_float(obj.get("usableArea")),
        plot=plot,
    )
    return PortalAreas(
        area_m2=area_m2, area_basis=area_basis,
        usable_area=_to_float(obj.get("usableArea")), estate_area=plot,
        garden_area=_to_float(obj.get("gardenArea")),
    )


def parse_detail(html: str, *, source_url: str) -> ScrapedListing:
    """Parse one mmreality listing page into a ScrapedListing.

    Category is derived from the embedded estate object (mixed-category index),
    so — unlike idnes — nothing is passed in from the index/URL."""
    listing_id = _id_from_url(source_url)
    obj = extract_property(html, listing_id)
    source_id = str(obj.get("id") or listing_id or "")

    category_type = _category_type(obj)
    category_main = _category_main(obj)
    price_unit = "za mesic" if category_type == "pronajem" else "za nemovitost"
    price_czk = _to_int(obj.get("price"))
    if price_czk == 0:
        price_czk = None

    lat, lon = _coords(obj)
    locality = _locality(obj)
    district = obj.get("district") or None
    street = clean_street(
        obj.get("street") if isinstance(obj.get("street"), str) else None
    ) or _title_street(obj, locality, district, lat, lon)
    read = partial(source_value, SOURCE, params=obj)
    label = partial(source_label, SOURCE, params=obj)
    accessories = vocabulary.accessory_names(obj.get("accessoryGroups"))
    # R11: parking BELONGING to the property. The portal files "Parkování na ulici" and
    # "Parkoviště poblíž" under the group "Parkování" and "Parkety" (parquet FLOORING)
    # under "Podlahy", so the group is half the fact — a flattened name search read all
    # three as parking and put has_parking at 73.4% true (7,573 of 10,317 active rows);
    # the group-qualified reading is 5,557 true / 2,361 false / 2,399 unknown.
    parking_group = vocabulary.accessory_names(
        obj.get("accessoryGroups"), group="Parkování")
    parking_lots = _to_int(read("parking_lots"))
    garage_flag, _ = source_values(SOURCE, "garage", obj)
    equipment = read("furnished")
    equipment = None if equipment is None else str(_to_int(equipment))
    overground, underground = (_to_int(v) for v in
                               source_values(SOURCE, "total_floors", obj))
    total_floors = (
        (overground or 0) + (underground or 0)
        if overground is not None or underground is not None
        else None
    )

    cellar_flag, _ = source_values(SOURCE, "cellar", obj)
    areas = areas_from_params(obj, category_main=category_main)

    image_urls = _image_urls(obj)
    raw = dict(obj)
    raw["image_urls"] = image_urls
    raw["source_url"] = source_url

    return ScrapedListing(
        source=SOURCE,
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=areas.area_m2,
        area_basis=areas.area_basis,
        usable_area=areas.usable_area,
        disposition=vocabulary.disposition(
            SOURCE, *(_name_of(v) for v in source_values(SOURCE, "disposition", obj))
        ),
        locality=locality,
        district=district,
        # Structured street first; else the originalTitle "ul. <Street>"
        # marker (W0 0g), both through the shared don't-fabricate guard.
        street=street,
        lat=lat,
        lon=lon,
        floor=_to_int(read("floor")),
        total_floors=total_floors,
        building_type=vocabulary.canonical("building_type", SOURCE, label("building_type")),
        condition=vocabulary.canonical("condition", SOURCE, label("condition")),
        ownership=vocabulary.canonical("ownership", SOURCE, label("ownership")),
        energy_rating=vocabulary.energy_rating(_code_of(read("energy_rating"))),
        # `equipment` is a 1|2|3 CODE, not a label — the one registry maps it (R5).
        furnished=vocabulary.canonical("furnished", SOURCE, equipment),
        has_lift=vocabulary.yes_no(read("has_lift")),
        cellar=(
            vocabulary.yes_no(cellar_flag)
            if cellar_flag is not None
            else vocabulary.mentions(accessories, "sklep")
        ),
        # The top-level booleans, not the accessory names: `balcony` and `loggia` are on
        # 22.4% of active rows with a real `false`, and NO accessory name has ever matched
        # balcony/loggia/terrace — both columns were 0/0 on all 10,317 active rows.
        has_balcony=vocabulary.any_true(
            *(vocabulary.yes_no(v) for v in source_values(SOURCE, "has_balcony", obj))
        ),
        terrace=vocabulary.present(read("terrace")),
        garage=vocabulary.any_true(
            vocabulary.yes_no(garage_flag), vocabulary.mentions(accessories, "garaz"),
        ),
        has_parking=vocabulary.any_true(
            None if parking_lots is None else parking_lots > 0,
            vocabulary.parking(parking_group),
        ),
        parking_lots=parking_lots,
        estate_area=areas.estate_area,
        garden_area=areas.garden_area,
        description=obj.get("description") or None,
        raw=raw,
    )
