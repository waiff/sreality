"""Deterministic HTML parsing for reality.idnes.cz (portal framework).

Pure functions, no I/O of their own: `parse_index` turns one search-results page
into the listing ids + the next page, and `parse_detail` turns one listing page
into a `ScrapedListing` (the shared multi-portal contract in
`scraper.scraped_listing`).

Unlike bazos (free-text classifieds), idnes is a STRUCTURED portal: a `<dl>`
spec table (paired `<dt>`/`<dd>`), a clean price element, and — crucially —
precise per-listing coordinates embedded in the page's map config
(`"center":[lon,lat]`). Coordinates come straight from the page; a page that
omits them carries no coordinate. The typed `<dl>` fields are
normalised to the same canonical labels the sreality parser emits (e.g.
"panelová" -> "panel", "osobní" -> "osobni") so cross-portal filters agree.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import re
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from unicodedata import combining, normalize

from selectolax.parser import HTMLParser, Node

from scraper import vocabulary
from scraper.area import PortalAreas, derive_headline_area, parse_area_text
from scraper.attribute_contract import source_value, source_values
from scraper.broker_idnes import parse_idnes_broker
from scraper.price_text import is_per_area_price
from scraper.scraped_listing import ScrapedListing
from scraper.street import street_from_locality

SOURCE = "idnes"


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

# Czech-bbox guard: an embedded pin outside it — a swapped lat/lon, or a stray
# decimal pair — is dropped rather than stored as geom.
_CZ_LAT_MIN, _CZ_LAT_MAX = 48.0, 51.5
_CZ_LON_MIN, _CZ_LON_MAX = 12.0, 19.0

# The detail-URL hash is the source_id_native (24 hex chars today; >=16 to be safe).
_ID_RE = re.compile(r"/detail/[^?#]*?/([0-9a-f]{16,})/?(?:[?#]|$)")
_INT_RE = re.compile(r"(\d+)")
_PAGE_RE = re.compile(r"[?&]page=(\d+)")
_DETAIL_PATH_RE = re.compile(r"/detail/([^/?#]+)/([^/?#]+)/")
# A price token: a leading digit then more digits split by ordinary / no-break /
# zero-width spaces (the Czech "9 790 000" thousands format idnes renders with
# &nbsp;/&zwj; between groups). Stops at the first non-space, non-digit char.
_PRICE_RUN_RE = re.compile(r"\d[\d\s\u00a0\u200b\u200c\u200d\u2060]*")
_PRICE_MAX = 2_147_483_647  # listings.price_czk is a Postgres integer
# Map config: "center":[lon, lat]. CZ lat/lon ranges don't overlap, so a swap is
# caught by the bbox guard rather than producing a bogus point.
_CENTER_RE = re.compile(r'"center"\s*:\s*\[\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\]')
_FLOOR_PATRO_RE = re.compile(r"(-?\d+)\.\s*patro")
_FLOOR_NP_RE = re.compile(r"(\d+)\.\s*np")
_FLOOR_PP_RE = re.compile(r"(\d+)\.\s*pp")
# The price cell's inline mortgage-calculator link ("Spočítat hypotéku" /
# "Chci spočítat hypotéku") — UI chrome, not price data; stripped before the
# text lands in raw_json. Safe: raw is not part of the typed content hash.
_MORTGAGE_CTA_RE = re.compile(r"\s*(?:chci\s+)?spočítat\s+hypotéku\s*", re.IGNORECASE)


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
    # True when the page POSITIVELY states it has no results. Distinct from
    # `total is None and not items`, which is also what a degraded or throttled
    # page looks like — see `_EMPTY_MARKERS`.
    empty_confirmed: bool = False


# idnes states emptiness OUT LOUD, and that is worth a lot. A slice with no
# results renders no count phrase at all, so `total` comes back None — which is
# byte-for-byte what a degraded page returns, and treating the two alike is how
# a broken fetch gets read as "this region has nothing in it" and authorises a
# delisting sweep. ceskereality has no such string and has to confirm a zero by
# READING THE PAGE TWICE (a throttle is transient, an empty slice is stable).
# Here the site tells us directly, so one read is enough.
_EMPTY_MARKERS: tuple[str, ...] = (
    "momentalne tu neni zadny inzerat",      # the live string, diacritics stripped
    "neodpovida zadny inzerat",
    "nenasli jsme zadny inzerat",
)


def _is_empty_result(text: str) -> bool:
    flat = " ".join(_strip_diacritics(text).lower().split())
    return any(m in flat for m in _EMPTY_MARKERS)


# --- descending into a slice that could not be enumerated ---------------------
#
# A search page advertises the places one level below whatever it is showing: a
# kraj page links its okresy (and Prague links its ten obvody), and the abroad
# page links one `s-l` value per country. That is the descent path when a slice
# is too big to page through reliably.
#
# The list is SCRAPED rather than declared, which on ceskereality would be a
# mistake (its rendered facet block is a top-10-by-popularity list, not a
# partition). It is safe here for one specific reason: the descent PROVES ITSELF
# against the parent's declared total. A missing child leaves the union short and
# the slice stays incomplete; a spurious child can only add rows from the same
# category, which cannot push the union past the declared count. Either way the
# arithmetic catches it, so the link list never has to be trusted.
#
# The non-place vocabularies that share the same URL shape — condition, project
# stage, and the `pokoj`/`atypicke` dispositions — are excluded by name. The
# numeric dispositions (`3+kk`) exclude themselves: `+` is outside the slug
# character class.
_NON_PLACE_SLUGS: frozenset[str] = frozenset({
    "novostavby", "projekty", "ve-vystavbe",
    "dobry-stav", "udrzovane", "spatny-stav", "k-demolici",
    "po-rekonstrukci", "v-rekonstrukci", "pred-rekonstrukci",
    "atypicke", "pokoj",
})

_SL_RE = re.compile(r'href="/s/[^"/]+/[^"/]+/\?s-l=([A-Za-z0-9_-]+)"')


def sub_places(
    html: str, sale_type: str, category: str, *, exclude: Collection[str] = (),
) -> tuple[list[str], list[str]]:
    """(path children, s-l children) advertised by this search page.

    `exclude` should carry the slice we are already looking at plus its siblings,
    so a kraj page's links to the other thirteen kraje are not read as children.
    """
    skip = set(exclude) | _NON_PLACE_SLUGS
    pat = re.compile(rf'href="/s/{re.escape(sale_type)}/{re.escape(category)}/([a-z0-9-]+)/"')
    paths = sorted({m for m in pat.findall(html) if m not in skip})
    sls = sorted({m for m in _SL_RE.findall(html) if m not in skip})
    return paths, sls


def _strip_diacritics(text: str) -> str:
    return "".join(c for c in normalize("NFD", text) if not combining(c))


def _text(node: Node | None) -> str | None:
    if node is None:
        return None
    txt = re.sub(r"\s+", " ", node.text(separator=" ", strip=False)).strip()
    return txt or None


def _strip_mortgage_cta(text: str | None) -> str | None:
    if not text:
        return text
    out = _MORTGAGE_CTA_RE.sub(" ", text).strip()
    return out or None


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
    # A per-m² figure must NEVER be stored as the absolute price — today idnes
    # shows per-m² only in a grey note span the parser doesn't read (verified on
    # 5,773 staged pozemek pages), so this is a drift rail for the day a page's
    # main price element becomes unit-priced.
    if is_per_area_price(text[m.end():]):
        return None, unit
    digits = re.sub(r"\D", "", m.group(0))
    if not digits:
        return None, unit
    value = int(digits)
    return (value if value <= _PRICE_MAX else None), unit


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

    # Only meaningful when there is genuinely nothing: a page that HAS results
    # or a count is never "confirmed empty", whatever boilerplate it carries.
    empty = (
        not items and total is None and _is_empty_result(_page_text(tree))
    )
    return IndexPage(
        total=total, items=items, next_offset=_next_page(tree),
        empty_confirmed=empty,
    )


def _next_page(tree: HTMLParser) -> int | None:
    for link in tree.css("a.paging__item"):
        cls = link.attributes.get("class") or ""
        if "next" not in cls:
            continue
        m = _PAGE_RE.search(link.attributes.get("href") or "")
        if m:
            return int(m.group(1))
    return None


def _resolve_coords(html: str) -> tuple[float | None, float | None, dict[str, Any]]:
    """The page's own embedded pin, or nothing."""
    m = _CENTER_RE.search(html)
    if m:
        lon, lat = float(m.group(1)), float(m.group(2))
        if _in_cz_bbox(lat, lon):
            return lat, lon, {"source": "page"}
    return None, None, {"source": None}


