"""Hermetic tests for scraper.portal_base.BasePortalClient.

No network: a fake session feeds canned responses. Covers the shared retry /
backoff / penalize / gone machinery every portal client inherits.
"""

from __future__ import annotations

import time

import pytest
import requests

from scraper.portal_base import BasePortalClient, ListingGoneError
from scraper.rate_limit import RateLimiter


class _Resp:
    def __init__(self, status: int, text: str = "ok") -> None:
        self.status_code = status
        self.text = text
        self.headers = {"Content-Type": "application/json"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class _Session:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict | None]] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


def _client(responses: list[_Resp], **kw) -> BasePortalClient:
    c = BasePortalClient(**kw)
    c._session = _Session(responses)
    return c


def test_returns_response_on_200():
    c = _client([_Resp(200, "body")])
    resp = c._request("http://x/")
    assert resp.status_code == 200 and resp.text == "body"
    assert c._session.calls == [("http://x/", None)]


def test_params_passed_only_when_present():
    c = _client([_Resp(200), _Resp(200)])
    c._request("http://x/")
    c._request("http://x/", params={"a": 1})
    assert c._session.calls == [("http://x/", None), ("http://x/", {"a": 1})]


def test_gone_404_raises_immediately_no_retry():
    c = _client([_Resp(404)], max_retries=3)
    with pytest.raises(ListingGoneError):
        c._request("http://x/")
    assert len(c._session.calls) == 1  # gone is terminal, never retried


def test_gone_410_raises():
    c = _client([_Resp(410)])
    with pytest.raises(ListingGoneError):
        c._request("http://x/")


def test_retryable_then_success(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    limiter = RateLimiter(1000.0)
    base = limiter.interval
    c = _client([_Resp(429), _Resp(200, "ok")], limiter=limiter, max_retries=2)
    resp = c._request("http://x/")
    assert resp.status_code == 200
    assert len(c._session.calls) == 2
    assert limiter.interval > base  # penalize widened the interval on 429


def test_retry_exhausted_raises_httperror(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    c = _client([_Resp(503), _Resp(503)], max_retries=1)
    with pytest.raises(requests.HTTPError):
        c._request("http://x/")
    assert len(c._session.calls) == 2


def test_non_retryable_4xx_raises_for_status(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    # 400 is neither gone nor retryable -> raise_for_status -> HTTPError.
    c = _client([_Resp(400)], max_retries=0)
    with pytest.raises(requests.HTTPError):
        c._request("http://x/")
    assert len(c._session.calls) == 1


class _FakeLimiter:
    def __init__(self) -> None:
        self.acquired = 0
        self.penalized = 0

    def acquire(self) -> None:
        self.acquired += 1

    def penalize(self) -> None:
        self.penalized += 1


def test_pace_uses_limiter():
    lim = _FakeLimiter()
    c = _client([_Resp(200)], limiter=lim)
    c._request("http://x/")
    assert lim.acquired == 1


def test_no_limiter_falls_back_to_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    c = _client([_Resp(200)], request_delay_s=2.0)
    c._last_at = 99.5  # 0.5s since last fetch -> should sleep ~1.5s
    c._request("http://x/")
    assert slept and abs(slept[0] - 1.5) < 1e-9


def test_accept_header_per_subclass():
    class JsonClient(BasePortalClient):
        ACCEPT = "application/json"

    assert JsonClient()._session.headers["Accept"] == "application/json"
    assert BasePortalClient()._session.headers["Accept"] == "*/*"
    assert "Mozilla" in BasePortalClient()._session.headers["User-Agent"]
