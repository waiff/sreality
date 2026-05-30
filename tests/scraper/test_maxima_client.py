"""Hermetic tests for scraper.maxima_client: URL building + retry/penalize + the
404-gone signal. No network: a fake session feeds canned responses. Mirrors
test_idnes_client.py.
"""

from __future__ import annotations

import time

import pytest
import requests

from scraper.maxima_client import MaximaClient, detail_url, index_url
from scraper.portal_base import ListingGoneError
from scraper.rate_limit import RateLimiter

_DETAIL = "https://nemovitosti.maxima.cz/nemovitosti/b50087758/"


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


def _client(responses: list[FakeResponse], **kw) -> MaximaClient:
    c = MaximaClient(**kw)
    c._session = FakeSession(responses)
    return c


def test_index_url_building():
    # page None / <=1 -> the bare first page; page N>=2 -> /page/N/.
    assert index_url() == "https://nemovitosti.maxima.cz/"
    assert index_url(1) == "https://nemovitosti.maxima.cz/"
    assert index_url(2) == "https://nemovitosti.maxima.cz/page/2/"
    assert index_url(16) == "https://nemovitosti.maxima.cz/page/16/"


def test_index_url_agenda():
    # af=1 (sale) reproduces the bare default URL; af=2 (rent) appends ?af=2.
    assert index_url(af=1) == "https://nemovitosti.maxima.cz/"
    assert index_url(af=2) == "https://nemovitosti.maxima.cz/?af=2"
    assert index_url(2, af=2) == "https://nemovitosti.maxima.cz/page/2/?af=2"
    assert index_url(3, af=1) == "https://nemovitosti.maxima.cz/page/3/"


def test_fetch_index_rent_agenda():
    c = _client([FakeResponse(200, "<html>rent</html>")])
    c.fetch_index(2, af=2)
    assert c._session.calls == ["https://nemovitosti.maxima.cz/page/2/?af=2"]


def test_detail_url_building():
    assert detail_url("/nemovitosti/b50087758/") == _DETAIL
    assert detail_url(_DETAIL) == _DETAIL


def test_fetch_index_ok():
    c = _client([FakeResponse(200, "<html>index</html>")])
    text, status = c.fetch_index()
    assert text == "<html>index</html>"
    assert status == 200
    assert c._session.calls == ["https://nemovitosti.maxima.cz/"]


def test_fetch_index_paged():
    c = _client([FakeResponse(200, "<html>p2</html>")])
    c.fetch_index(2)
    assert c._session.calls == ["https://nemovitosti.maxima.cz/page/2/"]


def test_fetch_detail_ok():
    c = _client([FakeResponse(200, "<html>detail</html>", url=_DETAIL)])
    text, status = c.fetch_detail("/nemovitosti/b50087758/")
    assert text == "<html>detail</html>"
    assert status == 200


def test_fetch_detail_gone_404_raises_immediately():
    c = _client([FakeResponse(404, "not found")])
    with pytest.raises(ListingGoneError):
        c.fetch_detail(_DETAIL)
    assert len(c._session.calls) == 1  # no retry on a gone status


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
