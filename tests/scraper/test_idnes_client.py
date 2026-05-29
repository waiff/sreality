"""Hermetic tests for scraper.idnes_client: URL building + retry/penalize + the
idnes-specific gone signals (404, redirect off /detail/, body marker).

No network: a fake session feeds canned responses. Mirrors test_bazos_client.py.
"""

from __future__ import annotations

import time

import pytest
import requests

from scraper.idnes_client import IdnesClient, detail_url, index_url
from scraper.portal_base import ListingGoneError
from scraper.rate_limit import RateLimiter

_DETAIL = "https://reality.idnes.cz/detail/prodej/byt/praha/6a18deadbeefdeadbeef0001/"


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


def _client(responses: list[FakeResponse], **kw) -> IdnesClient:
    c = IdnesClient(**kw)
    c._session = FakeSession(responses)
    return c


def test_index_url_building():
    # page=None -> the bare first page; ?page=N is offset-style (the 2nd page).
    assert index_url("prodej", "byty") == "https://reality.idnes.cz/s/prodej/byty/"
    assert index_url("prodej", "byty", 1) == "https://reality.idnes.cz/s/prodej/byty/?page=1"
    assert index_url("prodej", "byty", 2) == "https://reality.idnes.cz/s/prodej/byty/?page=2"
    assert (
        index_url("prodej", "byty", None, locality="praha")
        == "https://reality.idnes.cz/s/prodej/byty/praha/"
    )
    assert (
        index_url("prodej", "byty", 3, locality="praha")
        == "https://reality.idnes.cz/s/prodej/byty/praha/?page=3"
    )


def test_detail_url_building():
    assert detail_url("/detail/prodej/byt/x/abc/") == "https://reality.idnes.cz/detail/prodej/byt/x/abc/"
    assert detail_url(_DETAIL) == _DETAIL


def test_fetch_index_ok():
    c = _client([FakeResponse(200, "<html>index</html>")])
    text, status = c.fetch_index("prodej", "byty")
    assert text == "<html>index</html>"
    assert status == 200
    assert c._session.calls == ["https://reality.idnes.cz/s/prodej/byty/"]


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


def test_fetch_detail_redirect_to_search_is_gone():
    # A removed listing 302-redirects to /s/...; requests follows it (200) but the
    # final URL is off /detail/, which the client treats as gone.
    c = _client([FakeResponse(200, "<html>search</html>", url="https://reality.idnes.cz/s/prodej/byty/")])
    with pytest.raises(ListingGoneError):
        c.fetch_detail(_DETAIL)


def test_fetch_detail_gone_body_marker():
    c = _client([FakeResponse(200, "Tato nemovitost již není v nabídce.", url=_DETAIL)])
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
