"""Hermetic tests for scraper.price_stats_client (no network).

A fake session feeds canned JSON; mirrors tests/scraper/test_bezrealitky_client.py.
"""

from __future__ import annotations

import pytest
import requests

from scraper.price_stats_client import (
    AuthExpiredError,
    PriceStatsClient,
    _month_windows,
)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class FakeSession:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.gets: list[dict] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.gets.append({"url": url, "params": params})
        return self._responses.pop(0)


def _client(responses) -> PriceStatsClient:
    c = PriceStatsClient(request_delay_s=0.0, max_retries=0)
    c._session = FakeSession(responses)
    return c


def test_month_windows_chunking():
    wins = _month_windows((2015, 1), (2026, 6), 24)
    assert wins[0] == ("2015-01", "2016-12")
    assert wins[-1][1] == "2026-06"
    # contiguous, non-overlapping
    assert all(wins[i][1] < wins[i + 1][0] for i in range(len(wins) - 1))


def test_month_windows_single_chunk():
    assert _month_windows((2024, 3), (2024, 8), 24) == [("2024-03", "2024-08")]


def test_suggest_municipality_parses_response():
    payload = {"results": [
        {"userData": {"source": "muni", "id": 3412, "municipality": "Kolín",
                      "latitude": 50.0, "longitude": 15.2}},
    ]}
    c = _client([FakeResponse(payload)])
    match = c.suggest_municipality("Kolín")
    assert match["entity_id"] == 3412
    assert c._session.gets[0]["params"]["phrase"] == "Kolín"


def test_fetch_window_401_raises_auth_expired():
    c = _client([FakeResponse({}, status_code=401)])
    with pytest.raises(AuthExpiredError):
        c.fetch_window(
            {"category_main_cb": 1, "distance": 0},
            entity_id=1, entity_type="muni", category_type_cb=1,
            default_from="2024-01", default_to="2024-06",
        )


def test_fetch_series_merges_windows():
    w1 = {"result": {
        "advert_count": 5,
        "dev_price_by_month": [{"year": 2020, "month": 1, "price": 40000}],
        "dev_count_advert_by_month": [{"year": 2020, "month": 1, "active": 5,
                                       "new": 1, "deleted": 0}],
    }}
    w2 = {"result": {
        "advert_count": 7,
        "dev_price_by_month": [{"year": 2023, "month": 12, "price": 52000}],
        "dev_count_advert_by_month": [],
    }}
    c = _client([FakeResponse(w1), FakeResponse(w2)])
    series = c.fetch_series(
        {"category_main_cb": 1, "distance": 0},
        entity_id=1, entity_type="muni", category_type_cb=1,
        start_ym=(2020, 1), end_ym=(2023, 12), chunk_months=24,
    )
    yms = [(m["year"], m["month"]) for m in series["months"]]
    assert (2020, 1) in yms and (2023, 12) in yms
    assert len(c._session.gets) == 2  # two windows fetched
    assert series["aggregates"]["advert_count"] == 7  # latest non-empty window
