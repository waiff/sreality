"""The canonical sreality detail URL — the ONE assembly site for sreality's `listings.source_url`.

    https://www.sreality.cz/detail/{type}/{main}/{sub}/{locality}/{hash_id}

sreality validates the type / main / sub segments STRICTLY (any mismatch is a 404) and is
lenient on the locality segment only (any value 301-redirects to the canonical one). So the
codebooks below are 404-critical, and the locality rule is about canonicality, not reachability.

The sub-category vocabulary is sreality's own, recovered from its sitemap (107,766 detail URLs
-> 140 type/main/sub triples -> one id per pair -> the API's `category_sub_cb.value`) and
verified live per code on 2026-09-10/11. It is NOT derivable from a display label: 37 "Rodinný
dům" is `rodinny`, 28 "Obchodní prostory" is `obchodni-prostor`, 32 "Ostatní" is
`ostatni-komercni-prostory`, 36 "Ostatní" is `jine-nemovitosti`. An unknown code yields NO URL
and a counted reason — never a guess. Every code outside the table is rejected by sreality's
own search API (HTTP 422), so only a HISTORIC row can carry one.

One codebook, one assembly rule (`canonical_url`), ONE input adapter: `from_payload` for
ingest (sreality's own `*_seo_name` values pass through unslugged). The column adapter that
re-derived a URL from display text died with the columns it read (migration 508); the stored
`listings.source_url` is the fact now, and `critical_segments` below reads one back apart.
Nothing here raises: every non-derivation is a `Declined` reason the caller counts.

DISPLAY ONLY. sreality's server-rendered detail page 302s into a login.seznam.cz autologin
chain and then an infinite `cwtkn=` redirect loop (contracts/portals/sreality.yaml). Nothing
may FETCH a sreality source_url — the scraper reaches sreality by id through /api/v1/estates;
`scraper.db.detail_ref` is the guard. Design: docs/design/portal-listing-url.md.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

BASE_URL = "https://www.sreality.cz/detail"

# category_type_cb.value -> URL segment. NOT the stored listings.category_type vocabulary:
# scraper.parser stores 'drazba' / 'podil', and both 404 in a URL (sreality's plurals).
TYPE_SLUG: dict[int, str] = {1: "prodej", 2: "pronajem", 3: "drazby", 4: "podily"}

# category_main_cb.value -> URL segment; identical to the stored listings.category_main.
MAIN_SLUG: dict[int, str] = {1: "byt", 2: "dum", 3: "pozemek", 4: "komercni", 5: "ostatni"}

# CLOSED codebook: category_sub_cb.value -> (main segment, sub segment). Carrying the main
# alongside the slug makes a reused code visible: a row whose category_main disagrees with
# the code's main is declined, never linked. Keys are disjoint across mains by sreality's
# own numbering. The '+' in flat dispositions is LITERAL in the URL ('%2B' also resolves,
# '%20' 404s) — never run a stored URL through a form encoder.
SUB_SLUG: dict[int, tuple[str, str]] = {
    2: ("byt", "1+kk"), 3: ("byt", "1+1"), 4: ("byt", "2+kk"), 5: ("byt", "2+1"),
    6: ("byt", "3+kk"), 7: ("byt", "3+1"), 8: ("byt", "4+kk"), 9: ("byt", "4+1"),
    10: ("byt", "5+kk"), 11: ("byt", "5+1"), 12: ("byt", "6-a-vice"),
    16: ("byt", "atypicky"), 47: ("byt", "pokoj"),
    33: ("dum", "chata"), 35: ("dum", "pamatka"), 37: ("dum", "rodinny"),
    39: ("dum", "vila"), 40: ("dum", "na-klic"), 43: ("dum", "chalupa"),
    44: ("dum", "zemedelska-usedlost"), 54: ("dum", "vicegeneracni-dum"),
    25: ("komercni", "kancelare"), 26: ("komercni", "sklad"),
    27: ("komercni", "vyrobni-prostor"), 28: ("komercni", "obchodni-prostor"),
    29: ("komercni", "ubytovani"), 30: ("komercni", "restaurace"),
    31: ("komercni", "zemedelsky"), 32: ("komercni", "ostatni-komercni-prostory"),
    38: ("komercni", "cinzovni-dum"), 49: ("komercni", "virtualni-kancelar"),
    56: ("komercni", "ordinace"), 57: ("komercni", "apartman"),
    34: ("ostatni", "garaz"), 36: ("ostatni", "jine-nemovitosti"),
    50: ("ostatni", "vinny-sklep"), 51: ("ostatni", "pudni-prostor"),
    52: ("ostatni", "garazove-stani"), 53: ("ostatni", "mobilni-domek"),
    18: ("pozemek", "komercni"), 19: ("pozemek", "bydleni"), 20: ("pozemek", "pole"),
    21: ("pozemek", "les"), 22: ("pozemek", "louka"), 23: ("pozemek", "zahrada"),
    24: ("pozemek", "ostatni-pozemky"), 46: ("pozemek", "rybnik"),
    48: ("pozemek", "sady-vinice"),
}


class Declined(str, Enum):
    """Why no URL was assembled. Closed, so every consumer can count by reason."""

    ID_MISSING = "id_missing"
    TYPE_NULL = "type_null"
    TYPE_UNMAPPED = "type_unmapped"
    MAIN_NULL = "main_null"
    MAIN_UNMAPPED = "main_unmapped"
    SUB_NULL = "sub_cb_null"
    SUB_UNKNOWN = "sub_cb_unknown"
    SUB_MAIN_MISMATCH = "sub_cb_main_mismatch"
    LOCALITY_NULL = "locality_null"


Derived = tuple[str | None, Declined | None]


def canonical_url(
    *,
    type_slug: str | None,
    main_slug: str | None,
    sub_cb: int | None,
    city_slug: str | None,
    citypart_slug: str | None,
    street_slug: str | None,
    listing_id: int | None,
) -> Derived:
    """THE assembly. Takes already-slugged parts; never slugifies, never guesses, never raises.

    The locality segment is sreality's own canonical shape, verified byte-identical against
    its 301 target: citypart NULL -> the city REPEATS (even when the two are equal); street
    NULL -> the TRAILING HYPHEN stays (`prestavlky-prestavlky-`).
    """
    if not isinstance(listing_id, int) or isinstance(listing_id, bool) or listing_id <= 0:
        return None, Declined.ID_MISSING
    if not type_slug:
        return None, Declined.TYPE_NULL
    if type_slug not in TYPE_SLUG.values():
        return None, Declined.TYPE_UNMAPPED
    if not main_slug:
        return None, Declined.MAIN_NULL
    if main_slug not in MAIN_SLUG.values():
        return None, Declined.MAIN_UNMAPPED
    if sub_cb is None:
        return None, Declined.SUB_NULL
    pair = SUB_SLUG.get(sub_cb)
    if pair is None:
        return None, Declined.SUB_UNKNOWN
    sub_main, sub = pair
    if sub_main != main_slug:
        return None, Declined.SUB_MAIN_MISMATCH
    if not city_slug:
        return None, Declined.LOCALITY_NULL
    locality = f"{city_slug}-{citypart_slug or city_slug}-{street_slug or ''}"
    return f"{BASE_URL}/{type_slug}/{main_slug}/{sub}/{locality}/{listing_id}", None


def from_payload(raw: dict[str, Any]) -> Derived:
    """Adapter 1 — ingest. sreality's own `*_seo_name` values pass through unslugged.

    Total over every payload shape the parser sees (v1 detail, v1 index, the legacy v2
    shape without `category_*_cb` or `*_seo_name`): a missing part declines, nothing raises.
    """
    if not isinstance(raw, dict):
        return None, Declined.ID_MISSING
    loc = raw.get("locality")
    if not isinstance(loc, dict):
        loc = {}
    type_cb = _cb_value(raw.get("category_type_cb"))
    main_cb = _cb_value(raw.get("category_main_cb"))
    return canonical_url(
        type_slug=TYPE_SLUG.get(type_cb) if type_cb is not None else None,
        main_slug=MAIN_SLUG.get(main_cb) if main_cb is not None else None,
        sub_cb=_cb_value(raw.get("category_sub_cb")),
        city_slug=_seo(loc.get("city_seo_name")),
        citypart_slug=_seo(loc.get("citypart_seo_name")),
        street_slug=_seo(loc.get("street_seo_name")),
        listing_id=_int_or_none(raw.get("hash_id")),
    )


def critical_segments(url: str | None) -> tuple[str, str, str, str] | None:
    """(type, main, sub, id) of a stored sreality URL — the segments sreality 404s on. The
    locality segment is deliberately NOT part of it: sreality 301s any locality to the
    canonical, so two URLs differing only there name the same page."""
    if not url or not url.startswith(BASE_URL + "/"):
        return None
    parts = url[len(BASE_URL) + 1:].split("/")
    if len(parts) != 5:
        return None
    return parts[0], parts[1], parts[2], parts[4]


def _cb_value(obj: Any) -> int | None:
    """Integer enum code from a {name, value} object; 0 ('not specified') -> None."""
    if isinstance(obj, dict):
        v = obj.get("value")
        if isinstance(v, int) and not isinstance(v, bool) and v != 0:
            return v
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _seo(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None
