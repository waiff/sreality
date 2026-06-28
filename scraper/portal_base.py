"""Shared HTTP layer for portal clients (Phase 4 portal framework).

Every portal fetches over HTTP with the same machinery: a `requests.Session`
with browser-like headers, a shared `RateLimiter` (paced + `penalize()` on an
HTTP 429/403 throttle), a retry loop with exponential backoff over transient
statuses, and `ListingGoneError` on a 404/410. `BasePortalClient` owns all of
that; a concrete client supplies only what differs between portals: the `Accept`
header (JSON vs HTML), how it builds URLs / paginates, and how it interprets a
response body (incl. any "this listing was removed" body markers).

The portal-specific clients (`scraper.sreality_client`, `scraper.bazos_client`)
subclass this and stay thin. New portals do the same — see the modularity rule
in CLAUDE.md.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)

# 403/429 are how a portal throttles a too-fast egress IP; treat them as
# retryable (penalize + back off + retry) rather than fatal, so a transient
# block can't crash a whole run.
RETRYABLE_STATUS: frozenset[int] = frozenset(
    {403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
)

# Statuses that mean "this listing no longer exists" rather than transient.
GONE_STATUSES: frozenset[int] = frozenset({404, 410})

# UA + language shared by every portal; the per-portal `Accept` is set by the
# subclass's ACCEPT class attribute (JSON for an API, HTML for a crawler).
_BASE_HEADERS: dict[str, str] = {
    "Accept-Language": "cs,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
}


class ListingGoneError(Exception):
    """A listing's detail endpoint signals it no longer exists."""

    def __init__(self, url: str, status: int | None) -> None:
        self.status = status
        super().__init__(f"listing gone (status={status}) at {url}")


class BasePortalClient:
    """Session + retry/backoff + adaptive throttle shared by all portal clients.

    Subclasses set `ACCEPT` and call `_request(...)` to get a `< 400` response
    (or a raised `ListingGoneError` / `requests.HTTPError`), then interpret the
    body their own way (parse JSON, scan HTML, check removed-listing markers).
    """

    ACCEPT: str = "*/*"
    # An optional honest/identifying override of the shared browser User-Agent,
    # set by a subclass that crawls a site we want to identify ourselves to.
    # None = the shared browser UA. A per-portal fetcher concern, not a default.
    USER_AGENT: str | None = None
    # Opt-in: route every request through the residential proxy in the
    # SCRAPER_PROXY_URL env var. For portals fronted by an anti-bot edge that
    # throttles our datacenter (GitHub-Actions) IP — ceskereality, mmreality.
    # Unset env or False = direct (our IP), so the default for every other portal
    # is unchanged and free.
    USE_PROXY: bool = False
    PROXY_ENV = "SCRAPER_PROXY_URL"

    def __init__(
        self,
        *,
        limiter: "RateLimiter | None" = None,
        request_delay_s: float = 1.5,
        timeout_s: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        # A shared RateLimiter paces requests globally across worker threads and
        # widens its interval on a throttle. Serial callers pass none and fall
        # back to the per-instance request_delay_s self-throttle.
        self._limiter = limiter
        self.request_delay_s = request_delay_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._session = requests.Session()
        headers = {**_BASE_HEADERS, "Accept": self.ACCEPT}
        if self.USER_AGENT is not None:
            headers["User-Agent"] = self.USER_AGENT
        self._session.headers.update(headers)
        if self.USE_PROXY:
            proxy = os.environ.get(self.PROXY_ENV)
            if proxy:
                self._session.proxies = {"http": proxy, "https": proxy}
                LOG.info("PROXY enabled (residential egress via %s)", self.PROXY_ENV)
            else:
                LOG.warning(
                    "USE_PROXY set but %s is empty — falling back to the direct "
                    "(datacenter) IP; expect anti-bot throttling.", self.PROXY_ENV)
        self._last_at = 0.0

    def _pace(self) -> None:
        if self._limiter is not None:
            self._limiter.acquire()
            return
        elapsed = time.monotonic() - self._last_at
        if elapsed < self.request_delay_s:
            time.sleep(self.request_delay_s - elapsed)

    def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        method: str = "GET",
        pace: bool = True,
    ) -> requests.Response:
        """GET (or POST) with retry/backoff + adaptive throttle. Returns a
        `< 400` response.

        A `json_body` (or `method="POST"`) sends a POST with that JSON body —
        for JSON-API portals whose index/detail is a GraphQL/RPC call rather
        than a URL GET (bezrealitky). Everything else is identical to a GET.

        Raises `ListingGoneError` immediately on a 404/410 (no retry — a gone
        listing won't come back), retries the `RETRYABLE_STATUS` set with
        exponential backoff, `raise_for_status()` on any other `>= 400` (e.g.
        sreality's 422 deep-pagination cap, which the caller catches), and
        re-raises the last `RequestException` once retries are exhausted.

        `pace=False` lets a caller that already paced (e.g. a per-fetch limiter
        acquire outside the retry loop) skip the built-in throttle.
        """
        error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(2.0 ** (attempt - 1))
            if pace:
                self._pace()
            try:
                if json_body is not None or method == "POST":
                    response = self._session.post(
                        url, json=json_body, timeout=self.timeout_s
                    )
                elif params is None:
                    response = self._session.get(url, timeout=self.timeout_s)
                else:
                    response = self._session.get(
                        url, params=params, timeout=self.timeout_s
                    )
                self._last_at = time.monotonic()
                status = response.status_code
                if status in (429, 403) and self._limiter is not None:
                    LOG.warning("RATE penalize status=%d url=%s", status, url)
                    self._limiter.penalize()
                if status in GONE_STATUSES:
                    raise ListingGoneError(url, status)
                if status in RETRYABLE_STATUS:
                    raise requests.HTTPError(
                        f"{status} from {url}", response=response
                    )
                if status >= 400:
                    response.raise_for_status()
                return response
            except ListingGoneError:
                raise
            except requests.RequestException as exc:
                error = exc
                LOG.warning(
                    "GET %s attempt %d/%d failed: %s",
                    url, attempt + 1, self.max_retries + 1, exc,
                )
        assert error is not None
        raise error
