"""HTTP layer for ceskereality.cz (portal framework).

ceskereality.cz is a server-rendered listing site (no public JSON API — its
/real-estate/ajax/* endpoints are filter/autocomplete helpers, not a listings
feed), so this returns raw HTML for `scraper.ceskereality_parser`. The shared
retry/backoff + adaptive throttle (`RateLimiter` + `penalize()` on 429/403) +
`ListingGoneError` on a 404/410 all live in `scraper.portal_base.BasePortalClient`.

Two ceskereality specifics live here:
  - **Residential egress** (`USE_PROXY`): ceskereality's Cloudflare edge throttles
    our datacenter (GitHub-Actions) IP into degraded pages, so every request routes
    through the residential proxy in `SCRAPER_PROXY_URL`. With a residential exit IP
    we use the shared BROWSER User-Agent (most natural; the honest-bot UA both got
    throttled and is moot once we're proxied).
  - **The kraj search URLs**: the 12-page cap is NOT site-wide — it applies to
    UNFILTERED category URLs only (`/prodej/byty/?strana=13` 404s, but
    `/prodej/byty/stredocesky-kraj/?strana=13` is fine and that slice pages to 50
    of 50). A *filtered* URL caps at 99 pages (1,980 rows; `?strana=100` = 404).
    So the walk partitions each category by the 14 declared `KRAJ_SLUGS`
    (`search_url`), with an optional subtype axis under a kraj that would exceed
    99 pages. Detail pages are always fetched on www.
"""

from __future__ import annotations

import logging

from scraper.portal_base import BasePortalClient, ListingGoneError

LOG = logging.getLogger(__name__)

BASE_URL = "https://www.ceskereality.cz"

# The 14 modern kraje — a ROW-LEVEL partition, proved by ID enumeration
# (2026-08-27): a 48-row category yielded 48 ids across these 14 with zero
# overlap and zero gap, and every category's kraj sum matched its national
# declared total inside the live drift band. DECLARED, never scraped: the
# rendered facet list is a top-10-by-popularity list, not a partition (whole
# okresy never appear in it).
# TRAP: 'kraj-vysocina' is irregular ('vysocina-kraj' and 'vysocina' both 404).
# TRAP: 'praha' 301-redirects to 'praha-hlavni-mesto' (query string preserved);
#       requests follows it, so the slice is correct — it just costs a hop.
# TRAP: the site also serves 7 LEGACY regions (severoceský, východoceský, …)
#       and 7 macro-region SUBDOMAINS. Mixing vocabularies double-counts.
# TRAP: /zahranicni/ is a separate tree outside the national CZ totals
#       (12,942 flats vs 8,855 national). Never walk it from here.
KRAJ_SLUGS: tuple[str, ...] = (
    "praha", "stredocesky-kraj", "jihocesky-kraj", "plzensky-kraj",
    "karlovarsky-kraj", "ustecky-kraj", "liberecky-kraj",
    "kralovehradecky-kraj", "pardubicky-kraj", "kraj-vysocina",
    "jihomoravsky-kraj", "olomoucky-kraj", "moravskoslezsky-kraj",
    "zlinsky-kraj",
)

# The second axis, used ONLY when a kraj slice would exceed the 99-page cap.
# Declared per category, never scraped: a kraj page's facet block OMITS
# zero-count subtypes (`ostatni-rd` is absent from stredocesky), so walking the
# rendered list would silently drop a subtype the moment it emptied. Measured
# 2026-08-27: these 10 slugs sum to 2,312 in stredocesky-kraj — EXACTLY the
# kraj total, so subtype is a true partition within a kraj. Categories absent
# from this map fall back to the page's rendered facets, and the descent
# self-verifies its children's declared sum against the parent either way.
SUBTYPE_SLUGS: dict[str, tuple[str, ...]] = {
    "rodinne-domy": (
        "rodinne-domy", "chaty", "chalupy", "cinzovni-domy",
        "zemedelske-usedlosti", "vily", "na-klic", "dvougeneracni-domy",
        "historicke-objekty", "ostatni-rd",
    ),
}

# Substrings ceskereality serves (HTTP 200) for a listing no longer offered.
_GONE_MARKERS: tuple[str, ...] = (
    "nemovitost nebyla nalezena",
    "inzerát byl odstraněn",
    "nabídka již není aktivní",
    "tato nabídka již není",
)


def search_url(
    sale_type: str,
    category: str,
    *,
    kraj: str | None = None,
    subtype: str | None = None,
    page: int | None = None,
) -> str:
    """`https://www.ceskereality.cz/{sale}/{cat}[/{subtype}][/{kraj}]/[?strana=N]`.

    Subtype precedes kraj — live-verified 2026-08-27:
    /prodej/byty/byty-2-1/stredocesky-kraj/ is correctly filtered (105 results).
    Page 1 is the bare URL; `?strana=N` for N>=2."""
    path = f"/{sale_type}/{category}/"
    if subtype:
        path += f"{subtype}/"
    if kraj:
        path += f"{kraj}/"
    url = f"{BASE_URL}{path}"
    if page is not None and page >= 2:
        url += f"?strana={page}"
    return url


def index_url(sale_type: str, category: str, page: int | None = None) -> str:
    """Back-compat nationwide (www) search URL."""
    return search_url(sale_type, category, page=page)


def detail_url(path_or_url: str) -> str:
    if path_or_url.startswith("http"):
        return path_or_url
    return f"{BASE_URL}{path_or_url}"


class CeskerealityClient(BasePortalClient):
    ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    # Route through the residential proxy (SCRAPER_PROXY_URL) — the site throttles
    # our datacenter IP. With a residential exit, the shared browser UA is most
    # natural, so no USER_AGENT override.
    USE_PROXY = True

    def fetch_search(self, url: str) -> tuple[str, int]:
        """Fetch one search-results page (any region host / facet path / page)."""
        response = self._request(url)
        return response.text, response.status_code

    def fetch_index(
        self, sale_type: str, category: str, page: int | None = None
    ) -> tuple[str, int]:
        return self.fetch_search(index_url(sale_type, category, page))

    def fetch_detail(self, path_or_url: str) -> tuple[str, int]:
        url = detail_url(path_or_url)
        response = self._request(url)
        # A removed listing redirects off its .html detail page (to the category
        # results); after requests follows it the status is 200 but the URL is
        # no longer a listing page.
        final_url = getattr(response, "url", url) or url
        if ".html" not in final_url:
            raise ListingGoneError(url, response.status_code)
        text = response.text
        if any(marker in text.lower() for marker in _GONE_MARKERS):
            raise ListingGoneError(url, response.status_code)
        return text, response.status_code
