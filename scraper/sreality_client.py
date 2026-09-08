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
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import requests

from scraper.portal_base import (
    GONE_STATUSES,
    RETRYABLE_STATUS,
    BasePortalClient,
    ListingGoneError,
)

if TYPE_CHECKING:
    from scraper.portal import StopReason
    from scraper.rate_limit import RateLimiter

# Re-exported for backwards-compatible imports (`from scraper.sreality_client
# import ListingGoneError` is used across the codebase).
__all__ = [
    "SrealityClient",
    "ListingGoneError",
    "GONE_STATUSES",
    "RETRYABLE_STATUS",
    "SPLIT_THRESHOLD",
    "DISTRICT_IDS",
]

LOG = logging.getLogger(__name__)

INDEX_URL = "https://www.sreality.cz/api/v1/estates/search"
DETAIL_URL = "https://www.sreality.cz/api/v1/estates/{id}"

# Czech Republic in the new API's locality scheme (the old API used 10001).
CZ_COUNTRY_ID = 112

# The search endpoint refuses offsets past its deep-pagination window with
# HTTP 422. We stop the walk cleanly when we hit it (the completeness guard
# in main.py then declines to mark_inactive for that truncated slice); large
# categories are walked per-district so each slice stays under the window.
CAP_STATUSES: frozenset[int] = frozenset({422})

# Sreality's search caps deep pagination per filter, so a single walk of a
# large category never retrieves the whole set. Categories whose total
# exceeds SPLIT_THRESHOLD are walked once per DISTRICT (okres) instead — each
# okres is well under the cap (the largest, Praha=okres 47, is ~5k; every
# other okres is <1k), so the union is complete and mark_inactive can run.
# DISTRICT_IDS is the 77 okresy. Okres 47 already covers ALL of Praha (it
# equals locality_region_id=10's total and is a strict superset of the
# 5001..5022 Praha sub-district codes), so those sub-codes are deliberately
# NOT included: walking them too would re-fetch Praha a second time and
# double-count it in the reconciliation totals for no coverage gain.
SPLIT_THRESHOLD: int = 10000
DISTRICT_IDS: tuple[int, ...] = tuple(range(1, 78))

# Substrings of sreality's HTML "this page does not exist" page. Sreality
# sometimes serves this (HTTP 200, text/html) for a delisted detail URL
# instead of a 404/410 JSON error, in which case response.json() would
# otherwise raise a parse error and the listing would be logged as a fetch
# failure instead of recognised as gone.
_NOT_FOUND_MARKERS: tuple[str, ...] = (
    "tato stránka neexistuje",
    "stránka nebyla nalezena",
)


def _is_not_found_body(response: requests.Response) -> bool:
    """True when a non-JSON 200 body is sreality's 'page does not exist' page."""
    if "json" in response.headers.get("Content-Type", "").lower():
        return False
    body = response.text.lower()
    return any(marker in body for marker in _NOT_FOUND_MARKERS)


