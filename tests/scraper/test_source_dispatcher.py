"""Tests for scraper.source_dispatcher. Hermetic — fakes for everything."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from scraper import source_dispatcher as sd
from scraper.geocoding import GeocodeResult, GeocodingError


# ----------------------------------------------------------------------
# classify_url + canonical_url
# ----------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.sreality.cz/detail/pronajem/byt/2-kk/praha/12345", "sreality"),
    ("https://sreality.cz/x/y/z/12345678", "sreality"),
    ("https://m.sreality.cz/detail/...", "sreality"),
    ("https://www.bezrealitky.cz/nemovitosti-byty-domy/123", "bezrealitky"),
    ("https://bezrealitky.com/x", "bezrealitky"),
    ("https://reality.idnes.cz/detail/pronajem/123", "idnes_reality"),
    ("https://www.remax-czech.cz/reality/byty/123", "remax"),
    ("https://example.com/listing", "unsupported"),
    ("not a url", "unsupported"),
    ("", "unsupported"),
])
def test_classify_url(url, expected):
    assert sd.classify_url(url) == expected


def test_canonical_url_drops_query_and_fragment():
    assert (
        sd.canonical_url("https://www.bezrealitky.cz/x/y?utm_source=foo#bar")
        == "https://www.bezrealitky.cz/x/y"
    )


def test_canonical_url_lowercases_and_trims_slash():
    assert (
        sd.canonical_url("HTTPS://Www.Bezrealitky.cz/X/Y/")
        == "https://www.bezrealitky.cz/X/Y"
    )


def test_url_hash_stable_across_query_param_variants():
    a = sd.url_hash("https://www.bezrealitky.cz/listing/abc?utm=1")
    b = sd.url_hash("https://www.bezrealitky.cz/listing/abc")
    c = sd.url_hash("https://www.bezrealitky.cz/listing/abc/")
    assert a == b == c
    assert len(a) == 64


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._last: list[Any] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> None:
        s = " ".join(sql.split()).lower()
        if s.startswith("select source_kind, parse_result"):
            url_hash_val = params[0]
            row = self._conn.cache_rows.get(url_hash_val)
            if row is None or row["expires_at"] <= datetime.now(timezone.utc):
                self._last = None
                return
            self._last = [
                row["source_kind"],
                row["parse_result"],
                row["source_html"],
                row["cost_usd"],
                row["parsed_at"],
                row["expires_at"],
            ]
        elif s.startswith("insert into parsed_url_cache"):
            url_hash_val = params[0]
            payload = params[3]
            stored = (
                payload.adapted if hasattr(payload, "adapted") else payload
            )
            self._conn.cache_rows[url_hash_val] = {
                "source_url": params[1],
                "source_kind": params[2],
                "parse_result": stored,
                "source_html": params[4],
                "cost_usd": params[5],
                "parsed_at": datetime.now(timezone.utc),
                "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
            }
            self._last = None
        elif s.startswith("select value from app_settings"):
            self._last = ["You are an extractor."]
        elif s.startswith("insert into llm_calls"):
            self._last = [self._conn.next_id]
            self._conn.next_id += 1
        else:
            self._last = None

    def fetchone(self) -> Any:
        return self._last


class _FakeConn:
    def __init__(self) -> None:
        self.cache_rows: dict[str, dict[str, Any]] = {}
        self.next_id = 1

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    @contextmanager
    def transaction(self):
        yield self


def _envelope(value: Any, conf: str = "high") -> dict[str, Any]:
    return {"value": value, "confidence": conf}


_FAKE_LLM_EXTRACTION = {
    "area_m2": _envelope(65.0),
    "disposition": _envelope("2+kk"),
    "price_czk": _envelope(25_000),
    "price_unit": _envelope("měsíc"),
    "locality": _envelope("Anglická 12, Praha 2"),
    "district": _envelope("Praha 2"),
    "category_main": _envelope("byt"),
    "category_type": _envelope("pronajem"),
    "floor": _envelope(3),
    "total_floors": _envelope(5, "medium"),
    "has_balcony": _envelope(True),
    "has_lift": _envelope(True),
    "has_parking": _envelope(False),
    "building_type": _envelope("cihla"),
    "condition": _envelope("velmi dobrý stav"),
    "energy_rating": _envelope("C"),
    "description": _envelope("Pěkný byt v centru Prahy."),
    "warnings": [],
}


class _FakeLLM:
    def __init__(
        self,
        extraction: dict[str, Any] | None = None,
        cost_usd: float = 0.012,
        no_tool_call: bool = False,
    ) -> None:
        self.extraction = (
            extraction if extraction is not None else dict(_FAKE_LLM_EXTRACTION)
        )
        self.cost_usd = cost_usd
        self.no_tool_call = no_tool_call
        self.calls: list[dict[str, Any]] = []

    def resolve_system_prompt(self) -> str:
        return "You are an extractor."

    def call(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        tool_calls: list[dict[str, Any]] = []
        if not self.no_tool_call:
            tool_calls.append({
                "id": "tu_1",
                "name": "record_listing",
                "input": dict(self.extraction),
            })

        class _Resp:
            text = ""
            cost_usd = self.cost_usd
            llm_call_id = 99

        resp = _Resp()
        resp.tool_calls = tool_calls
        return resp


def _fake_geocoder(_locality: str) -> GeocodeResult:
    return GeocodeResult(
        lat=50.075, lng=14.43, confidence="high",
        matched_address="Anglická 12, Praha 2, Česko",
        matched_type="regional.address",
        bbox=None, raw={},
    )


def _failing_geocoder(_locality: str) -> GeocodeResult:
    raise GeocodingError("Mapy down")


def _fetch_html_returns(html: str):
    def _f(_url: str) -> str:
        return html
    return _f


# ----------------------------------------------------------------------
# Sreality fast path
# ----------------------------------------------------------------------

def test_sreality_branch_uses_existing_parser(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_parse_sreality_url(url, *, client, conn, persist=False):
        captured["called"] = True
        captured["persist"] = persist
        return {
            "sreality_id": 87654321,
            "spec": {
                "lat": 50.08, "lon": 14.43,
                "area_m2": 60.0, "disposition": "2+kk", "floor": 2,
            },
            "images": [],
            "fetched_at": "2026-05-04T10:00:00+00:00",
            "source_url": url,
            "in_database": False,
        }

    monkeypatch.setattr(sd.url_parser, "parse_sreality_url", fake_parse_sreality_url)
    conn = _FakeConn()

    result = sd.parse_listing_url(
        "https://www.sreality.cz/detail/pronajem/byt/2-kk/87654321",
        sreality_client=object(),
        llm_client=_FakeLLM(),
        conn=conn,
    )
    assert captured.get("called") is True
    assert captured.get("persist") is True
    assert result.source_kind == "sreality"
    assert result.parse_confidence == "high"
    assert result.parse_confidence_per_field is None
    assert result.from_cache is False
    assert result.cost_usd is None
    assert result.sreality_id == 87654321
    assert result.spec == {
        "lat": 50.08, "lng": 14.43,
        "area_m2": 60.0, "disposition": "2+kk", "floor": 2,
        "exclude_ids": [],
    }


# ----------------------------------------------------------------------
# LLM path: bezrealitky cache miss → LLM → geocode → store
# ----------------------------------------------------------------------

def test_bezrealitky_cache_miss_calls_llm_geocodes_and_stores():
    conn = _FakeConn()
    llm = _FakeLLM()
    url = "https://www.bezrealitky.cz/nemovitosti-byty-domy/12345-byt-2kk"

    result = sd.parse_listing_url(
        url,
        sreality_client=object(),
        llm_client=llm,
        conn=conn,
        fetch_html=_fetch_html_returns("<html>spec</html>"),
        geocode=_fake_geocoder,
    )

    assert result.source_kind == "bezrealitky"
    assert result.from_cache is False
    assert result.cost_usd == pytest.approx(0.012)
    assert result.spec["lat"] == pytest.approx(50.075)
    assert result.spec["lng"] == pytest.approx(14.43)
    assert result.spec["area_m2"] == 65.0
    assert result.spec["disposition"] == "2+kk"
    assert result.spec["floor"] == 3
    assert result.parse_confidence == "high"
    assert result.parse_confidence_per_field["area_m2"] == "high"
    assert result.parse_confidence_per_field["lat"] == "high"
    # LLM was called with the system prompt + tool schema.
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["called_for"] == "parse_url"
    assert call["system"] == "You are an extractor."
    assert call["tools"][0]["name"] == "record_listing"
    # Cache row written under the canonical-URL hash.
    assert sd.url_hash(url) in conn.cache_rows
    cached = conn.cache_rows[sd.url_hash(url)]
    assert cached["source_kind"] == "bezrealitky"
    assert cached["source_html"] == "<html>spec</html>"


def test_cache_hit_skips_llm_and_geocoder():
    conn = _FakeConn()
    url = "https://www.bezrealitky.cz/listing/123"
    # Pre-seed a cache row.
    cached_payload = {
        "spec": {
            "lat": 50.0, "lng": 14.4,
            "area_m2": 70.0, "disposition": "3+kk", "floor": 1,
            "exclude_ids": [],
        },
        "extraction": dict(_FAKE_LLM_EXTRACTION),
        "parse_confidence": "high",
        "parse_confidence_per_field": {
            "area_m2": "high", "disposition": "high", "lat": "high",
        },
        "warnings": ["from cache"],
    }
    conn.cache_rows[sd.url_hash(url)] = {
        "source_url": url,
        "source_kind": "bezrealitky",
        "parse_result": cached_payload,
        "source_html": "<html/>",
        "cost_usd": 0.02,
        "parsed_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=3),
    }
    llm = _FakeLLM()

    def _exploding_fetch(_url):
        raise AssertionError("fetch_html should not be called on cache hit")

    def _exploding_geocode(_loc):
        raise AssertionError("geocoder should not be called on cache hit")

    result = sd.parse_listing_url(
        url, sreality_client=object(), llm_client=llm, conn=conn,
        fetch_html=_exploding_fetch, geocode=_exploding_geocode,
    )
    assert result.from_cache is True
    assert result.cost_usd is None
    assert result.spec["disposition"] == "3+kk"
    assert result.warnings == ["from cache"]
    assert llm.calls == []  # not invoked


def test_force_refresh_skips_cache_lookup_even_when_fresh():
    conn = _FakeConn()
    url = "https://www.bezrealitky.cz/listing/123"
    # Pre-seed a cache row that would otherwise be returned.
    conn.cache_rows[sd.url_hash(url)] = {
        "source_url": url,
        "source_kind": "bezrealitky",
        "parse_result": {
            "spec": {
                "lat": 50.0, "lng": 14.4,
                "area_m2": 99.0, "disposition": "5+1", "floor": 9,
                "exclude_ids": [],
            },
            "extraction": dict(_FAKE_LLM_EXTRACTION),
            "parse_confidence": "high",
            "parse_confidence_per_field": {
                "area_m2": "high", "disposition": "high", "lat": "high",
            },
            "warnings": [],
        },
        "source_html": "<html/>",
        "cost_usd": 0.02,
        "parsed_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=3),
    }
    llm = _FakeLLM()

    result = sd.parse_listing_url(
        url, sreality_client=object(), llm_client=llm, conn=conn,
        force_refresh=True,
        fetch_html=_fetch_html_returns("<html>spec</html>"),
        geocode=_fake_geocoder,
    )
    # Got a fresh parse rather than the seeded "5+1, 99 m²" cache row.
    assert result.from_cache is False
    assert result.spec["disposition"] == "2+kk"
    assert result.spec["area_m2"] == 65.0
    assert len(llm.calls) == 1


def test_fresh_parse_sets_fetched_at():
    conn = _FakeConn()
    url = "https://www.bezrealitky.cz/listing/freshie"
    result = sd.parse_listing_url(
        url, sreality_client=object(), llm_client=_FakeLLM(), conn=conn,
        fetch_html=_fetch_html_returns("<html>spec</html>"),
        geocode=_fake_geocoder,
    )
    assert result.fetched_at is not None
    # ISO-8601 with timezone — datetime.fromisoformat round-trips it.
    assert datetime.fromisoformat(result.fetched_at).tzinfo is not None


def test_expired_cache_row_treated_as_miss():
    conn = _FakeConn()
    url = "https://www.bezrealitky.cz/listing/expired"
    conn.cache_rows[sd.url_hash(url)] = {
        "source_url": url,
        "source_kind": "bezrealitky",
        "parse_result": {"spec": {}, "warnings": []},
        "source_html": "old",
        "cost_usd": 0.1,
        "parsed_at": datetime.now(timezone.utc) - timedelta(days=10),
        "expires_at": datetime.now(timezone.utc) - timedelta(days=3),
    }
    result = sd.parse_listing_url(
        url, sreality_client=object(), llm_client=_FakeLLM(), conn=conn,
        fetch_html=_fetch_html_returns("<html/>"),
        geocode=_fake_geocoder,
    )
    assert result.from_cache is False


# ----------------------------------------------------------------------
# Confidence + warning paths
# ----------------------------------------------------------------------

def test_unsupported_source_overall_confidence_is_best_effort():
    extraction = dict(_FAKE_LLM_EXTRACTION)
    # Even when LLM marks area "high", overall stays best_effort
    # for unsupported sources.
    conn = _FakeConn()
    llm = _FakeLLM(extraction=extraction)
    result = sd.parse_listing_url(
        "https://example.com/listing",
        sreality_client=object(),
        llm_client=llm, conn=conn,
        fetch_html=_fetch_html_returns("<html/>"),
        geocode=_fake_geocoder,
    )
    assert result.source_kind == "unsupported"
    assert result.parse_confidence == "best_effort"


def test_low_confidence_field_drags_overall_down():
    extraction = dict(_FAKE_LLM_EXTRACTION)
    extraction["disposition"] = _envelope("2+kk", "low")
    conn = _FakeConn()
    result = sd.parse_listing_url(
        "https://www.bezrealitky.cz/listing/x",
        sreality_client=object(),
        llm_client=_FakeLLM(extraction=extraction),
        conn=conn,
        fetch_html=_fetch_html_returns("<html/>"),
        geocode=_fake_geocoder,
    )
    assert result.parse_confidence == "low"


def test_geocoding_failure_records_warning_and_low_lat_conf():
    conn = _FakeConn()
    result = sd.parse_listing_url(
        "https://www.bezrealitky.cz/listing/x",
        sreality_client=object(),
        llm_client=_FakeLLM(),
        conn=conn,
        fetch_html=_fetch_html_returns("<html/>"),
        geocode=_failing_geocoder,
    )
    assert result.spec["lat"] is None
    assert result.spec["lng"] is None
    assert result.parse_confidence_per_field["lat"] == "low"
    assert any("geocoding failed" in w for w in result.warnings)
    assert result.parse_confidence == "low"


def test_missing_locality_records_warning():
    extraction = dict(_FAKE_LLM_EXTRACTION)
    extraction["locality"] = _envelope(None, "low")
    conn = _FakeConn()

    def _exploding_geocode(_loc):
        raise AssertionError("geocoder must not be called when locality is null")

    result = sd.parse_listing_url(
        "https://www.bezrealitky.cz/listing/x",
        sreality_client=object(),
        llm_client=_FakeLLM(extraction=extraction),
        conn=conn,
        fetch_html=_fetch_html_returns("<html/>"),
        geocode=_exploding_geocode,
    )
    assert any("no locality" in w for w in result.warnings)
    assert result.parse_confidence == "low"


# ----------------------------------------------------------------------
# Failure modes
# ----------------------------------------------------------------------

def test_llm_omits_tool_call_raises_parse_error():
    conn = _FakeConn()
    llm = _FakeLLM(no_tool_call=True)
    with pytest.raises(sd.ParseError, match="record_listing"):
        sd.parse_listing_url(
            "https://www.bezrealitky.cz/listing/x",
            sreality_client=object(),
            llm_client=llm,
            conn=conn,
            fetch_html=_fetch_html_returns("<html/>"),
            geocode=_fake_geocoder,
        )


def test_html_fetch_failure_wraps_in_parse_error():
    conn = _FakeConn()

    def _broken_fetch(_url):
        raise RuntimeError("connection reset")

    with pytest.raises(sd.ParseError, match="failed to fetch"):
        sd.parse_listing_url(
            "https://www.bezrealitky.cz/listing/x",
            sreality_client=object(),
            llm_client=_FakeLLM(),
            conn=conn,
            fetch_html=_broken_fetch,
            geocode=_fake_geocoder,
        )


# ----------------------------------------------------------------------
# Helpers (smoke)
# ----------------------------------------------------------------------

def test_overall_confidence_high_when_all_relevant_high():
    cpf = {"area_m2": "high", "disposition": "high", "lat": "high"}
    assert sd._overall_confidence(cpf) == "high"


def test_overall_confidence_medium_when_one_medium():
    cpf = {"area_m2": "high", "disposition": "medium", "lat": "high"}
    assert sd._overall_confidence(cpf) == "medium"
