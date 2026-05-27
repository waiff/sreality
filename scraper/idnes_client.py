"""HTTP layer for reality.idnes.cz (multi-portal crawler).

reality.idnes.cz is server-rendered (the listing index cards and the detail
spec list are in the initial HTML), so this returns raw HTML for
`scraper.idnes_parser` to parse. Mirrors `scraper.bazos_client`: a shared
`RateLimiter` paces requests and `penalize()` widens the interval on an HTTP
429/403, with retry + backoff on transient errors. A 404/410 (or a
removed-listing body) on a detail page raises `ListingGoneError` so the
orchestrator can skip it cleanly.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import requests

from scraper.sreality_client import ListingGoneError

if TYPE_CHECKING:
    from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)

BASE_URL = "https://reality.idnes.cz"

DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
}

RETRYABLE_STATUS: frozenset[int] = frozenset(
    {403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
)
GONE_STATUSES: frozenset[int] = frozenset({404, 410})

# Substrings idnes serves (HTTP 200) on a "soft 404" for a withdrawn listing.
# Best-effort — the crawler never runs mark_inactive (partial walk), so a missed
# marker only yields a sparse parse, not a wrong delisting. Refine from logs.
_GONE_MARKERS: tuple[str, ...] = (
    "inzerát byl ukončen",
    "nabídka již není aktuální",
    "nabídka již není platná",
    "inzerát nebyl nalezen",
)


def index_url(sale_type: str, category: str, page: int = 1) -> str:
    url = f"{BASE_URL}/s/{sale_type}/{category}/"
    if page and page > 1:
        url += f"?page={page}"
    return url


def detail_url(path_or_url: str) -> str:
    if path_or_url.startswith("http"):
        return path_or_url
    return f"{BASE_URL}{path_or_url}"


class IdnesClient:
    def __init__(
        self,
        *,
        limiter: "RateLimiter | None" = None,
        request_delay_s: float = 1.5,
        timeout_s: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._limiter = limiter
        self.request_delay_s = request_delay_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._session = requests.Session()
        self._session.headers.update(DEFAULT_HEADERS)
        self._last_at = 0.0

    def fetch_index(
        self, sale_type: str, category: str, page: int = 1
    ) -> tuple[str, int]:
        return self._get_html(index_url(sale_type, category, page))

    def fetch_detail(self, path_or_url: str) -> tuple[str, int]:
        return self._get_html(detail_url(path_or_url))

    def _pace(self) -> None:
        if self._limiter is not None:
            self._limiter.acquire()
            return
        elapsed = time.monotonic() - self._last_at
        if elapsed < self.request_delay_s:
            time.sleep(self.request_delay_s - elapsed)

    def _get_html(self, url: str) -> tuple[str, int]:
        error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(2.0 ** (attempt - 1))
            self._pace()
            try:
                response = self._session.get(url, timeout=self.timeout_s)
                self._last_at = time.monotonic()
                status = response.status_code
                if status in (429, 403) and self._limiter is not None:
                    LOG.warning("RATE penalize status=%d url=%s", status, url)
                    self._limiter.penalize()
                if status in GONE_STATUSES:
                    raise ListingGoneError(url, status)
                if status in RETRYABLE_STATUS:
                    raise requests.HTTPError(f"{status} from {url}", response=response)
                if status >= 400:
                    response.raise_for_status()
                text = response.text
                if any(marker in text.lower() for marker in _GONE_MARKERS):
                    raise ListingGoneError(url, status)
                return text, status
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
