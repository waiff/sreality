"""HTTP layer for ceskereality.cz (portal framework).

ceskereality.cz is a server-rendered listing site (no public JSON API — its
/real-estate/ajax/* endpoints are filter/autocomplete helpers, not a listings
feed), so this returns raw HTML for `scraper.ceskereality_parser`. The shared
retry/backoff + adaptive throttle (`RateLimiter` + `penalize()` on 429/403) +
`ListingGoneError` on a 404/410 all live in `scraper.portal_base.BasePortalClient`;
this client adds only the HTML `Accept` header, an honest identifying
`User-Agent` (ceskereality's robots.txt allow-lists named bots; we crawl slowly
and identify ourselves rather than impersonate one), the ceskereality URL
builders (`?strana=N` paging), and the removed-listing signals (a redirect off
the `.html` detail path, or a deleted-listing body marker on a 200).
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


def index_url(sale_type: str, category: str, page: int | None = None) -> str:
    """Build a search URL. ceskereality paging is `?strana=N`: the bare URL is
    page 1, and the pager's "next" arrow carries the literal N to use. So
    `page=None` -> the bare first page; otherwise `?strana={page}`."""
    url = f"{BASE_URL}/{sale_type}/{category}/"
    if page is not None and page >= 2:
        url += f"?strana={page}"
    return url


def detail_url(path_or_url: str) -> str:
    if path_or_url.startswith("http"):
        return path_or_url
    return f"{BASE_URL}{path_or_url}"


class CeskerealityClient(BasePortalClient):
    ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    # Honest, identifying UA — we crawl politely + slowly rather than pretend to
    # be a browser. If the site's anti-bot edge blocks this (the mmreality
    # posture), the validation run surfaces it and the operator decides.
    USER_AGENT = "LimenRealityBot/1.0 (+https://www.ceskereality.cz/robots.txt)"

    def fetch_index(
        self, sale_type: str, category: str, page: int | None = None
    ) -> tuple[str, int]:
        response = self._request(index_url(sale_type, category, page))
        return response.text, response.status_code

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
