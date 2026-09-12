"""Hermetic tests for scraper.bazos_client: URL building + retry/penalize.

No network: a fake session feeds canned responses. Mirrors the shape of
tests/scraper/test_sreality_client.py.
"""

from __future__ import annotations

import pathlib
import time

import pytest
import requests

from scraper import bazos_client, rate_limit
from scraper.bazos_client import BazosClient, detail_url, index_url
from scraper.bazos_parser import parse_detail
from scraper.rate_limit import RateLimiter
from scraper.sreality_client import ListingGoneError


_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "portal_html"


class FakeResponse:
    def __init__(self, status_code: int, text: str = "ok", url: str | None = None):
        self.status_code = status_code
        self.text = text
        # requests exposes the FINAL url after redirects; None = "not redirected".
        self.url = url
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


def test_fetch_detail_gone_when_removed_ad_answers_with_the_category_page():
    """The 2026-09 defect: bazos serves a REMOVED ad the category INDEX page with
    HTTP 200, so the presence rail's page check never decided "gone" and 4,374
    removed ads stayed active. The captured page must now read as gone."""
    body = (_FIXTURES / "bazos_category_gone.html").read_text(encoding="utf-8")
    c = _client([FakeResponse(200, body)])

    with pytest.raises(ListingGoneError):
        c.fetch_detail("/inzerat/111/pronajem-bytu-2-kk.php")


def test_category_page_title_alone_is_enough():
    """Not a tautology over the banner: strip the "Inzerát byl vymazán" line and the
    page is still recognised, because the category <title> is its own signal."""
    body = (_FIXTURES / "bazos_category_gone.html").read_text(encoding="utf-8")
    assert "Inzerát byl vymazán." in body
    body = body.replace("<b>Inzerát byl vymazán.</b>", "")

    c = _client([FakeResponse(200, body)])
    with pytest.raises(ListingGoneError):
        c.fetch_detail("/inzerat/111/pronajem-bytu-2-kk.php")


def test_fetch_detail_gone_when_redirected_off_the_ad_path():
    """The cheap second signal: bazos lands the request on /inzeraty/<slug>/, so the
    final URL no longer carries /inzerat/<id>/ — gone whatever the body says."""
    c = _client([FakeResponse(200, "<html>nic</html>",
                              url="https://reality.bazos.cz/inzeraty/pronajem-bytu/")])

    with pytest.raises(ListingGoneError):
        c.fetch_detail("/inzerat/111/pronajem-bytu-2-kk.php")


def test_fetch_detail_gone_body_marker_vymazan():
    """The banner bazos actually prints is "vymazán"; the tuple carried only the
    unused "smazán" spelling until this fix."""
    c = _client([FakeResponse(200, "<b>Inzerát byl vymazán.</b>")])
    with pytest.raises(ListingGoneError):
        c.fetch_detail("/inzerat/1/x.php")


def test_fetch_detail_live_ad_page_is_returned_and_parses():
    """The other half of the guard: a REAL live ad page must still come back whole
    and parse — the recognizer must not read a live headline as a category title."""
    body = (_FIXTURES / "bazos_detail.html").read_text(encoding="utf-8")
    c = _client([FakeResponse(200, body)])

    text, status = c.fetch_detail("/inzerat/111/prodej-bytu-2-1.php")

    assert (text, status) == (body, 200)
    listing = parse_detail(
        text, source_url="https://reality.bazos.cz/inzerat/111/prodej-bytu-2-1.php",
        category_main="byt", category_type="prodej",
    )
    assert listing.raw["title"]
    assert listing.price_czk


def test_fetch_index_still_reads_a_category_page():
    """The index walk fetches exactly this page on purpose (minus the removed-ad
    banner): the detail-only recognizer must never leak into fetch_index, where a
    gone verdict reads as "the pager ended" and would truncate the walk."""
    body = (_FIXTURES / "bazos_category_gone.html").read_text(encoding="utf-8")
    body = body.replace("<b>Inzerát byl vymazán.</b>", "")
    c = _client([FakeResponse(200, body)])

    text, status = c.fetch_index("pronajmu", "byt")

    assert (text, status) == (body, 200)


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
