"""Tests for scraper.sreality_client gone-detection.

Hermetic: no network. Exercises the not-found-body detector and the
get_detail HTTPError -> ListingGoneError wrapping by stubbing _get_json.
"""

from __future__ import annotations

import pytest
import requests

from scraper.portal import stop_is_portal_end
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
    # Rule #3's structural gate reads WHY the loop stopped: here sreality's own
    # count was consumed, which is a portal end and may nominate.
    assert client.stop_reason == "declared_total_reached"
    assert stop_is_portal_end(client.stop_reason) is True


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
    # The 422 is sreality REFUSING to paginate deeper — rows past it exist and
    # were not seen — so it is our wall, not the end of the list. It used to be
    # indistinguishable from a genuine end (both returned []).
    assert client.cap_hit is True
    assert client.stop_reason == "cap_wall"
    assert stop_is_portal_end(client.stop_reason) is False


def test_iter_index_short_page_is_a_portal_end(monkeypatch):
    client = SrealityClient(per_page=2)
    # A total that overstates what the list actually holds (sreality's total
    # jitters): the walk ends on the tail page, not on the arithmetic.
    pages = {
        0: {"pagination": {"total": 9}, "results": [{"id": 1}, {"id": 2}]},
        2: {"pagination": {"total": 9}, "results": [{"id": 3}]},
    }
    monkeypatch.setattr(client, "_get_json", lambda url, params=None: pages[params["offset"]])
    assert [e["id"] for e in client.iter_index()] == [1, 2, 3]
    assert client.stop_reason == "short_page"
    assert stop_is_portal_end(client.stop_reason) is True


def test_iter_index_barren_page_is_refetched_once_and_stays_ours(monkeypatch):
    """ITEMS-FIRST + the barren rule. An items-less HTTP 200 below the declared
    total is what a soft block, a shell body and an edge-cached blank look like;
    it is only the end of the list if something corroborates it. Nothing does
    here, so the walk reports OUR stop and the category nominates nothing."""
    client = SrealityClient(per_page=2)
    calls: list[int] = []

    def fake(url, params=None):
        calls.append(params["offset"])
        if params["offset"] == 0:
            return {"pagination": {"total": 100}, "results": [{"id": 1}, {"id": 2}]}
        return {"pagination": {"total": 100}, "results": []}

    monkeypatch.setattr(client, "_get_json", fake)
    assert [e["id"] for e in client.iter_index()] == [1, 2]
    assert calls == [0, 2, 2]      # the same URL, re-fetched exactly once
    assert client.stop_reason == "barren"
    assert stop_is_portal_end(client.stop_reason) is False


def test_iter_index_barren_page_the_refetch_answers_keeps_walking(monkeypatch):
    """The re-fetch is not just a vote — when it comes back with items the walk
    continues from that page, so one blank response no longer truncates a slice."""
    client = SrealityClient(per_page=2)
    attempts: dict[int, int] = {}

    def fake(url, params=None):
        offset = params["offset"]
        attempts[offset] = attempts.get(offset, 0) + 1
        if offset == 0:
            return {"pagination": {"total": 3}, "results": [{"id": 1}, {"id": 2}]}
        if attempts[offset] == 1:
            return {"results": []}
        return {"pagination": {"total": 3}, "results": [{"id": 3}]}

    monkeypatch.setattr(client, "_get_json", fake)
    assert [e["id"] for e in client.iter_index()] == [1, 2, 3]
    assert attempts[2] == 2
    assert client.stop_reason == "declared_total_reached"


def test_iter_index_a_total_that_shrinks_on_the_barren_page_cannot_confirm_it(monkeypatch):
    """The suspect page must not supply its own corroboration. sreality's total
    jitters, so a blank page that also reports a SMALLER total than the walk has
    already passed would otherwise position-confirm itself. result_size is the
    high-water mark of everything sreality declared, so the blank stays ours."""
    client = SrealityClient(per_page=2)

    def fake(url, params=None):
        if params["offset"] == 0:
            return {"pagination": {"total": 4}, "results": [{"id": 1}, {"id": 2}]}
        return {"pagination": {"total": 2}, "results": []}

    monkeypatch.setattr(client, "_get_json", fake)
    assert [e["id"] for e in client.iter_index()] == [1, 2]
    assert client.result_size == 4
    assert client.stop_reason == "barren"
    assert stop_is_portal_end(client.stop_reason) is False


def test_iter_index_empty_page_past_the_declared_total_is_confirmed(monkeypatch):
    """Corroboration by position: the count was unreadable while the walk paged,
    so the loop asked for a page it did not need. Still empty on the re-fetch AND
    at or past what the total implies → sreality's end, which may nominate."""
    client = SrealityClient(per_page=2)

    def fake(url, params=None):
        if params["offset"] == 0:
            return {"results": [{"id": 1}, {"id": 2}]}
        return {"pagination": {"total": 2}, "results": []}

    monkeypatch.setattr(client, "_get_json", fake)
    assert [e["id"] for e in client.iter_index()] == [1, 2]
    assert client.stop_reason == "empty_confirmed"
    assert stop_is_portal_end(client.stop_reason) is True


