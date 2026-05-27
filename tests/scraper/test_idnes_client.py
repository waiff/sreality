"""Hermetic tests for scraper.idnes_client: URL building + retry/penalize.

No network: a fake session feeds canned responses. Mirrors
tests/scraper/test_bazos_client.py.
"""

from __future__ import annotations

import time

import pytest
import requests

from scraper.idnes_client import IdnesClient, detail_url, index_url
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


def _client(responses: list[FakeResponse], **kw) -> IdnesClient:
    c = IdnesClient(**kw)
    c._session = FakeSession(responses)
    return c


def test_index_url_building():
    assert index_url("prodej", "byty") == "https://reality.idnes.cz/s/prodej/byty/"
    assert index_url("prodej", "byty", 1) == "https://reality.idnes.cz/s/prodej/byty/"
    assert (
        index_url("prodej", "byty", 2)
        == "https://reality.idnes.cz/s/prodej/byty/?page=2"
    )


def test_detail_url_building():
    rel = "/detail/prodej/byt/x/6a16ab1da57ad6e19a0377e7/"
    assert detail_url(rel) == "https://reality.idnes.cz" + rel
    full = "https://reality.idnes.cz" + rel
    assert detail_url(full) == full


def test_fetch_detail_ok():
    c = _client([FakeResponse(200, "<html>hi</html>")])
    text, status = c.fetch_detail("/detail/prodej/byt/x/6a16ab1da57ad6e19a0377e7/")
    assert text == "<html>hi</html>"
    assert status == 200


def test_fetch_index_ok():
    c = _client([FakeResponse(200, "<html>idx</html>")])
    text, status = c.fetch_index("prodej", "byty", 2)
    assert (text, status) == ("<html>idx</html>", 200)
    assert c._session.calls == ["https://reality.idnes.cz/s/prodej/byty/?page=2"]


def test_fetch_detail_gone_404_raises_immediately():
    c = _client([FakeResponse(404, "not found")])
    with pytest.raises(ListingGoneError):
        c.fetch_detail("/detail/prodej/byt/x/6a16ab1da57ad6e19a0377e7/")
    assert len(c._session.calls) == 1  # no retry on a gone status


def test_fetch_detail_gone_body_marker():
    c = _client([FakeResponse(200, "Tento inzerát byl ukončen.")])
    with pytest.raises(ListingGoneError):
        c.fetch_detail("/detail/prodej/byt/x/6a16ab1da57ad6e19a0377e7/")


def test_retry_then_success_penalizes_on_429(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    limiter = RateLimiter(1000.0)
    base = limiter.interval
    c = _client([FakeResponse(429), FakeResponse(200, "ok")], limiter=limiter, max_retries=2)
    text, status = c.fetch_detail("/detail/prodej/byt/x/6a16ab1da57ad6e19a0377e7/")
    assert (text, status) == ("ok", 200)
    assert len(c._session.calls) == 2
    assert limiter.interval > base  # penalize widened the interval


def test_retry_exhausted_raises(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    c = _client([FakeResponse(500), FakeResponse(500)], max_retries=1)
    with pytest.raises(requests.HTTPError):
        c.fetch_detail("/detail/prodej/byt/x/6a16ab1da57ad6e19a0377e7/")
    assert len(c._session.calls) == 2
