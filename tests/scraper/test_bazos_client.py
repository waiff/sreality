"""Hermetic tests for scraper.bazos_client: URL building + retry/penalize.

No network: a fake session feeds canned responses. Mirrors the shape of
tests/scraper/test_sreality_client.py.
"""

from __future__ import annotations

import time

import pytest
import requests

from scraper import bazos_client, rate_limit
from scraper.bazos_client import BazosClient, detail_url, index_url
from scraper.rate_limit import RateLimiter
from scraper.sreality_client import ListingGoneError


class FakeResponse:
    def __init__(self, status_code: int, text: str = "ok"):
        self.status_code = status_code
        self.text = text
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


def _client(responses: list[FakeResponse], **kw) -> BazosClient:
    c = BazosClient(**kw)
    c._session = FakeSession(responses)
    return c


def test_index_url_building():
    assert index_url("prodam", "byt") == "https://reality.bazos.cz/prodam/byt/"
    assert index_url("prodam", "byt", 20) == "https://reality.bazos.cz/prodam/byt/20/"
    assert (
        index_url("prodam", "byt", 20, locality="Praha", radius_km=25)
        == "https://reality.bazos.cz/prodam/byt/20/?hlokalita=Praha&humkreis=25"
    )


def test_detail_url_building():
    assert (
        detail_url("/inzerat/1/x.php") == "https://reality.bazos.cz/inzerat/1/x.php"
    )
    full = "https://reality.bazos.cz/inzerat/1/x.php"
    assert detail_url(full) == full


def test_fetch_detail_ok():
    c = _client([FakeResponse(200, "<html>hi</html>")])
    text, status = c.fetch_detail("/inzerat/1/x.php")
    assert text == "<html>hi</html>"
    assert status == 200
    assert c._session.calls == ["https://reality.bazos.cz/inzerat/1/x.php"]


def test_fetch_detail_gone_404_raises_immediately():
    c = _client([FakeResponse(404, "not found")])
    with pytest.raises(ListingGoneError):
        c.fetch_detail("/inzerat/1/x.php")
    assert len(c._session.calls) == 1  # no retry on a gone status


def test_fetch_detail_gone_body_marker():
    c = _client([FakeResponse(200, "Tento inzerát byl smazán.")])
    with pytest.raises(ListingGoneError):
        c.fetch_detail("/inzerat/1/x.php")


def test_retry_then_success_penalizes_on_429(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    limiter = RateLimiter(1000.0)
    base = limiter.interval
    c = _client([FakeResponse(429), FakeResponse(200, "ok")], limiter=limiter, max_retries=2)
    text, status = c.fetch_detail("/inzerat/1/x.php")
    assert text == "ok"
    assert status == 200
    assert len(c._session.calls) == 2
    assert limiter.interval > base  # penalize widened the interval


def test_retry_exhausted_raises(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    c = _client([FakeResponse(500), FakeResponse(500)], max_retries=1)
    with pytest.raises(requests.HTTPError):
        c.fetch_detail("/inzerat/1/x.php")
    assert len(c._session.calls) == 2
