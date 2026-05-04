"""Hermetic tests for OverpassClient.

No real network: requests.Session.post is monkeypatched. Throttle uses
monotonic time so we monkeypatch time.monotonic / time.sleep too.

The single non-hermetic test at the bottom is gated on
RUN_OVERPASS_TESTS=1 and excluded from CI.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import requests

from scraper import overpass_client as oc


# Query rendering


def test_build_query_single_tag_dict_emits_three_element_types():
    body = oc._build_query(
        [{"railway": "tram_stop"}], lat=50.0, lng=14.0, radius_m=500,
    )
    assert 'node["railway"="tram_stop"](around:500,50.0,14.0);' in body
    assert 'way["railway"="tram_stop"](around:500,50.0,14.0);' in body
    assert 'relation["railway"="tram_stop"](around:500,50.0,14.0);' in body
    assert body.startswith("[out:json][timeout:25];")
    assert body.rstrip().endswith("out center tags;")


def test_build_query_multi_tag_dict_unions_all_combinations():
    body = oc._build_query(
        [
            {"railway": "tram_stop"},
            {"public_transport": "stop_position", "tram": "yes"},
        ],
        lat=50.0, lng=14.0, radius_m=1000,
    )
    # Two dicts × three element types = 6 lines.
    assert body.count("(around:1000,50.0,14.0);") == 6
    # Second dict ANDs both tag predicates on the same element.
    assert (
        'node["public_transport"="stop_position"]["tram"="yes"]'
        '(around:1000,50.0,14.0);'
    ) in body


def test_build_query_value_true_emits_key_only_filter():
    body = oc._build_query(
        [{"shop": True}], lat=50.0, lng=14.0, radius_m=300,
    )
    assert 'node["shop"](around:300,50.0,14.0);' in body
    assert 'node["shop"="True"]' not in body


def test_build_query_escapes_quotes_in_keys_and_values():
    body = oc._build_query(
        [{'weird"key': 'a"b'}], lat=50.0, lng=14.0, radius_m=100,
    )
    assert 'node["weird\\"key"="a\\"b"]' in body


def test_build_query_empty_list_produces_empty_union():
    body = oc._build_query([], lat=50.0, lng=14.0, radius_m=100)
    # No element type lines, but envelope is intact.
    assert body.startswith("[out:json][timeout:25];")
    assert "around:100" not in body


# Response parsing


def test_parse_elements_node_uses_top_level_lat_lon():
    rows = oc._parse_elements([
        {
            "type": "node", "id": 12345,
            "lat": 50.05, "lon": 14.42,
            "tags": {"name": "Anděl", "railway": "tram_stop"},
        },
    ])
    assert rows == [{
        "source_id": "node/12345",
        "name": "Anděl",
        "lat": 50.05, "lng": 14.42,
        "tags": {"name": "Anděl", "railway": "tram_stop"},
    }]


def test_parse_elements_way_uses_center_lat_lon():
    rows = oc._parse_elements([
        {
            "type": "way", "id": 99,
            "center": {"lat": 50.1, "lon": 14.5},
            "tags": {"leisure": "park", "name": "Stromovka"},
        },
    ])
    assert rows[0]["source_id"] == "way/99"
    assert rows[0]["lat"] == 50.1
    assert rows[0]["lng"] == 14.5
    assert rows[0]["name"] == "Stromovka"


def test_parse_elements_relation_uses_center():
    rows = oc._parse_elements([
        {
            "type": "relation", "id": 7,
            "center": {"lat": 50.0, "lon": 14.0},
            "tags": {"leisure": "park"},
        },
    ])
    assert rows[0]["source_id"] == "relation/7"
    assert rows[0]["name"] is None


def test_parse_elements_drops_rows_without_coordinates():
    rows = oc._parse_elements([
        {"type": "way", "id": 1, "tags": {"leisure": "park"}},   # no center
        {"type": "node", "id": 2, "tags": {}},                   # no lat/lon
        {"type": "node", "id": 3, "lat": 50.0, "lon": 14.0},     # ok
    ])
    assert len(rows) == 1
    assert rows[0]["source_id"] == "node/3"


def test_parse_elements_handles_missing_tags():
    rows = oc._parse_elements([
        {"type": "node", "id": 1, "lat": 50.0, "lon": 14.0},
    ])
    assert rows[0]["tags"] == {}
    assert rows[0]["name"] is None


# HTTP layer


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_data: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_data if json_data is not None else {"elements": []}

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class _Recorder:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(
        self, url: str, data: dict[str, Any], timeout: float,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        return self._responses.pop(0)


def _patch_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real sleeps and freeze monotonic — throttle becomes a no-op."""
    monkeypatch.setattr(oc.time, "sleep", lambda _s: None)
    monkeypatch.setattr(oc.time, "monotonic", lambda: 100.0)


def test_fetch_posts_to_overpass_url(monkeypatch: pytest.MonkeyPatch):
    _patch_time(monkeypatch)
    rec = _Recorder([_FakeResponse(json_data={"elements": []})])
    client = oc.OverpassClient()
    monkeypatch.setattr(client._session, "post", rec.post)

    client.fetch([{"railway": "tram_stop"}], 50.0, 14.0, 500)

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["url"] == oc.OVERPASS_URL
    assert "data" in call["data"]
    assert "around:500,50.0,14.0" in call["data"]["data"]


