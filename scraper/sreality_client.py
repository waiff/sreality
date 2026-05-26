"""HTTP layer for the Sreality public JSON API (v1).

Sreality rebuilt their site on Next.js in 2026; the old
`/api/cs/v2/estates` API was removed. Listings now come from
`/api/v1/estates/search` (offset/limit paging, `locality_country_id=112`)
and per-listing detail from `/api/v1/estates/{id}`. Both are public JSON
endpoints reachable without cookies. This module paginates the search
endpoint, fetches detail records, and handles retries, polite throttling,
and browser-like headers.
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

INDEX_URL = "https://www.sreality.cz/api/v1/estates/search"
DETAIL_URL = "https://www.sreality.cz/api/v1/estates/{id}"

# Czech Republic in the new API's locality scheme (the old API used 10001).
CZ_COUNTRY_ID = 112

DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "application/json",
    "Accept-Language": "cs,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
}

RETRYABLE_STATUS: frozenset[int] = frozenset(
    # 403/429 are how sreality throttles a too-fast egress IP; treat them as
    # retryable (penalize + back off + retry) rather than a fatal error, so a
    # transient block can't crash a whole run.
    {403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
)

# The search endpoint refuses offsets past its deep-pagination window with
# HTTP 422. We stop the walk cleanly when we hit it (the completeness guard
# in main.py then declines to mark_inactive for that truncated slice); large
# categories are walked per-district so each slice stays under the window.
CAP_STATUSES: frozenset[int] = frozenset({422})

# Sreality's search caps deep pagination per filter, so a single walk of a
# large category never retrieves the whole set. Categories whose total
# exceeds SPLIT_THRESHOLD are walked once per DISTRICT (okres) instead — each
# district is well under the cap, so the union is complete and mark_inactive
# can run. DISTRICT_IDS is the canonical sreality locality_district_id set:
# okresy 1..77 (47 = Praha city) plus the Praha sub-district codes 5001..5022.
SPLIT_THRESHOLD: int = 10000
DISTRICT_IDS: tuple[int, ...] = (*range(1, 78), *range(5001, 5023))

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
    body = response.text.lower()
    return any(marker in body for marker in _NOT_FOUND_MARKERS)


def _unwrap_estate(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the estate object from a detail response.

    The detail endpoint returns the estate object directly; tolerate a future
    envelope by unwrapping a single nested object that carries the marker key.
    """
    if "categoryMainCb" in payload:
        return payload
    for key in ("result", "estate", "data"):
        inner = payload.get(key)
        if isinstance(inner, dict) and "categoryMainCb" in inner:
            return inner
    return payload


class SrealityClient:
    def __init__(
        self,
        category_main: int = 1,
        category_type: int = 2,
        country_id: int = CZ_COUNTRY_ID,
        per_page: int = 500,
        detail_delay_s: float = 1.5,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        limiter: "RateLimiter | None" = None,
        locality_region_id: int | None = None,
        locality_district_id: int | None = None,
    ) -> None:
        self.category_main = category_main
        self.category_type = category_type
        self.country_id = country_id
        # When set, the walk is restricted to one okres — used to walk large
        # categories district-by-district so each slice stays under the
        # search endpoint's deep-pagination cap.
        self.locality_region_id = locality_region_id
        self.locality_district_id = locality_district_id
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
        # Total matching estates as the API reports it (pagination.total).
        # Used by the caller to decide whether a walk was complete enough to
        # drive mark_inactive (a silently-truncated walk must not flip live
        # listings to inactive).
        self.result_size: int | None = None

    def _index_params(self, offset: int, limit: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "category_main_cb": self.category_main,
            "category_type_cb": self.category_type,
            "locality_country_id": self.country_id,
            "limit": self.per_page if limit is None else limit,
            "offset": offset,
        }
        if self.locality_region_id is not None:
            params["locality_region_id"] = self.locality_region_id
        if self.locality_district_id is not None:
            params["locality_district_id"] = self.locality_district_id
        return params

    def probe_result_size(self) -> int | None:
        """Fetch pagination.total only (limit=0) and return it.

        Cheap pre-walk probe used to decide whether a category is large enough
        to warrant a district-split walk. Also latches self.result_size. Paced
        by the shared limiter so a split walk's many probes don't burst.
        """
        if self._limiter is not None:
            self._limiter.acquire()
        payload = self._get_json(INDEX_URL, params=self._index_params(0, limit=0))
        total = (payload.get("pagination") or {}).get("total")
        if isinstance(total, int):
            self.result_size = total
        return self.result_size

    def iter_index(self) -> Iterator[dict[str, Any]]:
        """Yield every estate dict from every search page until exhausted.

        Paged by offset/limit. Each page fetch is paced by the shared limiter
        when present. Stops cleanly at the deep-pagination cap (HTTP 422).
        """
        offset = 0
        while True:
            if self._limiter is not None:
                self._limiter.acquire()
            try:
                payload = self._get_json(INDEX_URL, params=self._index_params(offset))
            except requests.HTTPError as exc:
                status = (
                    exc.response.status_code
                    if getattr(exc, "response", None) is not None
                    else None
                )
                if status in CAP_STATUSES:
                    LOG.info(
                        "INDEX cap reached offset=%d status=%s; stopping walk",
                        offset, status,
                    )
                    return
                raise
            self.pages_fetched += 1
            total = (payload.get("pagination") or {}).get("total")
            if isinstance(total, int):
                self.result_size = total
            results = payload.get("results") or []
            LOG.info(
                "INDEX offset=%d estates=%d total=%s", offset, len(results),
                self.result_size,
            )
            if not results:
                return
            for estate in results:
                yield estate
            offset += self.per_page
            if self.result_size is not None and offset >= self.result_size:
                return
            if len(results) < self.per_page:
                return

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
            payload = self._get_json(url)
            if isinstance(payload, dict):
                estate = _unwrap_estate(payload)
                # The detail object omits its own id (it keys the request, not
                # the body); inject the known id so the parser can rely on it.
                estate.setdefault("id", sreality_id)
                return estate
            return payload
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
