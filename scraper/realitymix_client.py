"""HTTP layer for realitymix.cz (portal framework).

realitymix.cz is a server-rendered (nginx) listing site with no public read API
(its XML-RPC endpoint is a vendor-key push/import API for agencies, not a feed),
so this returns raw HTML for `scraper.realitymix_parser`. The shared retry/backoff
+ adaptive throttle (`RateLimiter` + `penalize()` on 429/403) + `ListingGoneError`
on a 404/410 all live in `scraper.portal_base.BasePortalClient`; this client adds
only the HTML `Accept` header, the realitymix URL builders (`/reality/{family}/
{sale}` index with `?stranka=N` paging, `/detail/{obec}/{slug}-{id}.html` detail),
and the removed-listing signals (a redirect off the `.html` detail path, or a
deleted-listing body marker on a 200). It uses the shared browser User-Agent (the
robots.txt allow-lists everyone — `Allow: /` — and the framework default avoids
the bot-UA throttling that bit ceskereality, PRs #637/#638)."""

from __future__ import annotations

import logging

from scraper.portal_base import BasePortalClient, ListingGoneError

LOG = logging.getLogger(__name__)

BASE_URL = "https://realitymix.cz"

# Substrings realitymix serves (HTTP 200) for a listing no longer offered.
_GONE_MARKERS: tuple[str, ...] = (
    "nabídka nebyla nalezena",
    "nabídka již neexistuje",
    "tato nabídka již není",
    "inzerát byl odstraněn",
    "tato stránka neexistuje",
)


def index_url(sale_type: str, category: str, page: int | None = None) -> str:
    """Build a search URL: /reality/{family}/{sale}. realitymix paging is
    `?stranka=N` (the bare URL is page 1), so page<=1/None -> bare first page."""
    url = f"{BASE_URL}/reality/{category}/{sale_type}"
    if page is not None and page >= 2:
        url += f"?stranka={page}"
    return url


def detail_url(path_or_url: str) -> str:
    if path_or_url.startswith("http"):
        return path_or_url
    return f"{BASE_URL}{path_or_url}"


class RealitymixClient(BasePortalClient):
    ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    def fetch_index(
        self, sale_type: str, category: str, page: int | None = None
    ) -> tuple[str, int]:
        response = self._request(index_url(sale_type, category, page))
        return response.text, response.status_code

    def fetch_detail(self, path_or_url: str) -> tuple[str, int]:
        url = detail_url(path_or_url)
        response = self._request(url)
        # A removed listing redirects off its .html detail page; after requests
        # follows it the status is 200 but the URL is no longer a listing page.
        final_url = getattr(response, "url", url) or url
        if "/detail/" not in final_url:
            raise ListingGoneError(url, response.status_code)
        text = response.text
        if any(marker in text.lower() for marker in _GONE_MARKERS):
            raise ListingGoneError(url, response.status_code)
        return text, response.status_code
