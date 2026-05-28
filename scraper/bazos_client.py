"""HTTP layer for reality.bazos.cz (multi-portal slice 3b).

Bazos is a server-rendered classifieds site (no JSON API), so this returns
raw HTML for `scraper.bazos_parser` to parse. The shared retry/backoff +
adaptive throttle (`RateLimiter` + `penalize()` on 429/403) + `ListingGoneError`
on a 404/410 all live in `scraper.portal_base.BasePortalClient`; this client
adds only the HTML `Accept` header, the bazos URL builders, and the
deleted-listing body markers.
"""

from __future__ import annotations

import logging

import requests

from scraper.portal_base import BasePortalClient, ListingGoneError

LOG = logging.getLogger(__name__)

BASE_URL = "https://reality.bazos.cz"

# Substrings bazos serves (HTTP 200) for a listing that has been removed.
_GONE_MARKERS: tuple[str, ...] = (
    "inzerát byl smazán",
    "inzerát neexistuje",
    "inzerát již neexistuje",
)


def index_url(
    sale_type: str,
    category: str,
    offset: int = 0,
    *,
    locality: str | None = None,
    radius_km: int | None = None,
) -> str:
    url = f"{BASE_URL}/{sale_type}/{category}/"
    if offset:
        url += f"{offset}/"
    params: list[str] = []
    if locality:
        params.append(f"hlokalita={requests.utils.quote(locality)}")
    if radius_km is not None:
        params.append(f"humkreis={radius_km}")
    if params:
        url += "?" + "&".join(params)
    return url


def detail_url(path_or_url: str) -> str:
    if path_or_url.startswith("http"):
        return path_or_url
    return f"{BASE_URL}{path_or_url}"


class BazosClient(BasePortalClient):
    ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    def fetch_index(
        self,
        sale_type: str,
        category: str,
        offset: int = 0,
        *,
        locality: str | None = None,
        radius_km: int | None = None,
    ) -> tuple[str, int]:
        url = index_url(
            sale_type, category, offset, locality=locality, radius_km=radius_km
        )
        return self._get_html(url)

    def fetch_detail(self, path_or_url: str) -> tuple[str, int]:
        return self._get_html(detail_url(path_or_url))

    def _get_html(self, url: str) -> tuple[str, int]:
        # _request paces (shared limiter or request_delay_s self-throttle),
        # retries transient statuses, and raises ListingGoneError on 404/410.
        # A 200 body bazos serves for a removed listing is caught here.
        response = self._request(url)
        text = response.text
        if any(marker in text.lower() for marker in _GONE_MARKERS):
            raise ListingGoneError(url, response.status_code)
        return text, response.status_code
