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


# Realistic Mapy.cz payload modelled on a confirmed live response for
# "Václavské náměstí 1, Praha 1": Mapy returns the street centroid first
# (regional.street, less specific) and the exact building second
# (regional.address, more specific). Our picker should choose the latter.
_LIVE_LIKE_PAYLOAD = {
    "items": [
        {
            "name": "Václavské náměstí",
            "label": "Náměstí ",
            "location": "Václavské náměstí, Praha, Česko",
            "position": {"lon": 14.42667, "lat": 50.08149},
            "bbox": [14.4234, 50.0792, 14.4308, 50.0841],
            "type": "regional.street",
            "regionalStructure": [],
        },
        {
            "name": "Václavské náměstí 846/1",
            "label": "Adresa ",
            "location": "Václavské náměstí 846/1, Praha 1 - Nové Město, Česko",
            "position": {"lon": 14.42403, "lat": 50.08418},
            "bbox": [14.4212, 50.0831, 14.4268, 50.0853],
            "type": "regional.address",
            "regionalStructure": [],
            "zip": "110 00",
        },
    ],
    "locality": [],
}


# ----------------------------------------------------------------------
# Picker selects the most-specific item, not items[0]
# ----------------------------------------------------------------------

def test_geocode_picks_address_over_street(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body=_LIVE_LIKE_PAYLOAD)])
    result = geocoding.geocode("Václavské náměstí 1, Praha 1", session=sess)
    # Picked item 1 (regional.address), not item 0 (regional.street).
    assert result.lat == pytest.approx(50.08418)
    assert result.lng == pytest.approx(14.42403)
    assert result.confidence == "high"
    assert result.matched_type == "regional.address"
    assert "846/1" in result.matched_address
    assert result.bbox == (14.4212, 50.0831, 14.4268, 50.0853)
    # apikey carried as query param.
    assert sess.calls[0]["params"]["apikey"] == "test"
    assert sess.calls[0]["params"]["query"] == "Václavské náměstí 1, Praha 1"


def test_only_street_match_is_medium(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [{
            "location": "Krátká, Praha 5, Česko",
            "position": {"lon": 14.0, "lat": 50.0},
            "type": "regional.street",
        }],
    })])
    result = geocoding.geocode("Krátká, Praha 5", session=sess)
    assert result.confidence == "medium"
    assert result.matched_address.startswith("Krátká")


def test_municipality_match_is_low(monkeypatch):
    """A whole-city centroid is too imprecise for an estimation; mark low."""
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [{
            "location": "Praha, Česko",
            "position": {"lon": 14.43, "lat": 50.08},
            "type": "regional.municipality",
        }],
    })])
    assert geocoding.geocode("Praha", session=sess).confidence == "low"


def test_municipality_part_match_is_low(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [{
            "location": "Nové Město, Praha, Česko",
            "position": {"lon": 14.43, "lat": 50.08},
            "type": "regional.municipality_part",
        }],
    })])
    assert geocoding.geocode("Nové Město", session=sess).confidence == "low"


def test_picker_skips_items_missing_position(monkeypatch):
    """A higher-specificity type is ignored if it has no position."""
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [
            {"type": "regional.address", "name": "broken"},
            {
                "location": "Praha 5, Česko",
                "position": {"lon": 14.0, "lat": 50.0},
                "type": "regional.street",
            },
        ],
    })])
    result = geocoding.geocode("X", session=sess)
    assert result.matched_type == "regional.street"
    assert result.confidence == "medium"


def test_picker_breaks_ties_by_api_order(monkeypatch):
    """Two items of the same type → take the first (Mapy's own ranking)."""
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [
            {
                "location": "First",
                "position": {"lon": 14.0, "lat": 50.0},
                "type": "regional.street",
            },
            {
                "location": "Second",
                "position": {"lon": 15.0, "lat": 51.0},
                "type": "regional.street",
            },
        ],
    })])
    assert geocoding.geocode("X", session=sess).matched_address == "First"


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


def test_all_items_missing_position(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [
            {"type": "regional.address", "name": "a"},
            {"type": "regional.street", "name": "b"},
        ],
    })])
    with pytest.raises(geocoding.GeocodingError, match="no usable"):
        geocoding.geocode("X", session=sess)


def test_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(geocoding.time, "sleep", lambda _s: None)
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([
        _FakeResponse(status_code=503),
        _FakeResponse(body={
            "items": [{
                "location": "Praha, Česko",
                "position": {"lat": 50.08, "lon": 14.43},
                "type": "regional.municipality",
            }],
        }),
    ])
    result = geocoding.geocode("Praha", session=sess)
    assert result.confidence == "low"
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


def test_bbox_optional_when_absent(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test")
    sess = _FakeSession([_FakeResponse(body={
        "items": [{
            "location": "X",
            "position": {"lon": 14.0, "lat": 50.0},
            "type": "regional.address",
        }],
    })])
    assert geocoding.geocode("X", session=sess).bbox is None