def test_fetch_returns_parsed_elements(monkeypatch: pytest.MonkeyPatch):
    _patch_time(monkeypatch)
    payload = {
        "elements": [
            {
                "type": "node", "id": 1,
                "lat": 50.05, "lon": 14.42,
                "tags": {"name": "Anděl", "railway": "tram_stop"},
            },
            {
                "type": "way", "id": 2,
                "center": {"lat": 50.06, "lon": 14.43},
                "tags": {"name": "Florenc"},
            },
        ],
    }
    rec = _Recorder([_FakeResponse(json_data=payload)])
    client = oc.OverpassClient()
    monkeypatch.setattr(client._session, "post", rec.post)

    rows = client.fetch([{"railway": "tram_stop"}], 50.0, 14.0, 500)
    assert [r["source_id"] for r in rows] == ["node/1", "way/2"]
    assert rows[0]["name"] == "Anděl"
    assert rows[1]["lat"] == 50.06


def test_fetch_empty_categories_returns_empty_no_request(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_time(monkeypatch)
    rec = _Recorder([])  # any call would IndexError
    client = oc.OverpassClient()
    monkeypatch.setattr(client._session, "post", rec.post)

    rows = client.fetch([], 50.0, 14.0, 500)
    assert rows == []
    assert rec.calls == []


def test_fetch_retries_on_retryable_status(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_time(monkeypatch)
    rec = _Recorder([
        _FakeResponse(status_code=503),
        _FakeResponse(status_code=429),
        _FakeResponse(status_code=200, json_data={"elements": []}),
    ])
    client = oc.OverpassClient(max_retries=3)
    monkeypatch.setattr(client._session, "post", rec.post)

    client.fetch([{"railway": "tram_stop"}], 50.0, 14.0, 500)
    assert len(rec.calls) == 3


def test_fetch_raises_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_time(monkeypatch)
    rec = _Recorder([_FakeResponse(status_code=503) for _ in range(4)])
    client = oc.OverpassClient(max_retries=3)
    monkeypatch.setattr(client._session, "post", rec.post)

    with pytest.raises(requests.HTTPError):
        client.fetch([{"railway": "tram_stop"}], 50.0, 14.0, 500)
    assert len(rec.calls) == 4  # 1 initial + 3 retries


def test_fetch_does_not_retry_on_non_retryable_4xx(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_time(monkeypatch)
    rec = _Recorder([_FakeResponse(status_code=400)])
    client = oc.OverpassClient(max_retries=3)
    monkeypatch.setattr(client._session, "post", rec.post)

    with pytest.raises(requests.HTTPError):
        client.fetch([{"railway": "tram_stop"}], 50.0, 14.0, 500)
    assert len(rec.calls) == 1


# Throttle


def test_fetch_throttles_between_back_to_back_calls(
    monkeypatch: pytest.MonkeyPatch,
):
    """Two fetches < request_delay_s apart should sleep for the difference."""
    sleep_calls: list[float] = []
    monkeypatch.setattr(oc.time, "sleep", lambda s: sleep_calls.append(s))
    # Monotonic ticks: 0 (first throttle check), 0 (last_request_at set after
    # first call uses the ending monotonic), 0.5 (second throttle check).
    # Implementation reads monotonic once per _throttle and once per call end;
    # we just need it to advance enough that elapsed=0.5s < 2.0s on call 2.
    times = iter([0.0, 0.0, 0.5, 0.5])
    monkeypatch.setattr(oc.time, "monotonic", lambda: next(times))

    rec = _Recorder([
        _FakeResponse(json_data={"elements": []}),
        _FakeResponse(json_data={"elements": []}),
    ])
    client = oc.OverpassClient(request_delay_s=2.0)
    monkeypatch.setattr(client._session, "post", rec.post)

    client.fetch([{"a": "b"}], 50.0, 14.0, 100)
    client.fetch([{"a": "b"}], 50.0, 14.0, 100)

    # Second call slept ~1.5s (2.0 - 0.5). First call should not sleep
    # because last_request_at started at 0 but monotonic also at 0
    # → elapsed=0, but 0 < request_delay_s so first call DOES sleep
    # the full delay. Both calls produce a sleep entry; the second one
    # is the meaningful throttle.
    assert any(abs(s - 1.5) < 0.01 for s in sleep_calls)


# Live integration test — gated, NOT part of CI.


@pytest.mark.skipif(
    os.environ.get("RUN_OVERPASS_TESTS") != "1",
    reason="set RUN_OVERPASS_TESTS=1 to hit the live Overpass API",
)
def test_live_fetch_tram_stops_in_prague():
    """Confirms the real Overpass endpoint returns at least one tram stop
    near I.P. Pavlova. Run manually for occasional sanity."""
    client = oc.OverpassClient()
    rows = client.fetch(
        [{"railway": "tram_stop"}],
        lat=50.0750, lng=14.4297, radius_m=500,
    )
    assert rows, "expected at least one tram stop near I.P. Pavlova"
    assert all(r["source_id"].startswith(("node/", "way/", "relation/")) for r in rows)
