"""HTTP layer for mmreality.cz (portal framework).

M&M Reality is a server-rendered listing site (the estate data is embedded as a
Vue prop on the detail page), so this returns raw HTML for
`scraper.mmreality_parser`. The shared retry/backoff + adaptive throttle
(`RateLimiter` + `penalize()` on 429/403) + `ListingGoneError` on a 404/410 all
live in `scraper.portal_base.BasePortalClient`; this client adds only the HTML
`Accept` header, the mmreality URL builders, and the removed-listing signal (a
redirect off the /nemovitosti/{id}/ detail path).
"""

from __future__ import annotations

import logging

from scraper.portal_base import BasePortalClient, ListingGoneError

LOG = logging.getLogger(__name__)

BASE_URL = "https://www.mmreality.cz"

# Substrings mmreality serves (HTTP 200) for a listing no longer offered.
_GONE_MARKERS: tuple[str, ...] = (
    "nabídka již není aktivní",
    "nemovitost již není v nabídce",
    "tato nabídka byla ukončena",
)


def index_url(page: int | None = None) -> str:
    """The mixed-category listing index. Page 1 is the bare URL; subsequent
    pages are `?page=N` (the `<link rel="next">` carries the literal N)."""
    url = f"{BASE_URL}/nemovitosti/"
    if page is not None and page >= 2:
        url += f"?page={page}"
    return url


def detail_url(id_or_path: str) -> str:
    if id_or_path.startswith("http"):
        return id_or_path
    if id_or_path.startswith("/"):
        return f"{BASE_URL}{id_or_path}"
    return f"{BASE_URL}/nemovitosti/{id_or_path}/"


class MmRealityClient(BasePortalClient):
    ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    # Route every request through the residential proxy in SCRAPER_PROXY_URL.
    # mmreality's Cloudflare edge HARD-403s our datacenter (GitHub-Actions) IP on
    # the first request (verified — the portal ingested 0 listings across 101
    # blocked runs); a residential exit IP returns 200. Same opt-in the sister
    # CF-blocked portal (ceskereality) uses; the shared browser UA is most natural
    # once proxied, so no USER_AGENT override. Unset env = direct IP (logs a warn,
    # then 403s) — a misconfigured deploy fails loudly, never silently green.
    USE_PROXY = True

    def fetch_index(self, page: int | None = None) -> tuple[str, int]:
        response = self._request(index_url(page))
        return response.text, response.status_code

    def fetch_detail(self, id_or_path: str) -> tuple[str, int]:
        url = detail_url(id_or_path)
        response = self._request(url)
        # A removed listing redirects off /nemovitosti/{id}/; after requests
        # follows it the status is 200 but the URL is no longer the detail path.
        final_url = getattr(response, "url", url) or url
        if "/nemovitosti/" not in final_url or final_url.rstrip("/").endswith(
            "/nemovitosti"
        ):
            raise ListingGoneError(url, response.status_code)
        text = response.text
        if any(marker in text.lower() for marker in _GONE_MARKERS):
            raise ListingGoneError(url, response.status_code)
        return text, response.status_code