# The gallery anchor IS the portal's assertion of what photos exist, so we deny what is
# provably not a photo rather than allow-listing hosts — mirroring scraper/media.py's
# reject-list philosophy. The previous `"1gr.cz" or "sta-reality"` allow-list silently
# dropped ~63% of anchors once iDNES migrated galleries to its own
# `reality.idnes.cz/file/thumbnail/{id}` service. Video anchors are deliberately NOT
# filtered here — media.split_media_rows owns the image/video split for every portal.
# A lazy-load placeholder is the dangerous one: it is byte-identical across every
# listing, so ingesting it collides at identical pHash / CLIP cosine 1.0, and rule #15
# auto-merges non-byt listings at >=0.98 — a property-graph corruption, not a display bug.
_IMG_DENY_TOKENS: tuple[str, ...] = (
    "my.matterport.com",  # 3D tour; passes media.is_image_url but is not a photo
    "no-image-gallery",  # the lazy-load placeholder itself
    "/ui/image/",  # static site chrome (icons, logos)
)
# `?profile=` is LOAD-BEARING on the first-party host: the path is extension-less and
# the bare URL 404s, so the old `href.split("?")[0]` would now store dead links. Strip
# only the tracking param.
_IMG_DROP_PARAMS: frozenset[str] = frozenset({"gt"})


