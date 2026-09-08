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
    assert body["variables"] == {
        "ot": ["PRODEJ"], "et": ["BYT"],
        "inc": True, "st": True,
        "lim": 50, "off": 0,
    }


def test_get_detail_returns_advert():
    payload = {"data": {"advert": {"id": "7", "uri": "7-x", "price": 9, "active": True}}}
    c = _client([FakeResponse(payload)])
    advert = c.get_detail("7")
    assert advert["id"] == "7"


def test_get_detail_active_false_raises_gone():
    """The portal's REAL gone signal (live API, 2026-09-08): a withdrawn advert
    is a full record with active=false, with or without timeDeactivated, and an
    unknown id is a stub with active=false and an empty title. None of them is
    a null advert, so a rule that waited for null never closed anything."""
    withdrawn = {"id": "678523", "uri": "678523-x", "title": "Pronájem garáže 12 m²",
                 "active": False, "timeActivated": None, "timeDeactivated": 1788780753}
    withdrawn_no_stamp = {"id": "310616", "uri": "310616-x", "title": "Pronájem chaty",
                          "active": False, "timeActivated": None, "timeDeactivated": None}
    unknown_stub = {"id": "1", "active": False, "timeDeactivated": None, "title": ""}
    for advert in (withdrawn, withdrawn_no_stamp, unknown_stub):
        c = _client([FakeResponse({"data": {"advert": advert}})])
        with pytest.raises(ListingGoneError):
            c.get_detail(advert["id"])


def test_get_detail_active_missing_or_null_is_an_error_not_gone():
    """Only an explicit False is the positive signal. A record with no `active`
    at all, or active=null (the shape of an access-denied field), is not
    evidence either way and must surface as an error, never as alive or gone."""
    for advert in ({"id": "7", "uri": "7-x", "price": 9},
                   {"id": "7", "uri": "7-x", "active": None}):
        c = _client([FakeResponse({"data": {"advert": advert}})])
        with pytest.raises(RuntimeError, match="'active' flag"):
            c.get_detail("7")


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


def test_search_passes_include_imports_flag():
    payload = {"data": {"listAdverts": {"totalCount": 0, "list": []}}}
    c = _client([FakeResponse(payload), FakeResponse(payload)])
    c.search("PRONAJEM", ["GARAZ", "REKREACNI_OBJEKT"], limit=1, offset=0, include_imports=False)
    c.search("PRODEJ", "BYT", limit=1, offset=0)
    assert c._session.posts[0][1]["variables"]["inc"] is False
    assert c._session.posts[0][1]["variables"]["st"] is True
    # default True so the index walk matches what bezrealitky.cz shows (CZ scope)
    assert c._session.posts[1][1]["variables"]["inc"] is True


def test_graphql_errors_raise():
    c = _client([FakeResponse({"errors": [{"message": "boom"}]})])
    with pytest.raises(RuntimeError):
        c.search("PRODEJ", "BYT", limit=1, offset=0)


def test_get_detail_missing_advert_field_is_an_error_not_gone():
    """Only an explicit null advert is the positive gone signal. An empty or
    advert-less `data` (edge stub, partial outage) must surface as an error,
    or one bad minute would delist a whole batch of presence checks."""
    for body in ({"data": {}}, {}):
        c = _client([FakeResponse(body)])
        with pytest.raises(RuntimeError, match="no 'advert' field"):
            c.get_detail("999")
