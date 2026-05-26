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


def test_get_detail_injects_id(monkeypatch):
    client = SrealityClient()
    monkeypatch.setattr(
        client, "_get_json", lambda url, params=None: {"category_main_cb": {"value": 1}}
    )
    assert client.get_detail(777)["hash_id"] == 777


def test_get_detail_unwraps_result_envelope(monkeypatch):
    client = SrealityClient()
    monkeypatch.setattr(
        client, "_get_json",
        lambda url, params=None: {
            "result": {"category_main_cb": {"value": 1}, "hash_id": 555},
            "status_code": 200,
            "status_message": "OK",
        },
    )
    estate = client.get_detail(555)
    assert estate["hash_id"] == 555
    assert estate["category_main_cb"] == {"value": 1}
    assert "status_code" not in estate


def test_probe_result_size_reads_pagination_total(monkeypatch):
    client = SrealityClient()
    monkeypatch.setattr(
        client, "_get_json",
        lambda url, params=None: {"pagination": {"total": 42}, "results": []},
    )
    assert client.probe_result_size() == 42


def test_iter_index_pages_by_offset(monkeypatch):
    client = SrealityClient(per_page=2)
    pages = {
        0: {"pagination": {"total": 3}, "results": [{"id": 1}, {"id": 2}]},
        2: {"pagination": {"total": 3}, "results": [{"id": 3}]},
    }
    monkeypatch.setattr(client, "_get_json", lambda url, params=None: pages[params["offset"]])
    assert [e["id"] for e in client.iter_index()] == [1, 2, 3]
    assert client.result_size == 3


def test_iter_index_stops_cleanly_at_cap(monkeypatch):
    client = SrealityClient(per_page=2)

    def fake(url, params=None):
        if params["offset"] == 0:
            return {"pagination": {"total": 100}, "results": [{"id": 1}, {"id": 2}]}
        resp = requests.Response()
        resp.status_code = 422
        raise requests.HTTPError("422", response=resp)

    monkeypatch.setattr(client, "_get_json", fake)
    assert [e["id"] for e in client.iter_index()] == [1, 2]
