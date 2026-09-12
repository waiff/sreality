"""HTTP layer for reality.bazos.cz (multi-portal slice 3b).

Bazos is a server-rendered classifieds site (no JSON API), so this returns
raw HTML for `scraper.bazos_parser` to parse. The shared retry/backoff +
adaptive throttle (`RateLimiter` + `penalize()` on 429/403) + `ListingGoneError`
on a 404/410 all live in `scraper.portal_base.BasePortalClient`; this client
adds only the HTML `Accept` header, the bazos URL builders, and the
removed-listing signals (a redirect onto the category index, that index's own
`<title>`, or a deleted-ad banner).
"""

from __future__ import annotations

import logging
import re

import requests

from scraper.portal_base import BasePortalClient, ListingGoneError

LOG = logging.getLogger(__name__)

BASE_URL = "https://reality.bazos.cz"

# Substrings bazos serves (HTTP 200) for a listing that has been removed. The
# live banner reads "vymazán", not the "smazán" this tuple carried alone until
# 2026-09 — which is half of why a removed ad read as present.
_GONE_MARKERS: tuple[str, ...] = (
    "inzerát byl vymazán",
    "inzerát byl smazán",
    "inzerát neexistuje",
    "inzerát již neexistuje",
)

# The other half: bazos answers a REMOVED ad with HTTP 200 and the CATEGORY INDEX
# page. The request lands on /inzeraty/<slug>/ instead of the ad's /inzerat/<id>/,
# and the body is a results grid whose <title> is the category's own
# "<slug> inzerce - Reality | Bazoš.cz" (a live ad's title is the ad headline).
# Both are read on the detail path only — an INDEX fetch legitimately returns
# exactly this page, so neither may live in the shared `_get_html`.
_DETAIL_PATH = "/inzerat/"
_CATEGORY_TITLE_RE = re.compile(
    r"<title>[^<]*\binzerce\s*-\s*Reality\s*\|\s*Bazo[sš]\.cz", re.IGNORECASE
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
        url = detail_url(path_or_url)
        response = self._request(url)
        # A removed ad redirects off /inzerat/<id>/ onto the category index; after
        # requests follows it the status is 200 but the URL is no longer the ad's.
        final_url = getattr(response, "url", url) or url
        if _DETAIL_PATH not in final_url:
            raise ListingGoneError(url, response.status_code)
        text = response.text
        # Same page served without a visible redirect: its title is the category's.
        if _CATEGORY_TITLE_RE.search(text):
            raise ListingGoneError(url, response.status_code)
        if any(marker in text.lower() for marker in _GONE_MARKERS):
            raise ListingGoneError(url, response.status_code)
        return text, response.status_code

    def _get_html(self, url: str) -> tuple[str, int]:
        # The INDEX path. _request paces (shared limiter or request_delay_s
        # self-throttle), retries transient statuses, and raises ListingGoneError
        # on 404/410 -- which the walk reads as "past the last page". The
        # removed-ad body signals are deliberately NOT read here: an index page is
        # a category page by definition, and a gone verdict on this path would
        # truncate the walk (and with it rule #3's nomination gate).
        response = self._request(url)
        return response.text, response.status_code
