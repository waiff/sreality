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
  - **The facet search URLs**: anonymous search hard-caps at 12 pages (~240 results;
    `?strana=13` = 404), so the walk slices each category by the COMPLETE okres
    (district) partition × a per-category disposition facet (`search_url` takes a
    `sub_slug`) to keep every query under the cap. The okres list is the
    cap-beater's geographic axis (`ceskereality_main`); both search + detail are
    fetched on the canonical www host.
"""

from __future__ import annotations

import logging

from scraper.portal_base import BasePortalClient, ListingGoneError

LOG = logging.getLogger(__name__)

BASE_URL = "https://www.ceskereality.cz"

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