def test_iter_index_a_clamped_page_size_is_not_a_short_page(monkeypatch):
    """`short_page` is a PORTAL end, so a clamped `limit` must never wear it: if
    sreality answered a requested 500 with 100 rows the first page would end the
    walk with the rest of the district unseen. The payload states the size it
    served, so adopt it and keep paging by what arrived."""
    client = SrealityClient(per_page=4)
    offsets: list[int] = []

    def fake(url, params=None):
        offsets.append(params["offset"])
        start = params["offset"]
        ids = [{"id": i} for i in range(start, min(start + 2, 5))]
        return {"pagination": {"total": 5, "limit": 2}, "results": ids}

    monkeypatch.setattr(client, "_get_json", fake)
    assert [e["id"] for e in client.iter_index()] == [0, 1, 2, 3, 4]
    assert offsets == [0, 2, 4]
    assert client.page_size_served == 2
    assert client.stop_reason == "declared_total_reached"


def test_iter_index_declared_zero_is_a_confirmed_empty_slice(monkeypatch):
    """An empty okres is a MEASUREMENT, not a gap — sreality's split leans on it
    (77 districts, most of them empty for a small category), so a declared zero
    with zero collected is a portal end."""
    client = SrealityClient(per_page=2)
    monkeypatch.setattr(
        client, "_get_json",
        lambda url, params=None: {"pagination": {"total": 0}, "results": []},
    )
    assert list(client.iter_index()) == []
    assert client.stop_reason == "empty_confirmed"


def test_iter_index_first_page_barren_with_no_total_is_never_confirmed(monkeypatch):
    """A confirmation that cannot be obtained is not a confirmation: no readable
    total and no earlier page of this slice with items leaves nothing to
    corroborate a blank first page against."""
    client = SrealityClient(per_page=2)
    monkeypatch.setattr(client, "_get_json", lambda url, params=None: {"results": []})
    assert list(client.iter_index()) == []
    assert client.stop_reason == "barren"
    assert stop_is_portal_end(client.stop_reason) is False


def test_iter_index_empty_after_items_with_no_total_is_confirmed(monkeypatch):
    """...but a slice that DID serve items and then runs dry with no total ever
    readable has corroborated its own end."""
    client = SrealityClient(per_page=2)

    def fake(url, params=None):
        if params["offset"] == 0:
            return {"results": [{"id": 1}, {"id": 2}]}
        return {"results": []}

    monkeypatch.setattr(client, "_get_json", fake)
    assert [e["id"] for e in client.iter_index()] == [1, 2]
    assert client.stop_reason == "empty_confirmed"


def test_fetch_index_page_returns_one_pages_results(monkeypatch):
    # The per-page seam the probe (docs/design/portal-order-fidelity.md, Phase 4)
    # needs: unlike iter_index (a generator that walks to exhaustion), this must
    # return control after exactly one page regardless of the reported total.
    client = SrealityClient(per_page=2)
    monkeypatch.setattr(
        client, "_get_json",
        lambda url, params=None: {
            "pagination": {"total": 500}, "results": [{"id": 1}, {"id": 2}],
        },
    )
    assert [e["id"] for e in client.fetch_index_page(0)] == [1, 2]
    assert client.result_size == 500
    assert client.pages_fetched == 1


def test_fetch_index_page_at_offset_forwards_it(monkeypatch):
    client = SrealityClient(per_page=2)
    seen_offsets = []

    def fake(url, params=None):
        seen_offsets.append(params["offset"])
        return {"pagination": {"total": 10}, "results": [{"id": params["offset"]}]}

    monkeypatch.setattr(client, "_get_json", fake)
    client.fetch_index_page(6)
    assert seen_offsets == [6]


def test_fetch_index_page_returns_empty_at_cap(monkeypatch):
    client = SrealityClient(per_page=2)

    def fake(url, params=None):
        resp = requests.Response()
        resp.status_code = 422
        raise requests.HTTPError("422", response=resp)

    monkeypatch.setattr(client, "_get_json", fake)
    assert client.fetch_index_page(9999) == []


def test_fetch_index_page_reraises_non_cap_http_error(monkeypatch):
    client = SrealityClient(per_page=2)

    def fake(url, params=None):
        resp = requests.Response()
        resp.status_code = 500
        raise requests.HTTPError("500", response=resp)

    monkeypatch.setattr(client, "_get_json", fake)
    with pytest.raises(requests.HTTPError):
        client.fetch_index_page(0)


def test_iter_index_on_page_receives_raw_payload(monkeypatch):
    # Location-data W0 item 0n: the archiving hook must see the RAW page
    # payload (index-only signals like geohash live outside the estate dicts),
    # once per non-empty page, with the offset and the resolved URL.
    client = SrealityClient(per_page=2)
    pages = {
        0: {"pagination": {"total": 3}, "results": [{"id": 1}, {"id": 2}]},
        2: {"pagination": {"total": 3}, "results": [{"id": 3}]},
    }
    monkeypatch.setattr(client, "_get_json", lambda url, params=None: pages[params["offset"]])
    seen: list[tuple[int, str, dict]] = []
    ids = [e["id"] for e in client.iter_index(on_page=lambda o, u, p: seen.append((o, u, p)))]
    assert ids == [1, 2, 3]
    assert [(o, p) for o, u, p in seen] == [(0, pages[0]), (2, pages[2])]
    assert all("offset=" in u and "category_main_cb=" in u for _, u, _p in seen)


def test_get_detail_rejects_an_estate_less_envelope(monkeypatch):
    """A 200 whose body is only the API envelope is not a listing. Since
    2026-09-07 delisted listings are re-fetched on purpose (rule #3 presence
    checks), which is exactly when such a body is likeliest; treating it as
    'ok' would set the row alive and blank its category, price and title."""
    client = SrealityClient()
    monkeypatch.setattr(
        client, "_get_json",
        lambda url, params=None: {"result": {}, "status_code": 200, "status_message": "OK"},
    )
    with pytest.raises(RuntimeError, match="carries no estate"):
        client.get_detail(4242)