def _unwrap_estate(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the estate object from a detail response.

    The detail endpoint wraps the estate as `{result, status_code,
    status_message}`; unwrap the nested object that carries the marker key.
    Tolerate a flat payload (estate at the top level) too.
    """
    if "category_main_cb" in payload:
        return payload
    for key in ("result", "estate", "data"):
        inner = payload.get(key)
        if isinstance(inner, dict) and "category_main_cb" in inner:
            return inner
    return payload


class SrealityClient(BasePortalClient):
    ACCEPT = "application/json"

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
        # A shared RateLimiter (when set) paces fetches across worker threads;
        # serial callers (freshness, --detail-only) pass none and keep the
        # per-instance detail_delay_s self-throttle in get_detail.
        super().__init__(
            limiter=limiter,
            request_delay_s=detail_delay_s,
            timeout_s=timeout_s,
            max_retries=max_retries,
        )
        self.category_main = category_main
        self.category_type = category_type
        self.country_id = country_id
        # When set, the walk is restricted to one okres — used to walk large
        # categories district-by-district so each slice stays under the
        # search endpoint's deep-pagination cap.
        self.locality_region_id = locality_region_id
        self.locality_district_id = locality_district_id
        self.per_page = per_page
        # The page size sreality ACTUALLY served, read back from pagination.limit.
        # It matters because `short_page` is a portal end: if the API ever clamps
        # the limit we ask for (500), every page would come back "short" and the
        # first one would end the walk with the rest of the district unseen. A
        # clamp is a structural fact the payload states, so adopt it instead of
        # reading it as a tail.
        self.page_size_served: int | None = None
        self.detail_delay_s = detail_delay_s
        self._last_detail_at = 0.0
        self.pages_fetched = 0
        # The last successfully fetched index page as (offset, url, payload) —
        # the raw payload carries index-only signals (geohash, POI distances,
        # locality.geometry) that the estate dicts alone don't surface to
        # archiving callers (location-data W0 item 0n).
        self.last_index_page: tuple[int, str, dict[str, Any]] | None = None
        # Total matching estates as the API reports it (pagination.total), kept
        # as the HIGH-WATER MARK of everything it reported during this walk: the
        # number jitters, and a downward step below the offset already reached
        # would otherwise stamp `declared_total_reached` on a FULL page with rows
        # left unwalked. Since 2026-09-08 it is a COVERAGE number, not the
        # nomination gate: rule #3 asks whether the walk reached sreality's last
        # page, and that is stop_reason below.
        self.result_size: int | None = None
        # Why the last iter_index page loop stopped, in scraper.portal's
        # StopReason vocabulary. None until a walk finishes; an abandoned
        # generator leaves it unset, which callers must read as OUR stop.
        self.stop_reason: StopReason | None = None
        # Set by fetch_index_page when sreality answers with the HTTP 422
        # deep-pagination refusal, and sticky for the rest of the walk: that is a
        # wall, not the end of the list -- rows past it exist and were not seen --
        # and the empty list fetch_index_page returns cannot say so on its own.
        self.cap_hit = False

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

    def fetch_index_page(self, offset: int) -> list[dict[str, Any]]:
        """Fetch ONE search page at `offset` (self.per_page items).

        For callers that need per-page control (e.g. the newest-first discovery
        probe, which must stop early rather than walk to exhaustion) instead of
        iter_index's generator. Also updates self.pages_fetched / self.result_size,
        same as a step of iter_index's loop. Returns [] at/past the deep-pagination
        cap (HTTP 422, which also latches self.cap_hit) and for a page that carried
        no results — two different stops that only iter_index tells apart.
        """
        if self._limiter is not None:
            self._limiter.acquire()
        params = self._index_params(offset)
        try:
            payload = self._get_json(INDEX_URL, params=params)
        except requests.HTTPError as exc:
            status = (
                exc.response.status_code
                if getattr(exc, "response", None) is not None
                else None
            )
            if status in CAP_STATUSES:
                self.cap_hit = True
                LOG.info(
                    "INDEX cap reached offset=%d status=%s; stopping page fetch",
                    offset, status,
                )
                return []
            raise
        self.pages_fetched += 1
        pagination = payload.get("pagination") or {}
        total = pagination.get("total")
        if isinstance(total, int) and (
            self.result_size is None or total > self.result_size
        ):
            self.result_size = total
        served = pagination.get("limit")
        if isinstance(served, int) and 0 < served < self.per_page:
            if self.page_size_served != served:
                LOG.warning(
                    "INDEX sreality served limit=%d for a requested %d; adopting it "
                    "as the page size so a clamped page is not read as the tail",
                    served, self.per_page,
                )
            self.page_size_served = served
        results = payload.get("results") or []
        self.last_index_page = (offset, f"{INDEX_URL}?{urlencode(params)}", payload)
        LOG.info(
            "INDEX offset=%d estates=%d total=%s", offset, len(results),
            self.result_size,
        )
        return results

    def _classify_empty_page(self, offset: int, saw_items: bool) -> StopReason:
        """An empty page that survived a re-fetch: sreality's end, or ours?

        Corroboration only. An items-less HTTP 200 is also what a soft block, a
        shell body and an edge-cached blank look like, so it stays `barren`
        (ours) unless the walk's own position proves the list is exhausted: at
        or past the declared total, or -- when no total was ever readable --
        after a page of this same slice carried items. A first page that is
        barren with nothing to compare against is never confirmed.
        """
        if self.result_size is not None and offset >= self.result_size:
            return "empty_confirmed"
        if self.result_size is None and saw_items:
            return "empty_confirmed"
        return "barren"

    def iter_index(
        self,
        on_page: Callable[[int, str, dict[str, Any]], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every estate dict from every search page until the walk stops.

        Paged by offset/limit via fetch_index_page, and stamps `self.stop_reason`
        with WHY it stopped: sreality's own end (`declared_total_reached`,
        `short_page`, a corroborated `empty_confirmed`) or ours (`cap_wall` at
        the 422, `barren` for an items-less 200 nothing could corroborate).
        Rule #3 lets only the former nominate. `on_page` is invoked once per
        non-empty page with (offset, url, raw payload) — the index-archiving hook.
        """
        self.stop_reason = None
        self.cap_hit = False
        offset = 0
        saw_items = False
        while True:
            results = self.fetch_index_page(offset)
            if not results:
                # ITEMS-FIRST: classify the empty page BEFORE reading anything
                # as an end. The 422 wall, a soft block and the page after the
                # last one all arrive here as the same empty list.
                if self.cap_hit:
                    self.stop_reason = "cap_wall"
                    return
                LOG.info("INDEX empty offset=%d; re-fetching once to corroborate", offset)
                results = self.fetch_index_page(offset)
                if not results:
                    self.stop_reason = (
                        "cap_wall" if self.cap_hit
                        else self._classify_empty_page(offset, saw_items)
                    )
                    return
            saw_items = True
            if on_page is not None and self.last_index_page is not None:
                on_page(*self.last_index_page)
            yield from results
            # Advance by what the page ACTUALLY carried, not by what we asked
            # for: stepping by per_page over a clamped page skips every row
            # between the two.
            offset += len(results)
            if self.result_size is not None and offset >= self.result_size:
                self.stop_reason = "declared_total_reached"
                return
            if len(results) < (self.page_size_served or self.per_page):
                self.stop_reason = "short_page"
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
                if "category_main_cb" not in estate:
                    # A 200 whose body is only the {result, status_code,
                    # status_message} envelope, or any dict without the estate
                    # marker, is NOT a listing. Writing it would set the row
                    # alive and blank its category, price and title (every
                    # column is EXCLUDED.col in the batch upsert). Since
                    # 2026-09-07 delisted listings are re-fetched on purpose
                    # (rule #3 presence checks), which is exactly when such a
                    # body is likeliest -- so it is an error, never "ok".
                    raise RuntimeError(
                        f"sreality detail {sreality_id}: payload carries no estate "
                        f"(keys={sorted(payload)[:6]}, status_message="
                        f"{payload.get('status_message')!r})"
                    )
                # The estate carries its id as `hash_id`; inject the known id
                # if a payload ever omits it so the parser can rely on it.
                estate.setdefault("hash_id", sreality_id)
                return estate
            return payload
        except ListingGoneError:
            raise
        except requests.HTTPError as exc:
            # _request already maps a 404/410 to ListingGoneError; this stays as
            # a defensive net for any HTTPError that still carries a gone status.
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
        # The callers (iter_index / probe / get_detail) already paced via the
        # shared limiter or the detail self-throttle, so skip the base's pace.
        # _request raises ListingGoneError on 404/410 and HTTPError on the 422
        # deep-pagination cap (caught by iter_index); a 200 body that is really
        # sreality's HTML "page does not exist" is caught here.
        response = self._request(url, params=params, pace=False)
        if _is_not_found_body(response):
            raise ListingGoneError(url, response.status_code)
        return response.json()
