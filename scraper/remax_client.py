"""HTTP layer for remax-czech.cz (portal framework).

RE/MAX publishes its franchise catalogue as a server-rendered listing site (no
JSON API), so this returns raw HTML for `scraper.remax_parser`. The shared
retry/backoff + adaptive throttle (`RateLimiter` + `penalize()` on 429/403) +
`ListingGoneError` on a 404/410 all live in `scraper.portal_base.BasePortalClient`;
this client adds only the HTML `Accept` header, the remax URL builders (a single
mixed search index paged `?sale={1|2}&stranka=N`, a `/reality/detail/{id}/{slug}`
detail), and the removed-listing markers (a redirect off the detail path, or a
"nenalezena"/"již není" body).
"""

from __future__ import annotations

import logging

from scraper.portal_base import BasePortalClient, ListingGoneError

LOG = logging.getLogger(__name__)

BASE_URL = "https://www.remax-czech.cz"
SEARCH_PATH = "/reality/vyhledavani/"

# Substrings remax serves (HTTP 200) for a listing no longer offered.
_GONE_MARKERS: tuple[str, ...] = (
    "nemovitost nebyla nalezena",
    "stránka nebyla nalezena",
    "nabídka již není aktivní",
    "tato nemovitost již není v nabídce",
)


def index_url(sale: int | None = None, stranka: int | None = None) -> str:
    """The mixed-category search index. `sale` is remax's offer-type flag (1 =
    prodej / sale, 2 = pronájem / rent); `stranka` is the 1-based page (omitted
    for page 1, which is the bare filtered URL)."""
    params: list[str] = []
    if sale is not None:
        params.append(f"sale={sale}")
    if stranka is not None and stranka >= 2:
        params.append(f"stranka={stranka}")
    return f"{BASE_URL}{SEARCH_PATH}" + (f"?{'&'.join(params)}" if params else "")


def detail_url(path_or_url: str) -> str:
    if path_or_url.startswith("http"):
        return path_or_url
    if path_or_url.startswith("/"):
        return f"{BASE_URL}{path_or_url}"
    return f"{BASE_URL}/reality/detail/{path_or_url}/"


class RemaxClient(BasePortalClient):
    ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    def fetch_index(
        self, sale: int | None = None, stranka: int | None = None
    ) -> tuple[str, int]:
        response = self._request(index_url(sale, stranka))
        return response.text, response.status_code

    def fetch_detail(self, path_or_url: str) -> tuple[str, int]:
        url = detail_url(path_or_url)
        response = self._request(url)
        # A removed listing redirects off /reality/detail/{id}/; after requests
        # follows it the status is 200 but the URL no longer carries the id.
        final_url = getattr(response, "url", url) or url
        if "/reality/detail/" not in final_url:
            raise ListingGoneError(url, response.status_code)
        text = response.text
        if any(marker in text.lower() for marker in _GONE_MARKERS):
            raise ListingGoneError(url, response.status_code)
        return text, response.status_code