def _clean_image_url(href: str) -> str:
    base, sep, query = href.partition("?")
    if not sep:
        return base
    kept = [p for p in query.split("&") if p and p.split("=", 1)[0] not in _IMG_DROP_PARAMS]
    return f"{base}?{'&'.join(kept)}" if kept else base


def _gallery_urls(tree: HTMLParser) -> list[str]:
    """Ordered, deduped media URLs from the detail gallery.

    Video anchors pass through on purpose — media.split_media_rows owns the
    image/video split for every portal (rule #21).
    """
    urls: list[str] = []
    seen: set[str] = set()
    for a in tree.css('a[data-fancybox="images"]'):
        href = a.attributes.get("href")
        if not href:
            continue
        if any(token in href.lower() for token in _IMG_DENY_TOKENS):
            continue
        href = _clean_image_url(href)
        if href not in seen:
            seen.add(href)
            urls.append(href)
    return urls


def areas_from_params(
    params: Mapping[str, str | None],
    *,
    title: str | None,
    category_main: str | None,
) -> PortalAreas:
    """idnes's area slots — the KEYS are the contract's, this owns the measure.

    Keys are the lowercased spec-`<dl>` labels `_detail_params` produces, which is also how
    `parse_detail` stores them in `raw_json['params']`: the live parse reads this off the
    page, and `scripts/reparse.py` replays `parse_detail` over the stored page. Two copies of a key order is the same defect as two copies of the number grammar,
    one level up (rule 21).

    `usable_area` IS THE "UŽITNÁ PLOCHA" CELL AND NOTHING ELSE (W21): a page stating only
    "Podlahová plocha" or a bare "Plocha" must not write that number into the column every
    consumer reads as the užitná measure. Neither label reaches the headline any more
    either — idnes emits neither on any row of the checked-in census or of the newest 2,500
    stored rows, so those two slots were unreachable and the contract drops them (gate A1).

    "Plocha pozemku" is the parcel: it reaches the resolver as `plot` (before W17 it went
    only to `estate_area`, so 5,292 land rows whose title states no area carried no headline
    at all) and stays in `estate_area` beside the dwelling's own area.

    No private clamps any more: `derive_headline_area` declines a headline at or beyond
    `MAX_AREA_M2` and `PortalAreas` bounds the three side columns at `MAX_SIDE_AREA_M2` —
    both BEFORE the content hash, which is the whole point (rules 2/8).
    """
    usable_text, plot_text = source_values(SOURCE, "area_m2", params)
    usable = parse_area_text(usable_text)
    estate_area = parse_area_text(plot_text)
    area_m2, area_basis = derive_headline_area(
        category_main=category_main,
        usable=usable,
        plot=estate_area,
        fallback=parse_area_text(title),
    )
    return PortalAreas(
        area_m2=area_m2, area_basis=area_basis,
        usable_area=usable, estate_area=estate_area,
        garden_area=parse_area_text(source_value(SOURCE, "garden_area", params)),
    )


