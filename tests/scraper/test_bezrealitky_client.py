"""Hermetic tests for scraper.bezrealitky_client: GraphQL POST + gone signal.

No network: a fake session feeds canned JSON responses. Mirrors the shape of
tests/scraper/test_bazos_client.py.
"""

from __future__ import annotations

import json

import pytest

from scraper.bezrealitky_client import BezrealitkyClient, detail_url
from scraper.portal_base import ListingGoneError


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {"Content-Type": "application/json"}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError("unexpected raise_for_status in test")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.posts: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self.posts.append((url, json))
        return self._responses.pop(0)


def _client(responses: list[FakeResponse]) -> BezrealitkyClient:
    c = BezrealitkyClient()
    c._session = FakeSession(responses)
    return c


def test_detail_url_building():
    assert (
        detail_url("123-nabidka-prodej-bytu")
        == "https://www.bezrealitky.cz/nemovitosti-byty-domy/123-nabidka-prodej-bytu"
    )


def test_origin_referer_headers_set():
    c = BezrealitkyClient()
    assert c._session.headers["Origin"] == "https://www.bezrealitky.cz"
    assert c._session.headers["Referer"] == "https://www.bezrealitky.cz/"
    assert c._session.headers["Accept"] == "application/json"


def test_search_returns_list_and_total():
    payload = {"data": {"listAdverts": {
        "totalCount": 42,
        "list": [{"id": "1", "price": 100, "uri": "1-x"},
                 {"id": "2", "price": 200, "uri": "2-y"}],
    }}}
    c = _client([FakeResponse(payload)])
    adverts, total = c.search("PRODEJ", "BYT", limit=50, offset=0)
    assert total == 42
    assert [a["id"] for a in adverts] == ["1", "2"]
    url, body = c._session.posts[0]
    assert url.endswith("/graphql/")
    assert body["variables"] == {"ot": ["PRODEJ"], "et": ["BYT"], "lim": 50, "off": 0}


def test_get_detail_returns_advert():
    payload = {"data": {"advert": {"id": "7", "uri": "7-x", "price": 9}}}
    c = _client([FakeResponse(payload)])
    advert = c.get_detail("7")
    assert advert["id"] == "7"


def test_get_detail_null_raises_gone():
    c = _client([FakeResponse({"data": {"advert": None}})])
    with pytest.raises(ListingGoneError):
        c.get_detail("999")


def test_search_accepts_list_estate_type():
    payload = {"data": {"listAdverts": {"totalCount": 3, "list": []}}}
    c = _client([FakeResponse(payload)])
    c.search("PRODEJ", ["KANCELAR", "NEBYTOVY_PROSTOR"], limit=10, offset=0)
    _, body = c._session.posts[0]
    assert body["variables"]["et"] == ["KANCELAR", "NEBYTOVY_PROSTOR"]


def test_search_str_estate_type_still_wraps():
    payload = {"data": {"listAdverts": {"totalCount": 0, "list": []}}}
    c = _client([FakeResponse(payload)])
    c.search("PRODEJ", "BYT", limit=10, offset=0)
    _, body = c._session.posts[0]
    assert body["variables"]["et"] == ["BYT"]


def test_graphql_errors_raise():
    c = _client([FakeResponse({"errors": [{"message": "boom"}]})])
    with pytest.raises(RuntimeError):
        c.search("PRODEJ", "BYT", limit=1, offset=0)
