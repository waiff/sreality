"""HTTP layer for reality.idnes.cz (portal framework).

iDNES Reality is a server-rendered listing site (no public JSON API), so this
returns raw HTML for `scraper.idnes_parser`. The shared retry/backoff + adaptive
throttle (`RateLimiter` + `penalize()` on 429/403) + `ListingGoneError` on a
404/410 all live in `scraper.portal_base.BasePortalClient`; this client adds only
the HTML `Accept` header, the idnes URL builders, and the removed-listing signals
(a redirect off the /detail/ path, or a deleted-listing body marker on a 200).
"""

from __future__ import annotations

import logging
import urllib.parse

from scraper.portal_base import BasePortalClient, ListingGoneError

LOG = logging.getLogger(__name__)

BASE_URL = "https://reality.idnes.cz"

# Substrings idnes serves (HTTP 200) for a listing that is no longer offered.
# "nabídka již není aktivní" is the verified live marker: idnes keeps the listing
# title in <h1>/og:title but replaces the detail body with a "Nabídka již není
# aktivní" / "evidujeme nabídku jako neaktivní" stub. The rest are tolerated
# variants kept as a defensive net.
_GONE_MARKERS: tuple[str, ...] = (
    "nabídka již není aktivní",
    "evidujeme nabídku jako neaktivní",
    "nemovitost již není v nabídce",
    "inzerát již není aktivní",
    "tato nabídka již není aktuální",
)


def index_url(
    sale_type: str,
    category: str,
    page: int | None = None,
    *,
    locality: str | None = None,
) -> str:
    """Build a search URL. idnes paging is offset-style: the bare URL is page 1,
    and `?page=N` is the (N+1)-th page (the pager's "next" link carries the literal
    N to use). `page=None` -> the bare first page; otherwise `?page={page}`."""
    url = f"{BASE_URL}/s/{sale_type}/{category}/"
    if locality:
        url += f"{urllib.parse.quote(locality.strip('/'))}/"
    if page is not None and page >= 1:
        url += f"?page={page}"
    return url


def detail_url(path_or_url: str) -> str:
    if path_or_url.startswith("http"):
        return path_or_url
    return f"{BASE_URL}{path_or_url}"


class IdnesClient(BasePortalClient):
    ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    def fetch_index(
        self,
        sale_type: str,
        category: str,
        page: int | None = None,
        *,
        locality: str | None = None,
    ) -> tuple[str, int]:
        response = self._request(index_url(sale_type, category, page, locality=locality))
        return response.text, response.status_code

    def fetch_detail(self, path_or_url: str) -> tuple[str, int]:
        url = detail_url(path_or_url)
        response = self._request(url)
        # idnes 302-redirects a removed listing to the search results page; after
        # requests follows it the status is 200 but the URL is off /detail/.
        final_url = getattr(response, "url", url) or url
        if "/detail/" not in final_url:
            raise ListingGoneError(url, response.status_code)
        text = response.text
        if any(marker in text.lower() for marker in _GONE_MARKERS):
            raise ListingGoneError(url, response.status_code)
        return text, response.status_code
