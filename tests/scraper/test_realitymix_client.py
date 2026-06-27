"""Hermetic tests for scraper.realitymix_client: URL building (?stranka paging)
+ retry/penalize + the realitymix gone signals (404, redirect off the /detail/
path, body marker).

No network: a fake session feeds canned responses. Mirrors test_ceskereality_client.py.
"""

from __future__ import annotations

import time

import pytest
import requests

from scraper.portal_base import ListingGoneError
from scraper.rate_limit import RateLimiter
from scraper.realitymix_client import RealitymixClient, detail_url, index_url

_DETAIL = "https://realitymix.cz/detail/nupaky/prostorny-byt-2-1-8414569.html"


class FakeResponse:
    def __init__(self, status_code: int, text: str = "ok", url: str | None = None):
        self.status_code = status_code
        self.text = text
        self.url = url if url is not None else _DETAIL
        self.headers = {"Content-Type": "text/html"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, timeout: float | None = None) -> FakeResponse:
        self.calls.append(url)
        return self._responses.pop(0)


def _client(responses: list[FakeResponse], **kw) -> RealitymixClient:
    c = RealitymixClient(**kw)
    c._session = FakeSession(responses)
    return c


def test_index_url_building():
    # /reality/{family}/{sale}; page=None/1 -> bare first page; ?stranka=N for N>=2.
    assert index_url("prodej", "byty") == "https://realitymix.cz/reality/byty/prodej"
    assert index_url("prodej", "byty", 1) == "https://realitymix.cz/reality/byty/prodej"
    assert (
        index_url("prodej", "byty", 2)
        == "https://realitymix.cz/reality/byty/prodej?stranka=2"
    )
    assert (
        index_url("pronajem", "komerce", 5)
        == "https://realitymix.cz/reality/komerce/pronajem?stranka=5"
    )


def test_detail_url_building():
    assert detail_url("/detail/x/y-123.html") == "https://realitymix.cz/detail/x/y-123.html"
    assert detail_url(_DETAIL) == _DETAIL


def test_fetch_index_ok():
    c = _client([FakeResponse(200, "<html>index</html>")])
    text, status = c.fetch_index("prodej", "byty")
    assert text == "<html>index</html>"
    assert status == 200
    assert c._session.calls == ["https://realitymix.cz/reality/byty/prodej"]


def test_fetch_detail_ok():
    c = _client([FakeResponse(200, "<html>detail</html>", url=_DETAIL)])
    text, status = c.fetch_detail(_DETAIL)
    assert text == "<html>detail</html>"
    assert status == 200


def test_fetch_detail_gone_404_raises_immediately():
    c = _client([FakeResponse(404, "not found")])
    with pytest.raises(ListingGoneError):
        c.fetch_detail(_DETAIL)
    assert len(c._session.calls) == 1  # no retry on a gone status


def test_fetch_detail_redirect_off_detail_is_gone():
    c = _client(
        [FakeResponse(200, "<html>list</html>", url="https://realitymix.cz/reality/byty/prodej")]
    )
    with pytest.raises(ListingGoneError):
        c.fetch_detail(_DETAIL)


def test_fetch_detail_gone_body_marker():
    c = _client([FakeResponse(200, "Tato stránka neexistuje.", url=_DETAIL)])
    with pytest.raises(ListingGoneError):
        c.fetch_detail(_DETAIL)


def test_retry_then_success_penalizes_on_429(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    limiter = RateLimiter(1000.0)
    base = limiter.interval
    c = _client(
        [FakeResponse(429), FakeResponse(200, "ok", url=_DETAIL)],
        limiter=limiter, max_retries=2,
    )
    text, status = c.fetch_detail(_DETAIL)
    assert text == "ok"
    assert status == 200
    assert len(c._session.calls) == 2
    assert limiter.interval > base


def test_retry_exhausted_raises(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    c = _client([FakeResponse(500), FakeResponse(500)], max_retries=1)
    with pytest.raises(requests.HTTPError):
        c.fetch_detail(_DETAIL)
    assert len(c._session.calls) == 2
