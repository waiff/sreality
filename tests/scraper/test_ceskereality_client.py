"""Hermetic tests for scraper.ceskereality_client: URL building (?strana paging)
+ retry/penalize + the ceskereality gone signals (404, redirect off the .html
detail path, body marker).

No network: a fake session feeds canned responses. Mirrors test_idnes_client.py.
"""

from __future__ import annotations

import time

import pytest
import requests

from scraper.ceskereality_client import CeskerealityClient, detail_url, index_url
from scraper.portal_base import ListingGoneError
from scraper.rate_limit import RateLimiter

_DETAIL = (
    "https://www.ceskereality.cz/prodej/byty/byty-1-1/praha/"
    "prodej-bytu-1-1-41-m2-moldavska-3754200.html"
)


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


def _client(responses: list[FakeResponse], **kw) -> CeskerealityClient:
    c = CeskerealityClient(**kw)
    c._session = FakeSession(responses)
    return c


def test_index_url_building():
    # page=None / page=1 -> the bare first page; ?strana=N for N>=2.
    assert index_url("prodej", "byty") == "https://www.ceskereality.cz/prodej/byty/"
    assert index_url("prodej", "byty", 1) == "https://www.ceskereality.cz/prodej/byty/"
    assert (
        index_url("prodej", "byty", 2)
        == "https://www.ceskereality.cz/prodej/byty/?strana=2"
    )
    assert (
        index_url("pronajem", "komercni-prostory", 5)
        == "https://www.ceskereality.cz/pronajem/komercni-prostory/?strana=5"
    )


def test_detail_url_building():
    assert (
        detail_url("/prodej/byty/x/y/abc-123.html")
        == "https://www.ceskereality.cz/prodej/byty/x/y/abc-123.html"
    )
    assert detail_url(_DETAIL) == _DETAIL


def test_fetch_index_ok():
    c = _client([FakeResponse(200, "<html>index</html>")])
    text, status = c.fetch_index("prodej", "byty")
    assert text == "<html>index</html>"
    assert status == 200
    assert c._session.calls == ["https://www.ceskereality.cz/prodej/byty/"]


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


def test_fetch_detail_redirect_off_html_is_gone():
    # A removed listing redirects to the category results; requests follows it
    # (200) but the final URL is no longer a .html listing page.
    c = _client(
        [FakeResponse(200, "<html>list</html>", url="https://www.ceskereality.cz/prodej/byty/")]
    )
    with pytest.raises(ListingGoneError):
        c.fetch_detail(_DETAIL)


def test_fetch_detail_gone_body_marker():
    c = _client([FakeResponse(200, "Inzerát byl odstraněn.", url=_DETAIL)])
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
    assert limiter.interval > base  # penalize widened the interval


def test_retry_exhausted_raises(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    c = _client([FakeResponse(500), FakeResponse(500)], max_retries=1)
    with pytest.raises(requests.HTTPError):
        c.fetch_detail(_DETAIL)
    assert len(c._session.calls) == 2
