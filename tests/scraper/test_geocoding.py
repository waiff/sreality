"""Tests for scraper.geocoding. Hermetic — no live HTTP."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from scraper import geocoding


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: dict[str, Any] | None = None,
        raise_decode: bool = False,
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self._raise_decode = raise_decode

    def json(self) -> dict[str, Any]:
        if self._raise_decode:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any] | None = None,
            timeout: float | None = None) -> _FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# ----------------------------------------------------------------------
# Happy path: address-quality match -> high
# ----------------------------------------------------------------------

def test_geocode_high_confidence_address(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [
            {
                "name": "Václavské náměstí 1",
                "label": "Václavské náměstí 1, 110 00 Praha 1",
                "position": {"lon": 14.4283, "lat": 50.0810},
                "type": "regional.address",
                "regionalStructure": [],
                "zip": "110 00",
            },
        ],
    })])
    result = geocoding.geocode("Václavské náměstí 1, Praha 1", session=sess)
    assert result.lat == pytest.approx(50.0810)
    assert result.lng == pytest.approx(14.4283)
    assert result.confidence == "high"
    assert result.matched_type == "regional.address"
    assert "Praha" in result.matched_label
    # apikey passed as query param.
    assert sess.calls[0]["params"]["apikey"] == "test"
    assert sess.calls[0]["params"]["query"] == "Václavské náměstí 1, Praha 1"


def test_geocode_street_match_is_medium(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [{
            "label": "Krátká, Praha 5",
            "position": {"lon": 14.0, "lat": 50.0},
            "type": "regional.street",
        }],
    })])
    assert geocoding.geocode("Krátká, Praha 5", session=sess).confidence == "medium"


def test_geocode_unknown_type_is_low(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [{
            "label": "X",
            "position": {"lon": 14.0, "lat": 50.0},
            "type": "poi",
        }],
    })])
    assert geocoding.geocode("X", session=sess).confidence == "low"


# ----------------------------------------------------------------------
# Failure modes
# ----------------------------------------------------------------------

def test_empty_locality_raises():
    with pytest.raises(geocoding.GeocodingError, match="empty"):
        geocoding.geocode("")


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("MAPY_CZ_API_KEY", raising=False)
    with pytest.raises(geocoding.GeocodingError, match="MAPY_CZ_API_KEY"):
        geocoding.geocode("Praha")


def test_no_items_in_response(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={"items": []})])
    with pytest.raises(geocoding.GeocodingError, match="no items"):
        geocoding.geocode("nowhere", session=sess)


def test_missing_position_raises(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [{"label": "X", "type": "regional.address"}],
    })])
    with pytest.raises(geocoding.GeocodingError, match="lat/lon"):
        geocoding.geocode("X", session=sess)


def test_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(geocoding.time, "sleep", lambda _s: None)
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([
        _FakeResponse(status_code=503),
        _FakeResponse(body={
            "items": [{
                "label": "Praha",
                "position": {"lat": 50.08, "lon": 14.43},
                "type": "regional.municipality",
            }],
        }),
    ])
    result = geocoding.geocode("Praha", session=sess)
    assert result.confidence == "medium"
    assert len(sess.calls) == 2


def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(geocoding.time, "sleep", lambda _s: None)
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([
        _FakeResponse(status_code=503),
        _FakeResponse(status_code=503),
        _FakeResponse(status_code=503),
    ])
    with pytest.raises(geocoding.GeocodingError):
        geocoding.geocode("X", session=sess, max_retries=2)
    assert len(sess.calls) == 3


def test_non_retryable_4xx_raises_http_error(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(status_code=401)])
    with pytest.raises(requests.HTTPError):
        geocoding.geocode("Praha", session=sess)


def test_malformed_json_raises_geocoding_error(monkeypatch):
    monkeypatch.setattr(geocoding.time, "sleep", lambda _s: None)
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([
        _FakeResponse(raise_decode=True),
        _FakeResponse(raise_decode=True),
        _FakeResponse(raise_decode=True),
    ])
    with pytest.raises(geocoding.GeocodingError):
        geocoding.geocode("X", session=sess, max_retries=2)
