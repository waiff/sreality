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
  - **The region/facet search URLs**: anonymous search hard-caps at 12 pages
    (~240 results; `?strana=13` = 404), so the walk slices each category by the 7
    REGIONAL SUBDOMAINS × a per-category disposition/type facet to keep every query
    under the cap (`search_url`). Detail pages are always fetched on the canonical
    www host.
"""

from __future__ import annotations

import logging

from scraper.portal_base import BasePortalClient, ListingGoneError

LOG = logging.getLogger(__name__)

BASE_URL = "https://www.ceskereality.cz"

# The 7 regional subdomains that PARTITION the country (each lists only its
# region's inventory, so they're disjoint). Walking per-region keeps each query
# ~7x smaller — the first axis of the cap-beating split.
REGION_HOSTS: tuple[str, ...] = (
    "stredo.ceskereality.cz",
    "jiho.ceskereality.cz",
    "severo.ceskereality.cz",
    "vychodo.ceskereality.cz",
    "zapado.ceskereality.cz",
    "jiho.moravskereality.cz",
    "severo.moravskereality.cz",
)

# Per-category disposition/type facet slugs (the 2nd split axis). Walking
# {region}/{sale}/{category}/{slug}/ keeps each query under the 12-page cap.
# Empty = no facet (walk the bare category per region). Hand-curated from the
# site's "Dispozice"/"Druh" filters; a missing/new slug just means that slice
# isn't split finer (it caps at 240, surfaced as an incomplete-slice count).
SUB_SLUGS: dict[str, tuple[str, ...]] = {
    "byty": ("byty-1-kk", "byty-1-1", "byty-2-kk", "byty-2-1", "byty-3-kk",
             "byty-3-1", "byty-4-kk", "byty-4-1", "byty-5-kk", "byty-5-1-vetsi"),
    "rodinne-domy": ("rodinne-domy", "vily", "chalupy", "chaty", "cinzovni-domy",
                     "dvougeneracni-domy", "historicke-objekty",
                     "zemedelske-usedlosti", "na-klic", "ostatni-rd"),
    "chaty-chalupy": (),
    "pozemky": ("stavebni-parcely", "zahrady", "lesy", "louky", "orna-puda",
                "vodni-plochy", "komercni", "ostatni-pozemky"),
    "komercni-prostory": ("kancelare", "obchody", "restaurace", "sklady", "hotely",
                          "ordinace", "apartman", "vyrobni-objekty",
                          "zemedelske-objekty", "ostatni-komercni-prostory"),
    "ostatni": ("garaze", "garazova-stani", "pudni-prostor", "vinny-sklep",
                "ostatni-ostatni"),
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
    host: str = "www.ceskereality.cz",
    sub_slug: str | None = None,
    page: int | None = None,
) -> str:
    """A search-results URL: `https://{host}/{sale}/{category}[/{sub_slug}]/[?strana=N]`.
    `sub_slug` is a disposition/type facet (e.g. `byty-3-1`, `vily`). Page 1 is the
    bare URL; `?strana=N` for N>=2."""
    path = f"/{sale_type}/{category}/"
    if sub_slug:
        path += f"{sub_slug}/"
    url = f"https://{host}{path}"
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
