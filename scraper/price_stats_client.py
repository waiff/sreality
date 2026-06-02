"""HTTP client for sreality's `ceny-nemovitosti` statistics API.

`localities/suggest` (public) resolves a city name to a sreality entity;
`estate_prices` (login-gated) returns the per-locality monthly series. Reuses
`BasePortalClient` for the session / retry / adaptive-throttle machinery, adds
the logged-in cookie + the `Referer` the API expects, and walks the history in
date chunks so we don't depend on any single-call window cap (the API spans
per-year indices, e.g. `advert_stats_2015..2026`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import requests

from scraper.portal_base import BasePortalClient
from scraper.price_stats_parser import (
    build_estate_prices_params,
    parse_estate_prices,
    parse_suggest_municipality,
)

if TYPE_CHECKING:
    from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)

SUGGEST_URL = "https://www.sreality.cz/api/v1/localities/suggest"
ESTATE_PRICES_URL = "https://www.sreality.cz/api/v1/estate_prices"
_SUGGEST_CATEGORIES = (
    "region_cz,district_cz,municipality_cz,quarter_cz,ward_cz,street_cz,area_cz"
)


class AuthExpiredError(Exception):
    """estate_prices returned 401 — the session cookie needs refreshing."""


class PriceStatsClient(BasePortalClient):
    ACCEPT = "application/json, text/plain, */*"

    def __init__(
        self,
        *,
        cookies: dict[str, str] | None = None,
        limiter: "RateLimiter | None" = None,
        request_delay_s: float = 1.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(limiter=limiter, request_delay_s=request_delay_s, **kwargs)
        self._session.headers["Referer"] = "https://www.sreality.cz/ceny-nemovitosti"
        if cookies:
            self.set_cookies(cookies)

    def set_cookies(self, cookies: dict[str, str]) -> None:
        for name, value in cookies.items():
            self._session.cookies.set(name, value, domain=".sreality.cz")

    def suggest_municipality(self, phrase: str) -> dict[str, Any] | None:
        """Resolve a city name to a municipality entity (id + geo). Public API."""
        resp = self._request(
            SUGGEST_URL,
            params={
                "phrase": phrase,
                "category": _SUGGEST_CATEGORIES,
                "lang": "cs",
                "limit": 10,
            },
        )
        return parse_suggest_municipality(resp.json(), phrase=phrase)

    def fetch_window(
        self,
        dataset: dict[str, Any],
        *,
        entity_id: int,
        entity_type: str,
        category_type_cb: int,
        default_from: str,
        default_to: str,
    ) -> dict[str, Any]:
        """One estate_prices call for a [default_from, default_to] window."""
        params = build_estate_prices_params(
            dataset,
            entity_id=entity_id,
            entity_type=entity_type,
            category_type_cb=category_type_cb,
            default_from=default_from,
            default_to=default_to,
        )
        try:
            resp = self._request(ESTATE_PRICES_URL, params=params)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                raise AuthExpiredError("estate_prices 401 — session expired") from exc
            raise
        return parse_estate_prices(resp.json())

    def fetch_series(
        self,
        dataset: dict[str, Any],
        *,
        entity_id: int,
        entity_type: str,
        category_type_cb: int,
        start_ym: tuple[int, int],
        end_ym: tuple[int, int],
        chunk_months: int = 24,
    ) -> dict[str, Any]:
        """Full monthly series across [start_ym, end_ym], walked in chunks.

        Chunking makes coverage deterministic regardless of any server-side
        per-call window cap. Months are merged by (year, month); the latest
        window's scalar aggregates are returned.
        """
        merged: dict[tuple[int, int], dict[str, Any]] = {}
        last_aggregates: dict[str, Any] = {}
        for win_from, win_to in _month_windows(start_ym, end_ym, chunk_months):
            window = self.fetch_window(
                dataset,
                entity_id=entity_id,
                entity_type=entity_type,
                category_type_cb=category_type_cb,
                default_from=win_from,
                default_to=win_to,
            )
            for row in window["months"]:
                merged[(row["year"], row["month"])] = row
            if window["aggregates"].get("advert_count") is not None:
                last_aggregates = window["aggregates"]
        return {
            "months": [merged[k] for k in sorted(merged)],
            "aggregates": last_aggregates,
        }


def _month_windows(
    start_ym: tuple[int, int], end_ym: tuple[int, int], chunk_months: int
) -> list[tuple[str, str]]:
    """Inclusive [start, end] split into <= chunk_months windows ('YYYY-MM')."""
    if chunk_months < 1:
        raise ValueError("chunk_months must be >= 1")
    start_idx = start_ym[0] * 12 + (start_ym[1] - 1)
    end_idx = end_ym[0] * 12 + (end_ym[1] - 1)
    if end_idx < start_idx:
        raise ValueError("end before start")
    windows: list[tuple[str, str]] = []
    cur = start_idx
    while cur <= end_idx:
        win_end = min(cur + chunk_months - 1, end_idx)
        windows.append((_fmt(cur), _fmt(win_end)))
        cur = win_end + 1
    return windows


def _fmt(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"
