"""Hermetic tests for scraper.reas_client (no network).

A fake session records the request; mirrors tests/scraper/test_price_stats_client.py.
The one thing worth pinning is the URL: `bounds` is a COMMA string and LAT FIRST —
the JSON-array form is accepted and silently ignored, so a regression here returns
the whole country under a 200 rather than an error.
"""

from __future__ import annotations

from scraper.reas_client import SOLD_LIST_URL, ReasClient, build_client


class FakeResponse:
    status_code = 200
    text = "<html></html>"


class FakeSession:
    def __init__(self) -> None:
        self.gets: list[dict] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.gets.append({"url": url, "params": params})
        return FakeResponse()


def _client() -> ReasClient:
    client = ReasClient(request_delay_s=0.0, max_retries=0)
    client._session = FakeSession()
    return client


def test_the_bounds_are_comma_separated_and_lat_first():
    client = _client()
    client.fetch_sold_page((49.5038, 17.1109, 49.6838, 17.3909))
    params = client._session.gets[0]["params"]
    assert client._session.gets[0]["url"] == SOLD_LIST_URL
    assert params["bounds"] == "49.5038,17.1109,49.6838,17.3909"
    assert params["listPerPage"] == 100
    assert params["listPage"] == 1
    assert params["sort"] == "newest"


def test_a_later_page_is_requested_by_number():
    client = _client()
    client.fetch_sold_page((49.0, 14.0, 50.0, 15.0), page=7)
    assert client._session.gets[0]["params"]["listPage"] == 7


def test_the_page_body_is_returned_unparsed():
    # client = HTTP only: the payload is `reas_parser`'s business.
    assert _client().fetch_sold_page((49.0, 14.0, 50.0, 15.0)) == "<html></html>"


def test_we_identify_ourselves_and_carry_no_contact_data():
    agent = ReasClient.USER_AGENT
    assert agent is not None and "sreality-tracker" in agent
    assert "@" not in agent, "a User-Agent is not a place for contact data"
    assert ReasClient.USE_PROXY is False


def test_the_built_client_shares_one_politeness_budget(monkeypatch):
    built: list[tuple] = []
    monkeypatch.setattr(
        "scraper.reas_client.build_rate_limiter",
        lambda source, rate, shared, lease_n: built.append(
            (source, rate, shared, lease_n)) or object(),
    )
    build_client()
    source, rate, shared, lease_n = built[0]
    assert (source, shared) == ("reas", True)
    assert rate <= 0.2, "one request per five seconds is the politeness budget"
    assert lease_n <= 5, "a cell is a handful of requests, not a drain"
