"""HTTP layer for the Sreality public JSON API.

Paginates the index endpoint, fetches per-listing detail records, and
handles retries, polite throttling, and the browser-like headers that
Sreality requires (raw cloud IPs get 403 without them).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)

INDEX_URL = "https://www.sreality.cz/api/cs/v2/estates"
DETAIL_URL = "https://www.sreality.cz/api/cs/v2/estates/{id}"

# Mobile Chrome on Android - the same UA the karlosmatos reference
# scraper uses successfully against this API.
DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en,cs;q=0.9",
    "Referer": "https://www.sreality.cz/hledani/pronajem/byty",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Mobile Safari/537.36"
    ),
}

RETRYABLE_STATUS: frozenset[int] = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
)

# Statuses that mean "this listing no longer exists" rather than a
# transient or unexpected error. Mirrors scraper.freshness.GONE_STATUSES.
GONE_STATUSES: frozenset[int] = frozenset({404, 410})

# Substrings of sreality's HTML "this page does not exist" page. Sreality
# sometimes serves this (HTTP 200, text/html) for a delisted detail URL
# instead of a 404/410 JSON error, in which case response.json() would
# otherwise raise a parse error and the listing would be logged as a fetch
# failure instead of recognised as gone.
_NOT_FOUND_MARKERS: tuple[str, ...] = (
    "tato stránka neexistuje",
    "stránka nebyla nalezena",
)


class ListingGoneError(Exception):
    """A listing's detail endpoint signals it no longer exists."""

    def __init__(self, url: str, status: int | None) -> None:
        self.status = status
        super().__init__(f"listing gone (status={status}) at {url}")


def _is_not_found_body(response: requests.Response) -> bool:
    """True when a non-JSON 200 body is sreality's 'page does not exist' page."""
    if "json" in response.headers.get("Content-Type", "").lower():
        return False
    text = (response.text or "").lower()
    return any(marker in text for marker in _NOT_FOUND_MARKERS)


class SrealityClient:
    def __init__(
        self,
        category_main: int = 1,
        category_type: int = 2,
        country_id: int = 10001,
        per_page: int = 100,
        detail_delay_s: float = 1.5,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        limiter: "RateLimiter | None" = None,
    ) -> None:
        self.category_main = category_main
        self.category_type = category_type
        self.country_id = country_id
        self.per_page = per_page
        self.detail_delay_s = detail_delay_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        # When set, a shared RateLimiter paces detail fetches (allowing
        # concurrency across threads) instead of the per-instance
        # detail_delay_s spacing. Serial callers (freshness, --detail-only)
        # pass no limiter and keep the 1.5s self-throttle.
        self._limiter = limiter
        self._session = requests.Session()
        self._session.headers.update(DEFAULT_HEADERS)
        self._last_detail_at = 0.0
        self.pages_fetched = 0
        # Total matching estates as the API reports it on page 1. Used by
        # the caller to decide whether a walk was complete enough to drive
        # mark_inactive (a silently-truncated walk must not flip live
        # listings to inactive).
        self.result_size: int | None = None

    def iter_index(self) -> Iterator[dict[str, Any]]:
        """Yield every estate dict from every index page until exhausted."""
        page = 1
        while True:
            params = {
                "category_main_cb": self.category_main,
                "category_type_cb": self.category_type,
                "locality_country_id": self.country_id,
                "per_page": self.per_page,
                "page": page,
            }
            payload = self._get_json(INDEX_URL, params=params)
            self.pages_fetched += 1
            if self.result_size is None:
                rs = payload.get("result_size")
                if isinstance(rs, int):
                    self.result_size = rs
            estates = payload.get("_embedded", {}).get("estates", [])
            LOG.info("INDEX page=%d estates=%d", page, len(estates))
            if not estates:
                return
            for estate in estates:
                yield estate
            if len(estates) < self.per_page:
                return
            page += 1

    def get_detail(self, sreality_id: int) -> dict[str, Any]:
        """Fetch the full detail record for one listing, rate-limited.

        With a shared limiter the spacing is global across worker threads;
        without one, fall back to the per-instance detail_delay_s spacing.
        """
        if self._limiter is not None:
            self._limiter.acquire()
        else:
            elapsed = time.monotonic() - self._last_detail_at
            if elapsed < self.detail_delay_s:
                time.sleep(self.detail_delay_s - elapsed)
        url = DETAIL_URL.format(id=sreality_id)
        try:
            return self._get_json(url)
        except ListingGoneError:
            raise
        except requests.HTTPError as exc:
            status = (
                exc.response.status_code
                if getattr(exc, "response", None) is not None
                else None
            )
            if status in GONE_STATUSES:
                raise ListingGoneError(url, status) from exc
            raise
        finally:
            self._last_detail_at = time.monotonic()

    def _get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(2.0 ** (attempt - 1))
            try:
                response = self._session.get(
                    url, params=params, timeout=self.timeout_s
                )
                if response.status_code in (429, 403) and self._limiter is not None:
                    LOG.warning(
                        "RATE penalize status=%d url=%s", response.status_code, url
                    )
                    self._limiter.penalize()
                if (
                    response.status_code >= 400
                    and response.status_code not in RETRYABLE_STATUS
                ):
                    response.raise_for_status()
                if response.status_code in RETRYABLE_STATUS:
                    raise requests.HTTPError(
                        f"{response.status_code} from {url}",
                        response=response,
                    )
                if _is_not_found_body(response):
                    raise ListingGoneError(url, response.status_code)
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                error = exc
                LOG.warning(
                    "GET %s attempt %d/%d failed: %s",
                    url,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
        assert error is not None
        raise error
