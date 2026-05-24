"""Tests for scraper.sreality_client gone-detection.

Hermetic: no network. Exercises the not-found-body detector and the
get_detail HTTPError -> ListingGoneError wrapping by stubbing _get_json.
"""

from __future__ import annotations

import pytest
import requests

from scraper.sreality_client import (
    ListingGoneError,
    SrealityClient,
    _is_not_found_body,
)


class _Resp:
    def __init__(self, status: int, content_type: str, text: str) -> None:
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.text = text


def test_is_not_found_body_detects_marker():
    resp = _Resp(200, "text/html; charset=utf-8", "<h1>Tato stránka neexistuje</h1>")
    assert _is_not_found_body(resp) is True


def test_is_not_found_body_ignores_json_payload():
    resp = _Resp(200, "application/json", '{"_embedded": {}}')
    assert _is_not_found_body(resp) is False


def test_is_not_found_body_ignores_normal_html():
    resp = _Resp(200, "text/html", "<h1>Pronájem bytu 2+kk</h1>")
    assert _is_not_found_body(resp) is False


def test_get_detail_wraps_404_as_gone(monkeypatch):
    client = SrealityClient()
    resp = requests.Response()
    resp.status_code = 404

    def boom(url, params=None):
        raise requests.HTTPError("404", response=resp)

    monkeypatch.setattr(client, "_get_json", boom)
    with pytest.raises(ListingGoneError):
        client.get_detail(12345)


def test_get_detail_wraps_410_as_gone(monkeypatch):
    client = SrealityClient()
    resp = requests.Response()
    resp.status_code = 410

    def boom(url, params=None):
        raise requests.HTTPError("410", response=resp)

    monkeypatch.setattr(client, "_get_json", boom)
    with pytest.raises(ListingGoneError):
        client.get_detail(12345)


def test_get_detail_propagates_listing_gone(monkeypatch):
    client = SrealityClient()

    def boom(url, params=None):
        raise ListingGoneError(url, 200)

    monkeypatch.setattr(client, "_get_json", boom)
    with pytest.raises(ListingGoneError):
        client.get_detail(12345)


def test_get_detail_reraises_non_gone_http_error(monkeypatch):
    client = SrealityClient()
    resp = requests.Response()
    resp.status_code = 503

    def boom(url, params=None):
        raise requests.HTTPError("503", response=resp)

    monkeypatch.setattr(client, "_get_json", boom)
    with pytest.raises(requests.HTTPError):
        client.get_detail(12345)
