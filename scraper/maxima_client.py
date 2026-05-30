"""HTTP layer for nemovitosti.maxima.cz (portal framework).

Maxima publishes its agency catalogue as a server-rendered WordPress listing site
(no JSON API), so this returns raw HTML for `scraper.maxima_parser`. The shared
retry/backoff + adaptive throttle (`RateLimiter` + `penalize()` on 429/403) +
`ListingGoneError` on a 404/410 all live in `scraper.portal_base.BasePortalClient`;
this client adds only the HTML `Accept` header and the maxima URL builders (a single
mixed index paginated `/page/N/`, a `/nemovitosti/{id}/` detail).
"""

from __future__ import annotations

import logging

from scraper.portal_base import BasePortalClient

LOG = logging.getLogger(__name__)

BASE_URL = "https://nemovitosti.maxima.cz"


def index_url(page: int | None = None, *, af: int | None = None) -> str:
    """Build a catalogue page URL. Page 1 is the bare base URL; page N>=2 is
    `/page/N/`. `af` is maxima's agenda flag (1 = sale, the default view; 2 =
    rent — the buy/rent toggle); it is appended as `?af=N` for the rent agenda
    (af=1/None reproduce the bare sale URL the catalogue serves by default)."""
    base = f"{BASE_URL}/page/{page}/" if page is not None and page >= 2 else f"{BASE_URL}/"
    if af is not None and af != 1:
        return f"{base}?af={af}"
    return base


def detail_url(path_or_url: str) -> str:
    if path_or_url.startswith("http"):
        return path_or_url
    return f"{BASE_URL}{path_or_url}"


class MaximaClient(BasePortalClient):
    ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    def fetch_index(self, page: int | None = None, *, af: int | None = None) -> tuple[str, int]:
        response = self._request(index_url(page, af=af))
        return response.text, response.status_code

    def fetch_detail(self, path_or_url: str) -> tuple[str, int]:
        response = self._request(detail_url(path_or_url))
        return response.text, response.status_code