def parse_detail(
    html: str,
    *,
    source_url: str,
    category_main: str | None,
    category_type: str | None,
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
    price_text = _strip_mortgage_cta(_text(price_node) or _text(params.get("cena")))
    price_czk, price_unit = _parse_price(price_text, category_type)

    locality = _text(tree.css_first(".b-detail__info"))
    lat, lon, coord_provenance = _resolve_coords(html)

    # The spec cells as TEXT — the shape `raw_json['params']` stores and the shape
    # `areas_from_params` reads, so the live parse and the heal read one map.
    params_text = {k: _text(v) for k, v in params.items()}
    if params_text.get("cena"):
        params_text["cena"] = _strip_mortgage_cta(params_text["cena"])

    areas = areas_from_params(params_text, title=title, category_main=category_main)

    # Amenities: each row is a check icon OR free text (size / orientation /
    # parking kind), so everything goes through _truthy_field. idnes has no
    # standalone "Garáž" row — the garage signal lives in the "Parkování"
    # value ("garáž , parkování na ulici") and the icon-only "Dvojgaráž" row.
    read = partial(source_value, SOURCE, params=params_text)
    terrace = _truthy_field(source_value(SOURCE, "terrace", params))
    parking_field, lots_node = source_values(SOURCE, "has_parking", params)
    parking_text = _text(parking_field)
    parking_lots = _parse_int(_text(lots_node))
    double_garage, garage_parking = source_values(SOURCE, "garage", params)
    garage = vocabulary.any_true(
        _truthy_field(double_garage),
        vocabulary.contains(_text(garage_parking), "garaz"),
    )

    image_urls = _gallery_urls(tree)

    raw: dict[str, Any] = {
        "id": source_id,
        "title": title,
        "price_text": price_text,
        "locality_text": locality,
        "idnes_ref": _text(params.get("číslo zakázky")),
        "image_urls": image_urls,
        "coords": coord_provenance,
        "params": params_text,
        # Broker/agency block for broker intelligence (resolver reads raw_json.broker).
        # Out of the content hash (_HASH_FIELDS is typed columns only), so it never
        # churns snapshots.
        "broker": parse_idnes_broker(html),
    }

    return ScrapedListing(
        source=SOURCE,
        source_id_native=source_id,
        source_url=source_url,
        category_main=category_main,
        category_type=category_type,
        subtype=subtype_from_title(f"{og_title} {title}", category_main),
        price_czk=price_czk,
        price_unit=price_unit,
        area_m2=areas.area_m2,
        area_basis=areas.area_basis,
        usable_area=areas.usable_area,
        disposition=vocabulary.disposition(title),
        locality=locality,
        district=None,
        # Street is the FIRST comma-segment of locality ("Bělehradská, Pardubice
        # - Polabiny"); the shared guard rejects foreign localities (idnes carries
        # ~37%), "Town - Quarter" tails, and "Town, okres X" forms.
        street=street_from_locality(locality, position="first", lat=lat, lon=lon),
        lat=lat,
        lon=lon,
        floor=_parse_floor(read("floor")),
        total_floors=_parse_int(read("total_floors")),
        building_type=vocabulary.canonical("building_type", SOURCE, read("building_type")),
        # A flat labels its condition row "Stav bytu"; a house or a commercial unit
        # labels it "Stav budovy".
        condition=vocabulary.canonical("condition", SOURCE, read("condition")),
        ownership=vocabulary.canonical("ownership", SOURCE, read("ownership")),
        furnished=vocabulary.canonical("furnished", SOURCE, read("furnished")),
        energy_rating=vocabulary.energy_rating(read("energy_rating")),
        # R11: balcony OR loggia. `terasa` used to be a third arm and is its own
        # column — dropping it takes 17,845 terrace-only active rows back to unknown.
        has_balcony=vocabulary.any_true(
            *(_truthy_field(n) for n in source_values(SOURCE, "has_balcony", params))
        ),
        has_lift=_truthy_field(source_value(SOURCE, "has_lift", params)),
        cellar=_truthy_field(source_value(SOURCE, "cellar", params)),
        terrace=terrace,
        garage=garage,
        # R11: a space or right BELONGING to the property. idnes's "Parkování" cell
        # lists the KINDS it has, so `_truthy_field` on it means "some parking is
        # stated" — including "parkování na ulici", which the portal files under the
        # listing's own facilities rather than as a neighbourhood note. Only the text
        # says which kind, so the check/cross icon is consulted only without it — this
        # portal renders the same amenity row both ways.
        has_parking=vocabulary.any_true(
            vocabulary.parking(parking_text) if parking_text is not None
            else _has_check(parking_field),
            garage,
            (parking_lots > 0) if parking_lots is not None else None,
        ),
        parking_lots=parking_lots,
        estate_area=areas.estate_area,
        garden_area=areas.garden_area,
        description=description,
        raw=raw,
    )
